"""Model probabilities for algo-v2 B-variants: NBM-direct and station EMOS (slice S4).

Both turn as-issued station forecasts (``forecast_issuances``, slice S3) into
bracket probabilities for one city-day at a decision time, using ONLY
issuances with ``available_at <= decision_ts`` (no lookahead).

- NBM-direct (B1/B2): Normal(NBM txn, NBM xnd) — the pre-registered "baseline to beat".
- Station EMOS (B3/B4): Gaussian NGR (Gneiting et al. 2005) fit by minimum CRPS:
      mu    = a + b1 * NBM + b2 * MOS           (b1, b2 >= 0; MOS = mean of GFS/NAM, else NBM)
      sigma = exp(c + d * log(NBM_sd))
  Refit per city at the start of each month on settled days in [t-365, t-2].

Bracket probabilities use the continuous x.5 bounds:
    P(lo < T < hi) = F(hi) - F(lo), catch-alls use F(hi) and 1 - F(lo).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import numpy as np
from scipy.optimize import minimize
from scipy.stats import norm
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.common.models import CityEnum, ForecastIssuance

MIN_SD_F = 1.0  # Floor on forecast spread (°F) — integer-rounded settlement noise
MIN_EMOS_TRAIN_DAYS = 60
EMOS_TRAIN_DAYS = 365
LABEL_LAG_DAYS = 2


# ─── Forecast features ───


@dataclass
class IssuanceIndex:
    """Issuances of one city keyed by (model, valid_date), sorted by available_at."""

    by_key: dict[tuple[str, date], list[tuple[datetime, float, float | None]]] = field(
        default_factory=dict
    )

    def latest(
        self, model: str, valid_date: date, ts: datetime
    ) -> tuple[float, float | None] | None:
        """Latest (tmax, sd) for ``model``/``valid_date`` available at or before ``ts``."""
        best = None
        for available_at, tmax, sd in self.by_key.get((model, valid_date), []):
            if available_at <= ts:
                best = (tmax, sd)
            else:
                break
        return best


async def load_issuances(session: AsyncSession, city: str, start: date, end: date) -> IssuanceIndex:
    """Load one city's issuances with valid_date in [start, end]."""
    rows = await session.execute(
        select(
            ForecastIssuance.model,
            ForecastIssuance.valid_date,
            ForecastIssuance.available_at,
            ForecastIssuance.tmax_f,
            ForecastIssuance.tmax_sd_f,
        )
        .where(
            ForecastIssuance.city == CityEnum(city),
            ForecastIssuance.valid_date >= start,
            ForecastIssuance.valid_date <= end,
        )
        .order_by(ForecastIssuance.available_at)
    )
    idx = IssuanceIndex()
    for model, valid_date, available_at, tmax, sd in rows.all():
        idx.by_key.setdefault((model, valid_date), []).append((available_at, tmax, sd))
    return idx


@dataclass
class Features:
    """Forecast inputs for one city-day at a decision time."""

    nbm: float
    nbm_sd: float
    mos: float  # mean of available GFS/NAM, else NBM


def features_at(idx: IssuanceIndex, valid_date: date, ts: datetime) -> Features | None:
    """Assemble features from issuances available at ``ts`` (None if no NBM yet)."""
    nbm = idx.latest("NBS", valid_date, ts)
    if nbm is None:
        return None
    mos_vals = [v[0] for m in ("GFS", "NAM") if (v := idx.latest(m, valid_date, ts)) is not None]
    sd = nbm[1] if nbm[1] is not None else 2.0
    return Features(
        nbm=nbm[0],
        nbm_sd=max(MIN_SD_F, sd),
        mos=float(np.mean(mos_vals)) if mos_vals else nbm[0],
    )


# ─── Bracket probabilities ───


def bracket_probs(
    mu: float, sigma: float, bounds: list[tuple[float | None, float | None]]
) -> list[float]:
    """Normal bracket probabilities for continuous (lower, upper) bounds, renormalized."""
    sigma = max(MIN_SD_F * 0.5, sigma)
    probs = []
    for lo, hi in bounds:
        p_hi = 1.0 if hi is None else norm.cdf((hi - mu) / sigma)
        p_lo = 0.0 if lo is None else norm.cdf((lo - mu) / sigma)
        probs.append(max(1e-6, p_hi - p_lo))
    total = sum(probs)
    return [p / total for p in probs]


def nbm_direct_probs(
    feats: Features, bounds: list[tuple[float | None, float | None]]
) -> list[float]:
    """B1/B2 model: Normal(NBM txn, NBM xnd)."""
    return bracket_probs(feats.nbm, feats.nbm_sd, bounds)


# ─── EMOS / NGR ───


def crps_normal(mu: np.ndarray, sigma: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Closed-form CRPS of N(mu, sigma^2) at observations y (Gneiting et al. 2005)."""
    z = (y - mu) / sigma
    return sigma * (z * (2 * norm.cdf(z) - 1) + 2 * norm.pdf(z) - 1 / math.sqrt(math.pi))


@dataclass
class EmosParams:
    """Fitted NGR parameters: mu = a + b1*nbm + b2*mos; sigma = exp(c + d*log(nbm_sd))."""

    a: float
    b1: float
    b2: float
    c: float
    d: float
    n_train: int

    def predict(self, f: Features) -> tuple[float, float]:
        mu = self.a + self.b1 * f.nbm + self.b2 * f.mos
        sigma = math.exp(self.c + self.d * math.log(f.nbm_sd))
        return mu, max(MIN_SD_F * 0.5, sigma)


def fit_emos(feats: list[Features], y: list[float]) -> EmosParams | None:
    """Fit NGR by minimum mean CRPS with b1, b2 >= 0 (L-BFGS-B).

    Returns None when there are fewer than ``MIN_EMOS_TRAIN_DAYS`` samples.
    """
    n = len(y)
    if n < MIN_EMOS_TRAIN_DAYS:
        return None
    nbm = np.array([f.nbm for f in feats])
    mos = np.array([f.mos for f in feats])
    lsd = np.log(np.array([f.nbm_sd for f in feats]))
    yy = np.array(y, dtype=float)

    def loss(theta: np.ndarray) -> float:
        a, b1, b2, c, d = theta
        sigma = np.exp(c + d * lsd)
        return float(crps_normal(a + b1 * nbm + b2 * mos, sigma, yy).mean())

    x0 = np.array([0.0, 1.0, 0.0, math.log(2.0), 0.0])
    bounds = [(None, None), (0.0, None), (0.0, None), (-3.0, 3.0), (-2.0, 2.0)]
    res = minimize(loss, x0, method="L-BFGS-B", bounds=bounds)
    a, b1, b2, c, d = res.x
    return EmosParams(a=float(a), b1=float(b1), b2=float(b2), c=float(c), d=float(d), n_train=n)


def month_start(d: date) -> date:
    """First day of ``d``'s month."""
    return date(d.year, d.month, 1)


def training_window(t: date) -> tuple[date, date]:
    """Label window for a fit used on/after ``t``: [t-365, t-2] (labels lag 2 days)."""
    return t - timedelta(days=EMOS_TRAIN_DAYS), t - timedelta(days=LABEL_LAG_DAYS)
