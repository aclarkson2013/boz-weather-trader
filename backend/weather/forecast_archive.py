"""As-issued station forecast archive: GFS MOS, NAM MOS and NBM (algo v2, slice S3).

Source: Iowa Environmental Mesonet (IEM) MOS archive, bulk CSV per station /
model / time window:
    https://mesonet.agron.iastate.edu/cgi-bin/request/mos.py
        ?station=KNYC&model={GFS|NAM|NBS}&sts=...Z&ets=...Z&format=csv

What we keep per (city, model, run, valid date): the forecast DAILY MAX as
issued (GFS/NAM ``n_x``, NBM ``txn``) and NBM's spread (``xnd``). Max values
sit on the row whose forecast time is 00Z — i.e. the evening of the previous
local day for every US station — so ``valid_date = (ftime_utc - 1 day).date()``.

``available_at = run_ts + conservative latency`` so backtests never use a
forecast before it was actually published (no lookahead).

IEM rate-limits aggressively (HTTP 429 after ~2 quick requests), so requests are
paced (1 per 15 s) and retried with backoff. The backfill works month by month
per station/model and records progress in ``forecast_archive_chunks``.
"""

from __future__ import annotations

import asyncio
import csv
import io
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.common.db_utils import upsert_rows
from backend.common.logging import get_logger
from backend.common.metrics import FORECAST_ARCHIVE_CHUNKS_TOTAL
from backend.common.models import CityEnum, ForecastArchiveChunk, ForecastIssuance
from backend.kalshi.rate_limiter import TokenBucketRateLimiter
from backend.weather.stations import STATION_CONFIGS

logger = get_logger("WEATHER")

IEM_MOS_CSV_URL = "https://mesonet.agron.iastate.edu/cgi-bin/request/mos.py"
ARCHIVE_MODELS: tuple[str, ...] = ("GFS", "NAM", "NBS")
FORECAST_ARCHIVE_START = date(2024, 6, 1)  # One month before the market archive

# Conservative publication latency after the model run time (no lookahead).
MODEL_LATENCY: dict[str, timedelta] = {
    "GFS": timedelta(hours=5),
    "NAM": timedelta(hours=4),
    "NBS": timedelta(hours=2),
}
MAX_COLUMN: dict[str, str] = {"GFS": "n_x", "NAM": "n_x", "NBS": "txn"}

REQUEST_INTERVAL_SECONDS = 20.0
# Few retries per chunk keeps the worst case (2 waits + 3 timeouts ~ 4.5 min) inside the
# task's time limit; a chunk that still fails is simply retried on a later run.
MAX_RETRIES = 2
REQUEST_TIMEOUT_SECONDS = 60.0
MAX_CHUNK_ATTEMPTS = 5
USER_AGENT = "BozWeatherTrader/1.11 (open-source research; respectful rate)"

REFRESH_RECENT_AFTER = timedelta(hours=6)

CHUNK_COMPLETE = "complete"
CHUNK_ERROR = "error"


class IEMRateLimitedError(Exception):
    """IEM kept returning 429 after all retries."""


def _parse_dt(value: str) -> datetime:
    """Parse IEM's 'YYYY-MM-DD HH:MM:SS' (UTC) into a naive UTC datetime."""
    return datetime.fromisoformat(value.strip())


