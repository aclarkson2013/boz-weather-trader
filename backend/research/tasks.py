"""Celery task that executes a queued research report (real-price backtest)."""

from __future__ import annotations

from asgiref.sync import async_to_sync
from celery import shared_task

from backend.common.database import get_task_session, reset_engine
from backend.common.logging import get_logger
from backend.research.reports import run_report_by_id

logger = get_logger("TRADING")


async def _run(report_id: int) -> dict:
    reset_engine()
    return await run_report_by_id(get_task_session, report_id)


@shared_task(bind=True, soft_time_limit=1200, time_limit=1260)
def run_backtest_report(self, report_id: int) -> dict:
    """Run one queued research report (a full backtest can take several minutes).

    Args:
        report_id: ``research_reports.id`` of a queued report.

    Returns:
        Dict with report id, final status and gate verdict.
    """
    result = async_to_sync(_run)(report_id)
    logger.info("Research report task finished", extra={"data": result})
    return result
