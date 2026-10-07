"""Housekeeping tasks — log retention for the ``log_entries`` table.

Every INFO+ log line is persisted to ``log_entries`` (the dashboard Logs page and
``GET /api/logs``). Without retention the table grows forever (4.3M rows / ~1 GB
by 2026-10). ``purge_old_logs`` runs nightly and deletes entries older than
``LOG_RETENTION_DAYS`` (default 90; 0 or less disables purging).

Deletes run in small batches with a time budget so the live bot's database is
never locked for long; if a run hits its budget (e.g. the first, large purge)
the task re-queues itself until the backlog is gone.

Trades, predictions, forecasts, settlements and the market archive are never
touched — only log lines.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from asgiref.sync import async_to_sync
from celery import shared_task
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.common.config import get_settings
from backend.common.database import get_task_session, reset_engine
from backend.common.logging import get_logger
from backend.common.metrics import LOG_ENTRIES_PURGED_TOTAL
from backend.common.models import LogEntry

logger = get_logger("SYSTEM")

PURGE_BATCH_SIZE = 20_000
PURGE_BUDGET_SECONDS = 200.0
PURGE_CHAIN_COUNTDOWN_SECONDS = 30


async def purge_old_log_entries(
    session_factory: Callable[[], Awaitable[AsyncSession]],
    retention_days: int,
    *,
    now: datetime | None = None,
    batch_size: int = PURGE_BATCH_SIZE,
    budget_seconds: float = PURGE_BUDGET_SECONDS,
    clock: Callable[[], float] = time.monotonic,
) -> dict:
    """Delete log entries older than ``retention_days``, in committed batches.

    Args:
        session_factory: Async callable returning a new DB session.
        retention_days: Keep entries newer than this many days; <= 0 disables.
        now: Reference time (naive UTC); defaults to the current time.
        batch_size: Rows deleted per batch (each batch is its own transaction).
        budget_seconds: Stop starting new batches after this many seconds.
        clock: Monotonic clock (injectable for tests).

    Returns:
        Dict with ``deleted`` count, ``cutoff`` (ISO) and ``more_remaining`` flag.
    """
    if retention_days <= 0:
        return {"deleted": 0, "cutoff": None, "more_remaining": False}

    reference = now or datetime.now(UTC).replace(tzinfo=None)
    cutoff = reference - timedelta(days=retention_days)
    t0 = clock()
    deleted = 0
    more_remaining = False

    session = await session_factory()
    try:
        while True:
            if clock() - t0 >= budget_seconds:
                more_remaining = True
                break
            oldest_batch = (
                select(LogEntry.id)
                .where(LogEntry.timestamp < cutoff)
                .order_by(LogEntry.id)
                .limit(batch_size)
            )
            result = await session.execute(delete(LogEntry).where(LogEntry.id.in_(oldest_batch)))
            await session.commit()
            n = result.rowcount or 0
            deleted += n
            if n < batch_size:
                break
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()

    if deleted:
        LOG_ENTRIES_PURGED_TOTAL.inc(deleted)
    logger.info(
        "Old log entries purged",
        extra={
            "data": {
                "deleted": deleted,
                "retention_days": retention_days,
                "cutoff": cutoff.isoformat(),
                "more_remaining": more_remaining,
            }
        },
    )
    return {"deleted": deleted, "cutoff": cutoff.isoformat(), "more_remaining": more_remaining}


async def _purge_old_logs_async(retention_days: int) -> dict:
    """Run one budgeted purge pass with a fresh engine (Celery child process)."""
    reset_engine()
    return await purge_old_log_entries(get_task_session, retention_days)


@shared_task(bind=True, soft_time_limit=240, time_limit=300)
def purge_old_logs(self) -> dict:
    """Nightly: delete ``log_entries`` rows older than LOG_RETENTION_DAYS.

    Re-queues itself while a large backlog remains (budgeted batches).

    Returns:
        Dict with run status and counts.
    """
    retention_days = get_settings().log_retention_days
    if retention_days <= 0:
        return {"status": "disabled"}
    try:
        result = async_to_sync(_purge_old_logs_async)(retention_days)
    except Exception as exc:
        logger.error(
            "Log purge failed",
            extra={"data": {"error": f"{type(exc).__name__}: {exc}"[:300]}},
        )
        return {"status": "error", "error": str(exc)[:300]}
    if result["more_remaining"]:
        purge_old_logs.apply_async(countdown=PURGE_CHAIN_COUNTDOWN_SECONDS)
    return {"status": "ok", **result}
