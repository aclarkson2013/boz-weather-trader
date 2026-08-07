# Agent 3: Prediction Engine

## Your Mission

Build the statistical prediction engine that takes weather forecast data and produces probability distributions across Kalshi brackets. You are the brain of the bot — your probabilities directly determine which trades get placed.

This document is the complete specification. An agent should be able to build every file in `backend/prediction/` from this document alone, without guessing.

## What You Build

```
backend/prediction/
├── __init__.py
├── pipeline.py         -> Prediction pipeline orchestrator (ensemble → multi-model ML blend → bias correction → error dist → brackets)
├── bias_correction.py  -> Rolling bias correction (14-day lookback, self-adjusting)
├── ensemble.py         -> Weighted ensemble of multiple forecast sources + confidence assessment
├── features.py         -> ML feature engineering (21 features, pure module, no I/O)
├── xgb_model.py        -> XGBoost model manager (load, predict, train, save, JSON serialization)
├── ml_models.py        -> RF + Ridge model managers (joblib serialization, NaN imputation)
├── model_ensemble.py   -> Multi-model orchestrator (XGBoost + RF + Ridge, inverse-RMSE weights)
├── train_xgb.py        -> XGBoost training Celery task (kept for backward compat)
├── train_models.py     -> Multi-model training Celery task (weekly retraining, all 3 models, persists TrainingReport + source weights)
├── source_weights.py   -> Source weight persistence (load/save/compute from accuracy data)
├── brackets.py         -> Bracket probability calculator (scipy CDF)
├── error_dist.py       -> Historical forecast error distribution analysis
├── accuracy.py         -> Per-source forecast accuracy (MAE, RMSE, bias) + error trends
├── calibration.py      -> Real calibration: Brier score + calibration buckets
├── postmortem.py       -> Generate trade post-mortem data after settlement
└── exceptions.py       -> Prediction-specific exceptions
```

## Multi-Model ML Ensemble (Phase 27, replaces Phase 23 single-model)

Three ML models provide learned temperature predictions that blend with the static ensemble:

### Architecture
1. **Statistical Ensemble** (weighted average of NWS, ECMWF, GFS, ICON, GEM) → `ensemble_temp`
2. **Multi-Model ML**: XGBoost + Random Forest + Ridge, all trained on same 21 features
3. **Inverse-RMSE weighting**: `weight_i = (1/rmse_i) / sum(1/rmse_j)` — lower error = higher weight
4. **Blend**: `final_temp = (1 - ml_weight) * ensemble_temp + ml_weight * ml_temp` (default 70/30)
5. Graceful fallback: if no models available → ensemble-only; if one model fails → others still contribute
6. Backward compat: if only XGBoost model on disk → loads XGBoost alone with weight=1.0

### Feature Vector (21 features)
- Per-source highs (4): NWS, ECMWF, GFS, ICON
- Per-source lows (4): NWS, ECMWF, GFS, ICON
- NWS weather vars (3): humidity, wind speed, cloud cover
- Ensemble stats (2): spread (max-min), source count
- Temporal (4): month, day_of_year, sin(month), cos(month)
- City one-hot (4): NYC, CHI, MIA, AUS
- XGBoost handles NaN natively — missing sources become NaN

### Training Pipeline (`train_models.py`)
- Celery task `train_all_models`, scheduled Sunday 3 AM ET via Celery Beat
- Reuses `_fetch_training_data()` and `_rows_to_arrays()` from `train_xgb.py`
- Chronological 80/20 split (NOT random — time series)
- Trains all 3 models, computes inverse-RMSE weights, saves accepted + `ml_weights.json`
- Model rejection: test RMSE > 5.0°F → don't save that model (partial acceptance OK)
- XGBoost: JSON serialization; RF/Ridge: joblib serialization
- Min 60 training samples required
- soft_time_limit=600 (10 min), time_limit=720 (12 min)

### Model Managers
- `xgb_model.py`: XGBoost — handles NaN natively, JSON save/load
- `ml_models.py`: RF + Ridge — require NaN imputation via column medians, joblib save/load
  - NaN fill values stored in metadata JSON, persist through save/load cycles
  - RF params: n_estimators=200, max_depth=8, min_samples_split=5, min_samples_leaf=3, random_state=42, n_jobs=1
  - Ridge params: alpha=1.0

### Configuration (`config.py`)
- `xgb_model_dir`: model file directory (default: "models")
- `ml_ensemble_weight`: multi-model blend weight (default: 0.30)
- `xgb_ensemble_weight`: deprecated alias (kept for backward compat)
- `xgb_min_training_samples`: minimum pairs to train (default: 20)
- `xgb_retrain_interval_days`: retrain frequency (default: 14)

## Dependencies

- **Input from Agent 1 (Weather):** `WeatherData` objects from `backend/common/schemas.py`
- **Input from Agent 2 (Kalshi):** Bracket definitions from `KalshiMarket` objects (bracket ranges)
- **Output to Agent 4 (Trading):** `BracketPrediction` objects -- probability for each bracket

### Python Package Dependencies

