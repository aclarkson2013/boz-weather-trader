"""Real-price backtest engine (algo v2, slice S2).

Replays a strategy day by day over the Kalshi market archive: for each city-day
and each of the strategy's decision times it builds a no-lookahead snapshot,
asks the strategy for orders, fills them as a taker at the archived bid/ask with
exact per-order fees, enforces the per-city-day budget, and settles against
Kalshi's recorded result.

Unlike the legacy synthetic engine (``backtesting/engine.py``), nothing here is
simulated except the fill itself: prices, brackets and outcomes are real.

Events are excluded (and counted) — never silently dropped — when their
brackets don't tile or a filled market has no yes/no result.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from backend.common.logging import get_logger
from backend.common.schemas import Fill
from backend.research.scoring import brier, log_loss, market_probabilities, rps
from backend.research.snapshots import build_snapshot, load_city_archive
from backend.strategy.base import Strategy
from backend.strategy.fills import fill_cost_cents, fill_order, settle_pnl_cents

logger = get_logger("TRADING")

DEFAULT_BUDGET_CENTS = 400  # $4 per city-day (pre-registration §3)


@dataclass
class CityDayResult:
    """Outcome of one traded city-day."""

    city: str
    event_date: date
    era: str
    fills: list[Fill]
    pnl_cents: int
    cost_cents: int
    fee_cents: int
    contracts: int
    expected_taker_cost_cents: float  # Sum over fills of (price - mid) * count + fee


@dataclass
class BacktestRun:
    """Everything a backtest produced (traded days + bookkeeping)."""

    strategy_id: str
    results: list[CityDayResult] = field(default_factory=list)
    days_seen: int = 0
    excluded: Counter = field(default_factory=Counter)
    unfillable_orders: int = 0
    market_scores: dict[str, list[float]] = field(
        default_factory=lambda: {"log_loss": [], "brier": [], "rps": []}
    )


def _date_range(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


async def run_real_backtest(
    session: AsyncSession,
    strategy: Strategy,
    cities: list[str],
    start: date,
    end: date,
    *,
    slippage_cents: int = 0,
    budget_cents: int = DEFAULT_BUDGET_CENTS,
    score_market: bool = True,
) -> BacktestRun:
    """Run one strategy over the archive.

    Args:
        session: Async DB session (read-only use).
        strategy: Strategy instance.
        cities: City codes.
        start: First event date (inclusive).
        end: Last event date (inclusive).
        slippage_cents: Extra cents paid per contract (stress test).
        budget_cents: Max cost + fees per city-day.
        score_market: Also score the market's own probabilities at each snapshot.

    Returns:
        BacktestRun with per-city-day results and exclusion counts.
    """
    await strategy.prepare(session, cities, start, end)
    run = BacktestRun(strategy_id=strategy.strategy_id)

    for city in cities:
        archive = await load_city_archive(session, city, start, end)
        for event_date in _date_range(start, end):
            if event_date not in archive.markets_by_date:
                run.excluded["no_markets"] += 1
                continue
            run.days_seen += 1
            budget = budget_cents
            fills: list[Fill] = []
            outcomes: dict[str, str | None] = {}
            era = ""
            excluded_reason = None

            for decision in strategy.decisions:
                snapshot, outcomes, era = build_snapshot(archive, event_date, decision)
                if snapshot is None:
                    excluded_reason = excluded_reason or "not_open_at_decision"
                    continue
                if not snapshot.tiles_ok:
                    excluded_reason = "brackets_do_not_tile"
                    break

                if score_market:
                    probs = market_probabilities(snapshot)
                    winners = [
                        i for i, q in enumerate(snapshot.quotes) if outcomes.get(q.ticker) == "yes"
                    ]
                    if probs is not None and len(winners) == 1:
                        run.market_scores["log_loss"].append(log_loss(probs, winners[0]))
                        run.market_scores["brier"].append(brier(probs, winners[0]))
                        run.market_scores["rps"].append(rps(probs, winners[0]))

                quotes = {q.ticker: q for q in snapshot.quotes}
                for order in strategy.decide(snapshot, budget):
                    quote = quotes.get(order.ticker)
                    fill = (
                        fill_order(
                            order, quote, decision, snapshot.decision_ts, budget, slippage_cents
                        )
                        if quote is not None
                        else None
                    )
                    if fill is None:
                        run.unfillable_orders += 1
                        continue
                    budget -= fill_cost_cents(fill)
                    fills.append(fill)

            if excluded_reason == "brackets_do_not_tile":
                run.excluded[excluded_reason] += 1
                continue
            if not fills:
                continue
            if any(outcomes.get(f.ticker) not in ("yes", "no") for f in fills):
                run.excluded["unsettled_fill"] += 1
                continue

            pnl = sum(settle_pnl_cents(f, outcomes[f.ticker]) for f in fills)
            run.results.append(
                CityDayResult(
                    city=city,
                    event_date=event_date,
                    era=era or "",
                    fills=fills,
                    pnl_cents=pnl,
                    cost_cents=sum(f.price_cents * f.count for f in fills),
                    fee_cents=sum(f.fee_cents for f in fills),
                    contracts=sum(f.count for f in fills),
                    expected_taker_cost_cents=sum(
                        ((f.price_cents - f.mid_cents) * f.count if f.mid_cents is not None else 0)
                        + f.fee_cents
                        for f in fills
                    ),
                )
            )

    logger.info(
        "Real-price backtest finished",
        extra={
            "data": {
                "strategy": strategy.strategy_id,
                "traded_city_days": len(run.results),
                "days_seen": run.days_seen,
                "excluded": dict(run.excluded),
                "unfillable_orders": run.unfillable_orders,
            }
        },
    )
    return run
