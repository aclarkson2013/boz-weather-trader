"""Tests for log retention (backend/common/maintenance.py)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.common import maintenance
from backend.common.maintenance import purge_old_log_entries
from backend.common.models import Base, LogEntry, Trade

NOW = datetime(2026, 10, 7, 12, 0, 0)


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator:
    """Fresh in-memory DB per test; factory returns new sessions (commits allowed)."""
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    async def factory() -> AsyncSession:
        return maker()

    yield factory
    await engine.dispose()


async def _seed(factory, ages_days: list[float]) -> None:
    session = await factory()
    session.add_all(
        [
            LogEntry(
                timestamp=NOW - timedelta(days=age),
                level="INFO",
                module_tag="SYSTEM",
                message=f"age {age}",
            )
            for age in ages_days
        ]
    )
    await session.commit()
    await session.close()


async def _remaining_ages(factory) -> list[str]:
    session = await factory()
    rows = (await session.execute(select(LogEntry.message).order_by(LogEntry.id))).scalars().all()
    await session.close()
    return list(rows)


class TestPurgeOldLogEntries:
    async def test_deletes_only_entries_older_than_retention(self, session_factory) -> None:
        """Given logs aged 1–200 days and 90-day retention, only those > 90 days old go."""
        await _seed(session_factory, [200, 120, 91, 89, 30, 1])
        result = await purge_old_log_entries(session_factory, 90, now=NOW)
        assert result["deleted"] == 3
        assert result["more_remaining"] is False
        assert await _remaining_ages(session_factory) == ["age 89", "age 30", "age 1"]

    async def test_batches_until_backlog_cleared(self, session_factory) -> None:
        await _seed(session_factory, [100] * 25 + [5] * 3)
        result = await purge_old_log_entries(session_factory, 90, now=NOW, batch_size=10)
        assert result["deleted"] == 25
        assert len(await _remaining_ages(session_factory)) == 3

    async def test_stops_on_budget_and_reports_more_remaining(self, session_factory) -> None:
        await _seed(session_factory, [100] * 25)
        ticks = iter([0.0, 0.0, 999.0])  # t0, first batch allowed, then over budget
        result = await purge_old_log_entries(
            session_factory,
            90,
            now=NOW,
            batch_size=10,
            budget_seconds=60,
            clock=lambda: next(ticks),
        )
        assert result["deleted"] == 10
        assert result["more_remaining"] is True
        assert len(await _remaining_ages(session_factory)) == 15

    async def test_zero_retention_disables_purge(self, session_factory) -> None:
        await _seed(session_factory, [400, 1])
        result = await purge_old_log_entries(session_factory, 0, now=NOW)
        assert result == {"deleted": 0, "cutoff": None, "more_remaining": False}
        assert len(await _remaining_ages(session_factory)) == 2

    async def test_never_touches_non_log_tables(self, session_factory) -> None:
        """Only log_entries is purged — trade history is untouched."""
        await _seed(session_factory, [300])
        session = await session_factory()
        before = (await session.execute(select(func.count()).select_from(Trade))).scalar_one()
        await session.close()
        await purge_old_log_entries(session_factory, 90, now=NOW)
        session = await session_factory()
        after = (await session.execute(select(func.count()).select_from(Trade))).scalar_one()
        await session.close()
        assert before == after


class TestPurgeTask:
    def _settings(self, days: int) -> MagicMock:
        s = MagicMock()
        s.log_retention_days = days
        return s

    def test_disabled_when_retention_not_positive(self) -> None:
        with patch.object(maintenance, "get_settings", return_value=self._settings(0)):
            assert maintenance.purge_old_logs.run() == {"status": "disabled"}

    def test_requeues_while_backlog_remains(self) -> None:
        result = {"deleted": 20000, "cutoff": "x", "more_remaining": True}
        with (
            patch.object(maintenance, "get_settings", return_value=self._settings(90)),
            patch.object(maintenance, "async_to_sync", return_value=lambda *_: result),
            patch.object(maintenance.purge_old_logs, "apply_async") as requeue,
        ):
            out = maintenance.purge_old_logs.run()
        assert out["status"] == "ok"
        requeue.assert_called_once()

    def test_no_requeue_when_done_and_errors_contained(self) -> None:
        done = {"deleted": 5, "cutoff": "x", "more_remaining": False}
        with (
            patch.object(maintenance, "get_settings", return_value=self._settings(90)),
            patch.object(maintenance, "async_to_sync", return_value=lambda *_: done),
            patch.object(maintenance.purge_old_logs, "apply_async") as requeue,
        ):
            maintenance.purge_old_logs.run()
        requeue.assert_not_called()

        def boom(*_: object) -> dict:
            raise RuntimeError("db down")

        with (
            patch.object(maintenance, "get_settings", return_value=self._settings(90)),
            patch.object(maintenance, "async_to_sync", return_value=boom),
        ):
            assert maintenance.purge_old_logs.run()["status"] == "error"

    def test_registered_in_celery_include_and_beat(self) -> None:
        from backend.celery_app import celery_app

        assert "backend.common.maintenance" in celery_app.conf.include
        tasks = {v["task"] for v in celery_app.conf.beat_schedule.values()}
        assert "backend.common.maintenance.purge_old_logs" in tasks

    def test_default_retention_is_90_days(self) -> None:
        from backend.common.config import Settings

        assert Settings.model_fields["log_retention_days"].default == 90
