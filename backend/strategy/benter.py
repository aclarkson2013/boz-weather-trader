"""B1-B4: Benter-style market-anchored combination (pre-registration §4).

Combined bracket probability (conditional logit over the event's brackets):

    P_i = softmax_i( alpha * log p_model_i + beta * log q_i + gamma * tail_i )

q = market mids normalized across brackets, p_model = NBM-direct (B1/B2) or
station EMOS (B3/B4), tail_i = 1 for the two catch-all brackets.

Fit (walk-forward, pooled across cities, refit monthly): maximum a posteriori
with FIXED priors alpha ~ N(0, 0.25^2), beta ~ N(1, 0.25^2), gamma ~ N(0, 0.5^2)
on settled city-days in [t-365, t-2]. Uncertainty: Laplace approximation
(inverse Hessian at the MAP), 200 draws.

Trade rule: buy side s of bracket i only if the 10th percentile (over draws) of
    edge = P_side - (price + fee) / 100
is > 0. One side per bracket. Size: fractional Kelly (lambda = 0.1) on the point
estimate, at least 1 contract, capped by the $4 city-day budget in the engine.

Model probabilities for TRAINING days come from models fit only on data before
those days (nested walk-forward), so no lookahead leaks into the combination.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np
from scipy.optimize import minimize
from sqlalchemy.ext.asyncio import AsyncSession

from backend.common.schemas import MarketSnapshot, StrategyOrder
from backend.research.forecast_models import (
    EmosParams,
    bracket_probs,
    features_at,
    fit_emos,
    load_issuances,
    month_start,
    nbm_direct_probs,
    training_window,
)
from backend.research.scoring import market_probabilities
from backend.research.snapshots import build_snapshot, decision_ts_for, load_city_archive
from backend.strategy.base import BaseStrategy
from backend.strategy.fees import kalshi_fee_cents
from backend.strategy.fills import side_price_cents

PRIOR_MEAN = np.array([0.0, 1.0, 0.0])  # alpha, beta, gamma
PRIOR_SD = np.array([0.25, 0.25, 0.5])
N_DRAWS = 200
EDGE_QUANTILE = 0.10
KELLY_LAMBDA = 0.1
DEFAULT_BANKROLL_CENTS = 6676
MIN_TRAIN_EVENTS = 100
HISTORY_DAYS = 400
EPS = 1e-4


@dataclass
class DayInputs:
    """Per city-day inputs at the strategy's decision time."""

    log_pm: np.ndarray  # (6,)
    log_q: np.ndarray  # (6,)
    tail: np.ndarray  # (6,)
    winner: int | None  # Index of the YES bracket, if settled cleanly


def combine(
    theta: np.ndarray, log_pm: np.ndarray, log_q: np.ndarray, tail: np.ndarray
) -> np.ndarray:
    """Softmax over the last axis of alpha*log_pm + beta*log_q + gamma*tail (batched)."""
    z = theta[..., 0, None] * log_pm + theta[..., 1, None] * log_q + theta[..., 2, None] * tail
    z = z - z.max(axis=-1, keepdims=True)
    e = np.exp(z)
    return e / e.sum(axis=-1, keepdims=True)


def _neg_log_posterior(
    theta: np.ndarray, arrays: tuple[np.ndarray, np.ndarray, np.ndarray], w: np.ndarray
) -> float:
    log_pm, log_q, tail = arrays
    p = combine(theta[None, :], log_pm, log_q, tail)
    ll = np.log(np.clip(p[np.arange(len(w)), w], 1e-12, None)).sum()
    prior = (((theta - PRIOR_MEAN) / PRIOR_SD) ** 2).sum() / 2.0
    return float(-ll + prior)


def _hessian(f, x: np.ndarray, h: float = 1e-3) -> np.ndarray:
    """Central finite-difference Hessian (small parameter vectors only)."""
    n = len(x)
    H = np.zeros((n, n))
    for i in range(n):
        for j in range(i, n):
            ei = np.zeros(n)
            ej = np.zeros(n)
            ei[i] = h
            ej[j] = h
            v = (f(x + ei + ej) - f(x + ei - ej) - f(x - ei + ej) + f(x - ei - ej)) / (4 * h * h)
            H[i, j] = H[j, i] = v
    return H


def fit_benter(days: list[DayInputs]) -> tuple[np.ndarray, np.ndarray] | None:
    """MAP fit + Laplace covariance on settled days. None if too few events."""
    usable = [d for d in days if d.winner is not None]
    if len(usable) < MIN_TRAIN_EVENTS:
        return None
    X = (
        np.stack([d.log_pm for d in usable]),
        np.stack([d.log_q for d in usable]),
        np.stack([d.tail for d in usable]),
    )
    w = np.array([d.winner for d in usable])

    def f(t: np.ndarray) -> float:
        return _neg_log_posterior(t, X, w)

    res = minimize(f, PRIOR_MEAN.copy(), method="BFGS")
    theta = res.x
    H = _hessian(f, theta)
    try:
        cov = np.linalg.inv(H)
        if not np.all(np.isfinite(cov)) or np.any(np.linalg.eigvalsh((cov + cov.T) / 2) <= 0):
            raise np.linalg.LinAlgError
    except np.linalg.LinAlgError:
        cov = np.diag(PRIOR_SD**2)
    return theta, (cov + cov.T) / 2


