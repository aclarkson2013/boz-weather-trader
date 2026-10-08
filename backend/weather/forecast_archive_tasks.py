"""Celery task for the as-issued forecast archive (algo v2, slice S3).

Hourly, budgeted, self-chaining while chunks remain (like the Kalshi archive).
Recent months are refreshed every ~6 hours. Gated by ``V2_ARCHIVE_ENABLED``.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime

import redis
from asgiref.sync import async_to_sync
from celery import shared_task

from backend.common.config import get_settings
from backend.common.database import get_task_session, reset_engine
from backend.common.logging import get_logger
from backend.weather.forecast_archive import (
    FORECAST_ARCHIVE_START,
    IEMMosClient,
    run_forecast_backfill,
)
from backend.weather.stations import VALID_CITIES

logger = get_logger("WEATHER")

BUDGET_SECONDS = 200.0  # Stop starting new chunks after this; worst chunk adds ~5.5 min
LOCK_KEY = "weather:forecast_archive:lock"
LOCK_TTL_SECONDS = 620
ERROR_RETRY_COUNTDOWN_SECONDS = 120
CHAIN_COUNTDOWN_SECONDS = 20


async def _run(budget_seconds: float) -> dict:
    reset_engine()
    client = IEMMosClient()
    try:
        return await run_forecast_backfill(
            client,
            get_task_session,
            cities=list(VALID_CITIES),
            start=FORECAST_ARCHIVE_START,
            end=datetime.now(UTC).date(),
            budget_seconds=budget_seconds,
        )
    finally:
        await client.close()


@shared_task(bind=True, soft_time_limit=540, time_limit=600)
def archive_forecasts(self) -> dict:
    """Archive pending GFS/NAM/NBM station forecast months within a time budget.

    Returns:
        Dict with run status and counts.
    """
    settings = get_settings()
    if not settings.v2_archive_enabled:
        return {"status": "disabled"}
    client = redis.Redis.from_url(settings.redis_url)
    lock = client.lock(LOCK_KEY, timeout=LOCK_TTL_SECONDS)
    if not lock.acquire(blocking=False):
        return {"status": "locked"}
    try:
        result = async_to_sync(_run)(BUDGET_SECONDS)
    except Exception as exc:
        logger.error(
            "Forecast archive run failed",
            extra={"data": {"error": f"{type(exc).__name__}: {exc}"[:300]}},
        )
        # Keep the chain alive: a crashed run must not stall the backfill until the
        # next hourly beat (the 2026-10-08 SoftTimeLimitExceeded stall).
        archive_forecasts.apply_async(countdown=ERROR_RETRY_COUNTDOWN_SECONDS)
        return {"status": "error", "error": str(exc)[:300]}
    finally:
        with contextlib.suppress(redis.exceptions.LockError):
            lock.release()
    if result["remaining"] > 0 and result["processed"] > 0:
        archive_forecasts.apply_async(countdown=CHAIN_COUNTDOWN_SECONDS)
    return {"status": "ok", **result}
