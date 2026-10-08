"""Strategy interface for algo v2 (shared by the backtester and paper trading).

A strategy sees one ``MarketSnapshot`` at a time (quotes only — never outcomes)
and returns taker buy orders. The engine fills them against the same snapshot
with exact fees and a per-city-day budget.
"""

from __future__ import annotations

from datetime import date
from typing import Protocol, runtime_checkable

from sqlalchemy.ext.asyncio import AsyncSession

from backend.common.schemas import MarketSnapshot, StrategyOrder


@runtime_checkable
class Strategy(Protocol):
    """A pre-registered trading strategy.

    Attributes:
        strategy_id: Pre-registration ID (e.g. "L1", "C2").
        kind: "control" (harness check, never eligible) or "market" / "model".
        decisions: Decision times the strategy acts at (e.g. ("D1E",)).
        params: Parameters recorded in reports (must match pre-registration).
    """

    strategy_id: str
    kind: str
    decisions: tuple[str, ...]
    params: dict

    async def prepare(
        self, session: AsyncSession, cities: list[str], start: date, end: date
    ) -> None:
        """Preload anything the strategy needs (e.g. stored predictions)."""
        ...

    def decide(self, snapshot: MarketSnapshot, budget_cents: int) -> list[StrategyOrder]:
        """Return orders for this snapshot given the remaining city-day budget."""
        ...


class BaseStrategy:
    """Convenience base with a no-op ``prepare``."""

    strategy_id: str = ""
    kind: str = "market"
    decisions: tuple[str, ...] = ("D1E",)
    params: dict = {}  # noqa: RUF012 — overridden per instance

    async def prepare(
        self, session: AsyncSession, cities: list[str], start: date, end: date
    ) -> None:
        """No preload needed by default."""
        return None

    def decide(self, snapshot: MarketSnapshot, budget_cents: int) -> list[StrategyOrder]:
        """Override in subclasses."""
        raise NotImplementedError