These must be in `requirements.txt`:

```
scipy>=1.11.0
numpy>=1.24.0
```

## How Prediction Works

### Step 1: Ensemble Forecast

Combine multiple weather sources into a single best-estimate forecast:

```
NWS point forecast:        55 F  (weight: 0.40)
Open-Meteo ICON:           55 F  (weight: 0.25)
Open-Meteo ECMWF:          53 F  (weight: 0.15)
Open-Meteo GFS:            54 F  (weight: 0.10)
Open-Meteo GEM:            54 F  (weight: 0.10)
-----------------------------------------------
Weighted ensemble:         54.35 F
```

Weights are:
- Overridden by `source_weights.json` once training runs (inverse-RMSE from accuracy data)
- Defaults tuned from production accuracy (Feb 2026): NWS/ICON best, ECMWF/GFS have +3°F warm bias

#### Implementation: `backend/prediction/ensemble.py`

```python
from __future__ import annotations

from backend.common.schemas import WeatherData
from backend.common.logging import get_logger

logger = get_logger("MODEL")

# Default weights -- overridden once source_weights.json is generated by training.
# Tuned from production accuracy data (Feb 2026).
DEFAULT_MODEL_WEIGHTS = {
    "NWS": 0.40,
    "Open-Meteo:ICON": 0.25,
    "Open-Meteo:ECMWF": 0.15,
    "Open-Meteo:GFS": 0.10,
    "Open-Meteo:GEM": 0.10,
}


def calculate_ensemble_forecast(
    forecasts: list[WeatherData],
    weights: dict[str, float] | None = None,
) -> tuple[float, float, list[str]]:
    """Calculate weighted ensemble forecast from multiple sources.

    Args:
        forecasts: List of WeatherData from different sources for the same city/date
        weights: Optional custom weight dict {source: weight}. Uses defaults if None.

    Returns:
        Tuple of (ensemble_temp_f, forecast_spread_f, source_names)
        - ensemble_temp_f: weighted average temperature
        - forecast_spread_f: max - min across all sources (spread indicator)
        - source_names: list of sources that contributed

    Raises:
        ValueError: If forecasts list is empty or all weights are zero.
    """
    weights = weights or DEFAULT_MODEL_WEIGHTS

    if not forecasts:
        raise ValueError("No forecasts provided for ensemble calculation")

    weighted_sum = 0.0
    weight_total = 0.0
    temps = []
    sources = []

    for fc in forecasts:
        w = weights.get(fc.source, 0.05)  # default small weight for unknown sources
        weighted_sum += fc.forecast_high_f * w
        weight_total += w
        temps.append(fc.forecast_high_f)
        sources.append(fc.source)

    if weight_total == 0:
        raise ValueError("All weights are zero")

    ensemble_temp = weighted_sum / weight_total
    spread = max(temps) - min(temps)

    logger.info(
        "Ensemble calculated",
        extra={
            "data": {
                "ensemble_f": round(ensemble_temp, 1),
                "spread_f": round(spread, 1),
                "sources": sources,
                "individual_temps": [round(t, 1) for t in temps],
            }
        },
    )

    return ensemble_temp, spread, sources
```

**Key behaviors:**
- When only 1 source is available, it gets 100% weight. This is correct -- the formula naturally handles it.
- Unknown sources (not in the weights dict) get a small default weight of 0.05 so they contribute without dominating.
- The `forecast_spread_f` (max - min) is used later by confidence assessment.

### Step 2: Forecast Error Distribution

The ensemble gives us a point estimate (54.05 F), but we need a probability distribution. The key question: **how often is the forecast wrong, and by how much?**

Use historical data to build an error distribution:
1. For each city, collect past forecasts and compare to actual NWS CLI high temps
2. Calculate forecast errors (actual - predicted) for each day
3. Fit a probability distribution (normal or t-distribution) to the errors
4. The standard deviation varies by city and season:
   - NYC in summer: roughly 1.5-2.0 F std dev (forecasts are accurate)
   - Chicago in winter: roughly 3.0-3.5 F std dev (forecasts are volatile)

#### Implementation: `backend/prediction/error_dist.py`

