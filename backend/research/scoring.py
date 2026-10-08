"""Scoring rules and city-day block bootstrap for algo v2 (hand-rolled, no scoringrules).

- Market probabilities: YES mids normalized across the event's brackets.
- Scores on the realized outcome: log loss, Brier (multi-category), RPS (ordered).
- Uncertainty: stationary block bootstrap over DATES (all cities of a date move
  together, so neighbouring days and same-day cities aren't treated as independent).
"""

from __future__ import annotations

import math

import numpy as np

from backend.common.schemas import MarketSnapshot

EPS = 1e-6


def market_probabilities(snapshot: MarketSnapshot) -> list[float] | None:
    """Normalized market-implied YES probabilities, ordered like ``snapshot.quotes``.

    Mid = (bid + ask) / 2 when both sides are quoted; a one-sided quote uses
    (0 + ask) / 2 or (bid + 100) / 2. Returns None if any bracket has no quote.
    """
    mids: list[float] = []
    for q in snapshot.quotes:
        bid = q.yes_bid if q.yes_bid is not None and q.yes_bid > 0 else 0
        ask = q.yes_ask if q.yes_ask is not None and q.yes_ask < 100 else 100
        if q.yes_bid is None and q.yes_ask is None:
            return None
        mids.append((bid + ask) / 2.0)
    total = sum(mids)
    if total <= 0:
        return None
    return [m / total for m in mids]


def log_loss(probs: list[float], winner: int) -> float:
    """Negative log probability assigned to the winning bracket (lower is better)."""
    return -math.log(max(EPS, probs[winner]))


def brier(probs: list[float], winner: int) -> float:
    """Multi-category Brier score: sum_i (p_i - y_i)^2 (lower is better)."""
    return sum((p - (1.0 if i == winner else 0.0)) ** 2 for i, p in enumerate(probs))


def rps(probs: list[float], winner: int) -> float:
    """Ranked probability score over ordered brackets, normalized by (n-1)."""
    n = len(probs)
    if n < 2:
        return 0.0
    cum_p = 0.0
    total = 0.0
    for i in range(n - 1):
        cum_p += probs[i]
        cum_o = 1.0 if winner <= i else 0.0
        total += (cum_p - cum_o) ** 2
    return total / (n - 1)


def stationary_bootstrap_indices(
    n: int, n_boot: int, mean_block: float, rng: np.random.Generator, length: int | None = None
) -> np.ndarray:
    """Politis-Romano stationary bootstrap index matrix (n_boot x length).

    Args:
        n: Number of time points to resample from.
        n_boot: Number of bootstrap replicates.
        mean_block: Mean block length (geometric block sizes).
        rng: NumPy random generator.
        length: Length of each replicate (defaults to n).

    Returns:
        Integer array of shape (n_boot, length) with indices into [0, n).
    """
    length = length or n
    p = 1.0 / mean_block
    idx = np.empty((n_boot, length), dtype=np.int64)
    idx[:, 0] = rng.integers(0, n, size=n_boot)
    starts = rng.random((n_boot, length)) < p
    fresh = rng.integers(0, n, size=(n_boot, length))
    for t in range(1, length):
        idx[:, t] = np.where(starts[:, t], fresh[:, t], (idx[:, t - 1] + 1) % n)
    return idx


def bootstrap_ratio(
    numer_by_date: np.ndarray,
    denom_by_date: np.ndarray,
    n_boot: int = 5000,
    mean_block: float = 7.0,
    seed: int = 7,
) -> np.ndarray:
    """Bootstrap distribution of sum(numer)/sum(denom) resampling dates in blocks.

    Args:
        numer_by_date: Per-date numerator (e.g. P&L cents), chronological.
        denom_by_date: Per-date denominator (e.g. traded city-days).

    Returns:
        Array of n_boot bootstrap statistics (NaN where a replicate's denominator is 0).
    """
    n = len(numer_by_date)
    rng = np.random.default_rng(seed)
    idx = stationary_bootstrap_indices(n, n_boot, mean_block, rng)
    num = numer_by_date[idx].sum(axis=1)
    den = denom_by_date[idx].sum(axis=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(den > 0, num / den, np.nan)


def bootstrap_window_sums(
    values_by_date: np.ndarray,
    window: int = 90,
    n_boot: int = 5000,
    mean_block: float = 7.0,
    seed: int = 11,
) -> np.ndarray:
    """Bootstrap distribution of the sum over a ``window``-date stretch."""
    rng = np.random.default_rng(seed)
    idx = stationary_bootstrap_indices(len(values_by_date), n_boot, mean_block, rng, window)
    return values_by_date[idx].sum(axis=1)


def max_drawdown(values: list[float]) -> float:
    """Largest peak-to-trough decline of the cumulative sum (positive number)."""
    peak = 0.0
    cum = 0.0
    worst = 0.0
    for v in values:
        cum += v
        peak = max(peak, cum)
        worst = max(worst, peak - cum)
    return worst
