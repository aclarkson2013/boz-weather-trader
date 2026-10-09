"""Tests for /api/paper endpoints and paper task gating (S5)."""

from __future__ import annotations

from datetime import date, datetime
from unittest.mock import MagicMock, patch

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from backend.common.models import CityEnum, PaperStrategyState, PaperTrade


class TestPaperAPI:
    async def test_not_started(self, client: AsyncClient) -> None:
        body = (await client.get("/api/paper/strategies")).json()
        assert {s["strategy_id"] for s in body} == {"L5", "B5"}
        assert all(s["status"] == "not_started" and s["pnl_cents"] == 0 for s in body)

    async def test_summary_and_trades(self, client: AsyncClient, db: AsyncSession) -> None:
        now = datetime(2026, 10, 12, 1, 0)
        db.add(
            PaperStrategyState(
                strategy_id="L5", status="active", started_at=now, sprt_llr=0.0, cusum_cents=0
            )
        )
        db.add_all(
            [
                PaperTrade(
                    strategy_id="L5",
                    city=CityEnum.NYC,
                    event_date=date(2026, 10, 11),
                    decision="D1L",
                    decision_ts=now,
                    quoted_at=now,
                    ticker="A",
                    side="no",
                    count=4,
                    price_cents=98,
                    fee_cents=1,
                    fill_feasible=True,
                    status="settled",
                    result="no",
                    pnl_cents=7,
                ),
                PaperTrade(
                    strategy_id="L5",
                    city=CityEnum.NYC,
                    event_date=date(2026, 10, 12),
                    decision="D1L",
                    decision_ts=now,
                    quoted_at=now,
                    ticker="B",
                    side="no",
                    count=4,
                    price_cents=97,
                    fee_cents=1,
                    fill_feasible=False,
                    status="open",
                ),
            ]
        )
        await db.commit()
        body = {s["strategy_id"]: s for s in (await client.get("/api/paper/strategies")).json()}
        l5 = body["L5"]
        assert l5["status"] == "active"
        assert (l5["trades_settled"], l5["trades_open"], l5["pnl_cents"], l5["wins"]) == (
            1,
            1,
            7,
            1,
        )
        assert l5["infeasible_fills"] == 1
        assert l5["sprt_stop"] < 0 < l5["sprt_edge"]
        trades = (await client.get("/api/paper/trades?strategy_id=L5")).json()
        assert [t["ticker"] for t in trades] == ["B", "A"]


class TestPaperTasks:
    def test_disabled_flag(self) -> None:
        from backend.strategy import paper_tasks

        s = MagicMock()
        s.v2_paper_enabled = False
        with patch.object(paper_tasks, "get_settings", return_value=s):
            assert paper_tasks.paper_cycle.run() == {"status": "disabled"}
            assert paper_tasks.paper_settle.run() == {"status": "disabled"}

    def test_registered(self) -> None:
        from backend.celery_app import celery_app

        assert "backend.strategy.paper_tasks" in celery_app.conf.include
        tasks = {v["task"] for v in celery_app.conf.beat_schedule.values()}
        assert {
            "backend.strategy.paper_tasks.paper_cycle",
            "backend.strategy.paper_tasks.paper_settle",
        } <= tasks