```python
from __future__ import annotations

import numpy as np
from scipy import stats
from sqlalchemy import select
from backend.common.models import WeatherForecast, Settlement
from backend.common.logging import get_logger

logger = get_logger("MODEL")

# Fallback error standard deviations (used when insufficient historical data)
# These are rough estimates based on typical NWS forecast accuracy.
# Values are in degrees Fahrenheit.
FALLBACK_ERROR_STD = {
    "NYC": {"winter": 3.0, "spring": 2.5, "summer": 1.8, "fall": 2.3},
    "CHI": {"winter": 3.5, "spring": 3.0, "summer": 2.0, "fall": 2.5},
    "MIA": {"winter": 1.5, "spring": 1.8, "summer": 2.0, "fall": 1.8},
    "AUS": {"winter": 2.5, "spring": 2.8, "summer": 2.0, "fall": 2.3},
}


def get_season(month: int) -> str:
    """Get season from month number.

    Args:
        month: Month number (1-12).

    Returns:
        One of "winter", "spring", "summer", "fall".
    """
    if month in (12, 1, 2):
        return "winter"
    elif month in (3, 4, 5):
        return "spring"
    elif month in (6, 7, 8):
        return "summer"
    else:
        return "fall"


async def calculate_error_std(
    city: str,
    month: int,
    db_session,
    min_samples: int = 30,
) -> float:
    """Calculate historical forecast error standard deviation for a city/season.

    Compares past NWS forecasts to actual NWS CLI settlements.
    If insufficient data (<min_samples), falls back to hardcoded estimates.

    Args:
        city: City code ("NYC", "CHI", "MIA", "AUS")
        month: Month number (1-12) to determine season
        db_session: SQLAlchemy async session
        min_samples: Minimum historical data points needed

    Returns:
        Standard deviation of forecast errors in degrees Fahrenheit.
    """
    season = get_season(month)

    try:
        # Query historical forecasts vs settlements for this city and season
        # Join WeatherForecast with Settlement on city + date
        # Filter to same season months
        season_months = {
            "winter": (12, 1, 2),
            "spring": (3, 4, 5),
            "summer": (6, 7, 8),
            "fall": (9, 10, 11),
        }[season]

        # Build query: get (forecast_high, actual_high) pairs
        # where the forecast was made for a date in the same season
        stmt = (
            select(
                WeatherForecast.forecast_high_f,
                Settlement.actual_high_f,
            )
            .join(
                Settlement,
                (
                    (WeatherForecast.city == Settlement.city)
                    & (WeatherForecast.target_date == Settlement.date)
                ),
            )
            .where(WeatherForecast.city == city)
            .where(WeatherForecast.source == "NWS")  # compare NWS forecasts to actuals
            .where(Settlement.actual_high_f.isnot(None))
        )

        result = await db_session.execute(stmt)
        rows = result.all()

        # Filter to the relevant season months in Python
        # (SQLAlchemy extract works but varies by DB; this is safer)
        errors = []
        for forecast_high, actual_high in rows:
            errors.append(actual_high - forecast_high)

        if len(errors) >= min_samples:
            error_std = float(np.std(errors, ddof=1))  # sample std dev
            logger.info(
                "Calculated historical error std",
                extra={
                    "data": {
                        "city": city,
                        "season": season,
                        "std_f": round(error_std, 2),
                        "sample_count": len(errors),
                    }
                },
            )
            return error_std
        else:
            logger.info(
                "Insufficient historical data for error std",
                extra={
                    "data": {
                        "city": city,
                        "season": season,
                        "sample_count": len(errors),
                        "min_required": min_samples,
                    }
                },
            )

    except Exception as e:
        logger.warning(
            "Error querying historical data, using fallback",
            extra={
                "data": {
                    "city": city,
                    "season": season,
                    "error": str(e),
                }
            },
        )

    # Fall back to hardcoded estimates
    fallback = FALLBACK_ERROR_STD.get(city, {}).get(season, 2.5)
    logger.info(
        "Using fallback error std",
        extra={
            "data": {
                "city": city,
                "season": season,
                "std_f": fallback,
                "reason": "insufficient_data",
            }
        },
    )
    return fallback
```

#### The Bootstrap Problem

**IMPORTANT:** When Boz Weather Trader first launches, there is NO historical data to calculate error distributions. This is the "bootstrap problem."

**Solution:**
1. Use `FALLBACK_ERROR_STD` values (hardcoded estimates) for the first 30 days.
2. Log a warning that we are using fallback values.
3. After 30+ days of data collection, switch to calculated historical errors.
4. The transition should be automatic -- `calculate_error_std` checks data count on each run.

The fallback values are conservative (slightly wider distributions), which means:
- Bracket probabilities will be more spread out (less confident).
- Fewer trades will meet the EV threshold.
- This is GOOD -- we are cautious when we do not have data to be confident.

#### Normal vs. t-Distribution Decision

Use Normal distribution (`scipy.stats.norm`) as the default.

**Reason:** With NWS forecast errors, the distribution is approximately normal for most cities and seasons.

Consider switching to t-distribution (`scipy.stats.t`) when:
- Sample size is small (< 30 data points for that city/season)
- There are known fat-tail events (Chicago winter has more extreme misses)
- Use degrees of freedom = `sample_size - 1`

**For MVP:** Use Normal everywhere. t-distribution is a Phase 2 optimization.

### Step 3: Bracket Probability Calculation

Given the ensemble forecast and error distribution, calculate P(temp in bracket) for each bracket:

```
Ensemble forecast: 54.05 F
Error distribution: Normal(mean=0, std=2.1)

Bracket 1: P(temp < 51) = P(error < -3.05) = 7.3%
Bracket 2: P(51 <= temp < 53) = P(-3.05 <= error < -1.05) = 15.2%
Bracket 3: P(53 <= temp < 55) = P(-1.05 <= error < 0.95) = 31.8%  <- most likely
Bracket 4: P(55 <= temp < 57) = P(0.95 <= error < 2.95) = 28.1%
Bracket 5: P(57 <= temp < 59) = P(2.95 <= error < 4.95) = 12.4%
Bracket 6: P(temp >= 59) = P(error >= 4.95) = 5.2%
                                              ---------
                                              100.0%  <- MUST sum to 100%
```

