"""Tests for the algo-v2 archive Celery tasks: kill switch, lock, self-chaining."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from backend.kalshi import tasks


def _settings(enabled: bool = True) -> MagicMock:
    s = MagicMock()
    s.v2_archive_enabled = enabled
    s.redis_url = "redis://localhost:6379/0"
    return s


class TestArchiveBackfillTask:
    def test_disabled_flag_skips_everything(self) -> None:
        with (
            patch.object(tasks, "get_settings", return_value=_settings(False)),
            patch.object(tasks.redis.Redis, "from_url") as from_url,
        ):
            assert tasks.archive_backfill.run() == {"status": "disabled"}
            from_url.assert_not_called()

    def test_skips_when_lock_held(self) -> None:
        lock = MagicMock()
        lock.acquire.return_value = False
        redis_client = MagicMock()
        redis_client.lock.return_value = lock
        with (
            patch.object(tasks, "get_settings", return_value=_settings()),
            patch.object(tasks.redis.Redis, "from_url", return_value=redis_client),
            patch.object(tasks, "async_to_sync") as a2s,
        ):
            assert tasks.archive_backfill.run() == {"status": "locked"}
            a2s.assert_not_called()

    def test_requeues_while_work_remains_and_releases_lock(self) -> None:
        lock = MagicMock()
        lock.acquire.return_value = True
        redis_client = MagicMock()
        redis_client.lock.return_value = lock
        result = {"processed": 3, "remaining": 10, "statuses": {"complete": 3}}
        with (
            patch.object(tasks, "get_settings", return_value=_settings()),
            patch.object(tasks.redis.Redis, "from_url", return_value=redis_client),
            patch.object(tasks, "async_to_sync", return_value=lambda *_: result),
            patch.object(tasks.archive_backfill, "apply_async") as requeue,
        ):
            out = tasks.archive_backfill.run()
        assert out["status"] == "ok"
        assert out["remaining"] == 10
        requeue.assert_called_once()
        lock.release.assert_called_once()

    def test_does_not_requeue_when_done(self) -> None:
        lock = MagicMock()
        lock.acquire.return_value = True
        redis_client = MagicMock()
        redis_client.lock.return_value = lock
        result = {"processed": 2, "remaining": 0, "statuses": {"complete": 2}}
        with (
            patch.object(tasks, "get_settings", return_value=_settings()),
            patch.object(tasks.redis.Redis, "from_url", return_value=redis_client),
            patch.object(tasks, "async_to_sync", return_value=lambda *_: result),
            patch.object(tasks.archive_backfill, "apply_async") as requeue,
        ):
            tasks.archive_backfill.run()
        requeue.assert_not_called()

    def test_errors_are_contained_and_lock_released(self) -> None:
        lock = MagicMock()
        lock.acquire.return_value = True
        redis_client = MagicMock()
        redis_client.lock.return_value = lock

        def boom(*_: object) -> dict:
            raise RuntimeError("db down")

        with (
            patch.object(tasks, "get_settings", return_value=_settings()),
            patch.object(tasks.redis.Redis, "from_url", return_value=redis_client),
            patch.object(tasks, "async_to_sync", return_value=boom),
        ):
            out = tasks.archive_backfill.run()
        assert out["status"] == "error"
        lock.release.assert_called_once()


class TestRecordQuotesTask:
    def test_disabled_flag(self) -> None:
        with patch.object(tasks, "get_settings", return_value=_settings(False)):
            assert tasks.record_quotes_task.run() == {"status": "disabled"}

    def test_failure_is_contained(self) -> None:
        def boom() -> int:
            raise RuntimeError("kalshi down")

        with (
            patch.object(tasks, "get_settings", return_value=_settings()),
            patch.object(tasks, "async_to_sync", return_value=boom),
        ):
            assert tasks.record_quotes_task.run()["status"] == "error"

    def test_tasks_registered_in_celery_include_and_beat(self) -> None:
        from backend.celery_app import celery_app

        assert "backend.kalshi.tasks" in celery_app.conf.include
        beat_tasks = {v["task"] for v in celery_app.conf.beat_schedule.values()}
        assert "backend.kalshi.tasks.archive_backfill" in beat_tasks
        assert "backend.kalshi.tasks.record_quotes_task" in beat_tasks
