"""Celery task for generating bracket predictions from weather data.

Bridges the gap between Agent 1 (Weather) and Agent 4 (Trading) by running
the prediction pipeline for each city/date combination and storing the results
in the predictions table.

Task schedule:
    - generate_predictions: Every 30 minutes, offset 5 min after weather fetch

Usage:
    # Manual trigger from Celery worker:
    from backend.prediction.scheduler import generate_predictions
    generate_predictions.delay()
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from asgiref.sync import async_to_sync
from celery import shared_task
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.common.database import get_task_session, reset_engine
from backend.common.logging import get_logger
from backend.common.models import CityEnum, Prediction, User, WeatherForecast
from backend.common.schemas import (
    WeatherData,
    WeatherVariables,
    parse_enabled_weather_sources,
)
from backend.kalshi.markets import build_event_ticker, parse_event_markets
from backend.prediction.pipeline import generate_prediction
from backend.websocket.events import publish_event_sync

logger = get_logger("PREDICTION")

ET = ZoneInfo("America/New_York")


# ─── ORM → Schema Converter ───


def _forecast_orm_to_schema(orm: WeatherForecast) -> WeatherData:
    """Convert a WeatherForecast ORM model to a WeatherData Pydantic schema.

    Args:
        orm: The WeatherForecast ORM instance from the database.

    Returns:
        A WeatherData schema object for use by the prediction pipeline.
    """
    city = orm.city.value if hasattr(orm.city, "value") else orm.city

    forecast_date = orm.forecast_date
    if isinstance(forecast_date, datetime):
        forecast_date = forecast_date.date()

    fetched_at = orm.fetched_at or datetime.now(UTC).replace(tzinfo=None)

    return WeatherData(
        city=city,
        date=forecast_date,
        forecast_high_f=orm.forecast_high_f,
        source=orm.source,
        model_run_timestamp=fetched_at,
        variables=WeatherVariables(
            temp_high_f=orm.forecast_high_f,
            temp_low_f=orm.forecast_low_f,
            humidity_pct=orm.humidity_pct,
            wind_speed_mph=orm.wind_speed_mph,
            cloud_cover_pct=orm.cloud_cover_pct,
        ),
        raw_data=orm.raw_data or {},
        fetched_at=fetched_at,
    )


# ─── Fallback Bracket Generation ───


def _generate_fallback_brackets(ensemble_mean_f: float) -> list[dict]:
    """Generate synthetic bracket definitions when Kalshi API is unavailable.

    Creates 6 brackets centered around the ensemble forecast, mimicking
    typical Kalshi weather market structure (2°F wide middle brackets).

    Args:
        ensemble_mean_f: The ensemble average forecast temperature in °F.

    Returns:
        List of 6 bracket dicts with lower_bound_f, upper_bound_f, and label.
    """
    # Center the brackets around the forecast, rounded to nearest even integer
    center = round(ensemble_mean_f / 2) * 2
    low = center - 4  # Start 4°F below center

    return [
        {
            "label": f"{low - 1}°F or below",
            "lower_bound_f": None,
            "upper_bound_f": float(low),
        },
        {
            "label": f"{low}° to {low + 1}°F",
            "lower_bound_f": float(low),
            "upper_bound_f": float(low + 2),
        },
        {
            "label": f"{low + 2}° to {low + 3}°F",
            "lower_bound_f": float(low + 2),
            "upper_bound_f": float(low + 4),
        },
        {
            "label": f"{low + 4}° to {low + 5}°F",
            "lower_bound_f": float(low + 4),
            "upper_bound_f": float(low + 6),
        },
        {
            "label": f"{low + 6}° to {low + 7}°F",
            "lower_bound_f": float(low + 6),
            "upper_bound_f": float(low + 8),
        },
        {
            "label": f"{low + 8}°F or above",
            "lower_bound_f": float(low + 8),
            "upper_bound_f": None,
        },
    ]


# ─── Kalshi Bracket Fetching ───


async def _get_kalshi_brackets(
    city: str,
    target_date: date,
) -> list[dict] | None:
    """Try to fetch bracket definitions from the Kalshi API.

    Creates a temporary KalshiClient from the stored user credentials,
    fetches the event markets for the given city/date, and extracts
    bracket definitions.

    Args:
        city: City code (NYC, CHI, MIA, AUS).
        target_date: The date of the weather event.

    Returns:
        List of bracket dicts from Kalshi, or None if unavailable.
    """
    session = await get_task_session()
    try:
        result = await session.execute(select(User).limit(1))
        user = result.scalar_one_or_none()
        if user is None:
            return None

        from backend.common.encryption import decrypt_api_key
        from backend.kalshi.client import KalshiClient

        private_key_pem = decrypt_api_key(user.encrypted_private_key)
        demo = user.demo_mode if user.demo_mode is not None else True
        client = KalshiClient(
            api_key_id=user.kalshi_key_id,
            private_key_pem=private_key_pem,
            demo=demo,
        )
        try:
            event_ticker = build_event_ticker(city, target_date)
            markets = await client.get_event_markets(event_ticker)
            if not markets:
                return None
            brackets = parse_event_markets(markets)
            logger.info(
                "Fetched Kalshi brackets",
                extra={
                    "data": {
                        "city": city,
                        "date": str(target_date),
                        "bracket_count": len(brackets),
                    }
                },
            )
            return brackets
        finally:
            await client.close()
    except Exception as exc:
        logger.debug(
            "Could not fetch Kalshi brackets, will use fallback",
            extra={"data": {"city": city, "date": str(target_date), "error": str(exc)}},
        )
        return None
    finally:
        await session.close()


# ─── Prediction Storage ───


async def _store_prediction(
    session: AsyncSession,
    city: str,
    target_date: date,
    prediction: object,
) -> None:
    """Store a BracketPrediction as a Prediction ORM record.

    Args:
        session: Async database session.
        city: City code.
        target_date: The prediction date.
        prediction: The BracketPrediction schema object.
    """
    generated_at = prediction.generated_at
    if hasattr(generated_at, "tzinfo") and generated_at.tzinfo is not None:
        generated_at = generated_at.replace(tzinfo=None)

    pred_orm = Prediction(
        city=CityEnum(city),
        prediction_date=datetime.combine(target_date, datetime.min.time()),
        ensemble_mean_f=prediction.ensemble_mean_f,
        ensemble_std_f=prediction.ensemble_std_f,
        confidence=prediction.confidence,
        model_sources=",".join(prediction.model_sources),
        brackets_json=[b.model_dump() for b in prediction.brackets],
        generated_at=generated_at,
    )
    session.add(pred_orm)


def _dedupe_and_filter_sources(
    forecast_orms: Sequence[WeatherForecast],
    enabled_sources: set[str],
) -> tuple[list[WeatherForecast], list[str]]:
    """Keep the newest forecast per source, dropping disabled sources.

    ``forecast_orms`` must already be ordered newest-first, so the first row
    seen for a source is the one kept.

    Args:
        forecast_orms: Forecast rows for one city/date, newest first.
        enabled_sources: Source names allowed into the ensemble.

    Returns:
        A ``(kept, excluded)`` tuple — the forecasts to blend, and the names
        of the distinct sources that were dropped because they are disabled.
    """
    seen: set[str] = set()
    kept: list[WeatherForecast] = []
    excluded: list[str] = []
    for orm in forecast_orms:
        if orm.source in seen:
            continue
        seen.add(orm.source)
        if orm.source not in enabled_sources:
            excluded.append(orm.source)
            continue
        kept.append(orm)
    return kept, excluded


# ─── Async Orchestration ───


async def _generate_predictions_async() -> dict:
    """Generate predictions for all cities using latest weather data.

    For each city and target date (today + tomorrow in ET):
    1. Load latest weather forecasts from the database
    2. Deduplicate by source (keep latest per source)
    3. Drop sources the user has disabled (UserSettings.enabled_weather_sources)
    4. Fetch Kalshi bracket definitions (API or fallback)
    5. Run the prediction pipeline
    6. Store the result as a Prediction ORM record

    Disabled sources are still fetched and stored by the weather pipeline —
    they are only excluded here, so /api/accuracy/sources keeps scoring them
    and re-enabling one is backed by continuous history.

    Returns:
        Dict with counts: generated, skipped, errors.
    """
    # Reset the async engine so it is recreated in THIS event loop.
    # async_to_sync creates a fresh loop per Celery task invocation; the
    # singleton engine from a previous loop causes "Future attached to a
    # different loop" errors.
    reset_engine()

    session = await get_task_session()
    generated = 0
    skipped = 0
    errors = 0

    try:
        # Target dates: today and tomorrow (ET timezone)
        now_et = datetime.now(ET)
        today = now_et.date()
        tomorrow = today + timedelta(days=1)
        target_dates = [today, tomorrow]

        cities = ["NYC", "CHI", "MIA", "AUS"]

        # Which forecast feeds are allowed into the ensemble. Falls back to the
        # default set when there is no user row yet (first boot / onboarding).
        result = await session.execute(select(User).limit(1))
        settings_user = result.scalar_one_or_none()
        enabled_sources = set(
            parse_enabled_weather_sources(
                settings_user.enabled_weather_sources if settings_user else None
            )
        )
        logger.info(
            "Enabled weather sources for this cycle",
            extra={"data": {"sources": sorted(enabled_sources)}},
        )

        for city in cities:
            for target_date in target_dates:
                try:
                    # Load latest forecasts for this city/date from DB
                    result = await session.execute(
                        select(WeatherForecast)
                        .where(
                            WeatherForecast.city == CityEnum(city),
                            WeatherForecast.forecast_date
                            == datetime.combine(target_date, datetime.min.time()),
                        )
                        .order_by(WeatherForecast.fetched_at.desc())
                    )
                    forecast_orms = result.scalars().all()

                    if not forecast_orms:
                        logger.debug(
                            "No forecasts for city/date, skipping",
                            extra={"data": {"city": city, "date": str(target_date)}},
                        )
                        skipped += 1
                        continue

                    # Deduplicate: keep latest forecast per source, and drop
                    # sources the user has disabled.
                    unique_forecasts, excluded = _dedupe_and_filter_sources(
                        forecast_orms, enabled_sources
                    )

                    # Convert ORM → schema
                    forecasts = [_forecast_orm_to_schema(f) for f in unique_forecasts]

                    if len(forecasts) < 2:
                        logger.debug(
                            "Insufficient forecast sources, skipping",
                            extra={
                                "data": {
                                    "city": city,
                                    "date": str(target_date),
                                    "sources": len(forecasts),
                                    "excluded_disabled": excluded,
                                }
                            },
                        )
                        skipped += 1
                        continue

                    # Get bracket definitions (Kalshi API or fallback)
                    brackets = await _get_kalshi_brackets(city, target_date)
                    if brackets is None:
                        # Generate fallback brackets from simple average
                        mean_temp = sum(f.forecast_high_f for f in forecasts) / len(forecasts)
                        brackets = _generate_fallback_brackets(mean_temp)
                        logger.info(
                            "Using fallback brackets",
                            extra={
                                "data": {
                                    "city": city,
                                    "date": str(target_date),
                                    "mean_temp": round(mean_temp, 1),
                                }
                            },
                        )

                    # Run prediction pipeline
                    prediction = await generate_prediction(
                        city=city,
                        target_date=target_date,
                        forecasts=forecasts,
                        kalshi_brackets=brackets,
                        db_session=session,
                    )

                    # Store as Prediction ORM record
                    await _store_prediction(session, city, target_date, prediction)
                    generated += 1

                    logger.info(
                        "Stored prediction",
                        extra={
                            "data": {
                                "city": city,
                                "date": str(target_date),
                                "mean_f": prediction.ensemble_mean_f,
                                "confidence": prediction.confidence,
                                "sources": prediction.model_sources,
                                "brackets": len(prediction.brackets),
                            }
                        },
                    )

                except Exception as exc:
                    errors += 1
                    logger.error(
                        "Prediction generation failed for city/date",
                        extra={
                            "data": {
                                "city": city,
                                "date": str(target_date),
                                "error": str(exc),
                            }
                        },
                        exc_info=True,
                    )

        await session.commit()

    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()

    return {
        "generated": generated,
        "skipped": skipped,
        "errors": errors,
    }


# ─── Celery Task ───


@shared_task(
    bind=True,
    max_retries=2,
    default_retry_delay=60,
    soft_time_limit=240,
    time_limit=300,
)
def generate_predictions(self) -> dict:
    """Generate bracket predictions for all cities using latest weather data.

    Runs every 30 minutes (offset 5 min after weather fetch). For each
    city/date combination:
    1. Loads weather forecasts from DB
    2. Fetches Kalshi bracket definitions (or uses fallback)
    3. Runs the prediction pipeline (ensemble + error dist + bracket CDF)
    4. Stores results in the predictions table

    Returns:
        Dict with task execution metadata.
    """
    start_time = datetime.now(UTC)

    logger.info("Starting prediction generation cycle", extra={"data": {}})

    try:
        result = async_to_sync(_generate_predictions_async)()
    except Exception as exc:
        logger.error(
            "Prediction generation cycle failed, retrying",
            extra={"data": {"error": str(exc)}},
        )
        raise self.retry(exc=exc) from exc

    elapsed = (datetime.now(UTC) - start_time).total_seconds()

    if result["generated"] > 0:
        publish_event_sync(
            "prediction.updated",
            {
                "generated": result["generated"],
                "cities": ["NYC", "CHI", "MIA", "AUS"],
            },
        )

    logger.info(
        "Prediction generation cycle completed",
        extra={"data": {**result, "elapsed_seconds": round(elapsed, 1)}},
    )

    return {
        "status": "completed",
        **result,
        "elapsed_seconds": round(elapsed, 1),
    }
