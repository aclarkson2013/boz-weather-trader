"""Taker fill model and settlement P&L for the real-price backtester (algo v2).

Execution model (pre-registered, docs/research/v2-preregistration.md §3):
- Buy YES at the YES ask; buy NO at ``100 - YES bid``.
- No fill when that side has no quote (bid 0/None for NO, ask 100/None for YES).
- Exact per-order fee (``kalshi_fee_cents``).
- Optional slippage: +N cents on the price paid (capped at 99).
- A per-city-day budget caps total cost + fees; the last order is shrunk to fit.
"""

from __future__ import annotations

from datetime import datetime

from backend.common.schemas import BracketQuote, Fill, StrategyOrder
from backend.strategy.fees import kalshi_fee_cents


def side_price_cents(quote: BracketQuote, side: str) -> int | None:
    """Return the taker price for buying ``side``, or None if that side can't be filled.

    Args:
        quote: Bracket quote (YES bid/ask in cents).
        side: "yes" or "no".

    Returns:
        Price per contract in cents (1-99), or None.
    """
    if side == "yes":
        ask = quote.yes_ask
        return ask if ask is not None and 1 <= ask <= 99 else None
    bid = quote.yes_bid
    return 100 - bid if bid is not None and 1 <= bid <= 99 else None


def side_mid_cents(quote: BracketQuote, side: str) -> float | None:
    """Side-adjusted mid price (cents) when both sides are quoted, else None."""
    bid, ask = quote.yes_bid, quote.yes_ask
    if bid is None or ask is None or not (1 <= bid <= ask <= 99):
        return None
    mid = (bid + ask) / 2.0
    return mid if side == "yes" else 100.0 - mid


def fill_order(
    order: StrategyOrder,
    quote: BracketQuote,
    decision: str,
    decision_ts: datetime,
    budget_cents: int,
    slippage_cents: int = 0,
) -> Fill | None:
    """Simulate a taker fill for one order within the remaining budget.

    Args:
        order: The strategy's order.
        quote: Quote for the order's market at the decision time.
        decision: Decision label (e.g. "D1E").
        decision_ts: Decision timestamp (naive UTC).
        budget_cents: Remaining budget for this city-day (cost + fees).
        slippage_cents: Extra cents paid per contract (stress test).

    Returns:
        A Fill (possibly with a reduced count), or None if unfillable/unaffordable.
    """
    base = side_price_cents(quote, order.side)
    if base is None:
        return None
    price = min(99, base + slippage_cents)

    count = order.count
    while count >= 1 and price * count + kalshi_fee_cents(price, count) > budget_cents:
        count -= 1
    if count < 1:
        return None

    return Fill(
        ticker=order.ticker,
        side=order.side,
        count=count,
        price_cents=price,
        fee_cents=kalshi_fee_cents(price, count),
        mid_cents=side_mid_cents(quote, order.side),
        decision=decision,
        decision_ts=decision_ts,
    )


def fill_cost_cents(fill: Fill) -> int:
    """Total cash out for a fill: price x count + fee."""
    return fill.price_cents * fill.count + fill.fee_cents


def settle_pnl_cents(fill: Fill, result: str) -> int:
    """Net P&L in cents of a fill once its market settles.

    Args:
        fill: The fill.
        result: Market result, "yes" or "no".

    Returns:
        Payout (100c per winning contract) minus cost minus fee.
    """
    payout = 100 * fill.count if result == fill.side else 0
    return payout - fill_cost_cents(fill)
