"""Tests for scoring rules, the block bootstrap and the pre-registered gate (S2)."""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta

import numpy as np

from backend.backtesting.real_engine import BacktestRun, CityDayResult
from backend.common.schemas import BracketQuote, Fill, MarketSnapshot
from backend.research.gate import evaluate_control, evaluate_gate, holdout_mean, summarize
from backend.research.scoring import (
    bootstrap_ratio,
    brier,
    log_loss,
    market_probabilities,
    max_drawdown,
    rps,
    stationary_bootstrap_indices,
)

START = date(2025, 1, 1)
CITIES = ["NYC", "CHI", "MIA", "AUS"]


def _fill(price: int = 97, count: int = 4, mid: float | None = 97.5) -> Fill:
    return Fill(
        ticker="T",
        side="no",
        count=count,
        price_cents=price,
        fee_cents=1,
        mid_cents=mid,
        decision="D1E",
        decision_ts=datetime(2025, 1, 1),
    )


def _results(pnl_fn, days: int = 400) -> list[CityDayResult]:
    out = []
    for i in range(days):
        d = START + timedelta(days=i)
        for c in CITIES:
            pnl = pnl_fn(i, c)
            out.append(
                CityDayResult(
                    city=c,
                    event_date=d,
                    era="E1",
                    fills=[_fill()],
                    pnl_cents=pnl,
                    cost_cents=388,
                    fee_cents=1,
                    contracts=4,
                    expected_taker_cost_cents=3.0,
                )
            )
    return out


def _run(results: list[CityDayResult]) -> BacktestRun:
    run = BacktestRun(strategy_id="L1")
    run.results = results
    return run


class TestScores:
    def test_scores_on_known_values(self) -> None:
        probs = [0.1, 0.6, 0.3]
        assert math.isclose(log_loss(probs, 1), -math.log(0.6))
        assert math.isclose(brier(probs, 1), 0.01 + 0.16 + 0.09)
        # cum p: 0.1, 0.7 ; cum o (winner 1): 0, 1 -> (0.01 + 0.09) / 2
        assert math.isclose(rps(probs, 1), 0.05)
        assert rps([1.0, 0.0], 0) == 0.0

    def test_market_probabilities_normalize_mids(self) -> None:
        quotes = [
            BracketQuote(ticker="A", label="A", yes_bid=40, yes_ask=42),
            BracketQuote(ticker="B", label="B", yes_bid=0, yes_ask=20),  # one-sided -> mid 10
            BracketQuote(ticker="C", label="C", yes_bid=48, yes_ask=50),
        ]
        snap = MarketSnapshot(
            city="NYC",
            event_date=START,
            decision="D1E",
            decision_ts=datetime(2025, 1, 1),
            quotes=quotes,
        )
        p = market_probabilities(snap)
        assert p is not None
        assert math.isclose(sum(p), 1.0)
        assert math.isclose(p[1], 10 / (41 + 10 + 49))

    def test_drawdown(self) -> None:
        assert max_drawdown([5, -3, -4, 2, 10, -1]) == 7
        assert max_drawdown([1, 2, 3]) == 0


class TestBootstrap:
    def test_indices_shape_and_range(self) -> None:
        idx = stationary_bootstrap_indices(50, 20, 7.0, np.random.default_rng(0), length=90)
        assert idx.shape == (20, 90)
        assert idx.min() >= 0 and idx.max() < 50

    def test_ratio_centers_on_true_mean(self) -> None:
        rng = np.random.default_rng(1)
        numer = rng.normal(5.0, 1.0, size=500)
        denom = np.ones(500)
        boot = bootstrap_ratio(numer, denom, n_boot=2000)
        assert abs(np.nanmean(boot) - numer.mean()) < 0.05


class TestGate:
    def test_consistently_profitable_strategy_passes(self) -> None:
        results = _results(lambda i, c: 20 if i % 10 else -40)  # +14c/day avg, small losses
        run = _run(results)
        gate = evaluate_gate(run, _run(results), START, START + timedelta(days=399), k=8)
        assert gate["passed"], gate
        assert gate["criteria"]["2_mean_pnl_lower_bound"]["lower_bound_cents"] > 0

    def test_zero_edge_strategy_fails_lower_bound(self) -> None:
        results = _results(lambda i, c: 3 if (i + len(c)) % 2 else -3)
        gate = evaluate_gate(_run(results), _run(results), START, START + timedelta(days=399), k=8)
        assert not gate["criteria"]["2_mean_pnl_lower_bound"]["passed"]
        assert not gate["passed"]

    def test_too_few_days_fails_sample_size(self) -> None:
        results = _results(lambda i, c: 20, days=30)
        gate = evaluate_gate(_run(results), _run(results), START, START + timedelta(days=29), k=8)
        assert not gate["criteria"]["1_sample_size"]["passed"]

    def test_slippage_failure_fails_robustness(self) -> None:
        good = _results(lambda i, c: 20)
        bad = _results(lambda i, c: -2)
        gate = evaluate_gate(_run(good), _run(bad), START, START + timedelta(days=399), k=8)
        assert not gate["criteria"]["3_robustness"]["passed"]

    def test_big_drawdown_fails_risk(self) -> None:
        results = _results(lambda i, c: 30 if i < 300 else -100)
        gate = evaluate_gate(_run(results), _run(results), START, START + timedelta(days=399), k=8)
        assert not gate["criteria"]["6_risk"]["passed"]

    def test_summary_and_holdout(self) -> None:
        results = _results(lambda i, c: 10, days=5)
        s = summarize(results)
        assert s["traded_city_days"] == 20
        assert s["pnl_cents"] == 200
        assert holdout_mean(_run(results))["passed"] is True
        assert holdout_mean(_run([]))["passed"] is False


class TestControls:
    def test_c0_null(self) -> None:
        assert evaluate_control("C0", _run([]), START, START)["passed"]
        assert not evaluate_control("C0", _run(_results(lambda i, c: 0, days=1)), START, START)[
            "passed"
        ]

    def test_c1_random_taker_matches_expected_cost(self) -> None:
        # Every contract loses exactly its taker cost: -(0.5c spread + 0.25c fee) per contract
        results = _results(lambda i, c: -3, days=200)
        verdict = evaluate_control("C1", _run(results), START, START + timedelta(days=199))
        assert verdict["expected_cents"] == -0.75
        assert verdict["passed"], verdict

    def test_c2_sign_match(self) -> None:
        losing = _run(_results(lambda i, c: -5, days=10))
        assert evaluate_control("C2", losing, START, START, real_record_pnl_cents=-9247)["passed"]
        assert not evaluate_control("C2", losing, START, START, real_record_pnl_cents=500)["passed"]
