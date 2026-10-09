"""Tests for the as-issued forecast archive (algo v2, slice S3).

S3 acceptance criteria (docs/ALGO_V2_PRD.md):
  AC1 available_at >= run_ts + latency · AC2 00Z max maps to the right local date ·
  AC3 idempotent re-ingest · AC4 missing data -> WARN + recorded gap, no crash.
All IEM access is mocked.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date, datetime, timedelta

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.common.models import Base, ForecastArchiveChunk, ForecastIssuance
from backend.weather.forecast_archive import (
    CHUNK_COMPLETE,
    CHUNK_ERROR,
    MODEL_LATENCY,
    IEMMosClient,
    IEMRateLimitedError,
    month_chunks,
    parse_mos_csv,
    pending_chunks,
    run_forecast_backfill,
)

GFS_CSV = """runtime,ftime,model,tmp,n_x,txn,xnd,station
2025-03-14 12:00:00,2025-03-15 12:00:00,GFS,55,52.0,,,KAUS
2025-03-14 12:00:00,2025-03-15 18:00:00,GFS,66,,,,KAUS
2025-03-14 12:00:00,2025-03-16 00:00:00,GFS,69,78.0,,,KAUS
2025-03-14 12:00:00,2025-03-16 12:00:00,GFS,45,42.0,,,KAUS
2025-03-14 12:00:00,2025-03-17 00:00:00,GFS,71,81.0,,,KAUS
"""

NBS_CSV = """runtime,ftime,model,tmp,txn,xnd,station
2025-03-14 13:00:00,2025-03-15 12:00:00,NBS,57,54.0,3.0,KAUS
2025-03-14 13:00:00,2025-03-16 00:00:00,NBS,73,79.0,2.0,KAUS
"""


class FakeIEM:
    """Stands in for IEMMosClient."""

    def __init__(self, bodies: dict[str, str] | None = None, fail: set[str] | None = None) -> None:
        self.bodies = bodies or {}
        self.fail = fail or set()
        self.calls: list[tuple] = []

    async def fetch_csv(self, station: str, model: str, start: datetime, end: datetime) -> str:
        self.calls.append((station, model, start))
        if model in self.fail:
            raise IEMRateLimitedError("simulated 429s")
        return self.bodies.get(model, "runtime,ftime,model\n")


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator:
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    async def factory() -> AsyncSession:
        return maker()

    yield factory
    await engine.dispose()


async def _count(factory, model) -> int:
    s = await factory()
    try:
        return (await s.execute(select(func.count()).select_from(model))).scalar_one()
    finally:
        await s.close()


class TestParse:
    def test_keeps_only_daily_max_rows_with_correct_local_date(self) -> None:
        """AC2: max rows sit at 00Z = evening of the previous local day."""
        rows = parse_mos_csv(GFS_CSV, "AUS", "GFS")
        assert [(r["valid_date"], r["tmax_f"]) for r in rows] == [
            (date(2025, 3, 15), 78.0),
            (date(2025, 3, 16), 81.0),
        ]
        assert rows[0]["station"] == "KAUS"
        assert rows[0]["lead_hours"] == 36
        assert rows[0]["tmax_sd_f"] is None

    def test_available_at_includes_conservative_latency(self) -> None:
        """AC1: a forecast is never usable before run_ts + latency."""
        for model, text in (("GFS", GFS_CSV), ("NBS", NBS_CSV)):
            for r in parse_mos_csv(text, "AUS", model):
                assert r["available_at"] - r["run_ts"] == MODEL_LATENCY[model]
                assert r["available_at"] >= r["run_ts"] + timedelta(hours=2)

    def test_nbm_spread_kept(self) -> None:
        rows = parse_mos_csv(NBS_CSV, "AUS", "NBS")
        assert [(r["valid_date"], r["tmax_f"], r["tmax_sd_f"]) for r in rows] == [
            (date(2025, 3, 15), 79.0, 2.0)
        ]

    def test_garbage_rows_skipped(self) -> None:
        bad = "runtime,ftime,model,n_x\nnot-a-date,also-bad,GFS,70\n"
        assert parse_mos_csv(bad, "NYC", "GFS") == []

    def test_month_chunks(self) -> None:
        assert month_chunks(date(2024, 11, 15), date(2025, 2, 1)) == [
            date(2024, 11, 1),
            date(2024, 12, 1),
            date(2025, 1, 1),
            date(2025, 2, 1),
        ]


class TestBackfill:
    async def test_reingest_is_idempotent(self, session_factory) -> None:
        """AC3: running the same chunks twice leaves row counts unchanged."""
        client = FakeIEM({"GFS": GFS_CSV, "NBS": NBS_CSV})
        kwargs = {
            "cities": ["AUS"],
            "start": date(2025, 3, 1),
            "end": date(2025, 3, 31),
            "budget_seconds": 1e9,
        }
        await run_forecast_backfill(client, session_factory, **kwargs)
        first = await _count(session_factory, ForecastIssuance)
        # Force a second fetch of the same (non-recent relative to now) month by clearing progress
        s = await session_factory()
        await s.execute(ForecastArchiveChunk.__table__.delete())
        await s.commit()
        await s.close()
        await run_forecast_backfill(client, session_factory, **kwargs)
        assert await _count(session_factory, ForecastIssuance) == first == 3

    async def test_failure_records_gap_and_continues(self, session_factory) -> None:
        """AC4: a failing model is recorded as an error chunk; others still archive."""
        client = FakeIEM({"GFS": GFS_CSV}, fail={"NAM"})
        out = await run_forecast_backfill(
            client,
            session_factory,
            cities=["AUS"],
            start=date(2025, 3, 1),
            end=date(2025, 3, 31),
            budget_seconds=1e9,
        )
        assert out["statuses"][CHUNK_ERROR] == 1
        s = await session_factory()
        chunks = {c.model: c for c in (await s.execute(select(ForecastArchiveChunk))).scalars()}
        await s.close()
        assert chunks["NAM"].status == CHUNK_ERROR
        assert "429" in chunks["NAM"].last_error or "simulated" in chunks["NAM"].last_error
        assert chunks["GFS"].status == CHUNK_COMPLETE
        # Empty NBS body = coverage gap, still a completed chunk with 0 rows
        assert chunks["NBS"].status == CHUNK_COMPLETE and chunks["NBS"].rows == 0

    async def test_pending_skips_done_but_refreshes_stale_recent_months(
        self, session_factory
    ) -> None:
        months = [date(2026, 8, 1), date(2026, 9, 1), date(2026, 10, 1)]
        now = datetime(2026, 10, 8, 12, 0)
        s = await session_factory()
        for m in months:
            s.add(
                ForecastArchiveChunk(
                    city="NYC",
                    model="GFS",
                    month=m,
                    status=CHUNK_COMPLETE,
                    attempts=1,
                    updated_at=now - timedelta(hours=1),
                )
            )
        s.add(
            ForecastArchiveChunk(
                city="NYC",
                model="NAM",
                month=months[2],
                status=CHUNK_COMPLETE,
                attempts=1,
                updated_at=now - timedelta(hours=7),
            )
        )
        await s.commit()
        todo = await pending_chunks(s, ["NYC"], months, now=now)
        await s.close()
        assert ("NYC", "GFS", months[2]) not in todo  # fresh
        assert ("NYC", "NAM", months[2]) in todo  # stale recent month
        assert ("NYC", "NAM", months[0]) in todo  # never fetched
        assert todo[0][2] == months[2]  # newest first

    async def test_budget_stops_run(self, session_factory) -> None:
        client = FakeIEM({"GFS": GFS_CSV})
        ticks = iter([0.0, 0.0, 999.0, 999.0])
        out = await run_forecast_backfill(
            client,
            session_factory,
            cities=["AUS"],
            start=date(2025, 3, 1),
            end=date(2025, 3, 31),
            budget_seconds=10,
            clock=lambda: next(ticks),
        )
        assert out["processed"] == 1
        assert out["remaining"] == 2


class TestIEMClient:
    async def test_retries_rate_limit_then_succeeds(self) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(
                    429, text="Too many requests from your IP address, slow down."
                )
            assert request.url.params["model"] == "NBS"
            return httpx.Response(200, text=NBS_CSV)

        waits: list[float] = []

        async def fake_sleep(s: float) -> None:
            waits.append(s)

        client = IEMMosClient(
            interval_seconds=0.001,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            sleep=fake_sleep,
        )
        text = await client.fetch_csv("KAUS", "NBS", datetime(2025, 3, 1), datetime(2025, 4, 1))
        await client.close()
        assert text == NBS_CSV
        assert waits and waits[0] >= 30

    async def test_rate_limit_body_with_200_is_treated_as_429(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="Too many requests from your IP address, slow down.")

        async def fake_sleep(s: float) -> None:
            return None

        client = IEMMosClient(
            interval_seconds=0.001,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            sleep=fake_sleep,
        )
        with pytest.raises(IEMRateLimitedError):
            await client.fetch_csv("KAUS", "GFS", datetime(2025, 3, 1), datetime(2025, 4, 1))
        await client.close()


DUP_CSV = NBS_CSV + "2025-03-14 13:00:00,2025-03-16 00:00:00,NBS,73,80.0,2.0,KAUS" + chr(10)


async def test_duplicate_rows_in_one_window_are_deduplicated(session_factory) -> None:
    """IEM sometimes repeats rows; the upsert must not touch one key twice."""
    client = FakeIEM({"NBS": DUP_CSV})
    out = await run_forecast_backfill(
        client,
        session_factory,
        cities=["AUS"],
        start=date(2025, 3, 1),
        end=date(2025, 3, 31),
        budget_seconds=1e9,
    )
    assert out["statuses"].get(CHUNK_ERROR) is None
    s = await session_factory()
    rows = (
        (await s.execute(select(ForecastIssuance).where(ForecastIssuance.model == "NBS")))
        .scalars()
        .all()
    )
    await s.close()
    assert len(rows) == 1 and rows[0].tmax_f == 80.0  # last occurrence wins


async def test_given_up_chunks_retry_after_rest(session_factory) -> None:
    now = datetime(2026, 10, 8, 22, 0)
    month = date(2025, 10, 1)
    s = await session_factory()
    s.add(
        ForecastArchiveChunk(
            city="NYC",
            model="NBS",
            month=month,
            status=CHUNK_ERROR,
            attempts=5,
            updated_at=now - timedelta(hours=1),
        )
    )
    s.add(
        ForecastArchiveChunk(
            city="CHI",
            model="NBS",
            month=month,
            status=CHUNK_ERROR,
            attempts=5,
            updated_at=now - timedelta(hours=7),
        )
    )
    await s.commit()
    todo = await pending_chunks(
        s, ["NYC", "CHI"], [month, date(2025, 11, 1), date(2025, 12, 1)], now=now
    )
    await s.close()
    assert ("NYC", "NBS", month) not in todo  # still resting
    assert ("CHI", "NBS", month) in todo  # rested long enough -> retried
