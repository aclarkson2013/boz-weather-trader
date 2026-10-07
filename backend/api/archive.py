"""Kalshi market-archive coverage endpoint (algo v2, slice S1).

Shows, per city and month, how much real market history has been archived:
city-days by status, markets with candles, outcome labels, and the median
bid/ask spread. This is the "is the data good enough?" check that gates the
real-price backtester (slice S2).

Usage:
    GET /api/archive/coverage
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import get_current_user
from backend.api.response_schemas import ArchiveCoverageResponse, ArchiveCoverageRow
from backend.common.database import get_db
from backend.common.logging import get_logger
from backend.common.models import KalshiArchiveDay, KalshiArchivedMarket, User
from backend.kalshi.archive import ARCHIVE_START
from backend.weather.stations import VALID_CITIES

logger = get_logger("MARKET")

router = APIRouter()


def _city_code(value: object) -> str:
    """Return the plain city code for an Enum or string column value."""
    return value.value if hasattr(value, "value") else str(value)


def _expected_days(month: str, start: date, end: date) -> int:
    """Count calendar days of ``month`` (YYYY-MM) that fall inside [start, end]."""
    year, mon = (int(x) for x in month.split("-"))
    first = date(year, mon, 1)
    nxt = date(year + (mon == 12), mon % 12 + 1, 1)
    lo, hi = max(first, start), min(nxt - timedelta(days=1), end)
    return max(0, (hi - lo).days + 1)


@router.get("/coverage", response_model=ArchiveCoverageResponse)
async def get_archive_coverage(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ArchiveCoverageResponse:
    """Summarize archived Kalshi market history per city and month.

    Args:
        user: The authenticated user.
        db: Async database session.

    Returns:
        ArchiveCoverageResponse with one row per (city, month) plus totals.
    """
    end = datetime.now(UTC).date() - timedelta(days=2)

    day_rows = (
        await db.execute(
            select(KalshiArchiveDay.city, KalshiArchiveDay.event_date, KalshiArchiveDay.status)
        )
    ).all()
    market_rows = (
        await db.execute(
            select(
                KalshiArchivedMarket.city,
                KalshiArchivedMarket.event_date,
                KalshiArchivedMarket.n_candles,
                KalshiArchivedMarket.median_spread_cents,
                KalshiArchivedMarket.result,
                KalshiArchivedMarket.tiles_ok,
            )
        )
    ).all()

    days: dict[tuple[str, str], dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for city, d, status in day_rows:
        days[(_city_code(city), d.strftime("%Y-%m"))][status] += 1

    markets: dict[tuple[str, str], list] = defaultdict(list)
    for city, d, n_candles, spread, result, tiles_ok in market_rows:
        markets[(_city_code(city), d.strftime("%Y-%m"))].append(
            (n_candles, spread, result, tiles_ok)
        )

    rows: list[ArchiveCoverageRow] = []
    for key in sorted(set(days) | set(markets)):
        city, month = key
        d = days.get(key, {})
        m = markets.get(key, [])
        spreads = [s for _, s, _, _ in m if s is not None]
        rows.append(
            ArchiveCoverageRow(
                city=city,
                month=month,
                days_expected=_expected_days(month, ARCHIVE_START, end),
                days_complete=d.get("complete", 0),
                days_unsettled=d.get("unsettled", 0),
                days_empty=d.get("empty", 0),
                days_error=d.get("error", 0),
                markets=len(m),
                markets_with_candles=sum(1 for n, _, _, _ in m if n),
                markets_labeled=sum(1 for _, _, r, _ in m if r in ("yes", "no")),
                markets_not_tiling=sum(1 for _, _, _, t in m if t is False),
                median_spread_cents=float(statistics.median(spreads)) if spreads else None,
            )
        )

    total_expected = max(0, (end - ARCHIVE_START).days + 1) * len(VALID_CITIES)
    return ArchiveCoverageResponse(
        archive_start=ARCHIVE_START,
        archive_end=end,
        days_complete=sum(r.days_complete for r in rows),
        days_expected=total_expected,
        markets=sum(r.markets for r in rows),
        markets_with_candles=sum(r.markets_with_candles for r in rows),
        rows=rows,
    )
