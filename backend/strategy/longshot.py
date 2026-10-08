"""Longshot NO fade (pre-registration L1-L4).

Evidence (docs/research/2026-10-07-algo-v2-research.md): favourite-longshot bias
— brackets bid at 1-4c settle YES less often than priced. The strategy buys NO
on every bracket whose YES bid is in (0, max_yes_bid] at the decision time,
spreading the city-day budget one contract at a time (round-robin, cheapest
YES bid first) so fee rounding is amortized over several contracts.
"""

from __future__ import annotations

from backend.common.schemas import MarketSnapshot, StrategyOrder
from backend.strategy.base import BaseStrategy
from backend.strategy.fees import kalshi_fee_cents


class LongshotFadeStrategy(BaseStrategy):
    """Buy NO on low-priced YES brackets at one decision time.

    Args:
        strategy_id: Pre-registration ID ("L1".."L4").
        max_yes_bid: Maximum YES bid (cents) that qualifies (2 or 4).
        decision: Decision time label ("D1E" or "D1L").
    """

    def __init__(self, strategy_id: str, max_yes_bid: int, decision: str) -> None:
        self.strategy_id = strategy_id
        self.kind = "market"
        self.decisions = (decision,)
        self.max_yes_bid = max_yes_bid
        self.params = {"max_yes_bid": max_yes_bid, "decision": decision}

    def decide(self, snapshot: MarketSnapshot, budget_cents: int) -> list[StrategyOrder]:
        """Allocate the budget across qualifying brackets, 1 contract at a time."""
        eligible = sorted(
            (
                q
                for q in snapshot.quotes
                if q.yes_bid is not None and 1 <= q.yes_bid <= self.max_yes_bid
            ),
            key=lambda q: (q.yes_bid, q.ticker),
        )
        if not eligible:
            return []

        counts = {q.ticker: 0 for q in eligible}
        prices = {q.ticker: 100 - q.yes_bid for q in eligible}

        def total(c: dict[str, int]) -> int:
            return sum(
                prices[t] * n + kalshi_fee_cents(prices[t], n) for t, n in c.items() if n > 0
            )

        progressed = True
        while progressed:
            progressed = False
            for q in eligible:
                trial = dict(counts)
                trial[q.ticker] += 1
                if total(trial) <= budget_cents:
                    counts = trial
                    progressed = True

        return [StrategyOrder(ticker=t, side="no", count=n) for t, n in counts.items() if n > 0]
