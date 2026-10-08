"""S2 acceptance criteria for snapshots and the real-price engine.

AC1 no-lookahead candle selection · AC2 unfillable one-sided quotes ·
AC4 null strategy P&L 0 · AC6 DST decision times · budget cap · exclusions.
"""

from __future__ import annotations

from datetime import date, datetime

from backend.backtesting.real_engine import run_real_backtest
from backend.common.schemas import MarketSnapshot, StrategyOrder
from backend.research.snapshots import build_snapshot, decision_ts_for, load_city_archive
from backend.strategy.base import BaseStrategy
from backend.strategy.controls import NullStrategy
from tests.research.conftest import seed_event

D = date(2026, 7, 10)  # Summer: NYC on EDT (UTC-4)
FLAT = [(40, 42), (20, 22), (15, 17), (10, 12), (5, 7), (2, 4)]
LATE = [(90, 92), (1, 3), (1, 3), (1, 3), (1, 3), (1, 3)]


class TestDecisionTimes:
    def test_d1e_is_5pm_local_day_before(self) -> None:
        # 2026-07-09 17:00 EDT == 21:00 UTC
        assert decision_ts_for("NYC", D, "D1E") == datetime(2026, 7, 9, 21, 0)
        # Chicago (CDT, UTC-5)
        assert decision_ts_for("CHI", D, "D1E") == datetime(2026, 7, 9, 22, 0)

    def test_dst_transition_days(self) -> None:
        """AC6: D0M on DST start/end days maps to the right UTC instant."""
        # DST starts 2026-03-08 at 2am: 10:00 is EDT (UTC-4)
        assert decision_ts_for("NYC", date(2026, 3, 8), "D0M") == datetime(2026, 3, 8, 14, 0)
        # Day before DST start: 17:00 EST (UTC-5) on 2026-03-07
        assert decision_ts_for("NYC", date(2026, 3, 8), "D1E") == datetime(2026, 3, 7, 22, 0)
        # DST ends 2026-11-01 at 2am: 10:00 is EST (UTC-5)
        assert decision_ts_for("NYC", date(2026, 11, 1), "D0M") == datetime(2026, 11, 1, 15, 0)
        assert decision_ts_for("AUS", date(2026, 11, 1), "D0M") == datetime(2026, 11, 1, 16, 0)


class TestSnapshots:
    async def test_uses_latest_candle_at_or_before_decision(self, session_factory) -> None:
        """AC1: decision 17:00 local with candles ending 16:00 and 18:00 -> uses 16:00."""
        session = await session_factory()
        await seed_event(
            session,
            "NYC",
            D,
            winner=0,
            candles=[(datetime(2026, 7, 9, 20, 0), FLAT), (datetime(2026, 7, 9, 22, 0), LATE)],
        )
        await session.commit()
        archive = await load_city_archive(session, "NYC", D, D)
        await session.close()

        snapshot, outcomes, era = build_snapshot(archive, D, "D1E")
        assert snapshot is not None
        assert [(q.yes_bid, q.yes_ask) for q in snapshot.quotes] == FLAT
        assert all(q.quote_ts == datetime(2026, 7, 9, 20, 0) for q in snapshot.quotes)
        assert era == "E1"
        # Outcomes are returned separately, never on the snapshot
        assert sum(1 for v in outcomes.values() if v == "yes") == 1
        assert "result" not in snapshot.quotes[0].model_dump()

    async def test_no_candle_before_decision_means_no_quote(self, session_factory) -> None:
        session = await session_factory()
        await seed_event(session, "NYC", D, winner=0, candles=[(datetime(2026, 7, 9, 22, 0), LATE)])
        await session.commit()
        archive = await load_city_archive(session, "NYC", D, D)
        await session.close()
        snapshot, _, _ = build_snapshot(archive, D, "D1E")
        assert snapshot is not None
        assert all(q.yes_bid is None and q.yes_ask is None for q in snapshot.quotes)

    async def test_market_not_yet_open_is_excluded(self, session_factory) -> None:
        session = await session_factory()
        await seed_event(
            session, "NYC", D, winner=0, candles=[], open_time=datetime(2026, 7, 9, 23, 0)
        )
        await session.commit()
        archive = await load_city_archive(session, "NYC", D, D)
        await session.close()
        snapshot, _, _ = build_snapshot(archive, D, "D1E")
        assert snapshot is None


