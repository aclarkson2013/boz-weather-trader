"""Tests for research report creation rules and end-to-end execution (S2)."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from backend.common.models import ResearchReport
from backend.research.gate import DEV_WINDOW
from backend.research.reports import ReportError, create_report, run_report_by_id
from tests.research.conftest import seed_event

TODAY = date(2026, 10, 8)


class TestCreateReport:
    async def test_unknown_strategy_rejected(self, session_factory) -> None:
        session = await session_factory()
        with pytest.raises(ReportError, match="not pre-registered"):
            await create_report(session, "X9", today=TODAY)
        await session.close()

    async def test_counted_variant_uses_dev_window(self, session_factory) -> None:
        session = await session_factory()
        r = await create_report(session, "L1", today=TODAY)
        await session.close()
        assert (r.window_start, r.window_end) == DEV_WINDOW
        assert r.kind == "backtest"
        assert r.status == "queued"
        assert r.k == 8
        assert r.params == {"max_yes_bid": 2, "decision": "D1E"}

    async def test_controls_kind_and_c2_window(self, session_factory) -> None:
        session = await session_factory()
        c0 = await create_report(session, "C0", today=TODAY)
        c2 = await create_report(session, "C2", today=TODAY)
        await session.close()
        assert c0.kind == "control"
        assert (c2.window_start, c2.window_end) == (date(2026, 2, 20), date(2026, 8, 28))

    async def test_holdout_requires_passing_dev_verdict(self, session_factory) -> None:
        session = await session_factory()
        with pytest.raises(ReportError, match="no PASSING"):
            await create_report(session, "L1", holdout=True, today=TODAY)
        await session.close()

    async def test_holdout_used_only_once(self, session_factory) -> None:
        session = await session_factory()
        common = {"strategy_id": "L1", "config_hash": "x", "k": 8, "status": "done"}
        session.add(
            ResearchReport(
                kind="backtest",
                gate_passed=True,
                window_start=DEV_WINDOW[0],
                window_end=DEV_WINDOW[1],
                **common,
            )
        )
        await session.commit()
        first = await create_report(session, "L1", holdout=True, today=TODAY)
        assert first.kind == "holdout"
        assert (first.window_start, first.window_end) == (date(2026, 7, 1), date(2026, 10, 6))
        first.status = "done"
        await session.commit()
        with pytest.raises(ReportError, match="once"):
            await create_report(session, "L1", holdout=True, today=TODAY)
        await session.close()

    async def test_controls_have_no_holdout(self, session_factory) -> None:
        session = await session_factory()
        with pytest.raises(ReportError, match="Controls"):
            await create_report(session, "C1", holdout=True, today=TODAY)
        await session.close()


class TestRunReport:
    async def test_null_control_end_to_end(self, session_factory) -> None:
        session = await session_factory()
        await seed_event(
            session,
            "NYC",
            date(2025, 3, 2),
            winner=2,
            candles=[
                (
                    datetime(2025, 3, 1, 21, 0),
                    [(40, 42), (20, 22), (15, 17), (10, 12), (5, 7), (2, 4)],
                )
            ],
        )
        await session.commit()
        report = await create_report(session, "C0", today=TODAY)
        await session.close()

        out = await run_report_by_id(session_factory, report.id)
        assert out["status"] == "done"
        assert out["gate_passed"] is True  # C0 behaves as expected

        session = await session_factory()
        stored = await session.get(ResearchReport, report.id)
        await session.close()
        assert stored.results["control"]["pnl_cents"] == 0
        assert stored.results["summary"]["traded_city_days"] == 0
        assert stored.app_version
        assert stored.completed_at is not None

    async def test_longshot_backtest_produces_gate_verdict(self, session_factory) -> None:
        session = await session_factory()
        await seed_event(
            session,
            "NYC",
            date(2025, 3, 2),
            winner=2,
            candles=[
                (
                    datetime(2025, 3, 1, 21, 0),
                    [(40, 42), (20, 22), (15, 17), (10, 12), (5, 7), (2, 4)],
                )
            ],
        )
        await session.commit()
        report = await create_report(session, "L1", today=TODAY)
        await session.close()

        out = await run_report_by_id(session_factory, report.id)
        assert out["status"] == "done"
        session = await session_factory()
        stored = await session.get(ResearchReport, report.id)
        await session.close()
        summary = stored.results["summary"]
        assert summary["traded_city_days"] == 1  # NO on the 2c-bid top bracket, which lost YES
        assert summary["pnl_cents"] > 0
        assert stored.results["gate"]["passed"] is False  # 1 day can't pass sample size
        assert stored.gate_passed is False

    async def test_failure_is_recorded_not_raised(self, session_factory, monkeypatch) -> None:
        from backend.research import reports as mod

        async def boom(*_a, **_k):
            raise RuntimeError("archive unavailable")

        monkeypatch.setattr(mod, "execute_report", boom)
        session = await session_factory()
        report = await create_report(session, "L2", today=TODAY)
        await session.close()
        out = await run_report_by_id(session_factory, report.id)
        assert out["status"] == "error"
        session = await session_factory()
        stored = await session.get(ResearchReport, report.id)
        await session.close()
        assert "archive unavailable" in stored.error