#### Implementation: `backend/prediction/brackets.py`

```python
from __future__ import annotations

from scipy import stats
import numpy as np
from backend.common.schemas import BracketProbability, BracketPrediction
from backend.common.logging import get_logger

logger = get_logger("MODEL")


def calculate_bracket_probabilities(
    ensemble_forecast_f: float,
    error_std_f: float,
    brackets: list[dict],  # from Kalshi: [{lower_bound_f, upper_bound_f, label}, ...]
) -> list[BracketProbability]:
    """Calculate probability of temperature landing in each bracket.

    Uses a normal distribution centered on the ensemble forecast,
    with standard deviation from historical forecast errors.

    The CDF approach:
    - For a bracket [lower, upper), the probability is CDF(upper) - CDF(lower).
    - The bottom edge bracket has no lower bound: P(temp < upper) = CDF(upper).
    - The top edge bracket has no upper bound: P(temp >= lower) = 1 - CDF(lower).

    Args:
        ensemble_forecast_f: Weighted ensemble temperature forecast (Fahrenheit).
        error_std_f: Standard deviation of historical forecast errors for this
            city/season. Must be > 0.
        brackets: List of bracket definitions from Kalshi (6 brackets).
            Each dict must have keys: "lower_bound_f" (float|None),
            "upper_bound_f" (float|None), "label" (str).

    Returns:
        List of 6 BracketProbability objects, probabilities sum to 1.0.

    Raises:
        ValueError: If error_std_f <= 0 or brackets list is empty.
    """
    if error_std_f <= 0:
        raise ValueError(f"error_std_f must be positive, got {error_std_f}")
    if not brackets:
        raise ValueError("Brackets list is empty")

    dist = stats.norm(loc=ensemble_forecast_f, scale=error_std_f)
    results = []

    for bracket in brackets:
        lower = bracket.get("lower_bound_f")
        upper = bracket.get("upper_bound_f")

        if lower is None and upper is not None:
            # Bottom edge bracket: P(temp < upper)
            prob = dist.cdf(upper)
        elif upper is None and lower is not None:
            # Top edge bracket: P(temp >= lower)
            prob = 1.0 - dist.cdf(lower)
        elif lower is not None and upper is not None:
            # Middle bracket: P(lower <= temp < upper)
            prob = dist.cdf(upper) - dist.cdf(lower)
        else:
            prob = 0.0  # should never happen

        results.append(
            BracketProbability(
                bracket_label=bracket["label"],
                lower_bound_f=lower,
                upper_bound_f=upper,
                probability=max(0.0, min(1.0, prob)),  # clamp to [0, 1]
            )
        )

    # Normalize to ensure sum == 1.0 (handles floating point drift)
    total = sum(r.probability for r in results)
    if total > 0:
        for r in results:
            r.probability = r.probability / total

    logger.info(
        "Bracket probabilities calculated",
        extra={
            "data": {
                "ensemble_f": round(ensemble_forecast_f, 1),
                "error_std_f": round(error_std_f, 2),
                "bracket_count": len(results),
                "probabilities": [round(r.probability, 4) for r in results],
                "sum_check": round(sum(r.probability for r in results), 6),
            }
        },
    )

    return results
```

**Why normalization matters:** Even though the CDF should produce probabilities that sum to 1.0 across all brackets, floating point arithmetic can introduce tiny drift. The normalization step guarantees the invariant.

**Edge bracket handling:** Kalshi brackets always have 6 items. The lowest bracket has `lower_bound_f = None` (unbounded below), and the highest bracket has `upper_bound_f = None` (unbounded above). The four middle brackets have both bounds.

### Step 4: Confidence Assessment

Rate confidence based on multiple factors:
- **Model agreement:** If all sources are within 1 F -> HIGH confidence
- **Model agreement:** If spread > 3 F -> LOW confidence
- **Historical accuracy:** If this city/season has high forecast error -> lower confidence
- **Data freshness:** If forecasts are > 2 hours old -> lower confidence

#### Implementation: Add to `backend/prediction/ensemble.py`

```python
def assess_confidence(
    forecast_spread_f: float,
    error_std_f: float,
    num_sources: int,
    data_age_minutes: float,
) -> str:
    """Assess prediction confidence level.

    Uses a scoring system that weighs model agreement most heavily,
    followed by historical accuracy, data source count, and freshness.

    Args:
        forecast_spread_f: Max minus min temperature across all sources (F).
        error_std_f: Historical forecast error standard deviation (F).
        num_sources: Number of weather forecast sources that contributed.
        data_age_minutes: Age of the oldest forecast data in minutes.

    Returns:
        One of "HIGH", "MEDIUM", or "LOW".
    """
    score = 0

    # Model agreement (most important factor)
    if forecast_spread_f <= 1.0:
        score += 3  # very tight agreement
    elif forecast_spread_f <= 2.0:
        score += 2
    elif forecast_spread_f <= 3.0:
        score += 1
    # spread > 3 = no points

    # Historical accuracy
    if error_std_f <= 2.0:
        score += 2  # city/season with good forecast accuracy
    elif error_std_f <= 3.0:
        score += 1

    # Data sources available
    if num_sources >= 4:
        score += 1

    # Data freshness
    if data_age_minutes <= 60:
        score += 1
    elif data_age_minutes > 120:
        score -= 1  # penalty for stale data

    if score >= 5:
        return "HIGH"
    elif score >= 3:
        return "MEDIUM"
    else:
        return "LOW"
```

