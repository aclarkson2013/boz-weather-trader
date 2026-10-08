"""Kalshi market archive — markets, outcomes and hourly bid/ask candles (algo v2, slice S1).

Archives every KXHIGH* market (and its pre-2024-10-24 ``HIGH*`` predecessor)
from Kalshi's public API into PostgreSQL so strategies can be backtested on
REAL prices instead of synthetic ones. See docs/ALGO_V2_PRD.md.

Design notes:
- Unit of work = one city-day (one event, ~6 markets). Progress is tracked in
  ``kalshi_archive_days`` so an interrupted backfill resumes where it stopped.
- Writes are idempotent upserts keyed on natural keys (ticker; ticker+period+end_ts).
- Labels come only from Kalshi (``result`` / ``expiration_value``), never from the
  NWS ``settlements`` table: Kalshi switched settlement source to The Weather
  Company for events from 2026-08-14 (era E2).
- Events whose brackets don't tile the temperature line are kept but flagged
  ``tiles_ok=False``, never silently dropped.

Usage:
    from backend.kalshi.archive import run_backfill, record_quotes
"""

from __future__ import annotations

import math
import statistics
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.common.db_utils import upsert_rows
from backend.common.logging import get_logger
from backend.common.metrics import KALSHI_ARCHIVE_DAYS_TOTAL, KALSHI_QUOTES_RECORDED_TOTAL
from backend.common.models import (
    CityEnum,
    KalshiArchiveDay,
    KalshiArchivedMarket,
    KalshiCandle,
    KalshiQuote,
)
from backend.kalshi.markets import (
    SERIES_TO_CITY,
    WEATHER_SERIES_TICKERS,
    parse_bracket_from_market,
    parse_market_date_from_ticker,
)
from backend.kalshi.public_client import KalshiPublicClient

logger = get_logger("MARKET")

# ─── Constants ───

ARCHIVE_START = date(2024, 7, 1)  # Era E1 start; earlier quotes too wide to use
LEGACY_CUTOVER = date(2024, 10, 24)  # First event date using the KX* series prefix
E2_START = date(2026, 8, 14)  # Settlement source -> The Weather Company
CANDLE_PERIOD_MINUTES = 60
MAX_DAY_ATTEMPTS = 5  # Give up on a city-day after this many failed attempts
EMPTY_FINAL_AFTER_ATTEMPTS = 2  # "No markets" is final after this many tries

LEGACY_SERIES_TICKERS: dict[str, str] = {
    "NYC": "HIGHNY",
    "CHI": "HIGHCHI",
    "MIA": "HIGHMIA",
    "AUS": "HIGHAUS",
}
LEGACY_SERIES_TO_CITY: dict[str, str] = {v: k for k, v in LEGACY_SERIES_TICKERS.items()}
ALL_SERIES_TO_CITY: dict[str, str] = {**SERIES_TO_CITY, **LEGACY_SERIES_TO_CITY}

DAY_COMPLETE = "complete"
DAY_EMPTY = "empty"
DAY_UNSETTLED = "unsettled"
DAY_ERROR = "error"
DAY_IRREGULAR = "irregular"  # Settled, but not cleanly yes/no (e.g. "scalar")


# ─── Pure helpers ───


def series_for(city: str, event_date: date) -> str:
    """Return the series ticker Kalshi used for a city on a given event date.

    Args:
        city: City code (NYC, CHI, MIA, AUS).
        event_date: Event (LST) date.

    Returns:
        "HIGHNY"-style legacy series before 2024-10-24, else "KXHIGHNY"-style.
    """
    if event_date < LEGACY_CUTOVER:
        return LEGACY_SERIES_TICKERS[city]
    return WEATHER_SERIES_TICKERS[city]


def event_ticker_for(city: str, event_date: date) -> str:
    """Build the event ticker for a city-day, e.g. "HIGHNY-24JUL01" or "KXHIGHNY-26OCT06"."""
    return f"{series_for(city, event_date)}-{event_date.strftime('%y%b%d').upper()}"


def era_for(event_date: date) -> str:
    """Classify an event date into an algo-v2 data era.

    Returns:
        "E0" (< 2024-07-01, excluded), "E1" (NWS CLI settlement, to 2026-08-13)
        or "E2" (The Weather Company settlement, from 2026-08-14).
    """
    if event_date < ARCHIVE_START:
        return "E0"
    if event_date < E2_START:
        return "E1"
    return "E2"


def city_for_ticker(ticker: str) -> str | None:
    """Map a market/event ticker (current or legacy prefix) to its city code."""
    return ALL_SERIES_TO_CITY.get(ticker.split("-")[0].upper())


