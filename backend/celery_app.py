"""Celery application configuration for background task processing.

Run worker: celery -A backend.celery_app worker --loglevel=info
Run beat:   celery -A backend.celery_app beat --loglevel=info
"""

from __future__ import annotations

import time as _time

from celery import Celery
from celery.schedules import crontab
from celery.signals import task_failure, task_postrun, task_prerun, task_retry

from backend.common.config import get_settings
from backend.common.metrics import CELERY_TASK_DURATION_SECONDS, CELERY_TASK_TOTAL

settings = get_settings()

celery_app = Celery(
    "boz_weather_trader",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="America/New_York",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    # Global task timeouts — override per-task where needed
    task_soft_time_limit=300,  # 5 min: raises SoftTimeLimitExceeded
    task_time_limit=360,  # 6 min: hard kill (SIGKILL)
    # Explicit task module imports — worker must know about all task modules.
    # Our tasks live in scheduler.py / train_models.py (not tasks.py),
    # so autodiscover_tasks() won't find them. List them explicitly.
    include=[
        "backend.weather.scheduler",
        "backend.trading.scheduler",
        "backend.prediction.scheduler",
        "backend.prediction.train_xgb",
        "backend.prediction.train_models",
        "backend.kalshi.tasks",
        "backend.common.maintenance",
    ],
)

# ─── Beat Schedule ───
# Tasks are defined in their respective agent modules.
# They're registered here so Celery Beat knows when to run them.

celery_app.conf.beat_schedule = {
    # Agent 1: Weather — fetch forecasts every 30 minutes
    "fetch-forecasts-every-30-min": {
        "task": "backend.weather.scheduler.fetch_all_forecasts",
        "schedule": crontab(minute="*/30"),
    },
    # Agent 1: Weather — fetch NWS CLI reports at 8 AM ET daily
    "fetch-cli-report-morning": {
        "task": "backend.weather.scheduler.fetch_cli_reports",
        "schedule": crontab(hour=8, minute=0),
    },
    # Agent 3: Prediction — generate predictions every 30 min (offset 5 min after weather)
    "generate-predictions": {
        "task": "backend.prediction.scheduler.generate_predictions",
        "schedule": crontab(minute="5,35"),
    },
    # Agent 4: Trading — main trading cycle every 15 minutes
    "trading-cycle": {
        "task": "backend.trading.scheduler.trading_cycle",
        "schedule": crontab(minute="*/15"),
    },
    # Agent 4: Trading — expire stale pending trades every 5 minutes
    "expire-pending-trades": {
        "task": "backend.trading.scheduler.check_pending_trades",
        "schedule": crontab(minute="*/5"),
    },
    # Agent 4: Trading — settle trades at 9 AM ET daily
    "settle-trades": {
        "task": "backend.trading.scheduler.settle_trades",
        "schedule": crontab(hour=9, minute=0),
    },
    # Agent 3: Prediction — retrain all ML models weekly (Sunday 3 AM ET)
    "retrain-ml-models": {
        "task": "backend.prediction.train_models.train_all_models",
        "schedule": crontab(hour=3, minute=0, day_of_week=0),
    },
    # ─── Algo v2: Kalshi market archive (S1) ───
    "kalshi-archive-backfill": {
        "task": "backend.kalshi.tasks.archive_backfill",
        "schedule": crontab(minute=20),  # Hourly; self-chains while work remains
    },
    "kalshi-record-quotes": {
        "task": "backend.kalshi.tasks.record_quotes_task",
        "schedule": crontab(minute="2-59/5"),  # Offset from trading-cycle minutes
    },
    # ─── Housekeeping ───
    "purge-old-logs": {
        "task": "backend.common.maintenance.purge_old_logs",
        "schedule": crontab(hour=4, minute=30),  # Daily 4:30 AM ET (quiet hours)
    },
}


# ─── Prometheus Metrics via Celery Signals ───
# These fire automatically for every task — no changes to task bodies required.

_task_start_times: dict[str, float] = {}


def _short_name(sender) -> str:  # noqa: ANN001
    """Extract the short task name (e.g. 'trading_cycle' from full dotted path)."""
    name = sender.name if sender else "unknown"
    return name.rsplit(".", 1)[-1] if name else "unknown"


@task_prerun.connect
def _on_task_prerun(sender=None, task_id=None, **kwargs) -> None:  # noqa: ANN001, ANN003
    """Record the start time when a Celery task begins."""
    if task_id:
        _task_start_times[task_id] = _time.monotonic()


@task_postrun.connect
def _on_task_postrun(sender=None, task_id=None, **kwargs) -> None:  # noqa: ANN001, ANN003
    """Record success and duration when a Celery task completes."""
    short = _short_name(sender)
    CELERY_TASK_TOTAL.labels(task_name=short, status="success").inc()

    start = _task_start_times.pop(task_id, None) if task_id else None
    if start is not None:
        CELERY_TASK_DURATION_SECONDS.labels(task_name=short).observe(_time.monotonic() - start)


@task_failure.connect
def _on_task_failure(sender=None, task_id=None, **kwargs) -> None:  # noqa: ANN001, ANN003
    """Record failure when a Celery task raises an exception."""
    CELERY_TASK_TOTAL.labels(task_name=_short_name(sender), status="failure").inc()
    if task_id:
        _task_start_times.pop(task_id, None)


@task_retry.connect
def _on_task_retry(sender=None, task_id=None, **kwargs) -> None:  # noqa: ANN001, ANN003
    """Record retry when a Celery task is retried."""
    CELERY_TASK_TOTAL.labels(task_name=_short_name(sender), status="retry").inc()
