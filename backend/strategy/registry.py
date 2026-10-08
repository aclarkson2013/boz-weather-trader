"""Registry of pre-registered strategies (docs/research/v2-preregistration.md §4).

Only IDs listed here can be backtested. Counted variants (K) are the gate-eligible
ones; controls validate the harness and are never eligible. B1-B4 (model-based)
arrive in slice S4.
"""

from __future__ import annotations

from collections.abc import Callable

from backend.strategy.base import Strategy
from backend.strategy.controls import NullStrategy, RandomTakerStrategy
from backend.strategy.longshot import LongshotFadeStrategy
from backend.strategy.v1_replica import V1ReplicaStrategy

PREREGISTERED_K = 8  # L1-L4 + B1-B4; changing it requires a pre-registration amendment

STRATEGIES: dict[str, Callable[[], Strategy]] = {
    "C0": NullStrategy,
    "C1": RandomTakerStrategy,
    "C2": V1ReplicaStrategy,
    "L1": lambda: LongshotFadeStrategy("L1", max_yes_bid=2, decision="D1E"),
    "L2": lambda: LongshotFadeStrategy("L2", max_yes_bid=4, decision="D1E"),
    "L3": lambda: LongshotFadeStrategy("L3", max_yes_bid=2, decision="D1L"),
    "L4": lambda: LongshotFadeStrategy("L4", max_yes_bid=4, decision="D1L"),
}


def get_strategy(strategy_id: str) -> Strategy:
    """Instantiate a registered strategy by pre-registration ID.

    Raises:
        KeyError: If the ID is not registered.
    """
    return STRATEGIES[strategy_id]()
