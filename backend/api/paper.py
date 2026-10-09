"""Paper-trading endpoints (algo v2, slice S5): forward-only test progress.

Usage:
    GET /api/paper/strategies           -> per strategy: status, kill-switch stats, P&L
    GET /api/paper/trades?strategy_id=  -> recent paper trades
"""

from __future__ import annotations

from collections import defaultdict

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import get_current_user
from backend.api.response_schemas import PaperStrategySummary, PaperTradeRow
from backend.common.database import get_db
from backend.common.models import PaperDecision, PaperStrategyState, PaperTrade, User
from backend.strategy.monitor import CUSUM_ALARM_CENTS, SPRT_EDGE, SPRT_STOP
from backend.strategy.paper import PAPER_STRATEGIES

router = APIRouter()


@router.get("/strategies", response_model=list[PaperStrategySummary])
async def paper_strategies(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[PaperStrategySummary]:
    """Progress of each forward-only paper strategy."""
    trades = (await db.execute(select(PaperTrade))).scalars().all()
    decisions = (await db.execute(select(PaperDecision))).scalars().all()
    by_sid: dict[str, list[PaperTrade]] = defaultdict(list)
    for t in trades:
        by_sid[t.strategy_id].append(t)
    days_by_sid: dict[str, set] = defaultdict(set)
    for t in trades:
        if t.status == "settled":
            days_by_sid[t.strategy_id].add((t.city, t.event_date))
    dec_by_sid: dict[str, int] = defaultdict(int)
    for d in decisions:
        dec_by_sid[d.strategy_id] += 1

    out = []
    for sid in PAPER_STRATEGIES:
        state = await db.get(PaperStrategyState, sid)
        ts = by_sid.get(sid, [])
        settled = [t for t in ts if t.status == "settled"]
        out.append(
            PaperStrategySummary(
                strategy_id=sid,
                status=state.status if state else "not_started",
                started_at=state.started_at if state else None,
                stopped_at=state.stopped_at if state else None,
                stop_reason=state.stop_reason if state else None,
                decisions=dec_by_sid.get(sid, 0),
                trades_open=sum(1 for t in ts if t.status == "open"),
                trades_settled=len(settled),
                traded_city_days=len(days_by_sid.get(sid, set())),
                contracts=sum(t.count for t in settled),
                pnl_cents=sum(t.pnl_cents or 0 for t in settled),
                wins=sum(1 for t in settled if t.result == t.side),
                infeasible_fills=sum(1 for t in ts if not t.fill_feasible),
                sprt_llr=state.sprt_llr if state else None,
                sprt_stop=SPRT_STOP,
                sprt_edge=SPRT_EDGE,
                cusum_cents=state.cusum_cents if state else None,
                cusum_alarm_cents=CUSUM_ALARM_CENTS,
            )
        )
    return out


@router.get("/trades", response_model=list[PaperTradeRow])
async def paper_trades(
    strategy_id: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[PaperTradeRow]:
    """Most recent paper trades (optionally one strategy)."""
    q = select(PaperTrade).order_by(PaperTrade.id.desc()).limit(limit)
    if strategy_id:
        q = q.where(PaperTrade.strategy_id == strategy_id)
    rows = (await db.execute(q)).scalars().all()
    return [
        PaperTradeRow(
            id=t.id,
            strategy_id=t.strategy_id,
            city=t.city.value if hasattr(t.city, "value") else str(t.city),
            event_date=t.event_date,
            ticker=t.ticker,
            label=t.label,
            side=t.side,
            count=t.count,
            price_cents=t.price_cents,
            fee_cents=t.fee_cents,
            model_probability=t.model_probability,
            fill_feasible=t.fill_feasible,
            status=t.status,
            result=t.result,
            pnl_cents=t.pnl_cents,
            quoted_at=t.quoted_at,
        )
        for t in rows
    ]
