"""Research endpoints: queue pre-registered backtests and read their verdicts (algo v2 S2).

Usage:
    GET  /api/research/strategies          -> registered strategy IDs
    POST /api/research/backtest            -> {"strategy_id": "L1", "holdout": false}
    GET  /api/research/reports             -> list (newest first)
    GET  /api/research/reports/{id}        -> full results incl. gate criteria
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import get_current_user
from backend.api.response_schemas import (
    BacktestRequestV2,
    ResearchReportDetail,
    ResearchReportSummary,
    StrategyInfo,
)
from backend.common.database import get_db
from backend.common.logging import get_logger
from backend.common.models import ResearchReport, User
from backend.research.reports import ReportError, create_report
from backend.strategy.registry import STRATEGIES

logger = get_logger("TRADING")

router = APIRouter()


def _summary(r: ResearchReport) -> ResearchReportSummary:
    return ResearchReportSummary(
        id=r.id,
        kind=r.kind,
        strategy_id=r.strategy_id,
        status=r.status,
        gate_passed=r.gate_passed,
        window_start=r.window_start,
        window_end=r.window_end,
        created_at=r.created_at,
        completed_at=r.completed_at,
    )


@router.get("/strategies", response_model=list[StrategyInfo])
async def list_strategies(user: User = Depends(get_current_user)) -> list[StrategyInfo]:
    """List pre-registered strategies that can be backtested."""
    out = []
    for sid, factory in STRATEGIES.items():
        s = factory()
        out.append(
            StrategyInfo(strategy_id=sid, kind=s.kind, decisions=list(s.decisions), params=s.params)
        )
    return out


@router.post("/backtest", response_model=ResearchReportSummary, status_code=202)
async def queue_backtest(
    body: BacktestRequestV2,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ResearchReportSummary:
    """Queue a pre-registered backtest (or the one-time holdout evaluation).

    Raises:
        HTTPException 400: Unknown strategy or holdout rules violated.
    """
    try:
        report = await create_report(db, body.strategy_id, holdout=body.holdout)
    except ReportError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    from backend.research.tasks import run_backtest_report

    run_backtest_report.delay(report.id)
    logger.info(
        "Research backtest queued",
        extra={"data": {"report_id": report.id, "strategy": body.strategy_id, "kind": report.kind}},
    )
    return _summary(report)


@router.get("/reports", response_model=list[ResearchReportSummary])
async def list_reports(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[ResearchReportSummary]:
    """List research reports, newest first."""
    rows = (
        await db.execute(select(ResearchReport).order_by(ResearchReport.id.desc()).limit(200))
    ).scalars()
    return [_summary(r) for r in rows]


@router.get("/reports/{report_id}", response_model=ResearchReportDetail)
async def get_report(
    report_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ResearchReportDetail:
    """Full report: summary, gate criteria / control verdict, market scores."""
    r = await db.get(ResearchReport, report_id)
    if r is None:
        raise HTTPException(status_code=404, detail="Report not found")
    return ResearchReportDetail(
        **_summary(r).model_dump(),
        params=r.params or {},
        k=r.k,
        config_hash=r.config_hash,
        app_version=r.app_version,
        results=r.results,
        error=r.error,
    )
