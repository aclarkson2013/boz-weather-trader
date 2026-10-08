"""Run a pre-registered backtest and store the verdict in ``research_reports``.

Rules enforced here (docs/research/v2-preregistration.md):
- Only registered strategy IDs run, with their registered parameters.
- Default window = the development window (2024-07-01 -> 2026-06-30).
- Controls are judged against their expected behaviour; counted variants
  against the gate (criteria 1-6) with alpha/K.
- The holdout (event dates >= 2026-07-01) can be used ONCE per strategy, and
  only after that strategy's development verdict PASSED.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.backtesting.real_engine import BacktestRun, run_real_backtest
from backend.common.logging import get_logger
from backend.common.models import ResearchReport, Trade, TradeStatus
from backend.research.gate import (
    DEV_WINDOW,
    evaluate_control,
    evaluate_gate,
    holdout_mean,
    holdout_window,
    summarize,
)
from backend.strategy.registry import FORWARD_ONLY, PREREGISTERED_K, get_strategy
from backend.weather.stations import VALID_CITIES

logger = get_logger("TRADING")

V1_FIRST_PREDICTION = date(2026, 2, 20)  # First stored v1 prediction
V1_PAUSE_DATE = date(2026, 8, 28)  # v1 switched to manual mode


class ReportError(Exception):
    """A report request violates the pre-registration rules."""


def config_hash(strategy_id: str, params: dict, window: tuple[date, date], kind: str) -> str:
    """Stable hash of everything that defines a run (for audit / dedupe)."""
    blob = json.dumps(
        {
            "strategy": strategy_id,
            "params": params,
            "window": [window[0].isoformat(), window[1].isoformat()],
            "kind": kind,
        },
        sort_keys=True,
    )
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def _market_score_summary(run: BacktestRun) -> dict:
    out = {}
    for name, values in run.market_scores.items():
        out[name] = round(sum(values) / len(values), 4) if values else None
    out["n_snapshots"] = len(run.market_scores["log_loss"])
    return out


async def _real_v1_record(
    session: AsyncSession, start: date, end: date
) -> dict[tuple[str, date], int]:
    """Settled v1 trade P&L per (city, market date) in the window."""
    rows = await session.execute(
        select(Trade.city, Trade.market_date, Trade.trade_date, Trade.pnl_cents).where(
            Trade.status.in_([TradeStatus.WON, TradeStatus.LOST]),
        )
    )
    record: dict[tuple[str, date], int] = defaultdict(int)
    for city, market_date, trade_date, pnl in rows.all():
        d = (market_date or trade_date).date() if (market_date or trade_date) else None
        if d is None or not start <= d <= end:
            continue
        code = city.value if hasattr(city, "value") else str(city)
        record[(code, d)] += pnl or 0
    return dict(record)


async def execute_report(session: AsyncSession, report: ResearchReport, today: date) -> dict:
    """Run the strategy described by ``report`` and return the results payload."""
    strategy = get_strategy(report.strategy_id)
    cities = list(VALID_CITIES)
    start, end = report.window_start, report.window_end

    run = await run_real_backtest(session, strategy, cities, start, end)
    payload: dict = {
        "summary": summarize(run.results),
        "days_seen": run.days_seen,
        "excluded": dict(run.excluded),
        "unfillable_orders": run.unfillable_orders,
        "market_scores": _market_score_summary(run),
    }

    if report.kind == "holdout":
        verdict = holdout_mean(run)
        payload["holdout"] = verdict
        return {**payload, "gate_passed": verdict["passed"]}

    if strategy.kind == "control":
        record = None
        if report.strategy_id == "C2":
            real = await _real_v1_record(session, start, end)
            keys = set(real)
            run.results = [r for r in run.results if (r.city, r.event_date) in keys]
            payload["summary_on_v1_days"] = summarize(run.results)
            record = sum(real.values())
        verdict = evaluate_control(report.strategy_id, run, start, end, record)
        payload["control"] = verdict
        return {**payload, "gate_passed": verdict["passed"]}

    slipped = await run_real_backtest(
        session,
        get_strategy(report.strategy_id),
        cities,
        start,
        end,
        slippage_cents=1,
        score_market=False,
    )
    gate = evaluate_gate(
        run,
        slipped,
        start,
        end,
        k=PREREGISTERED_K,
        model_strategy=strategy.kind == "model",
        daily_scores=getattr(strategy, "daily_scores", None),
    )
    if strategy.kind == "model":
        payload["fits"] = getattr(strategy, "fit_log", [])
    payload["gate"] = gate
    return {**payload, "gate_passed": gate["passed"]}


async def create_report(
    session: AsyncSession,
    strategy_id: str,
    holdout: bool = False,
    today: date | None = None,
) -> ResearchReport:
    """Validate a request against the pre-registration and insert a queued report row.

    Raises:
        ReportError: Unknown strategy, holdout misuse, or holdout already used.
    """
    try:
        strategy = get_strategy(strategy_id)
    except KeyError as exc:
        raise ReportError(f"Unknown strategy '{strategy_id}' (not pre-registered)") from exc
    today = today or datetime.now(UTC).date()

    if strategy_id in FORWARD_ONLY:
        raise ReportError(
            f"{strategy_id} is forward-only (pre-registration Amendment 1): it is judged on "
            "paper trading only, never on archived data"
        )

    if holdout:
        if strategy.kind == "control":
            raise ReportError("Controls have no holdout evaluation")
        prior = (
            (
                await session.execute(
                    select(ResearchReport).where(
                        ResearchReport.strategy_id == strategy_id,
                        ResearchReport.status == "done",
                    )
                )
            )
            .scalars()
            .all()
        )
        if any(r.kind == "holdout" for r in prior):
            raise ReportError(f"Holdout already used for {strategy_id} — it may be used once")
        if not any(r.kind == "backtest" and r.gate_passed for r in prior):
            raise ReportError(f"{strategy_id} has no PASSING development verdict yet")
        window = holdout_window(today)
        kind = "holdout"
    elif strategy_id == "C2":
        window = (V1_FIRST_PREDICTION, V1_PAUSE_DATE)  # v1's live window with stored predictions
        kind = "control"
    else:
        window = DEV_WINDOW
        kind = "control" if strategy.kind == "control" else "backtest"

    report = ResearchReport(
        kind=kind,
        strategy_id=strategy_id,
        params=strategy.params,
        config_hash=config_hash(strategy_id, strategy.params, window, kind),
        k=PREREGISTERED_K,
        window_start=window[0],
        window_end=window[1],
        status="queued",
    )
    session.add(report)
    await session.commit()
    await session.refresh(report)
    return report


async def run_report_by_id(
    session_factory: Callable[[], Awaitable[AsyncSession]], report_id: int
) -> dict:
    """Execute a queued report and persist results (status done / error)."""
    from backend import __version__

    session = await session_factory()
    try:
        report = await session.get(ResearchReport, report_id)
        if report is None:
            raise ReportError(f"Report {report_id} not found")
        report.status = "running"
        report.app_version = __version__
        await session.commit()
        try:
            payload = await execute_report(session, report, datetime.now(UTC).date())
            report.results = payload
            report.gate_passed = bool(payload.get("gate_passed"))
            report.status = "done"
        except Exception as exc:
            await session.rollback()
            report = await session.get(ResearchReport, report_id)
            report.status = "error"
            report.error = f"{type(exc).__name__}: {exc}"[:2000]
            logger.error(
                "Research report failed",
                extra={"data": {"report_id": report_id, "error": report.error[:300]}},
            )
        report.completed_at = datetime.now(UTC).replace(tzinfo=None)
        await session.commit()
        return {"report_id": report_id, "status": report.status, "gate_passed": report.gate_passed}
    finally:
        await session.close()