**Scoring breakdown (max 7 points):**

| Factor | HIGH | MEDIUM | LOW | None |
|---|---|---|---|---|
| Spread (max 3) | <= 1 F: 3 | <= 2 F: 2 | <= 3 F: 1 | > 3 F: 0 |
| Error std (max 2) | <= 2 F: 2 | <= 3 F: 1 | > 3 F: 0 | - |
| Sources (max 1) | >= 4: 1 | < 4: 0 | - | - |
| Freshness (max 1) | <= 60 min: 1 | 60-120 min: 0 | > 120 min: -1 | - |

- Score >= 5 -> HIGH
- Score >= 3 -> MEDIUM
- Score < 3 -> LOW

## Orchestration: Putting It All Together

The main prediction flow is called by the scheduler (Celery task) or the API. Here is the orchestration logic that ties Steps 1-4 together. This should live in `backend/prediction/__init__.py` or a dedicated `pipeline.py` file:

```python
from __future__ import annotations

from datetime import datetime, date
from backend.prediction.ensemble import calculate_ensemble_forecast, assess_confidence
from backend.prediction.brackets import calculate_bracket_probabilities
from backend.prediction.error_dist import calculate_error_std
from backend.common.schemas import BracketPrediction, WeatherData
from backend.common.logging import get_logger

logger = get_logger("MODEL")


async def generate_prediction(
    city: str,
    target_date: date,
    forecasts: list[WeatherData],
    kalshi_brackets: list[dict],
    db_session,
    model_weights: dict[str, float] | None = None,
) -> BracketPrediction:
    """Run the full prediction pipeline for one city and date.

    This is the main entry point for the prediction engine.

    Args:
        city: City code ("NYC", "CHI", "MIA", "AUS").
        target_date: The date we are predicting the high temperature for.
        forecasts: List of WeatherData from multiple sources.
        kalshi_brackets: Bracket definitions from Kalshi market data.
        db_session: SQLAlchemy async session (for historical error lookup).
        model_weights: Optional override for ensemble weights.

    Returns:
        A complete BracketPrediction ready for the trading engine.
    """
    # Step 1: Ensemble forecast
    ensemble_temp, spread, sources = calculate_ensemble_forecast(
        forecasts,
        weights=model_weights,
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

    # Step 4: Confidence assessment
    # Calculate data age from the oldest forecast timestamp
    now = datetime.utcnow()
    oldest_forecast = min(fc.fetched_at for fc in forecasts)
    data_age_minutes = (now - oldest_forecast).total_seconds() / 60.0

    confidence = assess_confidence(
        forecast_spread_f=spread,
        error_std_f=error_std,
        num_sources=len(sources),
        data_age_minutes=data_age_minutes,
    )

    prediction = BracketPrediction(
        city=city,
        date=target_date,
        brackets=bracket_probs,
        ensemble_forecast_f=round(ensemble_temp, 2),
        confidence=confidence,
        model_sources=sources,
        forecast_spread_f=round(spread, 2),
        error_std_f=round(error_std, 2),
        generated_at=now,
    )

    logger.info(
        "Prediction generated",
        extra={
            "data": {
                "city": city,
                "date": str(target_date),
                "ensemble_f": prediction.ensemble_forecast_f,
                "confidence": confidence,
                "spread_f": prediction.forecast_spread_f,
                "error_std_f": prediction.error_std_f,
                "sources": sources,
            }
        },
    )

    return prediction
```

## Output Schema

Your output MUST conform to `BracketPrediction` in `backend/common/schemas.py`:

```python
class BracketProbability(BaseModel):
    bracket_label: str  # e.g. "53-55" or "<51" or ">=59"
    lower_bound_f: float | None  # None for bottom edge bracket
    upper_bound_f: float | None  # None for top edge bracket
    probability: float  # 0.0 to 1.0


class BracketPrediction(BaseModel):
    city: str
    date: date
    brackets: list[BracketProbability]  # 6 items, probabilities sum to 1.0
    ensemble_forecast_f: float  # weighted ensemble temp
    confidence: Literal["HIGH", "MEDIUM", "LOW"]
    model_sources: list[str]  # which models contributed
    forecast_spread_f: float  # max - min across models
    error_std_f: float  # historical error std dev used
    generated_at: datetime
```

## Post-Mortem Data

