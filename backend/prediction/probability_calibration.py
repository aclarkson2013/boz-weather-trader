"""Per-city probability calibration via isotonic regression.

The bracket probabilities produced by ``calculate_bracket_probabilities``
are systematically miscalibrated against actual outcomes — at v1.9.5 the
0.7–0.9 buckets fired roughly half as often as predicted. This module
fits a non-parametric monotonic curve from raw predicted probabilities
to actual hit rates, learned from settled history, and applies it to
the bracket probabilities before they leave the prediction pipeline.

Lifecycle:
  - ``fit_calibration(city, db)`` learns one curve per city from joined
    Prediction × Settlement rows.
  - ``save_calibration``/``load_calibration`` persist the curves to a
    JSON file alongside ``source_weights.json`` and ``ml_weights.json``.
  - ``apply_calibration`` maps a list of raw bracket probabilities
    through the curve and renormalizes the result to sum to 1.0.

Refit cadence: weekly during ``train_all_models`` (Sunday 3 AM ET) and
on every manual ``/api/training/trigger`` call.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
from sklearn.isotonic import IsotonicRegression
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.common.logging import get_logger
from backend.common.models import Prediction, Settlement
from backend.prediction.calibration import _temp_in_bracket

logger = get_logger("MODEL")

CALIBRATION_FILENAME = "probability_calibration.json"

# Version of the bracket-bounds parsing the curves were fitted against.
# v2 = continuous half-degree bounds (v1.9.12 fix for the 1°F-wide middle
# bracket bug). Files saved before this field existed (or with an older
# version) are ignored on load so stale curves can't double-correct.
BOUNDS_VERSION = 2

# Cities that participate in calibration. Kept in sync with CityEnum.
SUPPORTED_CITIES: tuple[str, ...] = ("NYC", "CHI", "MIA", "AUS")

# Minimum prediction × settlement samples per city before fitting a curve.
# Below this, the city falls back to the identity curve (no calibration).
MIN_SAMPLES_PER_CITY = 200


# ─── In-memory representation ─────────────────────────────────────────


def _identity_curve() -> dict:
    """Curve that maps every input to itself — used as a safe fallback."""
    return {
        "x_thresholds": [0.0, 1.0],
        "y_thresholds": [0.0, 1.0],
        "sample_count": 0,
        "fitted_at": None,
        "is_identity": True,
    }


def _curve_from_isotonic(model: IsotonicRegression, sample_count: int) -> dict:
    """Extract the (x, y) breakpoints from a fitted IsotonicRegression."""
    return {
        "x_thresholds": [float(x) for x in model.X_thresholds_],
        "y_thresholds": [float(y) for y in model.y_thresholds_],
        "sample_count": int(sample_count),
        "fitted_at": datetime.now(UTC).isoformat(),
        "is_identity": False,
    }


# ─── Apply ─────────────────────────────────────────────────────────────


def apply_calibration(probs: list[float], curve: dict | None) -> list[float]:
    """Map raw bracket probabilities through the calibration curve.

    The curve is a piecewise-linear monotonic function defined by
    ``(x_thresholds, y_thresholds)``. We linearly interpolate each input
    probability against the breakpoints (clamping to [0, 1]) and then
    renormalize so the resulting list sums to 1.0.

    Args:
        probs: Raw bracket probabilities. Must be non-empty.
        curve: Calibration curve dict, or ``None`` for no-op.

    Returns:
        New list of calibrated probabilities, same length as ``probs``,
        summing to 1.0. If the input sums to 0, returns ``probs`` unchanged.
    """
    if not probs:
        return list(probs)
    if curve is None or curve.get("is_identity", False):
        return list(probs)

    xs = curve.get("x_thresholds")
    ys = curve.get("y_thresholds")
    if not xs or not ys or len(xs) != len(ys) or len(xs) < 2:
        return list(probs)

    # np.interp clamps to [ys[0], ys[-1]] for inputs outside the x range,
    # which is exactly what we want for inputs near 0 and 1.
    raw = np.clip(np.array(probs, dtype=np.float64), 0.0, 1.0)
    calibrated = np.interp(raw, np.array(xs), np.array(ys))
    calibrated = np.clip(calibrated, 0.0, 1.0)

    total = float(calibrated.sum())
    if total <= 0.0:
        # Calibration mapped everything to 0 — fall back to the input.
        return list(probs)

    return [float(p / total) for p in calibrated]


# ─── Persistence ───────────────────────────────────────────────────────


def load_calibration(model_dir: str = "models") -> dict[str, dict] | None:
    """Load saved per-city calibration curves from disk.

    Returns:
        Dict mapping city → curve, or None if no file exists or it is
        unreadable. Cities not present in the file are absent from the
        returned dict (callers should treat absent cities as identity).
    """
    path = Path(model_dir) / CALIBRATION_FILENAME
    try:
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("bounds_version") != BOUNDS_VERSION:
            # Curves fitted on pre-v1.9.12 predictions learned to correct
            # the 1°F-wide bracket-bounds bug; applying them to fixed
            # probabilities would double-correct. Ignore and refit fresh.
            logger.warning(
                "Stale probability calibration (pre bounds fix) — using identity until refit",
                extra={
                    "data": {
                        "path": str(path),
                        "file_bounds_version": data.get("bounds_version"),
                        "required_bounds_version": BOUNDS_VERSION,
                    }
                },
            )
            return None
        curves = data.get("curves")
        if isinstance(curves, dict) and len(curves) > 0:
            return curves
        return None
    except Exception:
        logger.warning(
            "Failed to load probability calibration — using identity",
            extra={"data": {"path": str(path)}},
        )
        return None


def save_calibration(curves: dict[str, dict], model_dir: str = "models") -> None:
    """Persist per-city calibration curves to disk as JSON."""
    path = Path(model_dir) / CALIBRATION_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "curves": curves,
        "computed_at": datetime.now(UTC).isoformat(),
        "bounds_version": BOUNDS_VERSION,
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    logger.info(
        "Probability calibration saved",
        extra={
            "data": {
                "path": str(path),
                "cities": sorted(curves.keys()),
                "sample_counts": {c: curves[c].get("sample_count", 0) for c in curves},
            }
        },
    )


# ─── Fit ───────────────────────────────────────────────────────────────


def _is_pre_bounds_fix_row(brackets: list[dict]) -> bool:
    """Detect predictions stored with pre-v1.9.12 bracket bounds.

    Old-format middle brackets carried raw Kalshi strikes and were ~1°F
    wide (e.g. [89.0, 90.0]); fixed-format middles are ~2°F wide at the
    half-degree (e.g. [88.5, 90.5]). Any middle bracket narrower than
    1.5°F marks the whole prediction row as pre-fix.
    """
    for bracket in brackets:
        lower = bracket.get("lower_bound_f")
        upper = bracket.get("upper_bound_f")
        if lower is not None and upper is not None and (upper - lower) < 1.5:
            return True
    return False


async def _collect_pairs(
    city: str,
    session: AsyncSession,
    lookback_days: int,
) -> list[tuple[float, int]]:
    """Collect (predicted_prob, actual_outcome) pairs for a city."""
    cutoff = datetime.utcnow() - timedelta(days=lookback_days)

    stmt = (
        select(Prediction.brackets_json, Settlement.actual_high_f)
        .join(
            Settlement,
            (Prediction.city == Settlement.city)
            & (Prediction.prediction_date == Settlement.settlement_date),
        )
        .where(
            Prediction.city == city,
            Settlement.actual_high_f.isnot(None),
            Prediction.prediction_date >= cutoff,
        )
    )

    result = await session.execute(stmt)
    rows = result.all()

    pairs: list[tuple[float, int]] = []
    for brackets_json, actual_high in rows:
        brackets = brackets_json
        if isinstance(brackets, str):
            brackets = json.loads(brackets)
        if _is_pre_bounds_fix_row(brackets):
            # Prediction stored before the v1.9.12 bracket-bounds fix:
            # its probabilities were computed on 1°F-wide middle brackets.
            # Training on them would re-learn the bug's distortion.
            continue
        for bracket in brackets:
            prob = bracket.get("probability")
            if prob is None:
                continue
            outcome = (
                1
                if _temp_in_bracket(
                    actual_high,
                    bracket.get("lower_bound_f"),
                    bracket.get("upper_bound_f"),
                )
                else 0
            )
            pairs.append((float(prob), outcome))
    return pairs


async def fit_calibration(
    city: str,
    session: AsyncSession,
    lookback_days: int = 90,
    min_samples: int = MIN_SAMPLES_PER_CITY,
) -> dict:
    """Fit a calibration curve for one city from joined prediction × settlement data.

    Returns the curve dict (or an identity curve if there is too little data).
    """
    pairs = await _collect_pairs(city, session, lookback_days)

    if len(pairs) < min_samples:
        logger.info(
            "Insufficient data for probability calibration — using identity",
            extra={
                "data": {
                    "city": city,
                    "sample_count": len(pairs),
                    "min_required": min_samples,
                }
            },
        )
        return _identity_curve()

    raw_probs = np.array([p for p, _ in pairs], dtype=np.float64)
    outcomes = np.array([o for _, o in pairs], dtype=np.float64)

    model = IsotonicRegression(
        y_min=0.0,
        y_max=1.0,
        out_of_bounds="clip",
        increasing=True,
    )
    model.fit(raw_probs, outcomes)

    curve = _curve_from_isotonic(model, sample_count=len(pairs))

    logger.info(
        "Probability calibration fitted",
        extra={
            "data": {
                "city": city,
                "sample_count": len(pairs),
                "thresholds": len(curve["x_thresholds"]),
                "x_range": [curve["x_thresholds"][0], curve["x_thresholds"][-1]],
                "y_range": [curve["y_thresholds"][0], curve["y_thresholds"][-1]],
            }
        },
    )

    return curve


async def fit_all_cities(
    session: AsyncSession,
    lookback_days: int = 90,
    min_samples: int = MIN_SAMPLES_PER_CITY,
) -> dict[str, dict]:
    """Fit a calibration curve for every supported city."""
    curves: dict[str, dict] = {}
    for city in SUPPORTED_CITIES:
        try:
            curves[city] = await fit_calibration(
                city,
                session,
                lookback_days=lookback_days,
                min_samples=min_samples,
            )
        except Exception:
            logger.warning(
                "Calibration fit failed — using identity for city",
                extra={"data": {"city": city}},
                exc_info=True,
            )
            curves[city] = _identity_curve()
    return curves
