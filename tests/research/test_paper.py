"""S5 paper trading: acceptance criteria + safety.

AC1 SAFETY: the real executor / manual queue are never called ·
AC2 settled from Kalshi result (scalar -> void) · AC3 kill switch stops + notifies ·
AC4 fills beyond displayed size are flagged infeasible. Plus idempotency and windows.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock, patch

from sqlalchemy import func, select

from backend.common.models import CityEnum, PaperDecision, PaperStrategyState, PaperTrade
from backend.strategy.live import build_live_snapshot
from backend.strategy.monitor import SPRT_STOP, evaluate_monitor, sprt_increment
from backend.strategy.paper import due_event_date, run_paper_cycle, settle_paper_trades

EVENT = date(2026, 10, 11)
# L5 decides at 20:00 local the day before: 2026-10-10 20:00 EDT == 2026-10-11 00:00 UTC
L5_NOW = datetime(2026, 10, 11, 0, 10)
SPECS = [
    ("T70", "less", None, 70),
    ("B70.5", "between", 70, 71),
    ("B72.5", "between", 72, 73),
    ("B74.5", "between", 74, 75),
    ("B76.5", "between", 76, 77),
    ("T77", "greater", 77, None),
]


def live_markets(
    quotes: list[tuple[int, int]],
    sizes: tuple[str, str] = ("500.00", "500.00"),
    status: str = "active",
    results: list[str] | None = None,
) -> list[dict]:
    ev = "KXHIGHNY-26OCT11"
    out = []
    for i, ((suffix, st, fl, cp), (bid, ask)) in enumerate(zip(SPECS, quotes, strict=True)):
        out.append(
            {
                "ticker": f"{ev}-{suffix}",
                "event_ticker": ev,
                "strike_type": st,
                "floor_strike": fl,
                "cap_strike": cp,
                "status": status,
                "result": results[i] if results else "",
                "yes_bid_dollars": f"{bid / 100:.4f}",
                "yes_ask_dollars": f"{ask / 100:.4f}",
                "yes_bid_size_fp": sizes[0],
                "yes_ask_size_fp": sizes[1],
            }
        )
    return out


class FakeLive:
    def __init__(self, markets: list[dict]) -> None:
        self.markets = markets
        self.calls = 0

    async def get_event_markets(self, event_ticker: str, historical: bool = False) -> list[dict]:
        self.calls += 1
        return [dict(m) for m in self.markets] if event_ticker == "KXHIGHNY-26OCT11" else []


LONGSHOT = [(2, 3), (10, 12), (30, 32), (40, 42), (12, 14), (1, 2)]


class TestScheduling:
    def test_due_event_date_window(self) -> None:
        assert due_event_date("NYC", "D1L", L5_NOW) == EVENT
        assert due_event_date("NYC", "D1L", L5_NOW - timedelta(minutes=11)) is None  # before
        assert due_event_date("NYC", "D1L", L5_NOW + timedelta(hours=2)) is None  # window over
        # D1E (17:00 EDT) for NYC == 21:00 UTC the day before
        assert due_event_date("NYC", "D1E", datetime(2026, 10, 10, 21, 30)) == EVENT


class TestLiveSnapshot:
    def test_builds_sorted_quotes_and_sizes(self) -> None:
        snap, sizes = build_live_snapshot(
            live_markets(LONGSHOT), "NYC", EVENT, "D1L", L5_NOW, L5_NOW
        )
        assert snap is not None and snap.tiles_ok
        assert snap.quotes[0].upper_bound_f == 69.5
        assert (snap.quotes[0].yes_bid, snap.quotes[0].yes_ask) == (2, 3)
        assert sizes[snap.quotes[0].ticker] == (500.0, 500.0)

    def test_inactive_event_gives_none(self) -> None:
        snap, _ = build_live_snapshot(
            live_markets(LONGSHOT, status="closed"), "NYC", EVENT, "D1L", L5_NOW, L5_NOW
        )
        assert snap is None


class TestPaperCycle:
    async def test_l5_records_paper_fills_once(self, session_factory) -> None:
        client = FakeLive(live_markets(LONGSHOT))
        for _ in range(2):  # second run must be a no-op (idempotent)
            await run_paper_cycle(
                session_factory, client, now=L5_NOW, cities=["NYC"], strategies=("L5",)
            )
        s = await session_factory()
        trades = (await s.execute(select(PaperTrade))).scalars().all()
        decisions = (await s.execute(select(func.count()).select_from(PaperDecision))).scalar_one()
        state = await s.get(PaperStrategyState, "L5")
        await s.close()
        assert decisions == 1
        assert state.status == "active"
        assert {t.side for t in trades} == {"no"}
        assert {t.ticker.rsplit("-", 1)[1] for t in trades} == {"T70", "T77"}  # bids 2c and 1c
        assert all(t.status == "open" and t.fill_feasible for t in trades)
        assert sum(t.count * t.price_cents + t.fee_cents for t in trades) <= 400
        assert client.calls == 1

    async def test_never_calls_real_executor_or_queue(self, session_factory) -> None:
        """AC1 SAFETY: paper trading cannot place or queue a real order."""
        client = FakeLive(live_markets(LONGSHOT))
        with (
            patch("backend.trading.executor.execute_trade", new=AsyncMock()) as execute,
            patch("backend.trading.trade_queue.queue_trade", new=AsyncMock()) as queue,
        ):
            await run_paper_cycle(
                session_factory, client, now=L5_NOW, cities=["NYC"], strategies=("L5",)
            )
            settled_client = FakeLive(
                live_markets(LONGSHOT, results=["no", "no", "yes", "no", "no", "no"])
            )
            await settle_paper_trades(
                session_factory, settled_client, now=L5_NOW + timedelta(days=2)
            )
        execute.assert_not_called()
        queue.assert_not_called()

    async def test_fill_beyond_displayed_size_is_infeasible(self, session_factory) -> None:
        """AC4: the bid size we'd sell into is smaller than the order -> flagged."""
        client = FakeLive(live_markets(LONGSHOT, sizes=("1.00", "1.00")))
        await run_paper_cycle(
            session_factory, client, now=L5_NOW, cities=["NYC"], strategies=("L5",)
        )
        s = await session_factory()
        trades = (await s.execute(select(PaperTrade))).scalars().all()
        await s.close()
        assert trades and all(not t.fill_feasible for t in trades if t.count > 1)

    async def test_closed_event_records_a_note_not_a_trade(self, session_factory) -> None:
        client = FakeLive(live_markets(LONGSHOT, status="closed"))
        await run_paper_cycle(
            session_factory, client, now=L5_NOW, cities=["NYC"], strategies=("L5",)
        )
        s = await session_factory()
        dec = (await s.execute(select(PaperDecision))).scalars().one()
        n = (await s.execute(select(func.count()).select_from(PaperTrade))).scalar_one()
        await s.close()
        assert n == 0 and dec.n_orders == 0 and "not open" in dec.note