class _BuyEverything(BaseStrategy):
    """Test strategy: buy `count` YES (or NO) on every bracket."""

    def __init__(self, side: str = "yes", count: int = 1) -> None:
        self.strategy_id = "T"
        self.kind = "market"
        self.decisions = ("D1E",)
        self.params = {}
        self.side, self.count = side, count

    def decide(self, snapshot: MarketSnapshot, budget_cents: int) -> list[StrategyOrder]:
        return [
            StrategyOrder(ticker=q.ticker, side=self.side, count=self.count)
            for q in snapshot.quotes
        ]


class TestEngine:
    async def _seed_two_days(self, session_factory, quotes=FLAT, tiles_ok=True) -> None:
        session = await session_factory()
        for d, ts in [
            (D, datetime(2026, 7, 9, 20, 0)),
            (date(2026, 7, 11), datetime(2026, 7, 10, 20, 0)),
        ]:
            await seed_event(session, "NYC", d, winner=1, candles=[(ts, quotes)], tiles_ok=tiles_ok)
        await session.commit()
        await session.close()

    async def test_null_strategy_has_zero_pnl(self, session_factory) -> None:
        """AC4: the null strategy never trades -> P&L exactly 0."""
        await self._seed_two_days(session_factory)
        session = await session_factory()
        run = await run_real_backtest(session, NullStrategy(), ["NYC"], D, date(2026, 7, 11))
        await session.close()
        assert run.results == []
        assert run.days_seen == 2

    async def test_yes_fill_at_ask_and_exact_settlement(self, session_factory) -> None:
        await self._seed_two_days(session_factory)
        session = await session_factory()
        run = await run_real_backtest(session, _BuyEverything("yes"), ["NYC"], D, D)
        await session.close()
        assert len(run.results) == 1
        r = run.results[0]
        prices = [f.price_cents for f in r.fills]
        assert prices == [ask for _, ask in FLAT]
        # Winner is bracket 1 (ask 22): pays 100. Fees: ceil per 1-contract order.
        expected_fees = sum(f.fee_cents for f in r.fills)
        assert r.pnl_cents == 100 - sum(prices) - expected_fees

    async def test_no_fill_at_100_minus_bid(self, session_factory) -> None:
        await self._seed_two_days(session_factory)
        session = await session_factory()
        run = await run_real_backtest(session, _BuyEverything("no"), ["NYC"], D, D)
        await session.close()
        assert [f.price_cents for f in run.results[0].fills] == [100 - bid for bid, _ in FLAT][:4]

    async def test_one_sided_quotes_are_unfillable(self, session_factory) -> None:
        """AC2: bid=0 (no NO fill) / ask=100 (no YES fill) are never filled."""
        quotes = [(0, 3), (20, 100), (15, 17), (10, 12), (5, 7), (2, 4)]
        await self._seed_two_days(session_factory, quotes=quotes)
        session = await session_factory()
        yes_run = await run_real_backtest(session, _BuyEverything("yes"), ["NYC"], D, D)
        no_run = await run_real_backtest(session, _BuyEverything("no"), ["NYC"], D, D)
        await session.close()
        yes_tickers = {f.ticker for f in yes_run.results[0].fills}
        no_tickers = {f.ticker for f in no_run.results[0].fills}
        assert not any(t.endswith("B70.5") for t in yes_tickers)  # ask 100
        assert not any(t.endswith("T70") for t in no_tickers)  # bid 0
        assert yes_run.unfillable_orders >= 1 and no_run.unfillable_orders >= 1

    async def test_budget_caps_city_day_spend(self, session_factory) -> None:
        await self._seed_two_days(session_factory)
        session = await session_factory()
        run = await run_real_backtest(
            session, _BuyEverything("no", count=10), ["NYC"], D, D, budget_cents=400
        )
        await session.close()
        r = run.results[0]
        assert r.cost_cents + r.fee_cents <= 400
        assert r.contracts >= 1

    async def test_non_tiling_events_are_excluded_and_counted(self, session_factory) -> None:
        await self._seed_two_days(session_factory, tiles_ok=False)
        session = await session_factory()
        run = await run_real_backtest(session, _BuyEverything("yes"), ["NYC"], D, date(2026, 7, 11))
        await session.close()
        assert run.results == []
        assert run.excluded["brackets_do_not_tile"] == 2

    async def test_market_scores_recorded(self, session_factory) -> None:
        await self._seed_two_days(session_factory)
        session = await session_factory()
        run = await run_real_backtest(session, NullStrategy(), ["NYC"], D, date(2026, 7, 11))
        await session.close()
        assert len(run.market_scores["log_loss"]) == 2
        assert all(v > 0 for v in run.market_scores["log_loss"])