After settlement (NWS CLI report comes in), generate post-mortem data:
- What we predicted vs. what happened
- Which model(s) were most accurate
- Was our probability distribution well-calibrated?
- Feed this back into calibration for future improvement

### Implementation: `backend/prediction/postmortem.py`

```python
from __future__ import annotations

from backend.common.schemas import TradeRecord, WeatherData
from backend.common.logging import get_logger

logger = get_logger("POSTMORTEM")


def generate_postmortem_narrative(trade: TradeRecord, settlement_temp_f: float) -> str:
    """Generate human-readable post-mortem narrative for a settled trade.

    This narrative is stored alongside the trade record and displayed
    in the dashboard for the user to understand why a trade won or lost.

    Args:
        trade: The completed trade record with all snapshot data.
        settlement_temp_f: Actual temperature from NWS CLI report.

    Returns:
        Multi-line narrative string explaining why the trade won/lost.
    """
    won = trade.status == "WON"

    # Find which model was closest to the actual temperature
    forecasts = trade.weather_forecasts or []
    closest_model = None
    closest_error = float("inf")
    for fc in forecasts:
        error = abs(fc.forecast_high_f - settlement_temp_f)
        if error < closest_error:
            closest_error = error
            closest_model = fc.source

    # Build narrative
    lines = []
    lines.append("WHAT WE TRADED")
    lines.append(
        f"  Bought {trade.side.upper()} on {trade.bracket_label} bracket "
        f"@ ${trade.entry_price:.2f} ({trade.quantity} contract)"
    )
    lines.append("")
    lines.append("WHAT HAPPENED")
    lines.append(f"  Actual high: {settlement_temp_f:.0f} F (NWS CLI Report)")
    lines.append(f"  Result: {'WIN' if won else 'LOSS'}")
    lines.append("")
    lines.append("WHY WE TOOK THIS TRADE")
    lines.append(f"  - Our model predicted {trade.model_probability:.0%} chance for this bracket")
    lines.append(
        f"  - Market was pricing it at {trade.market_probability:.0%} "
        f"(${trade.market_probability:.2f})"
    )
    edge = trade.model_probability - trade.market_probability
    lines.append(f"  - Edge: {edge:.0%} ({'+' if edge > 0 else ''}{edge:.1%})")
    lines.append(f"  - Confidence: {trade.confidence}")
    lines.append("")
    lines.append("MODEL ACCURACY")
    if closest_model:
        lines.append(f"  - Closest model: {closest_model} (off by {closest_error:.1f} F)")
    if forecasts:
        ensemble = trade.prediction.ensemble_forecast_f if trade.prediction else "N/A"
        lines.append(f"  - Ensemble forecast: {ensemble} F")
        lines.append(f"  - Actual: {settlement_temp_f:.0f} F")

    narrative = "\n".join(lines)

    logger.info(
        "Post-mortem generated",
        extra={
            "data": {
                "trade_id": trade.id,
                "city": trade.city,
                "result": "WIN" if won else "LOSS",
                "closest_model": closest_model,
                "closest_error_f": round(closest_error, 1) if closest_model else None,
            }
        },
    )

    return narrative
```

**Post-mortem is stored, not just logged.** The narrative string should be saved to the trade record in the database so the frontend dashboard can display it.

## Exceptions: `backend/prediction/exceptions.py`

```python
from __future__ import annotations


class PredictionError(Exception):
    """Base exception for prediction module errors."""

    pass


class InsufficientDataError(PredictionError):
    """Raised when there is not enough data to generate a prediction."""

    pass


class EnsembleError(PredictionError):
    """Raised when ensemble calculation fails (no sources, all weights zero)."""

    pass


class BracketError(PredictionError):
    """Raised when bracket probability calculation fails."""

    pass


class CalibrationError(PredictionError):
    """Raised when calibration process encounters an error."""

    pass
```

Use these throughout the module instead of bare `ValueError`/`RuntimeError`. The trading engine can catch `PredictionError` to handle any prediction failure gracefully.

## Market Plugin Architecture

The prediction engine is designed to be **market-agnostic**. The brackets come from whatever market provider is configured (Kalshi today, potentially others later). The prediction engine does not know or care about Kalshi-specific details.

The interface contract is simple:

```python
# What the prediction engine expects from the market provider:
brackets: list[dict] = [
    {"lower_bound_f": None, "upper_bound_f": 51.0, "label": "<51"},
    {"lower_bound_f": 51.0, "upper_bound_f": 53.0, "label": "51-53"},
    {"lower_bound_f": 53.0, "upper_bound_f": 55.0, "label": "53-55"},
    {"lower_bound_f": 55.0, "upper_bound_f": 57.0, "label": "55-57"},
    {"lower_bound_f": 57.0, "upper_bound_f": 59.0, "label": "57-59"},
    {"lower_bound_f": 59.0, "upper_bound_f": None, "label": ">=59"},
]
```

The Agent 2 (Kalshi) module is responsible for parsing Kalshi's market data into this format. The prediction engine just receives the brackets and calculates probabilities.

## Testing Requirements

