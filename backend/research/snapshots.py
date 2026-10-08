"""Decision-time market snapshots from the Kalshi archive (no lookahead).

Two clocks (docs/ALGO_V2_PRD.md §7):
- ``event_date`` is the LST settlement date of the market.
- Decision times are LOCAL CIVIL times at the station (what a trader's clock
  shows, DST-aware via ``ZoneInfo``), stored as naive UTC.

A snapshot uses, per market, the latest hourly candle whose ``end_ts`` is at or
before the decision time, and only markets already open at that time. Outcomes
are returned separately so strategies never see them.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.common.models import CityEnum, KalshiArchivedMarket, KalshiCandle
from backend.common.schemas import BracketQuote, MarketSnapshot
from backend.weather.stations import STATION_CONFIGS

# decision label -> (day offset from event date, local wall-clock time)
DECISION_TIMES: dict[str, tuple[int, time]] = {
    "D1E": (-1, time(17, 0)),  # 5 PM local, day before the event
    "D1L": (-1, time(20, 0)),  # 8 PM local, day before the event
    "D0M": (0, time(10, 0)),  # 10 AM local, event day
}


def decision_ts_for(city: str, event_date: date, decision: str) -> datetime:
    """Return the decision instant as naive UTC.

    Args:
        city: City code.
        event_date: Event (LST) date.
        decision: Decision label in ``DECISION_TIMES``.

    Returns:
        Naive UTC datetime of the local wall-clock decision time.
    """
    offset_days, wall = DECISION_TIMES[decision]
    tz = STATION_CONFIGS[city].timezone
    local = datetime.combine(event_date + timedelta(days=offset_days), wall, tzinfo=tz)
    return local.astimezone(UTC).replace(tzinfo=None)


@dataclass
class ArchivedMarket:
    """Minimal in-memory view of a ``kalshi_markets`` row."""

    ticker: str
    label: str
    lower_bound_f: float | None
    upper_bound_f: float | None
    open_time: datetime | None
    result: str | None
    tiles_ok: bool
    era: str
    expiration_value: float | None = None


@dataclass
class CityArchive:
    """All archived markets + hourly candles of one city over a date window."""

    city: str
    markets_by_date: dict[date, list[ArchivedMarket]] = field(default_factory=dict)
    # ticker -> (sorted end_ts list, list of (yes_bid_close, yes_ask_close))
    candles: dict[str, tuple[list[datetime], list[tuple[int | None, int | None]]]] = field(
        default_factory=dict
    )


async def load_city_archive(
    session: AsyncSession, city: str, start: date, end: date
) -> CityArchive:
    """Bulk-load one city's archived markets and hourly candles for [start, end].

    Args:
        session: Async DB session.
        city: City code.
        start: First event date.
        end: Last event date.

    Returns:
        CityArchive with markets grouped by event date and candles indexed by ticker.
    """
    archive = CityArchive(city=city)
    rows = (
        await session.execute(
            select(KalshiArchivedMarket).where(
                KalshiArchivedMarket.city == CityEnum(city),
                KalshiArchivedMarket.event_date >= start,
                KalshiArchivedMarket.event_date <= end,
            )
        )
    ).scalars()
    tickers: list[str] = []
    for m in rows:
        archive.markets_by_date.setdefault(m.event_date, []).append(
            ArchivedMarket(
                ticker=m.ticker,
                label=m.label,
                lower_bound_f=m.lower_bound_f,
                upper_bound_f=m.upper_bound_f,
                open_time=m.open_time,
                result=m.result,
                tiles_ok=bool(m.tiles_ok),
                era=m.era,
                expiration_value=m.expiration_value,
            )
        )
        tickers.append(m.ticker)

    for i in range(0, len(tickers), 1000):
        chunk = tickers[i : i + 1000]
        candle_rows = await session.execute(
            select(
                KalshiCandle.ticker,
                KalshiCandle.end_ts,
                KalshiCandle.yes_bid_close,
                KalshiCandle.yes_ask_close,
            )
            .where(KalshiCandle.ticker.in_(chunk), KalshiCandle.period_min == 60)
            .order_by(KalshiCandle.ticker, KalshiCandle.end_ts)
        )
        for ticker, end_ts, bid, ask in candle_rows.all():
            ts_list, quotes = archive.candles.setdefault(ticker, ([], []))
            ts_list.append(end_ts)
            quotes.append((bid, ask))
    return archive


def _sort_key(m: ArchivedMarket) -> float:
    return float("-inf") if m.lower_bound_f is None else m.lower_bound_f


def build_snapshot(
    archive: CityArchive, event_date: date, decision: str
) -> tuple[MarketSnapshot | None, dict[str, str | None], str | None]:
    """Build the market snapshot for one city-day at one decision time.

    Args:
        archive: Preloaded city archive.
        event_date: Event (LST) date.
        decision: Decision label.

    Returns:
        (snapshot or None, outcomes {ticker: "yes"/"no"/None}, era or None).
        The snapshot is None when the event has no markets open at the decision time.
    """
    markets = archive.markets_by_date.get(event_date) or []
    if not markets:
        return None, {}, None
    ts = decision_ts_for(archive.city, event_date, decision)
    open_markets = sorted(
        (m for m in markets if m.open_time is not None and m.open_time <= ts), key=_sort_key
    )
    if not open_markets:
        return None, {}, markets[0].era

    quotes: list[BracketQuote] = []
    for m in open_markets:
        bid = ask = None
        quote_ts = None
        series = archive.candles.get(m.ticker)
        if series:
            ts_list, values = series
            idx = bisect.bisect_right(ts_list, ts) - 1
            if idx >= 0:
                bid, ask = values[idx]
                quote_ts = ts_list[idx]
        quotes.append(
            BracketQuote(
                ticker=m.ticker,
                label=m.label,
                lower_bound_f=m.lower_bound_f,
                upper_bound_f=m.upper_bound_f,
                yes_bid=bid,
                yes_ask=ask,
                quote_ts=quote_ts,
            )
        )

    snapshot = MarketSnapshot(
        city=archive.city,
        event_date=event_date,
        decision=decision,
        decision_ts=ts,
        quotes=quotes,
        tiles_ok=all(m.tiles_ok for m in markets) and len(open_markets) == len(markets),
    )
    outcomes = {m.ticker: m.result for m in markets}
    return snapshot, outcomes, markets[0].era