def _parse_ts(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp into a naive UTC datetime (DB convention)."""
    if not value:
        return None
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC).replace(tzinfo=None)
    return dt


def _float_or_none(value: Any) -> float | None:
    """Convert Kalshi numeric strings ("60.00", "") to float, or None."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def dollars_to_cents(value: Any) -> tuple[int | None, bool]:
    """Convert a Kalshi dollar string (e.g. "0.4400") to integer cents.

    Args:
        value: Dollar amount as string/number, or None/"".

    Returns:
        (cents, subcent) — subcent is True when the value was not a whole cent.
    """
    f = _float_or_none(value)
    if f is None:
        return None, False
    raw = f * 100.0
    cents = int(round(raw))
    return cents, abs(raw - cents) > 1e-6


def parse_market(raw: dict, source: str) -> dict | None:
    """Normalize one raw Kalshi market dict into a ``kalshi_markets`` row.

    Args:
        raw: Market dict from the live or historical markets endpoint.
        source: "live" or "historical".

    Returns:
        Row dict, or None if the ticker isn't a known weather series.
    """
    ticker = raw.get("ticker") or ""
    city = city_for_ticker(ticker)
    event_date = parse_market_date_from_ticker(ticker)
    if city is None or event_date is None:
        logger.warning(
            "Skipping unrecognized market ticker",
            extra={"data": {"ticker": ticker}},
        )
        return None

    bracket = parse_bracket_from_market(raw)
    result = (raw.get("result") or "").lower() or None
    return {
        "ticker": ticker,
        "event_ticker": raw.get("event_ticker") or ticker.rsplit("-", 1)[0],
        "series_ticker": ticker.split("-")[0],
        "city": CityEnum(city),
        "event_date": event_date,
        "label": bracket["label"],
        "strike_type": raw.get("strike_type"),
        "floor_strike": _float_or_none(raw.get("floor_strike")),
        "cap_strike": _float_or_none(raw.get("cap_strike")),
        "lower_bound_f": bracket["lower_bound_f"],
        "upper_bound_f": bracket["upper_bound_f"],
        "tiles_ok": True,
        "open_time": _parse_ts(raw.get("open_time")),
        "close_time": _parse_ts(raw.get("close_time")),
        "status": raw.get("status"),
        "result": result,
        "expiration_value": _float_or_none(raw.get("expiration_value")),
        "era": era_for(event_date),
        "source": source,
    }


def infer_missing_strikes(raw_markets: list[dict]) -> int:
    """Fill in strikes Kalshi omitted, using the ticker suffix and sibling markets.

    Kalshi's historical data for events 2025-01-16 .. 2025-02-09 lacks
    ``strike_type`` / ``floor_strike`` / ``cap_strike`` on the WINNING market
    only. The ticker still encodes the strike:
    - ``B30.5`` -> "between", floor 30, cap 31 (covers integers 30-31)
    - ``T75``   -> bottom ("less", cap 75) if 75 is at or below every sibling
      middle's floor, else top ("greater", floor 75)

    Mutates the raw dicts in place.

    Args:
        raw_markets: Raw market dicts of ONE event.

    Returns:
        Number of markets whose strikes were inferred.
    """
    middle_floors = [
        float(m["floor_strike"])
        for m in raw_markets
        if m.get("floor_strike") is not None and m.get("cap_strike") is not None
    ]
    inferred = 0
    for m in raw_markets:
        if m.get("floor_strike") is not None or m.get("cap_strike") is not None:
            continue
        suffix = (m.get("ticker") or "").rsplit("-", 1)[-1].upper()
        try:
            value = float(suffix[1:])
        except ValueError:
            continue
        if suffix.startswith("B"):
            lo = math.floor(value)
            m.update(strike_type="between", floor_strike=lo, cap_strike=lo + 1)
        elif suffix.startswith("T"):
            if not middle_floors or value <= min(middle_floors):
                m.update(strike_type="less", floor_strike=None, cap_strike=value)
            else:
                m.update(strike_type="greater", floor_strike=value, cap_strike=None)
        else:
            continue
        inferred += 1
    if inferred:
        logger.info(
            "Inferred missing strikes from tickers",
            extra={"data": {"event_ticker": raw_markets[0].get("event_ticker"), "count": inferred}},
        )
    return inferred


def check_tiling(rows: list[dict]) -> bool:
    """Check that an event's brackets partition the temperature line exactly.

    Requires exactly one bottom catch-all, one top catch-all, and adjacent
    continuous bounds that meet (no gaps/overlaps). Sets ``tiles_ok`` on every row.

    Args:
        rows: Parsed market rows of ONE event.

    Returns:
        True if the event tiles correctly.
    """
    bottoms = [r for r in rows if r["lower_bound_f"] is None and r["upper_bound_f"] is not None]
    tops = [r for r in rows if r["upper_bound_f"] is None and r["lower_bound_f"] is not None]
    ok = len(bottoms) == 1 and len(tops) == 1 and all(r["label"] != "Unknown" for r in rows)
    if ok:
        ordered = sorted(
            rows,
            key=lambda r: float("-inf") if r["lower_bound_f"] is None else r["lower_bound_f"],
        )
        for prev, nxt in zip(ordered, ordered[1:], strict=False):
            if (
                prev["upper_bound_f"] is None
                or nxt["lower_bound_f"] is None
                or abs(prev["upper_bound_f"] - nxt["lower_bound_f"]) > 0.01
            ):
                ok = False
                break
    for r in rows:
        r["tiles_ok"] = ok
    if not ok:
        logger.warning(
            "Archived event brackets do not tile",
            extra={"data": {"event_ticker": rows[0]["event_ticker"] if rows else None}},
        )
    return ok


def _ohlc(raw: dict, group: str, field: str) -> tuple[int | None, bool]:
    """Read one OHLC field from a live ("close_dollars") or historical ("close") candle."""
    g = raw.get(group) or {}
    value = g.get(f"{field}_dollars", g.get(field))
    return dollars_to_cents(value)


def parse_candle(raw: dict, ticker: str, period_minutes: int = CANDLE_PERIOD_MINUTES) -> dict:
    """Normalize a live or historical candlestick into a ``kalshi_candles`` row.

    Args:
        raw: Candle dict from either candlestick endpoint.
        ticker: Market ticker the candle belongs to.
        period_minutes: Candle period.

    Returns:
        Row dict with integer-cent prices and a ``subcent`` flag.
    """
    row: dict[str, Any] = {
        "ticker": ticker,
        "period_min": period_minutes,
        "end_ts": datetime.fromtimestamp(int(raw["end_period_ts"]), UTC).replace(tzinfo=None),
    }
    subcent = False
    for group in ("yes_bid", "yes_ask"):
        for field in ("open", "high", "low", "close"):
            cents, sub = _ohlc(raw, group, field)
            row[f"{group}_{field}"] = cents
            subcent = subcent or sub
    price_close, sub = _ohlc(raw, "price", "close")
    row["price_close"] = price_close
    row["subcent"] = subcent or sub
    row["volume"] = _float_or_none(raw.get("volume_fp", raw.get("volume")))
    row["open_interest"] = _float_or_none(raw.get("open_interest_fp", raw.get("open_interest")))
    return row


def summarize_candles(candles: list[dict]) -> tuple[int, float | None]:
    """Return (candle count, median closing bid/ask spread in cents over two-sided quotes)."""
    spreads = [
        c["yes_ask_close"] - c["yes_bid_close"]
        for c in candles
        if c["yes_bid_close"] is not None
        and c["yes_ask_close"] is not None
        and 0 < c["yes_bid_close"] <= c["yes_ask_close"] < 100
    ]
    return len(candles), (float(statistics.median(spreads)) if spreads else None)


# ─── Persistence ───


def _unix(dt: datetime) -> int:
    """Naive-UTC datetime -> unix seconds."""
    return int(dt.replace(tzinfo=UTC).timestamp())


async def archive_event(
    client: KalshiPublicClient,
    session: AsyncSession,
    city: str,
    event_date: date,
    cutoff: datetime,
) -> tuple[str, int]:
    """Archive one city-day: its markets, outcomes and hourly candles.

    Settled events are fetched from ``/historical/...`` when they are older than
    the cutoff (falling back to the live endpoint, and vice versa). Candles are
    only archived once every market of the event has a yes/no result.

    Args:
        client: Public Kalshi client.
        session: Async DB session (caller commits/rolls back).
        city: City code.
        event_date: Event (LST) date.
        cutoff: Historical cutoff (aware UTC datetime).

    Returns:
        (status, market_count) where status is "complete", "unsettled" or "empty".
    """
    event_ticker = event_ticker_for(city, event_date)
    settle_by = datetime.combine(event_date + timedelta(days=2), datetime.min.time(), UTC)
    prefer_historical = settle_by <= cutoff

    raw = await client.get_event_markets(event_ticker, historical=prefer_historical)
    source = "historical" if prefer_historical else "live"
    if not raw:
        raw = await client.get_event_markets(event_ticker, historical=not prefer_historical)
        source = "live" if prefer_historical else "historical"

    infer_missing_strikes(raw)
    rows = [r for r in (parse_market(m, source) for m in raw) if r is not None]
    if not rows:
        return DAY_EMPTY, 0

    check_tiling(rows)
    settled = all(r["result"] is not None for r in rows)
    irregular = settled and any(r["result"] not in ("yes", "no") for r in rows)
    if not settled:
        for r in rows:
            r.update(n_candles=None, median_spread_cents=None, candles_fetched_at=None)
        await upsert_rows(session, KalshiArchivedMarket, rows, ["ticker"])
        return DAY_UNSETTLED, len(rows)

    fetched_at = datetime.now(UTC).replace(tzinfo=None)
    for r in rows:
        candles: list[dict] = []
        if r["open_time"] is not None and r["close_time"] is not None:
            raw_candles = await client.get_candlesticks(
                r["series_ticker"],
                r["ticker"],
                _unix(r["open_time"]),
                _unix(r["close_time"]),
                CANDLE_PERIOD_MINUTES,
                historical=(source == "historical"),
            )
            candles = [parse_candle(c, r["ticker"]) for c in raw_candles]
            await upsert_rows(session, KalshiCandle, candles, ["ticker", "period_min", "end_ts"])
        n, median_spread = summarize_candles(candles)
        r.update(n_candles=n, median_spread_cents=median_spread, candles_fetched_at=fetched_at)

    await upsert_rows(session, KalshiArchivedMarket, rows, ["ticker"])
    return (DAY_IRREGULAR if irregular else DAY_COMPLETE), len(rows)


async def pending_days(
    session: AsyncSession,
    start: date,
    end: date,
    cities: list[str],
) -> list[tuple[str, date]]:
    """List city-days in [start, end] that still need archiving, newest first.

    A city-day is done when it is "complete", or "empty"/"error" after enough
    attempts. "unsettled" and fresh failures are retried.
    """
    result = await session.execute(
        select(
            KalshiArchiveDay.city,
            KalshiArchiveDay.event_date,
            KalshiArchiveDay.status,
            KalshiArchiveDay.attempts,
        ).where(KalshiArchiveDay.event_date >= start, KalshiArchiveDay.event_date <= end)
    )
    done: set[tuple[str, date]] = set()
    result_rows = result.all()
    for city, d, status, attempts in result_rows:
        city_code = city.value if hasattr(city, "value") else str(city)
        if (
            status in (DAY_COMPLETE, DAY_IRREGULAR)
            or (status == DAY_EMPTY and (attempts or 0) >= EMPTY_FINAL_AFTER_ATTEMPTS)
            or (status == DAY_ERROR and (attempts or 0) >= MAX_DAY_ATTEMPTS)
        ):
            done.add((city_code, d))

    # Repair: "complete" days whose brackets don't tile are retried (e.g. after a
    # parser fix) until they tile or run out of attempts.
    broken = await session.execute(
        select(KalshiArchivedMarket.city, KalshiArchivedMarket.event_date)
        .where(
            KalshiArchivedMarket.tiles_ok.is_(False),
            KalshiArchivedMarket.event_date >= start,
            KalshiArchivedMarket.event_date <= end,
        )
        .distinct()
    )
    attempts_by_day = {
        (c.value if hasattr(c, "value") else str(c), d): a for c, d, _, a in result_rows
    }
    for city, d in broken.all():
        key = (city.value if hasattr(city, "value") else str(city), d)
        if (attempts_by_day.get(key) or 0) < MAX_DAY_ATTEMPTS:
            done.discard(key)

    todo: list[tuple[str, date]] = []
    d = end
    while d >= start:
        for city in cities:
            if (city, d) not in done:
                todo.append((city, d))
        d -= timedelta(days=1)
    return todo


async def _record_day(
    session: AsyncSession,
    city: str,
    event_date: date,
    status: str,
    n_markets: int,
    error: str | None,
) -> None:
    """Upsert the progress row for a city-day, incrementing its attempt counter."""
    existing = await session.get(KalshiArchiveDay, (CityEnum(city), event_date))
    attempts = (existing.attempts if existing else 0) + 1
    await upsert_rows(
        session,
        KalshiArchiveDay,
        [
            {
                "city": CityEnum(city),
                "event_date": event_date,
                "event_ticker": event_ticker_for(city, event_date),
                "status": status,
                "n_markets": n_markets,
                "attempts": attempts,
                "last_error": error,
                "updated_at": datetime.now(UTC).replace(tzinfo=None),
            }
        ],
        ["city", "event_date"],
    )


async def run_backfill(
    client: KalshiPublicClient,
    session_factory: Callable[[], Awaitable[AsyncSession]],
    *,
    start: date,
    end: date,
    cities: list[str],
    budget_seconds: float,
    clock: Callable[[], float] = time.monotonic,
) -> dict:
    """Archive pending city-days (newest first) until the time budget is spent.

    Each city-day is committed on its own, so an interruption at any point
    loses at most the day in flight; the next run resumes from what's missing.

    Args:
        client: Public Kalshi client.
        session_factory: Async callable returning a NEW session per city-day.
        start: First event date to consider.
        end: Last event date to consider (should already be settled).
        cities: City codes.
        budget_seconds: Stop starting new days after this many seconds.
        clock: Monotonic clock (injectable for tests).

    Returns:
        Dict with processed count, remaining count and per-status counts.
    """
    t0 = clock()
    session = await session_factory()
    try:
        todo = await pending_days(session, start, end, cities)
    finally:
        await session.close()

    cutoff = await client.get_historical_cutoff() if todo else datetime.now(UTC)
    statuses: Counter[str] = Counter()
    processed = 0

    for city, event_date in todo:
        if clock() - t0 >= budget_seconds:
            break
        session = await session_factory()
        try:
            try:
                status, n = await archive_event(client, session, city, event_date, cutoff)
                await session.commit()
                await _record_day(session, city, event_date, status, n, None)
            except Exception as exc:
                await session.rollback()
                status, n = DAY_ERROR, 0
                logger.warning(
                    "Archive of city-day failed",
                    extra={
                        "data": {
                            "city": city,
                            "event_date": str(event_date),
                            "error": f"{type(exc).__name__}: {exc}"[:300],
                        }
                    },
                )
                await _record_day(
                    session, city, event_date, status, 0, f"{type(exc).__name__}: {exc}"[:500]
                )
            await session.commit()
        finally:
            await session.close()
        statuses[status] += 1
        processed += 1
        KALSHI_ARCHIVE_DAYS_TOTAL.labels(city=city, status=status).inc()

    remaining = len(todo) - processed
    logger.info(
        "Kalshi archive backfill run finished",
        extra={
            "data": {
                "processed": processed,
                "remaining": remaining,
                "statuses": dict(statuses),
                "elapsed_seconds": round(clock() - t0, 1),
            }
        },
    )
    return {"processed": processed, "remaining": remaining, "statuses": dict(statuses)}


def parse_quote(raw: dict, ts: datetime) -> dict | None:
    """Normalize a live market dict into a ``kalshi_quotes`` top-of-book snapshot row."""
    ticker = raw.get("ticker") or ""
    city = city_for_ticker(ticker)
    event_date = parse_market_date_from_ticker(ticker)
    if city is None or event_date is None:
        return None
    bid, _ = dollars_to_cents(raw.get("yes_bid_dollars"))
    ask, _ = dollars_to_cents(raw.get("yes_ask_dollars"))
    last, _ = dollars_to_cents(raw.get("last_price_dollars"))
    return {
        "ticker": ticker,
        "event_ticker": raw.get("event_ticker") or ticker.rsplit("-", 1)[0],
        "city": CityEnum(city),
        "event_date": event_date,
        "ts": ts,
        "yes_bid": bid,
        "yes_ask": ask,
        "yes_bid_size": _float_or_none(raw.get("yes_bid_size_fp")),
        "yes_ask_size": _float_or_none(raw.get("yes_ask_size_fp")),
        "last_price": last,
        "volume": _float_or_none(raw.get("volume_fp")),
        "status": raw.get("status"),
    }


async def record_quotes(
    client: KalshiPublicClient,
    session: AsyncSession,
    events: list[tuple[str, date]],
    now: datetime | None = None,
) -> int:
    """Snapshot top-of-book quotes for the given open events (live endpoint).

    Args:
        client: Public Kalshi client.
        session: Async DB session (caller commits).
        events: (city, event_date) pairs to snapshot.
        now: Snapshot timestamp (naive UTC); defaults to now.

    Returns:
        Number of quote rows written.
    """
    ts = now or datetime.now(UTC).replace(tzinfo=None)
    written = 0
    for city, event_date in events:
        raw = await client.get_event_markets(event_ticker_for(city, event_date))
        rows = [
            q
            for q in (parse_quote(m, ts) for m in raw)
            if q is not None and q["status"] in ("active", "open")
        ]
        if rows:
            session.add_all([KalshiQuote(**r) for r in rows])
            written += len(rows)
            KALSHI_QUOTES_RECORDED_TOTAL.labels(city=city).inc(len(rows))
    return written