def _float(value: str | None) -> float | None:
    if value is None or value.strip() == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def parse_mos_csv(text: str, city: str, model: str) -> list[dict]:
    """Extract daily-max issuances from an IEM MOS CSV.

    Args:
        text: CSV body from ``mos.py``.
        city: City code the station belongs to.
        model: "GFS", "NAM" or "NBS".

    Returns:
        ``forecast_issuances`` row dicts (one per run x valid date with a max).
    """
    column = MAX_COLUMN[model]
    latency = MODEL_LATENCY[model]
    rows: list[dict] = []
    reader = csv.DictReader(io.StringIO(text))
    for r in reader:
        try:
            ftime = _parse_dt(r["ftime"])
            run_ts = _parse_dt(r["runtime"])
        except (KeyError, ValueError):
            continue
        if ftime.hour != 0:
            continue
        tmax = _float(r.get(column))
        if tmax is None:
            continue
        rows.append(
            {
                "city": CityEnum(city),
                "station": STATION_CONFIGS[city].station_id,
                "model": model,
                "run_ts": run_ts,
                "available_at": run_ts + latency,
                "valid_date": (ftime - timedelta(days=1)).date(),
                "lead_hours": int((ftime - run_ts).total_seconds() // 3600),
                "tmax_f": tmax,
                "tmax_sd_f": _float(r.get("xnd")) if model == "NBS" else None,
            }
        )
    return rows


class IEMMosClient:
    """Paced, retrying client for the IEM MOS CSV endpoint.

    Args:
        interval_seconds: Minimum spacing between requests.
        http_client: Optional pre-built httpx client (tests).
        sleep: Async sleep (patched in tests).
    """

    def __init__(
        self,
        interval_seconds: float = REQUEST_INTERVAL_SECONDS,
        http_client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._limiter = TokenBucketRateLimiter(rate=1.0 / interval_seconds, burst=1)
        self._client = http_client or httpx.AsyncClient(
            timeout=REQUEST_TIMEOUT_SECONDS, headers={"User-Agent": USER_AGENT}
        )
        self._sleep = sleep or asyncio.sleep

    async def close(self) -> None:
        """Close the HTTP client."""
        await self._client.aclose()

    async def fetch_csv(self, station: str, model: str, start: datetime, end: datetime) -> str:
        """Fetch MOS rows for runs in [start, end) as CSV text.

        Raises:
            IEMRateLimitedError: Still rate-limited after all retries.
            httpx.HTTPError: Other HTTP failures after retries.
        """
        params = {
            "station": station,
            "model": model,
            "sts": start.strftime("%Y-%m-%dT%H:%MZ"),
            "ets": end.strftime("%Y-%m-%dT%H:%MZ"),
            "format": "csv",
        }
        last_error = ""
        for attempt in range(MAX_RETRIES + 1):
            await self._limiter.acquire()
            try:
                resp = await self._client.get(IEM_MOS_CSV_URL, params=params)
            except httpx.TransportError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            else:
                if resp.status_code == 200 and not resp.text.startswith("Too many requests"):
                    return resp.text
                last_error = f"HTTP {resp.status_code}"
                if resp.status_code not in (429, 500, 502, 503, 504):
                    resp.raise_for_status()
            if attempt < MAX_RETRIES:
                wait = 30.0 * (2**attempt)
                logger.warning(
                    "IEM request retrying",
                    extra={
                        "data": {
                            "station": station,
                            "model": model,
                            "error": last_error,
                            "wait_seconds": wait,
                        }
                    },
                )
                await self._sleep(wait)
        raise IEMRateLimitedError(f"IEM {station} {model} failed after retries: {last_error}")


def month_chunks(start: date, end: date) -> list[date]:
    """First-of-month dates covering [start, end]."""
    out = []
    d = date(start.year, start.month, 1)
    while d <= end:
        out.append(d)
        d = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
    return out


def _month_bounds(month: date) -> tuple[datetime, datetime]:
    nxt = date(month.year + (month.month == 12), month.month % 12 + 1, 1)
    return datetime.combine(month, datetime.min.time()), datetime.combine(nxt, datetime.min.time())


async def archive_window(
    client: IEMMosClient,
    session: AsyncSession,
    city: str,
    model: str,
    start: datetime,
    end: datetime,
) -> int:
    """Fetch and upsert one station/model window. Returns rows written (caller commits)."""
    station = STATION_CONFIGS[city].station_id
    text = await client.fetch_csv(station, model, start, end)
    rows = parse_mos_csv(text, city, model)
    if not rows:
        logger.warning(
            "No forecast rows in IEM window (coverage gap)",
            extra={
                "data": {
                    "city": city,
                    "model": model,
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                }
            },
        )
    await upsert_rows(
        session,
        ForecastIssuance,
        [{**r, "fetched_at": datetime.now(UTC).replace(tzinfo=None)} for r in rows],
        ["city", "model", "run_ts", "valid_date"],
    )
    return len(rows)


async def pending_chunks(
    session: AsyncSession,
    cities: list[str],
    months: list[date],
    now: datetime | None = None,
) -> list[tuple[str, str, date]]:
    """(city, model, month) chunks still to fetch, newest month first.

    Completed chunks are skipped, except the current and previous month (still
    filling with new runs), which are re-fetched once their last fetch is older
    than ``REFRESH_RECENT_AFTER``.
    """
    now = (now or datetime.now(UTC)).replace(tzinfo=None)
    rows = (await session.execute(select(ForecastArchiveChunk))).scalars().all()
    recent = set(months[-2:])
    done = set()
    for r in rows:
        key = (r.city.value if hasattr(r.city, "value") else str(r.city), r.model, r.month)
        gave_up = (r.attempts or 0) >= MAX_CHUNK_ATTEMPTS and r.status != CHUNK_COMPLETE
        fresh = r.updated_at is not None and now - r.updated_at < REFRESH_RECENT_AFTER
        if gave_up or (r.status == CHUNK_COMPLETE and (r.month not in recent or fresh)):
            done.add(key)
    return [
        (city, model, m)
        for m in sorted(months, reverse=True)
        for city in cities
        for model in ARCHIVE_MODELS
        if (city, model, m) not in done
    ]


async def _record_chunk(
    session: AsyncSession,
    city: str,
    model: str,
    month: date,
    status: str,
    rows: int,
    error: str | None,
) -> None:
    existing = await session.get(ForecastArchiveChunk, (CityEnum(city), model, month))
    attempts = (existing.attempts if existing else 0) + 1
    await upsert_rows(
        session,
        ForecastArchiveChunk,
        [
            {
                "city": CityEnum(city),
                "model": model,
                "month": month,
                "status": status,
                "rows": rows,
                "attempts": attempts,
                "last_error": error,
                "updated_at": datetime.now(UTC).replace(tzinfo=None),
            }
        ],
        ["city", "model", "month"],
    )


async def run_forecast_backfill(
    client: IEMMosClient,
    session_factory: Callable[[], Awaitable[AsyncSession]],
    *,
    cities: list[str],
    start: date,
    end: date,
    budget_seconds: float,
    clock: Callable[[], float] = time.monotonic,
) -> dict:
    """Archive pending month chunks until the time budget is spent (each committed alone).

    Returns:
        Dict with processed / remaining counts and per-status counts.
    """
    t0 = clock()
    session = await session_factory()
    try:
        todo = await pending_chunks(session, cities, month_chunks(start, end))
    finally:
        await session.close()

    statuses: Counter[str] = Counter()
    processed = 0
    for city, model, month in todo:
        if clock() - t0 >= budget_seconds:
            break
        lo, hi = _month_bounds(month)
        session = await session_factory()
        try:
            try:
                n = await archive_window(client, session, city, model, lo, hi)
                await session.commit()
                status, err = CHUNK_COMPLETE, None
            except Exception as exc:
                await session.rollback()
                n, status, err = 0, CHUNK_ERROR, f"{type(exc).__name__}: {exc}"[:500]
                logger.warning(
                    "Forecast archive chunk failed",
                    extra={
                        "data": {
                            "city": city,
                            "model": model,
                            "month": str(month),
                            "error": err[:200],
                        }
                    },
                )
            await _record_chunk(session, city, model, month, status, n, err)
            await session.commit()
        finally:
            await session.close()
        statuses[status] += 1
        processed += 1
        FORECAST_ARCHIVE_CHUNKS_TOTAL.labels(model=model, status=status).inc()

    remaining = len(todo) - processed
    logger.info(
        "Forecast archive run finished",
        extra={
            "data": {"processed": processed, "remaining": remaining, "statuses": dict(statuses)}
        },
    )
    return {"processed": processed, "remaining": remaining, "statuses": dict(statuses)}
