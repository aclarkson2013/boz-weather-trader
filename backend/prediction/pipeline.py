"""Prediction pipeline orchestrator.

Ties together the four prediction steps into a single entry point:
    1. Ensemble forecast (weighted average of multiple sources)
    2. Historical error distribution (per city/season)
    3. Bracket probability calculation (normal CDF)
    4. Confidence assessment (model agreement, accuracy, freshness)

Usage:
    from backend.prediction.pipeline import generate_prediction

    prediction = await generate_prediction(
        city="NYC",
        target_date=date(2026, 2, 18),
        forecasts=weather_data_list,
        kalshi_brackets=bracket_defs,
        db_session=db,
    )
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from backend.common.config import get_settings
from backend.common.logging import get_logger
from backend.common.metrics import ML_PREDICTIONS_TOTAL
from backend.common.schemas import BracketPrediction, WeatherData
from backend.prediction.bias_correction import calculate_rolling_bias
from backend.prediction.brackets import calculate_bracket_probabilities
from backend.prediction.ensemble import assess_confidence, calculate_ensemble_forecast
from backend.prediction.error_dist import calculate_error_std
from backend.prediction.features import extract_features
from backend.prediction.model_ensemble import WEIGHTS_FILENAME as ML_WEIGHTS_FILENAME
from backend.prediction.model_ensemble import MultiModelEnsemble
from backend.prediction.probability_calibration import CALIBRATION_FILENAME, apply_calibration
from backend.prediction.source_weights import SOURCE_WEIGHTS_FILENAME

logger = get_logger("MODEL")

# ─── Multi-model ensemble singleton (lazy-loaded on first use) ───
_ml_ensemble: MultiModelEnsemble | None = None
_ml_ensemble_mtime: float = -1.0

# ─── Source weights singleton (lazy-loaded from disk) ───
_source_weights: dict[str, float] | None = None
_source_weights_loaded: bool = False
_source_weights_mtime: float = -1.0

# ─── Probability calibration singleton (lazy-loaded from disk) ───
_calibration_curves: dict[str, dict] | None = None
_calibration_loaded: bool = False
_calibration_mtime: float = -1.0


def _file_mtime(model_dir: str, filename: str) -> float:
    """Return the mtime of a file in the model dir, or -1.0 if unreadable.

    Used to detect that ``train_all_models`` rewrote a model artifact so the
    in-process cache can be refreshed. ``reload_models()`` only clears globals
    in the *calling* process, and Celery runs a prefork pool — without this
    check, sibling worker children keep serving whatever they cached at
    startup until the container restarts. See docs/ALGO_CHANGELOG.md
    (2026-08-21 review, "ROOT CAUSE A").
    """
    try:
        return (Path(model_dir) / filename).stat().st_mtime
    except OSError:
        return -1.0


def _get_ml_ensemble() -> MultiModelEnsemble:
    """Get or initialize the multi-model ensemble singleton.

    Reloads automatically when ``ml_weights.json`` changes on disk, so a
    retrain in one Celery child is picked up by all of them.
    """
    global _ml_ensemble, _ml_ensemble_mtime  # noqa: PLW0603
    settings = get_settings()
    mtime = _file_mtime(settings.xgb_model_dir, ML_WEIGHTS_FILENAME)
    if _ml_ensemble is not None and mtime != _ml_ensemble_mtime:
        logger.info(
            "ML model files changed on disk — reloading",
            extra={"data": {"previous_mtime": _ml_ensemble_mtime, "new_mtime": mtime}},
        )
        _ml_ensemble = None
    if _ml_ensemble is None:
        _ml_ensemble_mtime = mtime
        _ml_ensemble = MultiModelEnsemble(model_dir=settings.xgb_model_dir)
        status = _ml_ensemble.load_all()
        available = [k for k, v in status.items() if v]
        if available:
            logger.info(
                "ML models loaded",
                extra={"data": {"models": available, "weights": _ml_ensemble.weights}},
            )
        else:
            logger.info("No ML models available — ensemble-only mode")
    return _ml_ensemble


def _get_source_weights() -> dict[str, float] | None:
    """Get saved source weights from disk, or None to use defaults.

    Reloads automatically when ``source_weights.json`` changes on disk.
    """
    global _source_weights, _source_weights_loaded, _source_weights_mtime  # noqa: PLW0603
    settings = get_settings()
    mtime = _file_mtime(settings.xgb_model_dir, SOURCE_WEIGHTS_FILENAME)
    if _source_weights_loaded and mtime != _source_weights_mtime:
        _source_weights_loaded = False
    if not _source_weights_loaded:
        from backend.prediction.source_weights import load_source_weights

        _source_weights_mtime = mtime
        _source_weights = load_source_weights(settings.xgb_model_dir)
        _source_weights_loaded = True
        if _source_weights is not None:
            logger.info(
                "Source weights loaded from disk",
                extra={"data": {"weights": _source_weights}},
            )
    return _source_weights


def _get_calibration_curves() -> dict[str, dict] | None:
    """Get saved per-city probability calibration curves from disk.

    Reloads automatically when ``probability_calibration.json`` changes on
    disk. This is what stops a stale "no calibration" cache from surviving a
    refit in a sibling Celery child.
    """
    global _calibration_curves, _calibration_loaded, _calibration_mtime  # noqa: PLW0603
    settings = get_settings()
    mtime = _file_mtime(settings.xgb_model_dir, CALIBRATION_FILENAME)
    if _calibration_loaded and mtime != _calibration_mtime:
        logger.info(
            "Calibration file changed on disk — reloading",
            extra={"data": {"previous_mtime": _calibration_mtime, "new_mtime": mtime}},
        )
        _calibration_loaded = False
    if not _calibration_loaded:
        from backend.prediction.probability_calibration import load_calibration

        _calibration_mtime = mtime
        _calibration_curves = load_calibration(settings.xgb_model_dir)
        _calibration_loaded = True
        if _calibration_curves is not None:
            sample_counts = {
                city: curve.get("sample_count", 0) for city, curve in _calibration_curves.items()
            }
            logger.info(
                "Probability calibration loaded from disk",
                extra={"data": {"sample_counts": sample_counts}},
            )
    return _calibration_curves


def reload_models() -> None:
    """Invalidate cached models, source weights, and calibration curves.

    Called after retraining completes (from train_models.py) so the
    prediction pipeline picks up fresh weights and calibration.

    This only clears globals in the *calling* process. Sibling Celery prefork
    children are covered by the mtime check in the ``_get_*`` loaders, which
    is the durable mechanism; this call just makes the refresh immediate for
    the process that did the retraining.
    """
    global _ml_ensemble, _source_weights, _source_weights_loaded  # noqa: PLW0603
    global _calibration_curves, _calibration_loaded  # noqa: PLW0603
    global _ml_ensemble_mtime, _source_weights_mtime, _calibration_mtime  # noqa: PLW0603
    _ml_ensemble = None
    _source_weights = None
    _source_weights_loaded = False
    _calibration_curves = None
    _calibration_loaded = False
    _ml_ensemble_mtime = -1.0
    _source_weights_mtime = -1.0
    _calibration_mtime = -1.0
    logger.info("ML model cache invalidated — will reload on next prediction")


def _try_multi_model_prediction(
    forecasts: list[WeatherData],
    city: str,
    target_date: date,
) -> tuple[float | None, list[str]]:
    """Attempt a multi-model ML prediction, returning (None, []) on any failure.

    This function wraps all ML logic in try/except so the pipeline
    never crashes due to ML issues — it just falls back to ensemble-only.

    Returns:
        (predicted_temp, list_of_model_names) or (None, []) on failure.
    """
    settings = get_settings()
    if settings.ml_ensemble_weight <= 0.0:
        return None, []

    try:
        ensemble = _get_ml_ensemble()
        if not ensemble.is_any_available():
            return None, []

        features = extract_features(forecasts, city, target_date)
        prediction, model_names = ensemble.predict(features)

        if prediction is not None:
            for name in model_names:
                ML_PREDICTIONS_TOTAL.labels(city=city, model=name, status="success").inc()

        return prediction, model_names

    except Exception:
        ML_PREDICTIONS_TOTAL.labels(city=city, model="ensemble", status="error").inc()
        logger.warning(
            "Multi-model prediction failed — falling back to ensemble",
            extra={"data": {"city": city, "date": str(target_date)}},
            exc_info=True,
        )
        return None, []


async def generate_prediction(
    city: str,
    target_date: date,
    forecasts: list[WeatherData],
    kalshi_brackets: list[dict],
    db_session: AsyncSession,
    model_weights: dict[str, float] | None = None,
) -> BracketPrediction:
    """Run the full prediction pipeline for one city and date.

    This is the main entry point for the prediction engine. It orchestrates
    all four steps and returns a complete BracketPrediction ready for the
    trading engine.

    Args:
        city: City code ("NYC", "CHI", "MIA", "AUS").
        target_date: The date we are predicting the high temperature for.
        forecasts: List of WeatherData from multiple sources for this city/date.
        kalshi_brackets: Bracket definitions from Kalshi market data. Each dict
            must have keys: "lower_bound_f" (float|None), "upper_bound_f"
            (float|None), "label" (str).
        db_session: SQLAlchemy async session (for historical error lookup).
        model_weights: Optional override for ensemble weights.

    Returns:
        A complete BracketPrediction ready for the trading engine.
    """
    # Step 1: Ensemble forecast (use saved source weights if available)
    effective_weights = model_weights or _get_source_weights()
    ensemble_temp, spread, sources = calculate_ensemble_forecast(
        forecasts,
        weights=effective_weights,
    )

    # Step 1b: Multi-model ML prediction (blended with ensemble, graceful degradation)
    ml_temp, ml_models = _try_multi_model_prediction(forecasts, city, target_date)
    if ml_temp is not None:
        ml_weight = get_settings().ml_ensemble_weight
        final_temp = (1 - ml_weight) * ensemble_temp + ml_weight * ml_temp
        sources.extend(ml_models)
        logger.debug(
            "ML ensemble blended",
            extra={
                "data": {
                    "city": city,
                    "ensemble_temp": round(ensemble_temp, 2),
                    "ml_temp": round(ml_temp, 2),
                    "ml_weight": ml_weight,
                    "ml_models": ml_models,
                    "final_temp": round(final_temp, 2),
                }
            },
        )
        ensemble_temp = final_temp

    # Step 1c: Rolling bias correction (adjusts for systematic forecast error)
    bias_adjustment = await calculate_rolling_bias(
        city=city,
        target_date=target_date,
        db_session=db_session,
    )
    if bias_adjustment != 0.0:
        pre_bias_temp = ensemble_temp
        ensemble_temp += bias_adjustment
        logger.debug(
            "Bias correction applied",
            extra={
                "data": {
                    "city": city,
                    "date": str(target_date),
                    "pre_bias_temp_f": round(pre_bias_temp, 2),
                    "bias_adjustment_f": round(bias_adjustment, 2),
                    "adjusted_temp_f": round(ensemble_temp, 2),
                }
            },
        )

    # Step 2: Historical error distribution
    error_std = await calculate_error_std(
        city=city,
        month=target_date.month,
        db_session=db_session,
    )

    # Step 3: Bracket probabilities
    bracket_probs = calculate_bracket_probabilities(
        ensemble_forecast_f=ensemble_temp,
        error_std_f=error_std,
        brackets=kalshi_brackets,
    )

    # Step 3b: Probability calibration (per-city isotonic curve learned
    # from settled history — fixes systematic over-/under-confidence in
    # the raw CDF bracket probabilities).
    curves = _get_calibration_curves()
    city_curve = curves.get(city) if curves else None
    if city_curve is not None and not city_curve.get("is_identity", False):
        raw_probs = [b.probability for b in bracket_probs]
        calibrated = apply_calibration(raw_probs, city_curve)
        for bracket, new_prob in zip(bracket_probs, calibrated, strict=True):
            bracket.probability = new_prob
        logger.debug(
            "Probability calibration applied",
            extra={
                "data": {
                    "city": city,
                    "raw_probs": [round(p, 4) for p in raw_probs],
                    "calibrated_probs": [round(p, 4) for p in calibrated],
                }
            },
        )

    # Step 4: Confidence assessment
    # Calculate data age from the oldest forecast timestamp.
    now = datetime.now(UTC)
    oldest_forecast = min(fc.fetched_at for fc in forecasts)
    # Handle timezone-aware vs naive comparison safely.
    if oldest_forecast.tzinfo is None:
        data_age_minutes = (now.replace(tzinfo=None) - oldest_forecast).total_seconds() / 60.0
    else:
        data_age_minutes = (now - oldest_forecast).total_seconds() / 60.0

    confidence = assess_confidence(
        forecast_spread_f=spread,
        error_std_f=error_std,
        num_sources=len(sources),
        data_age_minutes=data_age_minutes,
    )

    # Build the BracketPrediction using the ACTUAL schema field names:
    # ensemble_mean_f (not ensemble_forecast_f), ensemble_std_f (not error_std_f).
    prediction = BracketPrediction(
        city=city,
        date=target_date,
        brackets=bracket_probs,
        ensemble_mean_f=round(ensemble_temp, 2),
        ensemble_std_f=round(error_std, 2),
        confidence=confidence,
        model_sources=sources,
        generated_at=now,
    )

    logger.info(
        "Prediction generated",
        extra={
            "data": {
                "city": city,
                "date": str(target_date),
                "ensemble_mean_f": prediction.ensemble_mean_f,
                "ensemble_std_f": prediction.ensemble_std_f,
                "confidence": confidence,
                "spread_f": round(spread, 2),
                "sources": sources,
                "bracket_count": len(bracket_probs),
            }
        },
    )

    return prediction
