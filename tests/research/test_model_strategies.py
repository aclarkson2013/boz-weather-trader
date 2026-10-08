"""S4 acceptance criteria: forecast models, Benter combination, criterion 5.

AC1 poisoned future rows never used · AC2 no-information model -> alpha ~ 0, no trades ·
AC3 genuine edge -> trades + criterion 5 passes · AC4 closed-form CRPS == numerical integral.
"""

from __future__ import annotations

import math
import random
from datetime import date, datetime, timedelta

import numpy as np
import pytest
from scipy.integrate import quad
from scipy.stats import norm

from backend.backtesting.real_engine import CityDayResult, run_real_backtest
from backend.common.models import CityEnum, ForecastIssuance
from backend.common.schemas import Fill
from backend.research.forecast_models import (
    Features,
    IssuanceIndex,
    bracket_probs,
    crps_normal,
    features_at,
    fit_emos,
    training_window,
)
from backend.research.gate import edge_slope, model_edge_criterion
from backend.research.snapshots import decision_ts_for
from backend.strategy.benter import BenterStrategy, DayInputs, combine, fit_benter
from tests.research.conftest import BRACKETS, seed_event

CENTERS = [68.0, 70.5, 72.5, 74.5, 76.5, 79.0]
BOUNDS = [(lo, hi) for _, _, lo, hi in BRACKETS]


class TestForecastModels:
    def test_crps_closed_form_matches_numerical_integral(self) -> None:
        """AC4: CRPS(N(mu, s), y) = integral of (F(x) - 1{x >= y})^2 dx."""
        for mu, s, y in [(70.0, 2.0, 71.3), (55.0, 3.5, 49.0), (80.0, 1.2, 80.0)]:
            closed = float(crps_normal(np.array([mu]), np.array([s]), np.array([y]))[0])
            left, _ = quad(lambda x, mu=mu, s=s: norm.cdf(x, mu, s) ** 2, mu - 40 * s, y)
            right, _ = quad(lambda x, mu=mu, s=s: (norm.cdf(x, mu, s) - 1) ** 2, y, mu + 40 * s)
            assert abs(closed - (left + right)) < 1e-6

    def test_bracket_probs_sum_to_one_and_follow_cdf(self) -> None:
        p = bracket_probs(72.5, 2.0, BOUNDS)
        assert math.isclose(sum(p), 1.0)
        assert p[2] == max(p)
        expected_mid = norm.cdf((73.5 - 72.5) / 2) - norm.cdf((71.5 - 72.5) / 2)
        assert math.isclose(p[2], expected_mid, rel_tol=1e-6)

    def test_features_ignore_issuances_published_after_decision(self) -> None:
        """AC1: a forecast with available_at after the decision is never used."""
        d = date(2025, 6, 2)
        ts = datetime(2025, 6, 1, 21, 0)
        idx = IssuanceIndex(
            by_key={
                ("NBS", d): [
                    (ts - timedelta(hours=2), 72.0, 2.0),
                    (ts + timedelta(minutes=1), 95.0, 1.0),  # poisoned future row
                ],
                ("GFS", d): [(ts + timedelta(hours=1), 99.0, None)],  # also future
            }
        )
        f = features_at(idx, d, ts)
        assert f is not None
        assert (f.nbm, f.nbm_sd, f.mos) == (72.0, 2.0, 72.0)
        assert features_at(IssuanceIndex(), d, ts) is None

    def test_training_window_lags_labels_two_days(self) -> None:
        assert training_window(date(2025, 7, 1)) == (date(2024, 7, 1), date(2025, 6, 29))

    def test_emos_recovers_bias_and_keeps_weights_nonnegative(self) -> None:
        rng = random.Random(3)
        feats, ys = [], []
        for _ in range(300):
            truth = rng.uniform(40, 95)
            nbm = truth + 1.5 + rng.gauss(0, 1.0)  # NBM runs 1.5F warm
            mos = truth + rng.gauss(0, 4.0)  # MOS noisier
            feats.append(Features(nbm=nbm, nbm_sd=2.0, mos=mos))
            ys.append(truth)
        p = fit_emos(feats, ys)
        assert p is not None
        mu, _ = p.predict(Features(nbm=71.5, nbm_sd=2.0, mos=70.0))
        assert abs(mu - 70.0) < 1.0  # warm bias corrected
        assert p.b1 >= 0 and p.b2 >= 0
        assert fit_emos(feats[:10], ys[:10]) is None


def _day(winner: int, informative: bool, rng: random.Random) -> DayInputs:
    center = CENTERS[winner if informative else rng.randrange(6)]
    pm = bracket_probs(center, 1.5, BOUNDS)
    return DayInputs(
        log_pm=np.log(np.array(pm)),
        log_q=np.log(np.full(6, 1 / 6)),
        tail=np.array([1.0, 0, 0, 0, 0, 1.0]),
        winner=winner,
    )


