"""Tests for /api/research endpoints (algo v2 S2)."""

from __future__ import annotations

from datetime import date
from unittest.mock import patch

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from backend.common.models import ResearchReport


class TestResearchAPI:
    async def test_lists_preregistered_strategies(self, client: AsyncClient) -> None:
        body = (await client.get("/api/research/strategies")).json()
        ids = {s["strategy_id"] for s in body}
        assert {"C0", "C1", "C2", "L1", "L2", "L3", "L4"} <= ids

    async def test_queue_backtest_enqueues_task(self, client: AsyncClient) -> None:
        with patch("backend.research.tasks.run_backtest_report.delay") as delay:
            resp = await client.post("/api/research/backtest", json={"strategy_id": "L1"})
        assert resp.status_code == 202
        body = resp.json()
        assert body["status"] == "queued"
        assert body["kind"] == "backtest"
        delay.assert_called_once_with(body["id"])

    async def test_unknown_strategy_is_400(self, client: AsyncClient) -> None:
        resp = await client.post("/api/research/backtest", json={"strategy_id": "ZZ"})
        assert resp.status_code == 400

    async def test_holdout_without_passing_dev_is_400(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/research/backtest", json={"strategy_id": "L1", "holdout": True}
        )
        assert resp.status_code == 400
        assert "PASSING" in resp.json()["detail"]

    async def test_get_report_detail_and_404(self, client: AsyncClient, db: AsyncSession) -> None:
        r = ResearchReport(
            kind="backtest",
            strategy_id="L1",
            params={"max_yes_bid": 2},
            config_hash="abc",
            k=8,
            window_start=date(2024, 7, 1),
            window_end=date(2026, 6, 30),
            status="done",
            gate_passed=False,
            results={"gate": {"passed": False}},
        )
        db.add(r)
        await db.commit()
        listing = (await client.get("/api/research/reports")).json()
        assert listing[0]["strategy_id"] == "L1"
        detail = (await client.get(f"/api/research/reports/{r.id}")).json()
        assert detail["results"] == {"gate": {"passed": False}}
        assert detail["k"] == 8
        assert (await client.get("/api/research/reports/99999")).status_code == 404
