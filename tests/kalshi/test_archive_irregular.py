"""Tests for irregular ("scalar") Kalshi settlements (S4 fix)."""

from __future__ import annotations

from datetime import date

from backend.backtesting.real_engine import run_real_backtest
from backend.common.models import CityEnum, KalshiArchiveDay
from backend.kalshi.archive import DAY_IRREGULAR, archive_event, pending_days
from backend.strategy.controls import NullStrategy
from tests.kalshi.test_archive import FakeClient, make_event, session_factory  # noqa: F401
from tests.research.conftest import seed_event


async def test_scalar_settlement_is_final_irregular(session_factory) -> None:  # noqa: F811
    ev = "KXHIGHMIA-26APR11"
    markets = make_event(ev, winner=2)
    markets[2]["result"] = "scalar"
    markets[3]["result"] = "scalar"
    client = FakeClient({(ev, True): markets})
    session = await session_factory()
    status, n = await archive_event(client, session, "MIA", date(2026, 4, 11), client.cutoff)
    await session.commit()
    assert (status, n) == (DAY_IRREGULAR, 6)
    assert any(c[0] == "candles" for c in client.calls)  # prices still archived
    session.add(
        KalshiArchiveDay(
            city=CityEnum.MIA,
            event_date=date(2026, 4, 11),
            event_ticker=ev,
            status=DAY_IRREGULAR,
            attempts=1,
        )
    )
    await session.commit()
    assert await pending_days(session, date(2026, 4, 11), date(2026, 4, 11), ["MIA"]) == []
    await session.close()


async def test_engine_excludes_irregular_events(session_factory) -> None:  # noqa: F811
    from datetime import datetime

    from sqlalchemy import update

    from backend.common.models import KalshiArchivedMarket

    d = date(2025, 7, 10)
    session = await session_factory()
    tickers = await seed_event(
        session, "NYC", d, winner=1, candles=[(datetime(2025, 7, 9, 20, 0), [(40, 42)] * 6)]
    )
    await session.execute(
        update(KalshiArchivedMarket)
        .where(KalshiArchivedMarket.ticker == tickers[1])
        .values(result="scalar")
    )
    await session.commit()
    run = await run_real_backtest(session, NullStrategy(), ["NYC"], d, d)
    await session.close()
    assert run.excluded["irregular_settlement"] == 1
    assert run.market_scores["log_loss"] == []
