"""C2 control: replay the v1 decision rule on real archived prices.

Uses the predictions v1 actually stored (latest ``predictions`` row with
``generated_at <= decision_ts`` — no lookahead) and v1's own
``scan_bracket`` with v1's live settings (blend 0.3 / clamp +-0.25 / NO 6% /
YES 12% / realistic fees, priced off the YES ask like v1's trading cycle).
Fills use the honest v2 execution model (NO at 100 - bid, exact fees).

Pass condition (pre-registration C2): over v1's live window the replica's
P&L sign must match the real trade record. If it doesn't, the harness — not
the strategy — is suspect, and nothing else is evaluated.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.common.models import CityEnum, Prediction
from backend.common.schemas import MarketSnapshot, StrategyOrder
from backend.strategy.base import BaseStrategy
from backend.trading.ev_calculator import GuardrailSettings, scan_bracket

V1_SETTINGS = {
    "model_weight": 0.3,
    "max_model_market_divergence": 0.25,
    "min_market_prob_for_yes": 0.15,
    "min_ev_threshold_yes": 0.12,
    "min_ev_threshold_no": 0.06,
    "fee_estimate_mode": "realistic",
}


def _same(a: float | None, b: float | None) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return abs(float(a) - float(b)) < 0.01


def _legacy_bounds(lower: float | None, upper: float | None) -> tuple[float | None, float | None]:
    """Map x.5 market bounds to the raw-strike bounds v1 stored before v1.9.12.

    Pre-2026-08-06 predictions carried raw Kalshi strikes as bounds (middle
    89/90, bottom cap 89, top floor 96) instead of the continuous x.5 bounds.
    """
    if lower is not None and upper is not None:
        return lower + 0.5, upper - 0.5
    if lower is None and upper is not None:
        return None, upper + 0.5
    if lower is not None and upper is None:
        return lower - 0.5, None
    return None, None


def _match_probability(
    brackets: list[dict], lower: float | None, upper: float | None
) -> float | None:
    """Find v1's stored probability for a market (current bounds, then legacy bounds)."""
    for lo, hi in ((lower, upper), _legacy_bounds(lower, upper)):
        for b in brackets:
            if _same(b.get("lower_bound_f"), lo) and _same(b.get("upper_bound_f"), hi):
                return float(b["probability"])
    return None


class V1ReplicaStrategy(BaseStrategy):
    """Replays v1's EV/guardrail rule at the given decision times (1 contract per signal)."""

    def __init__(self, decisions: tuple[str, ...] = ("D1E", "D0M")) -> None:
        self.strategy_id = "C2"
        self.kind = "control"
        self.decisions = decisions
        self.params = {**V1_SETTINGS, "decisions": list(decisions), "count": 1}
        # (city, event_date) -> list of (generated_at, brackets) sorted by generated_at
        self._predictions: dict[tuple[str, date], list[tuple[datetime, list[dict]]]] = {}

    async def prepare(
        self, session: AsyncSession, cities: list[str], start: date, end: date
    ) -> None:
        """Preload stored v1 predictions for the window."""
        rows = await session.execute(
            select(
                Prediction.city,
                Prediction.prediction_date,
                Prediction.generated_at,
                Prediction.brackets_json,
            ).where(
                Prediction.city.in_([CityEnum(c) for c in cities]),
                Prediction.prediction_date >= datetime.combine(start, datetime.min.time()),
                Prediction.prediction_date
                < datetime.combine(end + timedelta(days=1), datetime.min.time()),
            )
        )
        store: dict[tuple[str, date], list[tuple[datetime, list[dict]]]] = {}
        for city, pdate, generated_at, brackets in rows.all():
            code = city.value if hasattr(city, "value") else str(city)
            if generated_at is None or not brackets:
                continue
            store.setdefault((code, pdate.date()), []).append((generated_at, brackets))
        for v in store.values():
            v.sort(key=lambda x: x[0])
        self._predictions = store

    def _latest_brackets(self, city: str, event_date: date, ts: datetime) -> list[dict] | None:
        rows = self._predictions.get((city, event_date)) or []
        best = None
        for generated_at, brackets in rows:
            if generated_at <= ts:
                best = brackets
            else:
                break
        return best

    def decide(self, snapshot: MarketSnapshot, budget_cents: int) -> list[StrategyOrder]:
        """Run v1's scan_bracket on each bracket with the YES ask as price."""
        brackets = self._latest_brackets(snapshot.city, snapshot.event_date, snapshot.decision_ts)
        if not brackets:
            return []
        guardrails = GuardrailSettings(
            model_weight=V1_SETTINGS["model_weight"],
            max_model_market_divergence=V1_SETTINGS["max_model_market_divergence"],
            min_market_prob_for_yes=V1_SETTINGS["min_market_prob_for_yes"],
        )
        orders: list[StrategyOrder] = []
        for q in snapshot.quotes:
            if q.yes_ask is None or not 1 <= q.yes_ask <= 99:
                continue
            prob = _match_probability(brackets, q.lower_bound_f, q.upper_bound_f)
            if prob is None:
                continue
            signal = scan_bracket(
                bracket_label=q.label,
                bracket_probability=prob,
                market_price_cents=q.yes_ask,
                min_ev_threshold_yes=V1_SETTINGS["min_ev_threshold_yes"],
                min_ev_threshold_no=V1_SETTINGS["min_ev_threshold_no"],
                city=snapshot.city,
                prediction_date=snapshot.event_date.isoformat(),
                confidence="medium",
                market_ticker=q.ticker,
                guardrail_settings=guardrails,
                fee_estimate_mode=V1_SETTINGS["fee_estimate_mode"],
            )
            if signal is not None:
                orders.append(
                    StrategyOrder(
                        ticker=q.ticker, side=signal.side, count=1, model_probability=prob
                    )
                )
        return orders