class TestSettlement:
    async def test_settles_from_kalshi_result_and_voids_scalar(self, session_factory) -> None:
        """AC2: P&L from Kalshi's result; 'scalar' resolutions are voided."""
        await run_paper_cycle(
            session_factory,
            FakeLive(live_markets(LONGSHOT)),
            now=L5_NOW,
            cities=["NYC"],
            strategies=("L5",),
        )
        results = ["no", "no", "yes", "no", "no", "scalar"]
        out = await settle_paper_trades(
            session_factory,
            FakeLive(live_markets(LONGSHOT, results=results)),
            now=L5_NOW + timedelta(days=2),
        )
        assert out["settled"] == 1 and out["voided"] == 1
        s = await session_factory()
        trades = {
            t.ticker.rsplit("-", 1)[1]: t for t in (await s.execute(select(PaperTrade))).scalars()
        }
        await s.close()
        low = trades["T70"]
        assert low.status == "settled" and low.result == "no"
        assert low.pnl_cents == low.count * (100 - low.price_cents) - low.fee_cents
        assert trades["T77"].status == "void" and trades["T77"].pnl_cents == 0

    async def test_unsettled_markets_stay_open(self, session_factory) -> None:
        await run_paper_cycle(
            session_factory,
            FakeLive(live_markets(LONGSHOT)),
            now=L5_NOW,
            cities=["NYC"],
            strategies=("L5",),
        )
        out = await settle_paper_trades(
            session_factory, FakeLive(live_markets(LONGSHOT)), now=L5_NOW + timedelta(days=2)
        )
        assert out["settled"] == 0


class TestKillSwitch:
    def test_sprt_math(self) -> None:
        assert math.isclose(sprt_increment(0.6, 40, True), math.log(0.6 / 0.4))
        assert math.isclose(sprt_increment(0.6, 40, False), math.log(0.4 / 0.6))
        assert math.isclose(SPRT_STOP, math.log(0.2 / 0.95))

    def test_overconfident_losing_model_stops(self) -> None:
        trades = [(0.7, 40, False)] * 5
        res = evaluate_monitor(trades, [-41] * 5)
        assert res.stop and "SPRT" in res.reason

    def test_cusum_stops_strategy_without_probabilities(self) -> None:
        res = evaluate_monitor([(None, 98, False)] * 3, [-393, -393, -393])
        assert res.stop and "CUSUM" in res.reason

    def test_healthy_record_continues(self) -> None:
        res = evaluate_monitor([(0.5, 40, True), (0.5, 40, False)], [59, -41])
        assert not res.stop

    async def test_settlement_stops_strategy_and_notifies(self, session_factory) -> None:
        """AC3: crossing the SPRT stop boundary stops the strategy and sends a notification."""
        s = await session_factory()
        s.add(PaperStrategyState(strategy_id="B5", status="active", started_at=L5_NOW))
        for i in range(6):
            s.add(
                PaperTrade(
                    strategy_id="B5",
                    city=CityEnum.NYC,
                    event_date=date(2026, 10, 1) + timedelta(days=i),
                    decision="D1E",
                    decision_ts=L5_NOW,
                    quoted_at=L5_NOW,
                    ticker=f"T{i}",
                    side="yes",
                    count=1,
                    price_cents=40,
                    fee_cents=1,
                    fill_feasible=True,
                    model_probability=0.75,
                    status="settled",
                    result="no",
                    pnl_cents=-41,
                )
            )
        await s.commit()
        await s.close()
        notify = AsyncMock()
        out = await settle_paper_trades(session_factory, FakeLive([]), now=L5_NOW, notify=notify)
        assert out["stopped"] == ["B5"]
        notify.assert_awaited_once()
        assert "B5" in notify.await_args.args[0]
        s = await session_factory()
        state = await s.get(PaperStrategyState, "B5")
        await s.close()
        assert state.status == "stopped" and state.sprt_llr <= SPRT_STOP

        # A stopped strategy makes no further decisions
        out = await run_paper_cycle(
            session_factory,
            FakeLive(live_markets(LONGSHOT)),
            now=datetime(2026, 10, 10, 21, 30),
            cities=["NYC"],
            strategies=("B5",),
        )
        assert out.get("stopped_skipped") == 1
