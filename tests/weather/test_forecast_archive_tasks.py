"""Tests for the forecast archive Celery task and the forecast coverage endpoint."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from backend.weather import forecast_archive_tasks as tasks


def _settings(enabled: bool = True) -> MagicMock:
    s = MagicMock()
    s.v2_archive_enabled = enabled
    s.redis_url = "redis://localhost:6379/0"
    return s


class TestTask:
    def test_disabled(self) -> None:
        with patch.object(tasks, "get_settings", return_value=_settings(False)):
            assert tasks.archive_forecasts.run() == {"status": "disabled"}

    def test_locked(self) -> None:
        lock = MagicMock()
        lock.acquire.return_value = False
        rc = MagicMock()
        rc.lock.return_value = lock
        with (
            patch.object(tasks, "get_settings", return_value=_settings()),
            patch.object(tasks.redis.Redis, "from_url", return_value=rc),
        ):
            assert tasks.archive_forecasts.run() == {"status": "locked"}

    def test_requeues_and_releases(self) -> None:
        lock = MagicMock()
        lock.acquire.return_value = True
        rc = MagicMock()
        rc.lock.return_value = lock
        result = {"processed": 4, "remaining": 100, "statuses": {"complete": 4}}
        with (
            patch.object(tasks, "get_settings", return_value=_settings()),
            patch.object(tasks.redis.Redis, "from_url", return_value=rc),
            patch.object(tasks, "async_to_sync", return_value=lambda *_: result),
            patch.object(tasks.archive_forecasts, "apply_async") as requeue,
        ):
            assert tasks.archive_forecasts.run()["status"] == "ok"
        requeue.assert_called_once()
        lock.release.assert_called_once()

    def test_registered(self) -> None:
        from backend.celery_app import celery_app

        assert "backend.weather.forecast_archive_tasks" in celery_app.conf.include
        assert "backend.weather.forecast_archive_tasks.archive_forecasts" in {
            v["task"] for v in celery_app.conf.beat_schedule.values()
        }
