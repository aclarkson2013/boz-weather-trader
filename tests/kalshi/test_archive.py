"""Tests for the algo-v2 Kalshi market archive (slice S1).

Covers the S1 acceptance criteria in docs/ALGO_V2_PRD.md:
  AC2 idempotent re-ingest · AC3 historical routing · AC4 legacy + KX parsing ·
  AC5 era tagging · AC6 resumable backfill.
All Kalshi access goes through a fake client — no network.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.common.models import (
    Base,
    CityEnum,
    KalshiArchiveDay,
    KalshiArchivedMarket,
    KalshiCandle,
    KalshiQuote,
)
from backend.kalshi.archive import (
    DAY_COMPLETE,
    DAY_EMPTY,
    DAY_ERROR,
    DAY_UNSETTLED,
    archive_event,
    check_tiling,
    city_for_ticker,
    dollars_to_cents,
    era_for,
    event_ticker_for,
    parse_candle,
    parse_market,
    pending_days,
    record_quotes,
    run_backfill,
    series_for,
    summarize_candles,
)

# ─── Fixtures & fakes ───


def _market(
    ticker: str,
    strike_type: str,
    floor: float | None,
    cap: float | None,
    result: str = "no",
    expiration_value: str = "74.00",
    status: str = "finalized",
) -> dict:
    return {
        "ticker": ticker,
        "event_ticker": ticker.rsplit("-", 1)[0],
        "strike_type": strike_type,
        "floor_strike": floor,
        "cap_strike": cap,
        "result": result,
        "expiration_value": expiration_value,
        "status": status,
        "open_time": "2026-10-05T14:00:00Z",
        "close_time": "2026-10-07T05:00:00Z",
    }


def make_event(event_ticker: str, winner: int = 2, settled: bool = True) -> list[dict]:
    """Six tiling brackets: <=69, 70-71, 72-73, 74-75, 76-77, >=78."""
    specs = [
        ("T70", "less", None, 70),
        ("B70.5", "between", 70, 71),
        ("B72.5", "between", 72, 73),
        ("B74.5", "between", 74, 75),
        ("B76.5", "between", 76, 77),
        ("T77", "greater", 77, None),
    ]
    out = []
    for i, (suffix, st, fl, cp) in enumerate(specs):
        if settled:
            result = "yes" if i == winner else "no"
            status = "finalized"
        else:
            result = ""
            status = "active"
        out.append(_market(f"{event_ticker}-{suffix}", st, fl, cp, result=result, status=status))
    return out


def make_candles(n: int = 3, start_ts: int = 1791200000) -> list[dict]:
    return [
        {
            "end_period_ts": start_ts + 3600 * i,
            "price": {"close_dollars": "0.4500"},
            "yes_bid": {
                "open_dollars": "0.4000",
                "high_dollars": "0.4400",
                "low_dollars": "0.3900",
                "close_dollars": "0.4400",
            },
            "yes_ask": {
                "open_dollars": "0.4200",
                "high_dollars": "0.4700",
                "low_dollars": "0.4100",
                "close_dollars": "0.4600",
            },
            "volume_fp": "12.50",
            "open_interest_fp": "100.00",
        }
        for i in range(n)
    ]


class FakeClient:
    """Stands in for KalshiPublicClient; records every call."""

    def __init__(
        self,
        markets: dict[tuple[str, bool], list[dict]] | None = None,
        cutoff: datetime = datetime(2026, 8, 8, tzinfo=UTC),
        fail_tickers: set[str] | None = None,
    ) -> None:
        self.markets = markets or {}
        self.cutoff = cutoff
        self.fail_tickers = fail_tickers or set()
        self.calls: list[tuple] = []

    async def get_historical_cutoff(self) -> datetime:
        return self.cutoff

    async def get_event_markets(self, event_ticker: str, historical: bool = False) -> list[dict]:
        self.calls.append(("markets", event_ticker, historical))
        return [dict(m) for m in self.markets.get((event_ticker, historical), [])]

    async def get_candlesticks(
        self,
        series_ticker: str,
        market_ticker: str,
        start_ts: int,
        end_ts: int,
        period_minutes: int = 60,
        historical: bool = False,
    ) -> list[dict]:
        self.calls.append(("candles", market_ticker, historical))
        if market_ticker in self.fail_tickers:
            raise RuntimeError("simulated network failure")
        return make_candles()


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator:
    """Fresh in-memory DB per test; returns an async session factory (commits allowed)."""
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    async def factory() -> AsyncSession:
        return maker()

    yield factory
    await engine.dispose()


async def _count(factory, model) -> int:
    session = await factory()
    try:
        return (await session.execute(select(func.count()).select_from(model))).scalar_one()
    finally:
        await session.close()


# ─── Pure helpers ───


class TestTickersAndEras:
    def test_series_switches_on_legacy_cutover(self) -> None:
        assert series_for("NYC", date(2024, 10, 23)) == "HIGHNY"
        assert series_for("NYC", date(2024, 10, 24)) == "KXHIGHNY"
        assert series_for("AUS", date(2024, 7, 1)) == "HIGHAUS"
        assert event_ticker_for("CHI", date(2026, 10, 6)) == "KXHIGHCHI-26OCT06"
        assert event_ticker_for("MIA", date(2024, 7, 1)) == "HIGHMIA-24JUL01"

    def test_city_for_legacy_and_current_tickers(self) -> None:
        assert city_for_ticker("HIGHNY-24JUL01-B80.5") == "NYC"
        assert city_for_ticker("KXHIGHAUS-26OCT06-T90") == "AUS"
        assert city_for_ticker("KXHIGHLAX-26OCT06-T90") is None

    @pytest.mark.parametrize(
        ("d", "era"),
        [
            (date(2024, 6, 30), "E0"),
            (date(2024, 7, 1), "E1"),
            (date(2026, 8, 13), "E1"),
            (date(2026, 8, 14), "E2"),  # AC5
            (date(2026, 10, 6), "E2"),
        ],
    )
    def test_era_boundaries(self, d: date, era: str) -> None:
        assert era_for(d) == era


class TestParseMarket:
    def test_legacy_middle_bracket(self) -> None:
        """AC4: HIGHNY-24JUL01-B80.5 → NYC, 2024-07-01, covers 80–81 → [79.5, 81.5)."""
        raw = _market(
            "HIGHNY-24JUL01-B80.5", "between", 80, 81, result="yes", expiration_value="81.00"
        )
        row = parse_market(raw, "historical")
        assert row is not None
        assert row["city"] == CityEnum.NYC
        assert row["event_date"] == date(2024, 7, 1)
        assert (row["lower_bound_f"], row["upper_bound_f"]) == (79.5, 81.5)
        assert row["label"] == "80° to 81°F"
        assert row["series_ticker"] == "HIGHNY"
        assert row["era"] == "E1"
        assert row["result"] == "yes"
        assert row["expiration_value"] == 81.0

    def test_current_bottom_catch_all(self) -> None:
        """AC4: KXHIGHNY-26OCT06-T63 ("less", cap 63) → covers ≤62 → upper bound 62.5."""
        raw = _market(
            "KXHIGHNY-26OCT06-T63", "less", None, 63, result="yes", expiration_value="60.00"
        )
        row = parse_market(raw, "live")
        assert row is not None
        assert row["city"] == CityEnum.NYC
        assert row["event_date"] == date(2026, 10, 6)
        assert row["lower_bound_f"] is None
        assert row["upper_bound_f"] == 62.5
        assert row["era"] == "E2"
        assert row["source"] == "live"

    def test_unsettled_result_and_blank_expiration_are_none(self) -> None:
        raw = _market("KXHIGHNY-26OCT08-B74.5", "between", 74, 75, result="", expiration_value="")
        row = parse_market(raw, "live")
        assert row is not None
        assert row["result"] is None
        assert row["expiration_value"] is None

    def test_unknown_series_is_skipped(self) -> None:
        assert parse_market(_market("KXHIGHLAX-26OCT06-T63", "less", None, 63), "live") is None


class TestTiling:
    def test_complete_event_tiles(self) -> None:
        rows = [parse_market(m, "live") for m in make_event("KXHIGHNY-26OCT06")]
        assert check_tiling(rows) is True
        assert all(r["tiles_ok"] for r in rows)

    def test_gap_flags_every_row_without_dropping(self) -> None:
        markets = make_event("KXHIGHNY-26OCT06")
        markets[2]["floor_strike"], markets[2]["cap_strike"] = 73, 74  # gap after 70–71
        rows = [parse_market(m, "live") for m in markets]
        assert check_tiling(rows) is False
        assert len(rows) == 6
        assert not any(r["tiles_ok"] for r in rows)


class TestCandles:
    def test_live_and_historical_formats_match(self) -> None:
        live = make_candles(1)[0]
        hist = {
            "end_period_ts": live["end_period_ts"],
            "price": {"close": "0.4500"},
            "yes_bid": {"open": "0.4000", "high": "0.4400", "low": "0.3900", "close": "0.4400"},
            "yes_ask": {"open": "0.4200", "high": "0.4700", "low": "0.4100", "close": "0.4600"},
            "volume": "12.50",
            "open_interest": "100.00",
        }
        a, b = parse_candle(live, "T"), parse_candle(hist, "T")
        assert a == b
        assert a["yes_bid_close"] == 44
        assert a["yes_ask_close"] == 46
        assert a["price_close"] == 45
        assert a["volume"] == 12.5
        assert a["subcent"] is False
        assert a["end_ts"].tzinfo is None

    def test_subcent_and_missing_trade_price(self) -> None:
        raw = make_candles(1)[0]
        raw["yes_bid"]["close_dollars"] = "0.4450"
        raw["price"] = {}
        row = parse_candle(raw, "T")
        assert row["subcent"] is True
        assert row["price_close"] is None

    def test_dollars_to_cents(self) -> None:
        assert dollars_to_cents("0.0100") == (1, False)
        assert dollars_to_cents("1.0000") == (100, False)
        assert dollars_to_cents("") == (None, False)
        assert dollars_to_cents(None) == (None, False)

    def test_median_spread_ignores_one_sided_quotes(self) -> None:
        candles = [
            {"yes_bid_close": 40, "yes_ask_close": 42},
            {"yes_bid_close": 0, "yes_ask_close": 1},  # no bid
            {"yes_bid_close": 99, "yes_ask_close": 100},  # no ask
            {"yes_bid_close": 50, "yes_ask_close": 54},
        ]
        assert summarize_candles(candles) == (4, 3.0)


# ─── DB-backed behavior ───


EV = "KXHIGHNY-26JUL01"  # Settled well before the fake cutoff (2026-08-08)


class TestArchiveEvent:
    async def test_reingest_is_idempotent(self, session_factory) -> None:
        """AC2: archiving the same settled event twice leaves row counts unchanged."""
        client = FakeClient({(EV, True): make_event(EV)})
        for _ in range(2):
            session = await session_factory()
            status, n = await archive_event(client, session, "NYC", date(2026, 7, 1), client.cutoff)
            await session.commit()
            await session.close()
            assert (status, n) == (DAY_COMPLETE, 6)
        assert await _count(session_factory, KalshiArchivedMarket) == 6
        assert await _count(session_factory, KalshiCandle) == 18

    async def test_settled_before_cutoff_uses_historical(self, session_factory) -> None:
        """AC3: a market settled before the cutoff is read from /historical and tagged so."""
        client = FakeClient({(EV, True): make_event(EV)})
        session = await session_factory()
        await archive_event(client, session, "NYC", date(2026, 7, 1), client.cutoff)
        await session.commit()
        rows = (await session.execute(select(KalshiArchivedMarket))).scalars().all()
        await session.close()
        assert {r.source for r in rows} == {"historical"}
        assert client.calls[0] == ("markets", EV, True)
        assert all(c[2] is True for c in client.calls if c[0] == "candles")
        assert all(r.n_candles == 3 and r.median_spread_cents == 2.0 for r in rows)

    async def test_falls_back_to_live_when_historical_empty(self, session_factory) -> None:
        client = FakeClient({(EV, False): make_event(EV)})
        session = await session_factory()
        status, _ = await archive_event(client, session, "NYC", date(2026, 7, 1), client.cutoff)
        await session.commit()
        rows = (await session.execute(select(KalshiArchivedMarket))).scalars().all()
        await session.close()
        assert status == DAY_COMPLETE
        assert {r.source for r in rows} == {"live"}

    async def test_unsettled_event_stores_markets_but_no_candles(self, session_factory) -> None:
        ev = "KXHIGHNY-26OCT08"
        client = FakeClient({(ev, False): make_event(ev, settled=False)})
        session = await session_factory()
        status, n = await archive_event(client, session, "NYC", date(2026, 10, 8), client.cutoff)
        await session.commit()
        await session.close()
        assert (status, n) == (DAY_UNSETTLED, 6)
        assert await _count(session_factory, KalshiCandle) == 0
        assert not any(c[0] == "candles" for c in client.calls)

    async def test_missing_event_is_empty(self, session_factory) -> None:
        client = FakeClient({})
        session = await session_factory()
        assert await archive_event(client, session, "NYC", date(2026, 7, 1), client.cutoff) == (
            DAY_EMPTY,
            0,
        )
        await session.close()


class TestBackfill:
    async def test_interrupted_backfill_resumes_without_gaps_or_duplicates(
        self, session_factory
    ) -> None:
        """AC6: a run that fails mid-day and stops on budget resumes cleanly next time."""
        days = [date(2026, 7, d) for d in (1, 2, 3)]
        markets = {}
        for d in days:
            ev = event_ticker_for("NYC", d)
            markets[(ev, True)] = make_event(ev)
        # Day 3 (processed first — newest first) fails on its 4th market's candles.
        broken = FakeClient(markets, fail_tickers={f"{event_ticker_for('NYC', days[2])}-B74.5"})

        ticks = iter([0.0, 0.0, 1.0, 100.0, 100.0])  # budget=50: day3 + day2 run, then stop

        first = await run_backfill(
            broken,
            session_factory,
            start=days[0],
            end=days[-1],
            cities=["NYC"],
            budget_seconds=50.0,
            clock=lambda: next(ticks),
        )
        assert first["processed"] == 2
        assert first["statuses"] == {DAY_ERROR: 1, DAY_COMPLETE: 1}
        assert first["remaining"] == 1
        # The failed day rolled back entirely: only day 2's 6 markets / 18 candles exist.
        assert await _count(session_factory, KalshiArchivedMarket) == 6
        assert await _count(session_factory, KalshiCandle) == 18

        healthy = FakeClient(markets)
        second = await run_backfill(
            healthy,
            session_factory,
            start=days[0],
            end=days[-1],
            cities=["NYC"],
            budget_seconds=1e9,
        )
        assert second["processed"] == 2  # day 3 (retry) + day 1; day 2 not redone
        assert second["remaining"] == 0
        assert await _count(session_factory, KalshiArchivedMarket) == 18
        assert await _count(session_factory, KalshiCandle) == 54

        session = await session_factory()
        rows = (await session.execute(select(KalshiArchiveDay))).scalars().all()
        await session.close()
        by_date = {r.event_date: r for r in rows}
        assert {r.status for r in rows} == {DAY_COMPLETE}
        assert by_date[days[2]].attempts == 2
        assert by_date[days[1]].attempts == 1

    async def test_pending_days_skips_complete_and_final(self, session_factory) -> None:
        session = await session_factory()
        session.add_all(
            [
                KalshiArchiveDay(
                    city=CityEnum.NYC,
                    event_date=date(2026, 7, 1),
                    event_ticker="x",
                    status=DAY_COMPLETE,
                    attempts=1,
                ),
                KalshiArchiveDay(
                    city=CityEnum.NYC,
                    event_date=date(2026, 7, 2),
                    event_ticker="x",
                    status=DAY_EMPTY,
                    attempts=2,
                ),
                KalshiArchiveDay(
                    city=CityEnum.NYC,
                    event_date=date(2026, 7, 3),
                    event_ticker="x",
                    status=DAY_UNSETTLED,
                    attempts=1,
                ),
            ]
        )
        await session.commit()
        todo = await pending_days(session, date(2026, 7, 1), date(2026, 7, 4), ["NYC"])
        await session.close()
        assert todo == [("NYC", date(2026, 7, 4)), ("NYC", date(2026, 7, 3))]


class TestQuotes:
    async def test_records_only_active_markets(self, session_factory) -> None:
        ev = "KXHIGHNY-26OCT08"
        live = make_event(ev, settled=False)
        for m in live:
            m.update(
                yes_bid_dollars="0.4900",
                yes_ask_dollars="0.5200",
                yes_bid_size_fp="25.00",
                yes_ask_size_fp="2.00",
                last_price_dollars="0.5000",
                volume_fp="100.00",
            )
        live[0]["status"] = "closed"
        client = FakeClient({(ev, False): live})
        session = await session_factory()
        written = await record_quotes(
            client, session, [("NYC", date(2026, 10, 8))], now=datetime(2026, 10, 7, 18, 0)
        )
        await session.commit()
        rows = (await session.execute(select(KalshiQuote))).scalars().all()
        await session.close()
        assert written == 5
        assert len(rows) == 5
        q = rows[0]
        assert (q.yes_bid, q.yes_ask, q.yes_ask_size) == (49, 52, 2.0)
        assert q.event_date == date(2026, 10, 8)
