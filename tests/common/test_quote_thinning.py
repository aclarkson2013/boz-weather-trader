"""Tests for thinning old kalshi_quotes snapshots to hourly (S2 housekeeping)."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import select

from backend.common.maintenance import thin_old_quotes
from backend.common.models import CityEnum, KalshiQuote
from tests.common.test_maintenance import session_factory  # noqa: F401

NOW = datetime(2026, 10, 8, 12, 0)


def _quote(ts: datetime) -> KalshiQuote:
    return KalshiQuote(
        ticker="KXHIGHNY-26OCT08-B74.5",
        event_ticker="KXHIGHNY-26OCT08",
        city=CityEnum.NYC,
        event_date=date(2026, 10, 8),
        ts=ts,
        yes_bid=49,
        yes_ask=52,
    )


class TestThinOldQuotes:
    async def test_keeps_recent_and_first_snapshot_of_each_old_hour(self, session_factory) -> None:  # noqa: F811
        old_hour = NOW - timedelta(days=45)
        old = [old_hour.replace(minute=m) for m in (2, 7, 12, 57)]
        recent = [(NOW - timedelta(days=2)).replace(minute=m) for m in (2, 7, 12)]
        session = await session_factory()
        session.add_all([_quote(ts) for ts in old + recent])
        await session.commit()
        await session.close()

        result = await thin_old_quotes(session_factory, now=NOW)
        assert result["deleted"] == 3
        session = await session_factory()
        kept = sorted(r for r in (await session.execute(select(KalshiQuote.ts))).scalars())
        await session.close()
        assert kept == sorted([old[0], *recent])
