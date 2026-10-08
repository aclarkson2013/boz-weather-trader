"""Tests for exact fees, the taker fill model and the pre-registered strategies (S2)."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from backend.common.schemas import BracketQuote, MarketSnapshot, StrategyOrder
from backend.strategy.controls import NullStrategy, RandomTakerStrategy
from backend.strategy.fees import kalshi_fee_cents
from backend.strategy.fills import fill_order, settle_pnl_cents, side_mid_cents, side_price_cents
from backend.strategy.longshot import LongshotFadeStrategy
from backend.strategy.registry import PREREGISTERED_K, STRATEGIES, get_strategy
from backend.strategy.v1_replica import V1ReplicaStrategy, _legacy_bounds, _match_probability

TS = datetime(2026, 7, 9, 21, 0)


def q(ticker: str, bid: int | None, ask: int | None, lo=None, hi=None) -> BracketQuote:
    return BracketQuote(
        ticker=ticker, label=ticker, lower_bound_f=lo, upper_bound_f=hi, yes_bid=bid, yes_ask=ask
    )


def snap(quotes: list[BracketQuote], city: str = "NYC") -> MarketSnapshot:
    return MarketSnapshot(
        city=city, event_date=date(2026, 7, 10), decision="D1E", decision_ts=TS, quotes=quotes
    )


class TestFees:
    @pytest.mark.parametrize(
        ("price", "count", "fee"),
        [(97, 1, 1), (97, 4, 1), (50, 10, 18), (50, 1, 2), (1, 1, 1), (55, 100, 174)],
    )
    def test_taker_schedule(self, price: int, count: int, fee: int) -> None:
        """AC3 (+ Kalshi's published example: 100 contracts @ 55c -> $1.74)."""
        assert kalshi_fee_cents(price, count) == fee

    def test_maker_is_quarter_rate(self) -> None:
        assert kalshi_fee_cents(50, 100, maker=True) == 44  # 0.0175*100*0.25 = 0.4375 -> 44c

    @pytest.mark.parametrize(("price", "count"), [(0, 1), (100, 1), (50, 0)])
    def test_rejects_invalid(self, price: int, count: int) -> None:
        with pytest.raises(ValueError):
            kalshi_fee_cents(price, count)


class TestFills:
    def test_prices_per_side(self) -> None:
        quote = q("A", 40, 44)
        assert side_price_cents(quote, "yes") == 44
        assert side_price_cents(quote, "no") == 60
        assert side_mid_cents(quote, "yes") == 42.0
        assert side_mid_cents(quote, "no") == 58.0

    def test_one_sided_quotes_unfillable(self) -> None:
        assert side_price_cents(q("A", 0, 3), "no") is None
        assert side_price_cents(q("A", 97, 100), "yes") is None
        assert side_price_cents(q("A", None, None), "yes") is None

    def test_fill_shrinks_to_budget_and_settles(self) -> None:
        order = StrategyOrder(ticker="A", side="no", count=10)
        fill = fill_order(order, q("A", 3, 5), "D1E", TS, budget_cents=400)
        assert fill is not None
        assert fill.price_cents == 97
        assert fill.count == 4  # 4*97 + fee 1 = 389 <= 400; 5 would be 486
        assert fill.fee_cents == 1
        assert settle_pnl_cents(fill, "no") == 4 * 3 - 1
        assert settle_pnl_cents(fill, "yes") == -(4 * 97 + 1)

    def test_slippage_raises_price(self) -> None:
        fill = fill_order(
            StrategyOrder(ticker="A", side="yes", count=1), q("A", 40, 44), "D1E", TS, 400, 1
        )
        assert fill is not None and fill.price_cents == 45

    def test_unaffordable_returns_none(self) -> None:
        assert (
            fill_order(StrategyOrder(ticker="A", side="no", count=1), q("A", 3, 5), "D1E", TS, 50)
            is None
        )


class TestLongshot:
    def test_only_low_bid_brackets_get_no_orders(self) -> None:
        s = LongshotFadeStrategy("L2", max_yes_bid=4, decision="D1E")
        orders = s.decide(snap([q("A", 1, 2), q("B", 4, 6), q("C", 5, 7), q("D", 0, 1)]), 400)
        assert {o.ticker for o in orders} == {"A", "B"}
        assert all(o.side == "no" for o in orders)

    def test_budget_spread_round_robin_within_cap(self) -> None:
        s = LongshotFadeStrategy("L1", max_yes_bid=2, decision="D1E")
        orders = s.decide(snap([q("A", 1, 2), q("B", 2, 3)]), 400)
        counts = {o.ticker: o.count for o in orders}
        total = sum(
            (100 - b) * counts[t] + kalshi_fee_cents(100 - b, counts[t])
            for t, b in [("A", 1), ("B", 2)]
            if t in counts
        )
        assert total <= 400
        assert counts["A"] >= 1 and counts["B"] >= 1

    def test_nothing_eligible(self) -> None:
        s = LongshotFadeStrategy("L1", max_yes_bid=2, decision="D1E")
        assert s.decide(snap([q("A", 10, 12)]), 400) == []


class TestControls:
    def test_null_never_orders(self) -> None:
        assert NullStrategy().decide(snap([q("A", 40, 44)]), 400) == []

    def test_random_taker_is_deterministic(self) -> None:
        s1, s2 = RandomTakerStrategy(), RandomTakerStrategy()
        quotes = [q("A", 40, 44), q("B", 20, 22), q("C", 0, 3)]
        assert s1.decide(snap(quotes), 400) == s2.decide(snap(quotes), 400)
        assert len(s1.decide(snap(quotes), 400)) == 1


class TestV1Replica:
    def test_legacy_bounds_mapping(self) -> None:
        assert _legacy_bounds(88.5, 90.5) == (89.0, 90.0)
        assert _legacy_bounds(None, 88.5) == (None, 89.0)
        assert _legacy_bounds(96.5, None) == (96.0, None)

    def test_matches_current_and_legacy_prediction_bounds(self) -> None:
        current = [{"lower_bound_f": 88.5, "upper_bound_f": 90.5, "probability": 0.3}]
        legacy = [{"lower_bound_f": 89.0, "upper_bound_f": 90.0, "probability": 0.2}]
        assert _match_probability(current, 88.5, 90.5) == 0.3
        assert _match_probability(legacy, 88.5, 90.5) == 0.2
        assert _match_probability(legacy, 70.5, 72.5) is None

    def test_reproduces_v1_max_divergence_no_fade(self) -> None:
        """Model 13% vs ask 50%: v1 clamp/blend yields a NO signal (its failure mode)."""
        s = V1ReplicaStrategy(decisions=("D1E",))
        s._predictions = {
            ("NYC", date(2026, 7, 10)): [
                (
                    datetime(2026, 7, 9, 20, 5),
                    [{"lower_bound_f": None, "upper_bound_f": 79.5, "probability": 0.13}],
                )
            ]
        }
        orders = s.decide(snap([q("BOT", 49, 50, None, 79.5)]), 400)
        assert [(o.ticker, o.side) for o in orders] == [("BOT", "no")]

    def test_ignores_predictions_generated_after_decision(self) -> None:
        s = V1ReplicaStrategy(decisions=("D1E",))
        s._predictions = {
            ("NYC", date(2026, 7, 10)): [
                (
                    datetime(2026, 7, 9, 21, 30),
                    [{"lower_bound_f": None, "upper_bound_f": 79.5, "probability": 0.13}],
                )
            ]
        }
        assert s.decide(snap([q("BOT", 49, 50, None, 79.5)]), 400) == []


class TestRegistry:
    def test_registered_ids_and_k(self) -> None:
        assert set(STRATEGIES) == {
            "C0",
            "C1",
            "C2",
            "L1",
            "L2",
            "L3",
            "L4",
            "L5",
            "B1",
            "B2",
            "B3",
            "B4",
        }
        assert PREREGISTERED_K == 9  # Amendment 1
        assert get_strategy("L3").params == {"max_yes_bid": 2, "decision": "D1L"}
        assert get_strategy("C0").kind == "control"
        with pytest.raises(KeyError):
            get_strategy("X9")