class TestBenterFit:
    def test_no_information_model_gets_near_zero_weight(self) -> None:
        """AC2: alpha ~ 0 when the model carries no information."""
        rng = random.Random(1)
        days = [_day(rng.randrange(6), informative=False, rng=rng) for _ in range(600)]
        theta, cov = fit_benter(days)
        assert abs(theta[0]) < 0.1
        assert cov.shape == (3, 3)

    def test_informative_model_gets_positive_weight(self) -> None:
        rng = random.Random(2)
        days = []
        for _ in range(600):
            w = rng.randrange(6)
            days.append(_day(w, informative=rng.random() < 0.7, rng=rng))
        theta, _ = fit_benter(days)
        assert theta[0] > 0.3

    def test_too_few_events(self) -> None:
        rng = random.Random(0)
        assert fit_benter([_day(1, True, rng) for _ in range(20)]) is None

    def test_combine_is_softmax(self) -> None:
        p = combine(
            np.array([[0.0, 1.0, 0.0]]),
            np.zeros((1, 6)),
            np.log(np.full((1, 6), 1 / 6)),
            np.zeros((1, 6)),
        )
        assert np.allclose(p, 1 / 6)


async def _seed_synthetic(session_factory, start: date, n_days: int, informative: bool) -> None:
    rng = random.Random(42 if informative else 7)
    session = await session_factory()
    for i in range(n_days):
        d = start + timedelta(days=i)
        winner = rng.randrange(6)
        ts = decision_ts_for("NYC", d, "D1E")
        await seed_event(
            session, "NYC", d, winner=winner, candles=[(ts - timedelta(hours=1), [(14, 16)] * 6)]
        )
        center = CENTERS[winner] if informative else CENTERS[rng.randrange(6)]
        session.add(
            ForecastIssuance(
                city=CityEnum.NYC,
                model="NBS",
                run_ts=ts - timedelta(hours=4),
                valid_date=d,
                station="KNYC",
                available_at=ts - timedelta(hours=2),
                tmax_f=center,
                tmax_sd_f=1.5,
            )
        )
    await session.commit()
    await session.close()


class TestBenterEndToEnd:
    async def test_informative_model_trades_and_passes_criterion_5(self, session_factory) -> None:
        """AC3: genuine edge -> the strategy trades and the model criterion passes."""
        start = date(2025, 1, 1)
        await _seed_synthetic(session_factory, start, 160, informative=True)
        strategy = BenterStrategy("B1", model="nbm", decision="D1E")
        eval_start, eval_end = date(2025, 5, 15), date(2025, 6, 9)
        session = await session_factory()
        run = await run_real_backtest(session, strategy, ["NYC"], eval_start, eval_end)
        await session.close()

        assert strategy.fit_log and strategy.fit_log[0]["theta"][0] > 0.3
        assert len(run.results) >= 15
        assert all(f.model_probability is not None for r in run.results for f in r.fills)
        assert sum(r.pnl_cents for r in run.results) > 0
        crit = model_edge_criterion(run.results, strategy.daily_scores, eval_start, eval_end)
        assert crit["log_loss_gain"]["passed"], crit
        assert crit["log_loss_gain"]["mean_gain"] > 0

    async def test_no_information_model_does_not_trade(self, session_factory) -> None:
        """AC2: no information -> combined P ~ market -> no positive 10th-pct edge after costs."""
        start = date(2025, 1, 1)
        await _seed_synthetic(session_factory, start, 160, informative=False)
        strategy = BenterStrategy("B1", model="nbm", decision="D1E")
        session = await session_factory()
        run = await run_real_backtest(
            session, strategy, ["NYC"], date(2025, 5, 15), date(2025, 6, 9)
        )
        await session.close()
        assert abs(strategy.fit_log[0]["theta"][0]) < 0.15
        assert run.results == []


class TestEdgeSlope:
    def _results(self, slope_truth: float, n: int = 400) -> list[CityDayResult]:
        rng = random.Random(9)
        out = []
        for i in range(n):
            price = 40
            edge = rng.uniform(0.0, 0.2)
            p_true = 0.41 + slope_truth * edge
            won = rng.random() < p_true
            f = Fill(
                ticker=f"T{i}",
                side="yes",
                count=1,
                price_cents=price,
                fee_cents=1,
                model_probability=0.41 + edge,
                decision="D1E",
                decision_ts=datetime(2025, 1, 1),
            )
            out.append(
                CityDayResult(
                    city="NYC",
                    event_date=date(2025, 1, 1) + timedelta(days=i),
                    era="E1",
                    fills=[f],
                    pnl_cents=(100 if won else 0) - 41,
                    cost_cents=40,
                    fee_cents=1,
                    contracts=1,
                    expected_taker_cost_cents=2.0,
                    outcomes={f"T{i}": "yes" if won else "no"},
                )
            )
        return out

    def test_calibrated_edges_have_slope_near_one(self) -> None:
        res = edge_slope(self._results(1.0, n=3000))
        assert res["ci95"][0] <= 1.0 <= res["ci95"][1]
        assert res["passed"]

    def test_noise_edges_fail(self) -> None:
        res = edge_slope(self._results(0.0, n=3000))
        assert not res["passed"]


def test_forward_only_and_k() -> None:
    from backend.strategy.registry import FORWARD_ONLY, PREREGISTERED_K

    assert "L5" in FORWARD_ONLY
    assert PREREGISTERED_K == 9


async def test_l5_backtest_refused(session_factory) -> None:
    from backend.research.reports import ReportError, create_report

    session = await session_factory()
    with pytest.raises(ReportError, match="forward-only"):
        await create_report(session, "L5")
    await session.close()
