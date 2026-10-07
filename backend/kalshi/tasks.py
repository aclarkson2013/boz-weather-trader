"""Celery tasks for the algo-v2 Kalshi market archive (slice S1).

- ``archive_backfill``: archives settled city-days (markets, outcomes, hourly
  candles) in ~2-minute chunks, newest first, re-queueing itself while work
  remains. A Redis lock prevents overlapping runs (beat + self-chain).
- ``record_quotes``: snapshots top-of-book for today's and tomorrow's events in
  all 4 cities every 5 minutes, for later fill-realism checks.

Both are gated by ``V2_ARCHIVE_ENABLED`` (default on — read-only public data).
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta

import redis
from asgiref.sync import async_to_sync
from celery import shared_task

from backend.common.config import get_settings
from backend.common.database import get_task_session, reset_engine
from backend.common.logging import get_logger
from backend.kalshi.archive import ARCHIVE_START, record_quotes, run_backfill
from backend.kalshi.public_client import KalshiPublicClient
from backend.weather.stations import VALID_CITIES, get_standard_time_now

logger = get_logger("MARKET")

BACKFILL_BUDGET_SECONDS = 120.0
BACKFILL_LOCK_KEY = "kalshi:archive:backfill:lock"
BACKFILL_LOCK_TTL_SECONDS = 280
CHAIN_COUNTDOWN_SECONDS = 15
SETTLEMENT_LAG_DAYS = 2  # Only archive events settled for sure (D+2 in UTC)


async def _archive_backfill_async(budget_seconds: float) -> dict:
    """Run one budgeted backfill pass against the public Kalshi API."""
    reset_engine()
    end = datetime.now(UTC).date() - timedelta(days=SETTLEMENT_LAG_DAYS)
    async with KalshiPublicClient() as client:
        return await run_backfill(
            client,
            get_task_session,
            start=ARCHIVE_START,
            end=end,
            cities=list(VALID_CITIES),
            budget_seconds=budget_seconds,
        )


async def _record_quotes_async() -> int:
    """Snapshot quotes for each city's LST today + tomorrow events."""
    reset_engine()
    events = []
    for city in VALID_CITIES:
        today = get_standard_time_now(city).date()
        events.extend([(city, today), (city, today + timedelta(days=1))])
    session = await get_task_session()
    try:
        async with KalshiPublicClient() as client:
            written = await record_quotes(client, session, events)
        await session.commit()
        return written
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


@shared_task(bind=True, soft_time_limit=240, time_limit=300)
def archive_backfill(self) -> dict:
    """Archive pending settled city-days within a ~2 minute budget.

    Re-queues itself while days remain, so a cold backfill finishes in hours
    instead of waiting for the next beat tick. Skips if another run holds the lock.

    Returns:
        Dict with run status and counts.
    """
    settings = get_settings()
    if not settings.v2_archive_enabled:
        return {"status": "disabled"}

    client = redis.Redis.from_url(settings.redis_url)
    lock = client.lock(BACKFILL_LOCK_KEY, timeout=BACKFILL_LOCK_TTL_SECONDS)
    if not lock.acquire(blocking=False):
        logger.info("Archive backfill already running — skipping", extra={"data": {}})
        return {"status": "locked"}

    try:
        result = async_to_sync(_archive_backfill_async)(BACKFILL_BUDGET_SECONDS)
    except Exception as exc:
        logger.error(
            "Archive backfill run failed",
            extra={"data": {"error": f"{type(exc).__name__}: {exc}"[:300]}},
        )
        return {"status": "error", "error": str(exc)[:300]}
    finally:
        with contextlib.suppress(redis.exceptions.LockError):
            lock.release()

    if result["remaining"] > 0 and result["processed"] > 0:
        archive_backfill.apply_async(countdown=CHAIN_COUNTDOWN_SECONDS)
    return {"status": "ok", **result}


@shared_task(bind=True, soft_time_limit=120, time_limit=150)
def record_quotes_task(self) -> dict:
    """Record top-of-book quotes for open weather events in all cities.

    Returns:
        Dict with run status and rows written.
    """
    settings = get_settings()
    if not settings.v2_archive_enabled:
        return {"status": "disabled"}
    try:
        written = async_to_sync(_record_quotes_async)()
    except Exception as exc:
        logger.warning(
            "Quote recording failed",
            extra={"data": {"error": f"{type(exc).__name__}: {exc}"[:300]}},
        )
        return {"status": "error", "error": str(exc)[:300]}
    return {"status": "ok", "written": written}
