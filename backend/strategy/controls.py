"""Control strategies (pre-registration C0, C1) that validate the backtest harness.

- C0 ``NullStrategy``: never trades -> P&L must be exactly 0.
- C1 ``RandomTakerStrategy``: one random fillable bracket/side per city-day at
  D1E, 1 contract -> mean P&L should be about -(half spread + fee).
C2 (v1 replica) lives in ``v1_replica.py``. Controls are never gate-eligible.
"""

from __future__ import annotations

import hashlib
import random

from backend.common.schemas import MarketSnapshot, StrategyOrder
from backend.strategy.base import BaseStrategy
from backend.strategy.fills import side_price_cents


class NullStrategy(BaseStrategy):
    """C0: never places an order."""

    def __init__(self) -> None:
        self.strategy_id = "C0"
        self.kind = "control"
        self.decisions = ("D1E",)
        self.params = {}

    def decide(self, snapshot: MarketSnapshot, budget_cents: int) -> list[StrategyOrder]:
        """Return no orders."""
        return []


class RandomTakerStrategy(BaseStrategy):
    """C1: buy 1 contract of a random fillable (bracket, side) per city-day.

    Deterministic: the RNG is seeded from (seed, city, event_date) so reruns match.
    """

    def __init__(self, seed: int = 20261007) -> None:
        self.strategy_id = "C1"
        self.kind = "control"
        self.decisions = ("D1E",)
        self.params = {"seed": seed, "count": 1}
        self.seed = seed

    def decide(self, snapshot: MarketSnapshot, budget_cents: int) -> list[StrategyOrder]:
        """Pick one random fillable (ticker, side)."""
        options = [
            (q.ticker, side)
            for q in snapshot.quotes
            for side in ("yes", "no")
            if side_price_cents(q, side) is not None
        ]
        if not options:
            return []
        key = f"{self.seed}|{snapshot.city}|{snapshot.event_date.isoformat()}".encode()
        rng = random.Random(int(hashlib.sha256(key).hexdigest()[:16], 16))
        ticker, side = rng.choice(sorted(options))
        return [StrategyOrder(ticker=ticker, side=side, count=1)]
