"""B5 (= B3 rule) can decide on a LIVE day that isn't in the archive yet (S5)."""

from __future__ import annotations

from datetime import date, timedelta

from backend.common.models import CityEnum, ForecastIssuance
from backend.common.schemas import BracketQuote, MarketSnapshot
from backend.research.snapshots import decision_ts_for
from backend.strategy.registry import FORWARD_ONLY, PREREGISTERED_K, get_strategy
from tests.research.conftest import BRACKETS
from tests.research.test_model_strategies import CENTERS, _seed_synthetic


def test_b5_is_b3_and_forward_only() -> None:
    b3, b5 = get_strategy("B3"), get_strategy("B5")
    assert b5.params == b3.params
    assert b5.decisions == b3.decisions == ("D1E",)
    assert {"L5", "B5"} <= FORWARD_ONLY
    assert PREREGISTERED_K == 10


async def test_b5_decides_on_live_day(session_factory) -> None:
    start = date(2025, 1, 1)
    await _seed_synthetic(session_factory, start, 160, informative=True)
    live_day = start + timedelta(days=170)  # Not in the archive
    ts = decision_ts_for("NYC", live_day, "D1E")
    session = await session_factory()
    session.add(
        ForecastIssuance(
            city=CityEnum.NYC,
            model="NBS",
            run_ts=ts - timedelta(hours=4),
            valid_date=live_day,
            station="KNYC",
            available_at=ts - timedelta(hours=2),
            tmax_f=CENTERS[2],
            tmax_sd_f=1.5,
        )
    )
    await session.commit()

    strategy = get_strategy("B1")  # NBM-direct variant of the same machinery (EMOS needs labels)
    await strategy.prepare(session, ["NYC"], live_day, live_day)
    await session.close()

    quotes = [
        BracketQuote(
            ticker=f"T{i}", label=lbl, lower_bound_f=lo, upper_bound_f=hi, yes_bid=14, yes_ask=16
        )
        for i, (_, lbl, lo, hi) in enumerate(BRACKETS)
    ]
    snap = MarketSnapshot(
        city="NYC", event_date=live_day, decision="D1E", decision_ts=ts, quotes=quotes
    )
    assert strategy.add_live_snapshot(snap) is True
    orders = strategy.decide(snap, 400)
    assert orders, "an informative model vs a flat market should find an edge"
    assert any(o.ticker == "T2" and o.side == "yes" for o in orders)

    # No NBM issuance available before the decision -> no inputs, no trade
    late = MarketSnapshot(
        city="NYC",
        event_date=live_day + timedelta(days=1),
        decision="D1E",
        decision_ts=decision_ts_for("NYC", live_day + timedelta(days=1), "D1E"),
        quotes=quotes,
    )
    assert strategy.add_live_snapshot(late) is False
