"""Fixtures for algo-v2 research/backtest tests: a small in-memory market archive."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date, datetime, timedelta

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.common.models import Base, CityEnum, KalshiArchivedMarket, KalshiCandle
from backend.kalshi.archive import era_for, event_ticker_for

# Six tiling brackets: <=69, 70-71, 72-73, 74-75, 76-77, >=78
BRACKETS = [
    ("T70", "69°F or below", None, 69.5),
    ("B70.5", "70° to 71°F", 69.5, 71.5),
    ("B72.5", "72° to 73°F", 71.5, 73.5),
    ("B74.5", "74° to 75°F", 73.5, 75.5),
    ("B76.5", "76° to 77°F", 75.5, 77.5),
    ("T77", "78°F or above", 77.5, None),
]


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator:
    """Fresh in-memory DB per test; returns an async session factory."""
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    async def factory() -> AsyncSession:
        return maker()

    yield factory
    await engine.dispose()


async def seed_event(
    session: AsyncSession,
    city: str,
    event_date: date,
    winner: int,
    candles: list[tuple[datetime, list[tuple[int | None, int | None]]]],
    *,
    tiles_ok: bool = True,
    open_time: datetime | None = None,
) -> list[str]:
    """Insert one 6-bracket event and its hourly candles.

    Args:
        session: DB session (caller commits).
        city: City code.
        event_date: Event date.
        winner: Index (0-5) of the bracket that settled YES.
        candles: [(end_ts, [(bid, ask) per bracket])] in naive UTC.
        tiles_ok: Tiling flag to store.
        open_time: Market open time (default: day before at 14:00 UTC).

    Returns:
        Tickers in bracket order.
    """
    ev = event_ticker_for(city, event_date)
    opened = open_time or datetime.combine(
        event_date - timedelta(days=1), datetime.min.time()
    ) + timedelta(hours=14)
    tickers = []
    for i, (suffix, label, lo, hi) in enumerate(BRACKETS):
        ticker = f"{ev}-{suffix}"
        tickers.append(ticker)
        session.add(
            KalshiArchivedMarket(
                ticker=ticker,
                event_ticker=ev,
                series_ticker=ev.split("-")[0],
                city=CityEnum(city),
                event_date=event_date,
                label=label,
                lower_bound_f=lo,
                upper_bound_f=hi,
                tiles_ok=tiles_ok,
                open_time=opened,
                close_time=opened + timedelta(hours=39),
                status="finalized",
                result="yes" if i == winner else "no",
                expiration_value=None,
                era=era_for(event_date),
                source="historical",
            )
        )
    for end_ts, quotes in candles:
        for ticker, (bid, ask) in zip(tickers, quotes, strict=True):
            session.add(
                KalshiCandle(
                    ticker=ticker,
                    period_min=60,
                    end_ts=end_ts,
                    yes_bid_close=bid,
                    yes_ask_close=ask,
                )
            )
    return tickers