Your tests go in `tests/prediction/`:
- `test_ensemble.py` -- weighted average calculation, handle missing sources, weight normalization
- `test_brackets.py` -- probability distribution sums to 100%, edge cases, all bracket types
- `test_error_dist.py` -- error distribution fitting, per-city/season variation, fallback behavior
- `test_calibration.py` -- calibration against known historical data
- `test_postmortem.py` -- post-mortem generation with correct data
- `test_confidence.py` -- confidence scoring across various scenarios

### Critical Test Cases

**Bracket probabilities:**
- Bracket probabilities MUST sum to 1.0 (within floating point tolerance of 1e-9)
- All probabilities must be >= 0 and <= 1
- Extreme temperatures (below 0 F, above 110 F) handled correctly
- Bracket boundaries match exactly what Kalshi provides (do not drift by 1 F)
- When forecast is exactly on a bracket boundary, probabilities are still correct
- Very small error_std_f (e.g., 0.1) should concentrate nearly all probability in one bracket
- Very large error_std_f (e.g., 20.0) should spread probability roughly evenly

**Ensemble:**
- If only 1 weather source available -> still produces valid output (100% weight)
- If 0 weather sources -> raises `ValueError`, does NOT produce garbage probabilities
- Unknown source names get default weight (0.05)
- All weights zero -> raises `ValueError`
- Weights do not need to sum to 1.0 (they are normalized internally)

**Error distribution:**
- Falls back to hardcoded values when DB has insufficient data
- Falls back gracefully on DB errors
- Returns a float > 0 in all cases
- Season detection is correct for all 12 months

**Confidence scoring:**
- Perfect conditions (tight spread, low std, 5 sources, fresh data) -> HIGH
- Worst conditions (wide spread, high std, 1 source, stale data) -> LOW
- Boundary cases around score thresholds

### Example Test: `tests/prediction/test_brackets.py`

```python
from __future__ import annotations

import pytest
from backend.prediction.brackets import calculate_bracket_probabilities

SAMPLE_BRACKETS = [
    {"lower_bound_f": None, "upper_bound_f": 51.0, "label": "<51"},
    {"lower_bound_f": 51.0, "upper_bound_f": 53.0, "label": "51-53"},
    {"lower_bound_f": 53.0, "upper_bound_f": 55.0, "label": "53-55"},
    {"lower_bound_f": 55.0, "upper_bound_f": 57.0, "label": "55-57"},
    {"lower_bound_f": 57.0, "upper_bound_f": 59.0, "label": "57-59"},
    {"lower_bound_f": 59.0, "upper_bound_f": None, "label": ">=59"},
]


def test_probabilities_sum_to_one():
    results = calculate_bracket_probabilities(
        ensemble_forecast_f=54.0,
        error_std_f=2.0,
        brackets=SAMPLE_BRACKETS,
    )
    total = sum(r.probability for r in results)
    assert abs(total - 1.0) < 1e-9


def test_six_brackets_returned():
    results = calculate_bracket_probabilities(
        ensemble_forecast_f=54.0,
        error_std_f=2.0,
        brackets=SAMPLE_BRACKETS,
    )
    assert len(results) == 6


def test_all_probabilities_non_negative():
    results = calculate_bracket_probabilities(
        ensemble_forecast_f=54.0,
        error_std_f=2.0,
        brackets=SAMPLE_BRACKETS,
    )
    for r in results:
        assert r.probability >= 0.0
        assert r.probability <= 1.0


def test_most_likely_bracket_contains_forecast():
    """The bracket containing the forecast should have the highest probability."""
    results = calculate_bracket_probabilities(
        ensemble_forecast_f=54.0,
        error_std_f=2.0,
        brackets=SAMPLE_BRACKETS,
    )
    # 54 F falls in the 53-55 bracket (index 2)
    probs = [r.probability for r in results]
    assert probs.index(max(probs)) == 2


def test_extreme_low_temperature():
    """Forecast far below all brackets -> most probability in bottom bracket."""
    results = calculate_bracket_probabilities(
        ensemble_forecast_f=30.0,
        error_std_f=2.0,
        brackets=SAMPLE_BRACKETS,
    )
    assert results[0].probability > 0.99


def test_extreme_high_temperature():
    """Forecast far above all brackets -> most probability in top bracket."""
    results = calculate_bracket_probabilities(
        ensemble_forecast_f=80.0,
        error_std_f=2.0,
        brackets=SAMPLE_BRACKETS,
    )
    assert results[5].probability > 0.99


def test_very_small_std_concentrates_probability():
    """Very small error std should put nearly all probability in one bracket."""
    results = calculate_bracket_probabilities(
        ensemble_forecast_f=54.0,
        error_std_f=0.1,
        brackets=SAMPLE_BRACKETS,
    )
    # 54 F is in bracket 2 (53-55)
    assert results[2].probability > 0.99


def test_large_std_spreads_probability():
    """Very large error std should spread probability more evenly."""
    results = calculate_bracket_probabilities(
        ensemble_forecast_f=54.0,
        error_std_f=20.0,
        brackets=SAMPLE_BRACKETS,
    )
    # No single bracket should dominate with a huge std dev
    for r in results:
        assert r.probability < 0.5


def test_error_std_zero_raises():
    """Zero error std should raise ValueError."""
    with pytest.raises(ValueError):
        calculate_bracket_probabilities(
            ensemble_forecast_f=54.0,
            error_std_f=0.0,
            brackets=SAMPLE_BRACKETS,
        )


def test_empty_brackets_raises():
    with pytest.raises(ValueError):
        calculate_bracket_probabilities(
            ensemble_forecast_f=54.0,
            error_std_f=2.0,
            brackets=[],
        )
```