class BenterStrategy(BaseStrategy):
    """Market-anchored combination of a station forecast model with Kalshi prices.

    Args:
        strategy_id: "B1".."B4".
        model: "nbm" (NBM-direct) or "emos" (station NGR).
        decision: "D1E" or "D0M".
    """

    def __init__(self, strategy_id: str, model: str, decision: str, seed: int = 20261008) -> None:
        self.strategy_id = strategy_id
        self.kind = "model"
        self.decisions = (decision,)
        self.model = model
        self.decision = decision
        self.seed = seed
        self.params = {
            "model": model,
            "decision": decision,
            "prior_mean": PRIOR_MEAN.tolist(),
            "prior_sd": PRIOR_SD.tolist(),
            "draws": N_DRAWS,
            "edge_quantile": EDGE_QUANTILE,
            "kelly_lambda": KELLY_LAMBDA,
            "bankroll_cents": DEFAULT_BANKROLL_CENTS,
        }
        self._inputs: dict[tuple[str, date], DayInputs] = {}
        self._fits: dict[date, tuple[np.ndarray, np.ndarray]] = {}
        self.daily_scores: list[dict] = []  # Per eval city-day: model vs market log loss
        # Kept from prepare() so live (not yet archived) days can be scored too
        self._issuances: dict[str, object] = {}
        self._labels: dict[str, dict[date, float]] = {}
        self._emos_cache: dict[str, dict] = {}
        self.fit_log: list[dict] = []

    # ── preparation (walk-forward) ──

    async def prepare(
        self, session: AsyncSession, cities: list[str], start: date, end: date
    ) -> None:
        """Build model probabilities, then monthly Benter fits, strictly walk-forward."""
        hist_start = start - timedelta(days=HISTORY_DAYS)
        for city in cities:
            archive = await load_city_archive(session, city, hist_start, end)
            issuances = await load_issuances(session, city, hist_start - timedelta(days=5), end)
            labels: dict[date, float] = {}
            for d, markets in archive.markets_by_date.items():
                values = [m.expiration_value for m in markets if m.expiration_value is not None]
                if values and all(m.result in ("yes", "no") for m in markets):
                    labels[d] = values[0]

            emos_fits: dict[date, EmosParams | None] = {}
            self._issuances[city] = issuances
            self._labels[city] = labels
            self._emos_cache[city] = emos_fits

            d = hist_start
            while d <= end:
                snapshot, outcomes, _ = build_snapshot(archive, d, self.decision)
                d_next = d + timedelta(days=1)
                if snapshot is None or not snapshot.tiles_ok or len(snapshot.quotes) != 6:
                    d = d_next
                    continue
                q = market_probabilities(snapshot)
                feats = features_at(issuances, d, snapshot.decision_ts)
                if q is None or feats is None:
                    d = d_next
                    continue
                bounds = [(qq.lower_bound_f, qq.upper_bound_f) for qq in snapshot.quotes]
                params = (
                    self._emos_for(month_start(d), city, labels, issuances, emos_fits)
                    if self.model == "emos"
                    else None
                )
                if params is not None:
                    mu, sigma = params.predict(feats)
                    pm = bracket_probs(mu, sigma, bounds)
                else:
                    pm = nbm_direct_probs(feats, bounds)
                winners = [
                    i for i, qq in enumerate(snapshot.quotes) if outcomes.get(qq.ticker) == "yes"
                ]
                clean = all(outcomes.get(qq.ticker) in ("yes", "no") for qq in snapshot.quotes)
                self._inputs[(city, d)] = DayInputs(
                    log_pm=np.log(np.clip(np.array(pm), EPS, None)),
                    log_q=np.log(np.clip(np.array(q), EPS, None)),
                    tail=np.array(
                        [
                            1.0 if (qq.lower_bound_f is None or qq.upper_bound_f is None) else 0.0
                            for qq in snapshot.quotes
                        ]
                    ),
                    winner=winners[0] if clean and len(winners) == 1 else None,
                )
                d = d_next

        # Monthly pooled Benter fits (training labels strictly before t-1)
        month = month_start(start)
        while month <= end:
            lo, hi = training_window(month)
            train = [v for (c, d), v in self._inputs.items() if lo <= d <= hi]
            fit = fit_benter(train)
            if fit is not None:
                self._fits[month] = fit
                self.fit_log.append(
                    {
                        "month": month.isoformat(),
                        "n": sum(1 for t in train if t.winner is not None),
                        "theta": [round(float(x), 4) for x in fit[0]],
                    }
                )
            month = date(month.year + (month.month == 12), month.month % 12 + 1, 1)

        # Out-of-sample log-loss comparison on every eval city-day (criterion 5)
        for (city, d), v in sorted(self._inputs.items()):
            if not (start <= d <= end) or v.winner is None:
                continue
            fit = self._fits.get(month_start(d))
            if fit is None:
                continue
            p = combine(fit[0][None, :], v.log_pm[None, :], v.log_q[None, :], v.tail[None, :])[0]
            self.daily_scores.append(
                {
                    "city": city,
                    "date": d,
                    "ll_model": -math.log(max(1e-12, float(p[v.winner]))),
                    "ll_market": -float(v.log_q[v.winner]),
                }
            )

    def _emos_for(
        self,
        month: date,
        city: str,
        labels: dict[date, float],
        issuances,
        cache: dict[date, EmosParams | None],
    ) -> EmosParams | None:
        """EMOS fit for ``month`` from labels in [month-365, month-2] (cached)."""
        if month not in cache:
            lo, hi = training_window(month)
            xs, ys = [], []
            for d, y in labels.items():
                if lo <= d <= hi:
                    f = features_at(issuances, d, decision_ts_for(city, d, self.decision))
                    if f is not None:
                        xs.append(f)
                        ys.append(y)
            cache[month] = fit_emos(xs, ys)
        return cache[month]

    def add_live_snapshot(self, snapshot: MarketSnapshot) -> bool:
        """Compute model inputs for a live (not yet archived) city-day.

        Requires ``prepare()`` to have loaded that city. Uses only forecast
        issuances available at the snapshot's scheduled decision time.

        Returns:
            True if inputs were computed and ``decide()`` can run.
        """
        city, d = snapshot.city, snapshot.event_date
        issuances = self._issuances.get(city)
        if issuances is None or not snapshot.tiles_ok or len(snapshot.quotes) != 6:
            return False
        q = market_probabilities(snapshot)
        feats = features_at(issuances, d, snapshot.decision_ts)
        if q is None or feats is None:
            return False
        bounds = [(qq.lower_bound_f, qq.upper_bound_f) for qq in snapshot.quotes]
        params = (
            self._emos_for(
                month_start(d), city, self._labels[city], issuances, self._emos_cache[city]
            )
            if self.model == "emos"
            else None
        )
        if params is not None:
            mu, sigma = params.predict(feats)
            pm = bracket_probs(mu, sigma, bounds)
        else:
            pm = nbm_direct_probs(feats, bounds)
        self._inputs[(city, d)] = DayInputs(
            log_pm=np.log(np.clip(np.array(pm), EPS, None)),
            log_q=np.log(np.clip(np.array(q), EPS, None)),
            tail=np.array(
                [
                    1.0 if (qq.lower_bound_f is None or qq.upper_bound_f is None) else 0.0
                    for qq in snapshot.quotes
                ]
            ),
            winner=None,
        )
        return True

    # ── decisions ──

    def decide(self, snapshot: MarketSnapshot, budget_cents: int) -> list[StrategyOrder]:
        """Trade brackets whose 10th-percentile edge (over posterior draws) is positive."""
        v = self._inputs.get((snapshot.city, snapshot.event_date))
        fit = self._fits.get(month_start(snapshot.event_date))
        if v is None or fit is None or len(snapshot.quotes) != 6:
            return []
        theta, cov = fit
        rng = np.random.default_rng(
            self.seed + snapshot.event_date.toordinal() * 10 + sum(map(ord, snapshot.city)) % 10
        )
        draws = rng.multivariate_normal(theta, cov, size=N_DRAWS)
        P_draws = combine(draws, v.log_pm[None, :], v.log_q[None, :], v.tail[None, :])
        P_point = combine(theta[None, :], v.log_pm[None, :], v.log_q[None, :], v.tail[None, :])[0]

        candidates = []
        for i, q in enumerate(snapshot.quotes):
            best = None
            for side in ("yes", "no"):
                price = side_price_cents(q, side)
                if price is None:
                    continue
                c = (price + kalshi_fee_cents(price, 1)) / 100.0
                p_side_draws = P_draws[:, i] if side == "yes" else 1.0 - P_draws[:, i]
                p_side = float(P_point[i] if side == "yes" else 1.0 - P_point[i])
                if float(np.quantile(p_side_draws - c, EDGE_QUANTILE)) <= 0:
                    continue
                edge = p_side - c
                if best is None or edge > best[0]:
                    best = (edge, side, price, p_side, c)
            if best is not None:
                candidates.append((q.ticker, *best))

        orders = []
        for ticker, _edge, side, price, p_side, c in sorted(candidates, key=lambda x: -x[1]):
            kelly_f = max(0.0, (p_side - c) / max(1e-6, 1.0 - c))
            stake_cents = KELLY_LAMBDA * kelly_f * DEFAULT_BANKROLL_CENTS
            count = max(1, int(stake_cents // price))
            orders.append(
                StrategyOrder(ticker=ticker, side=side, count=count, model_probability=p_side)
            )
        return orders
