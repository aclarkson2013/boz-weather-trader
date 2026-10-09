"""Live paper trading for forward-only strategies (algo v2, slice S5).

Runs the pre-registered forward-only strategies (Amendments 1-2: L5, B5) on LIVE
Kalshi quotes at their decision times, records simulated taker fills with exact
fees in ``paper_trades`` and settles them on Kalshi's result. **It never places
an order** — nothing here touches the trading executor or the manual queue.

Per strategy x city x event day it acts at most once, inside a window after the
scheduled decision time; every decision (including "no orders") is recorded in
``paper_decisions`` so reruns are idempotent and gaps are visible.

The kill switch (``monitor.py``) runs after each settlement pass and stops a
strategy permanently on an SPRT stop or CUSUM alarm, with a push notification.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.common.logging import get_logger
from backend.common.models import (
    CityEnum,
    PaperDecision,
    PaperStrategyState,
    PaperTrade,
)
from backend.common.schemas import Fill
from backend.kalshi.archive import event_ticker_for
from backend.research.snapshots import decision_ts_for
from backend.strategy.fills import fill_cost_cents, fill_order, settle_pnl_cents
from backend.strategy.live import build_live_snapshot
from backend.strategy.monitor import evaluate_monitor
from backend.strategy.registry import get_strategy
from backend.weather.stations import STATION_CONFIGS

logger = get_logger("TRADING")

PAPER_STRATEGIES: tuple[str, ...] = ("L5", "B5")  # Forward-only (Amendments 1-2)
DECISION_WINDOW = timedelta(minutes=90)  # Act within 90 min after the scheduled time
BUDGET_CENTS = 400  # $4 per city-day per strategy

STATUS_ACTIVE = "active"
STATUS_STOPPED = "stopped"


def due_event_date(city: str, decision: str, now: datetime) -> date | None:
    """Event date whose ``decision`` time falls in [decision_ts, decision_ts + window).

    Args:
        city: City code.
        decision: Decision label.
        now: Current time, naive UTC.

    Returns:
        The event date to act on, or None if no decision is due.
    """
    local_today = now.replace(tzinfo=UTC).astimezone(STATION_CONFIGS[city].timezone).date()
    for d in (local_today, local_today + timedelta(days=1), local_today + timedelta(days=2)):
        ts = decision_ts_for(city, d, decision)
        if ts <= now < ts + DECISION_WINDOW:
            return d
    return None


async def _get_state(session: AsyncSession, strategy_id: str, now: datetime) -> PaperStrategyState:
    state = await session.get(PaperStrategyState, strategy_id)
    if state is None:
        state = PaperStrategyState(strategy_id=strategy_id, status=STATUS_ACTIVE, started_at=now)
        session.add(state)
        await session.flush()
    return state


async def run_paper_cycle(
    session_factory: Callable[[], Awaitable[AsyncSession]],
    client,  # KalshiPublicClient-like: get_event_markets(event_ticker)
    *,
    now: datetime | None = None,
    cities: list[str] | None = None,
    strategies: tuple[str, ...] = PAPER_STRATEGIES,
    refresh_forecasts: Callable[[list[str]], Awaitable[None]] | None = None,
) -> dict:
    """Make any due paper decisions for every active forward-only strategy.

    Args:
        session_factory: Async callable returning a new DB session.
        client: Public Kalshi client (live quotes).
        now: Current time (naive UTC); defaults to now.
        cities: City codes (default: all four).
        strategies: Strategy IDs to run.
        refresh_forecasts: Optional coroutine refreshing recent forecast issuances for
            the given cities before a model strategy decides.

    Returns:
        Dict of counts (decisions made, trades recorded, skipped).
    """
    now = now or datetime.now(UTC).replace(tzinfo=None)
    cities = cities or list(STATION_CONFIGS.keys())
    summary: dict[str, int] = defaultdict(int)

    for sid in strategies:
        session = await session_factory()
        try:
            state = await _get_state(session, sid, now)
            await session.commit()
            if state.status != STATUS_ACTIVE:
                summary["stopped_skipped"] += 1
                continue

            strategy = get_strategy(sid)
            decision = strategy.decisions[0]
            due: list[tuple[str, date]] = []
            for city in cities:
                d = due_event_date(city, decision, now)
                if d is None:
                    continue
                existing = await session.get(PaperDecision, (sid, CityEnum(city), d, decision))
                if existing is None:
                    due.append((city, d))
            if not due:
                continue

            if strategy.kind == "model":
                if refresh_forecasts is not None:
                    try:
                        await refresh_forecasts([c for c, _ in due])
                    except Exception as exc:  # Stale inputs are safer than none: log and go on
                        logger.warning(
                            "Forecast refresh before paper decision failed",
                            extra={"data": {"strategy": sid, "error": str(exc)[:200]}},
                        )
                target = max(d for _, d in due)
                await strategy.prepare(session, cities, target, target)

            for city, d in due:
                await _decide_one(session, client, strategy, sid, city, d, decision, now, summary)
                await session.commit()
        except Exception as exc:
            await session.rollback()
            summary["errors"] += 1
            logger.error(
                "Paper cycle failed for strategy",
                extra={"data": {"strategy": sid, "error": f"{type(exc).__name__}: {exc}"[:300]}},
            )
        finally:
            await session.close()

    return dict(summary)


async def _decide_one(
    session: AsyncSession,
    client,
    strategy,
    sid: str,
    city: str,
    d: date,
    decision: str,
    now: datetime,
    summary: dict[str, int],
) -> None:
    """Snapshot live quotes, ask the strategy, record decision + paper fills."""
    decision_ts = decision_ts_for(city, d, decision)
    raw = await client.get_event_markets(event_ticker_for(city, d))
    snapshot, sizes = build_live_snapshot(raw, city, d, decision, decision_ts, now)
    note = None
    orders = []
    if snapshot is None:
        note = "event not open / not all brackets active"
    elif not snapshot.tiles_ok:
        note = "brackets do not tile"
    else:
        if strategy.kind == "model" and not strategy.add_live_snapshot(snapshot):
            note = "model inputs unavailable (no NBM issuance before decision)"
        else:
            orders = strategy.decide(snapshot, BUDGET_CENTS)

    budget = BUDGET_CENTS
    quotes = {q.ticker: q for q in snapshot.quotes} if snapshot else {}
    recorded = 0
    for order in orders:
        quote = quotes.get(order.ticker)
        fill = fill_order(order, quote, decision, decision_ts, budget) if quote else None
        if fill is None:
            summary["unfillable"] += 1
            continue
        budget -= fill_cost_cents(fill)
        bid_size, ask_size = sizes.get(order.ticker, (None, None))
        available = ask_size if fill.side == "yes" else bid_size
        session.add(
            PaperTrade(
                strategy_id=sid,
                city=CityEnum(city),
                event_date=d,
                decision=decision,
                decision_ts=decision_ts,
                quoted_at=now,
                ticker=fill.ticker,
                label=quote.label,
                side=fill.side,
                count=fill.count,
                price_cents=fill.price_cents,
                fee_cents=fill.fee_cents,
                yes_bid=quote.yes_bid,
                yes_ask=quote.yes_ask,
                available_size=available,
                fill_feasible=available is not None and available >= fill.count,
                model_probability=fill.model_probability,
                status="open",
            )
        )
        recorded += 1

    session.add(
        PaperDecision(
            strategy_id=sid,
            city=CityEnum(city),
            event_date=d,
            decision=decision,
            decided_at=now,
            n_orders=recorded,
            note=note,
        )
    )
    summary["decisions"] += 1
    summary["trades"] += recorded
    logger.info(
        "Paper decision recorded",
        extra={
            "data": {
                "strategy": sid,
                "city": city,
                "event_date": str(d),
                "trades": recorded,
                "note": note,
            }
        },
    )


async def settle_paper_trades(
    session_factory: Callable[[], Awaitable[AsyncSession]],
    client,  # get_event_markets(event_ticker, historical=bool)
    *,
    now: datetime | None = None,
    notify: Callable[[str, str], Awaitable[None]] | None = None,
) -> dict:
    """Settle open paper trades from Kalshi results, then run the kill switch.

    Returns:
        Dict with settled / voided counts and any strategies stopped.
    """
    now = now or datetime.now(UTC).replace(tzinfo=None)
    session = await session_factory()
    settled = voided = 0
    stopped: list[str] = []
    try:
        open_trades = (
            (
                await session.execute(
                    select(PaperTrade).where(
                        PaperTrade.status == "open", PaperTrade.event_date < now.date()
                    )
                )
            )
            .scalars()
            .all()
        )
        by_event: dict[str, list[PaperTrade]] = defaultdict(list)
        for t in open_trades:
            city = t.city.value if hasattr(t.city, "value") else str(t.city)
            by_event[event_ticker_for(city, t.event_date)].append(t)

        for event_ticker, trades in by_event.items():
            markets = await client.get_event_markets(event_ticker)
            if not markets:
                markets = await client.get_event_markets(event_ticker, historical=True)
            results = {m.get("ticker"): (m.get("result") or "").lower() for m in markets}
            for t in trades:
                result = results.get(t.ticker, "")
                if result in ("yes", "no"):
                    fill = Fill(
                        ticker=t.ticker,
                        side=t.side,
                        count=t.count,
                        price_cents=t.price_cents,
                        fee_cents=t.fee_cents,
                        decision=t.decision,
                        decision_ts=t.decision_ts,
                    )
                    t.result = result
                    t.pnl_cents = settle_pnl_cents(fill, result)
                    t.status = "settled"
                    t.settled_at = now
                    settled += 1
                elif result:  # scalar / void etc.
                    t.result = result
                    t.pnl_cents = 0
                    t.status = "void"
                    t.settled_at = now
                    voided += 1
        await session.commit()

        for sid in PAPER_STRATEGIES:
            state = await session.get(PaperStrategyState, sid)
            if state is None or state.status != STATUS_ACTIVE:
                continue
            rows = (
                (
                    await session.execute(
                        select(PaperTrade)
                        .where(PaperTrade.strategy_id == sid, PaperTrade.status == "settled")
                        .order_by(PaperTrade.event_date, PaperTrade.id)
                    )
                )
                .scalars()
                .all()
            )
            daily: dict[tuple[date, str], int] = defaultdict(int)
            for r in rows:
                daily[(r.event_date, str(r.city))] += r.pnl_cents or 0
            res = evaluate_monitor(
                [(r.model_probability, r.price_cents, r.result == r.side) for r in rows],
                [daily[k] for k in sorted(daily)],
            )
            state.sprt_llr = res.sprt_llr
            state.cusum_cents = res.cusum_cents
            state.last_evaluated_at = now
            if res.stop:
                state.status = STATUS_STOPPED
                state.stopped_at = now
                state.stop_reason = res.reason
                stopped.append(sid)
                logger.error(
                    "Paper strategy stopped by kill switch",
                    extra={"data": {"strategy": sid, "reason": res.reason}},
                )
                if notify is not None:
                    try:
                        await notify(f"Paper test stopped: {sid}", res.reason or "")
                    except Exception as exc:
                        logger.warning(
                            "Paper stop notification failed",
                            extra={"data": {"error": str(exc)[:200]}},
                        )
        await session.commit()
    finally:
        await session.close()
    return {"settled": settled, "voided": voided, "stopped": stopped}
