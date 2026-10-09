"""Registry of pre-registered strategies (docs/research/v2-preregistration.md §4).

Only IDs listed here can be backtested. Counted variants (K) are the gate-eligible
ones; controls validate the harness and are never eligible. B1-B4 (model-based)
added in slice S4; L5 is forward-only (Amendment 1).
"""

from __future__ import annotations

from collections.abc import Callable

from backend.strategy.base import Strategy
from backend.strategy.benter import BenterStrategy
from backend.strategy.controls import NullStrategy, RandomTakerStrategy
from backend.strategy.longshot import LongshotFadeStrategy
from backend.strategy.v1_replica import V1ReplicaStrategy

PREREGISTERED_K = 10  # L1-L5 + B1-B5 (Amendment 2, 2026-10-09); changes need an amendment

# Strategies that may only be judged on forward paper trading (Amendment 1)
FORWARD_ONLY: frozenset[str] = frozenset({"L5", "B5"})

STRATEGIES: dict[str, Callable[[], Strategy]] = {
    "C0": NullStrategy,
    "C1": RandomTakerStrategy,
    "C2": V1ReplicaStrategy,
    "L1": lambda: LongshotFadeStrategy("L1", max_yes_bid=2, decision="D1E"),
    "L2": lambda: LongshotFadeStrategy("L2", max_yes_bid=4, decision="D1E"),
    "L3": lambda: LongshotFadeStrategy("L3", max_yes_bid=2, decision="D1L"),
    "L4": lambda: LongshotFadeStrategy("L4", max_yes_bid=4, decision="D1L"),
    "L5": lambda: LongshotFadeStrategy("L5", max_yes_bid=4, decision="D1L"),
    "B1": lambda: BenterStrategy("B1", model="nbm", decision="D1E"),
    "B2": lambda: BenterStrategy("B2", model="nbm", decision="D0M"),
    "B3": lambda: BenterStrategy("B3", model="emos", decision="D1E"),
    "B4": lambda: BenterStrategy("B4", model="emos", decision="D0M"),
    "B5": lambda: BenterStrategy("B5", model="emos", decision="D1E"),  # = B3, forward-only
}


def get_strategy(strategy_id: str) -> Strategy:
    """Instantiate a registered strategy by pre-registration ID.

    Raises:
        KeyError: If the ID is not registered.
    """
    return STRATEGIES[strategy_id]()
