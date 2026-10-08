"""Tests for strike inference and repair of non-tiling archived days (S2 fix).

Kalshi's historical data for 2025-01-16 .. 2025-02-09 omits strikes on the
winning market only; they're recovered from the ticker suffix.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import select

from backend.common.models import CityEnum, KalshiArchiveDay, KalshiArchivedMarket
from backend.kalshi.archive import (
    DAY_COMPLETE,
    archive_event,
    check_tiling,
    infer_missing_strikes,
    parse_market,
    pending_days,
)
from tests.kalshi.test_archive import FakeClient, make_event, session_factory  # noqa: F401


def _strip(markets: list[dict], suffix: str) -> None:
    for m in markets:
        if m["ticker"].endswith(suffix):
            m.update(strike_type=None, floor_strike=None, cap_strike=None)


class TestInferMissingStrikes:
    def test_middle_bracket_from_ticker(self) -> None:
        markets = make_event("KXHIGHNY-25JAN16")
        _strip(markets, "B72.5")
        assert infer_missing_strikes(markets) == 1
        rows = [parse_market(m, "historical") for m in markets]
        assert check_tiling(rows) is True
        fixed = next(r for r in rows if r["ticker"].endswith("B72.5"))
        assert (fixed["lower_bound_f"], fixed["upper_bound_f"]) == (71.5, 73.5)

    def test_bottom_and_top_catch_alls_from_siblings(self) -> None:
        for suffix, expected in [("T70", (None, 69.5)), ("T77", (77.5, None))]:
            markets = make_event("KXHIGHAUS-25FEB01")
            _strip(markets, suffix)
            assert infer_missing_strikes(markets) == 1
            rows = [parse_market(m, "historical") for m in markets]
            assert check_tiling(rows) is True
            fixed = next(r for r in rows if r["ticker"].endswith(suffix))
            assert (fixed["lower_bound_f"], fixed["upper_bound_f"]) == expected

    def test_complete_markets_untouched(self) -> None:
        assert infer_missing_strikes(make_event("KXHIGHNY-25JAN16")) == 0


class TestRepair:
    async def test_archive_event_recovers_missing_winner_strikes(self, session_factory) -> None:  # noqa: F811
        ev = "KXHIGHNY-25JAN16"
        markets = make_event(ev, winner=2)
        _strip(markets, "B72.5")  # the winner
        client = FakeClient({(ev, True): markets})
        session = await session_factory()
        await archive_event(client, session, "NYC", date(2025, 1, 16), client.cutoff)
        await session.commit()
        rows = (await session.execute(select(KalshiArchivedMarket))).scalars().all()
        await session.close()
        assert all(r.tiles_ok for r in rows)
        assert {r.label for r in rows if r.result == "yes"} == {"72° to 73°F"}

    async def test_complete_but_non_tiling_days_are_retried(self, session_factory) -> None:  # noqa: F811
        d = date(2025, 1, 16)
        session = await session_factory()
        session.add(
            KalshiArchiveDay(
                city=CityEnum.NYC, event_date=d, event_ticker="x", status=DAY_COMPLETE, attempts=1
            )
        )
        session.add(
            KalshiArchivedMarket(
                ticker="KXHIGHNY-25JAN16-B30.5",
                event_ticker="KXHIGHNY-25JAN16",
                series_ticker="KXHIGHNY",
                city=CityEnum.NYC,
                event_date=d,
                label="Unknown",
                tiles_ok=False,
                era="E1",
                source="historical",
            )
        )
        await session.commit()
        todo = await pending_days(session, d, d, ["NYC"])
        await session.close()
        assert todo == [("NYC", d)]

    async def test_repair_gives_up_after_max_attempts(self, session_factory) -> None:  # noqa: F811
        d = date(2025, 1, 16)
        session = await session_factory()
        session.add(
            KalshiArchiveDay(
                city=CityEnum.NYC, event_date=d, event_ticker="x", status=DAY_COMPLETE, attempts=5
            )
        )
        session.add(
            KalshiArchivedMarket(
                ticker="KXHIGHNY-25JAN16-B30.5",
                event_ticker="KXHIGHNY-25JAN16",
                series_ticker="KXHIGHNY",
                city=CityEnum.NYC,
                event_date=d,
                label="Unknown",
                tiles_ok=False,
                era="E1",
                source="historical",
            )
        )
        await session.commit()
        assert await pending_days(session, d, d, ["NYC"]) == []
        await session.close()