### Example Test: `tests/prediction/test_ensemble.py`

```python
from __future__ import annotations

import pytest
from unittest.mock import MagicMock
from backend.prediction.ensemble import (
    calculate_ensemble_forecast,
    assess_confidence,
    DEFAULT_MODEL_WEIGHTS,
)


def _make_forecast(source: str, temp: float) -> MagicMock:
    """Helper to create a mock WeatherData object."""
    fc = MagicMock()
    fc.source = source
    fc.forecast_high_f = temp
    return fc


def test_ensemble_basic():
    forecasts = [
        _make_forecast("NWS", 55.0),
        _make_forecast("Open-Meteo:ECMWF", 53.0),
        _make_forecast("Open-Meteo:GFS", 54.0),
    ]
    temp, spread, sources = calculate_ensemble_forecast(forecasts)
    assert 53.0 < temp < 55.0
    assert spread == 2.0
    assert len(sources) == 3


def test_ensemble_single_source():
    forecasts = [_make_forecast("NWS", 55.0)]
    temp, spread, sources = calculate_ensemble_forecast(forecasts)
    assert temp == 55.0
    assert spread == 0.0
    assert sources == ["NWS"]


def test_ensemble_empty_raises():
    with pytest.raises(ValueError):
        calculate_ensemble_forecast([])


def test_ensemble_unknown_source_gets_default_weight():
    forecasts = [
        _make_forecast("NWS", 55.0),
        _make_forecast("SomeNewSource", 60.0),
    ]
    temp, spread, sources = calculate_ensemble_forecast(forecasts)
    # NWS weight 0.35, unknown weight 0.05
    # (55*0.35 + 60*0.05) / (0.35+0.05) = (19.25+3.0)/0.4 = 55.625
    assert abs(temp - 55.625) < 0.01


def test_confidence_high():
    result = assess_confidence(
        forecast_spread_f=0.5,  # tight: +3
        error_std_f=1.5,  # low: +2
        num_sources=5,  # many: +1
        data_age_minutes=30,  # fresh: +1
    )
    assert result == "HIGH"  # score = 7


def test_confidence_low():
    result = assess_confidence(
        forecast_spread_f=5.0,  # wide: +0
        error_std_f=4.0,  # high: +0
        num_sources=2,  # few: +0
        data_age_minutes=180,  # stale: -1
    )
    assert result == "LOW"  # score = -1


def test_confidence_medium():
    result = assess_confidence(
        forecast_spread_f=2.0,  # ok: +2
        error_std_f=2.5,  # ok: +1
        num_sources=3,  # few: +0
        data_age_minutes=90,  # ok: +0
    )
    assert result == "MEDIUM"  # score = 3
```

## Calibration: `backend/prediction/calibration.py` (Phase 26 — real implementation)

Computes Brier score and calibration buckets by joining Prediction.brackets_json with Settlement.actual_high_f.

- `check_calibration(city, db_session, lookback_days=90) -> CalibrationReport`
- Requires minimum 10 prediction/settlement pairs, otherwise returns "insufficient_data"
- `_temp_in_bracket(temp, lower, upper)` — helper for bracket matching
- Calibration buckets: 10 bins of 10% width (0-10%, 10-20%, ..., 90-100%)
- Brier score: `(1/N) * sum((predicted_prob - actual_outcome)^2)`. Lower is better. 0.0 = perfect.

## Accuracy: `backend/prediction/accuracy.py` (Phase 26)

Per-source forecast accuracy metrics by joining WeatherForecast with Settlement.

- `get_source_accuracy(city, db_session, lookback_days=90) -> list[SourceAccuracy]`
  - GROUP BY source → MAE, RMSE, bias
- `get_forecast_error_trend(city, source, db_session, lookback_days=90) -> ForecastErrorTrend`
  - Individual (date, error) points + 7-day rolling MAE

## Summary of Files to Create

| File | Status | Description |
|---|---|---|
| `__init__.py` | Create | Exports `generate_prediction` |
| `ensemble.py` | Create | `calculate_ensemble_forecast()` + `assess_confidence()` |
| `brackets.py` | Create | `calculate_bracket_probabilities()` |
| `error_dist.py` | Create | `calculate_error_std()` + `get_season()` + fallback constants |
| `calibration.py` | Create | `check_calibration()` stub for Phase 2 |
| `postmortem.py` | Create | `generate_postmortem_narrative()` |
| `exceptions.py` | Create | `PredictionError` hierarchy |
