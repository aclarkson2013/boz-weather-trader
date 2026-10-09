"""Live kill-switch statistics for paper strategies (pre-registration §6, Amendments 1-2).

- SPRT (Wald) on trades with a model probability: per trade
      Lambda = y*log(P/q) + (1-y)*log((1-P)/(1-q))
  with P = strategy's probability for its side, q = price paid / 100 (market),
  y = 1 if the side won. Sum <= log(0.2/0.95) = -1.56 -> STOP (the market is
  right, the model isn't); sum >= log(0.8/0.05) = 2.77 -> evidence of edge
  (reported only — promotion still needs the full forward gate).
- CUSUM on per-city-day net P&L (all strategies, incl. L5 which has no P):
      S_t = max(0, S_{t-1} - pnl_t)   -> alarm when S >= $10 of unrecovered losses.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

SPRT_STOP = math.log(0.2 / 0.95)  # -1.558
SPRT_EDGE = math.log(0.8 / 0.05)  # +2.773
CUSUM_ALARM_CENTS = 1000  # $10 unrecovered drawdown (gate criterion 6 limit)


@dataclass
class MonitorResult:
    """Kill-switch state after the latest settled paper trades."""

    sprt_llr: float
    cusum_cents: float
    n_trades: int
    stop: bool
    reason: str | None


def sprt_increment(p_side: float, price_cents: int, won: bool) -> float:
    """Log-likelihood ratio contribution of one settled trade (model vs market)."""
    p = min(max(p_side, 1e-4), 1 - 1e-4)
    q = min(max(price_cents / 100.0, 1e-4), 1 - 1e-4)
    return math.log(p / q) if won else math.log((1 - p) / (1 - q))


def evaluate_monitor(
    trades: list[tuple[float | None, int, bool]], daily_pnl_cents: list[int]
) -> MonitorResult:
    """Compute SPRT and CUSUM from settled trades and chronological city-day P&L.

    Args:
        trades: (model probability or None, price paid, won) per settled trade.
        daily_pnl_cents: Net P&L per settled city-day, chronological.

    Returns:
        MonitorResult with stop flag and reason.
    """
    llr = sum(sprt_increment(p, price, won) for p, price, won in trades if p is not None)
    s = 0.0
    alarm = False
    for pnl in daily_pnl_cents:
        s = max(0.0, s - pnl)
        alarm = alarm or s >= CUSUM_ALARM_CENTS
    if llr <= SPRT_STOP:
        return MonitorResult(
            llr, s, len(trades), True, f"SPRT stop boundary crossed (LLR {llr:.2f})"
        )
    if alarm:
        return MonitorResult(llr, s, len(trades), True, "CUSUM alarm: $10 of unrecovered losses")
    return MonitorResult(llr, s, len(trades), False, None)
