"""Pre-registered gate evaluation (docs/research/v2-preregistration.md §5-§6).

A counted variant PASSES only if every criterion holds on the development
window. Controls are evaluated against their own expected behaviour instead.
All P&L figures are in cents unless named ``*_usd``.
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np

from backend.backtesting.real_engine import BacktestRun, CityDayResult
from backend.research.scoring import (
    bootstrap_ratio,
    bootstrap_window_sums,
    max_drawdown,
)

ALPHA_FAMILY = 0.05
MIN_TRADED_DAYS = 300
MIN_DAYS_PER_CITY = 40
MIN_SPAN_MONTHS = 6
MAX_DRAWDOWN_CENTS = 1000  # $10
MIN_P5_90DAY_CENTS = -500  # -$5


def _per_date_arrays(
    results: list[CityDayResult], start: date, end: date, value: str = "pnl_cents"
) -> tuple[np.ndarray, np.ndarray]:
    """Per calendar date (including untraded dates): (sum of value, traded city-days)."""
    n = (end - start).days + 1
    numer = np.zeros(n)
    denom = np.zeros(n)
    for r in results:
        i = (r.event_date - start).days
        if 0 <= i < n:
            numer[i] += getattr(r, value)
            denom[i] += 1
    return numer, denom


def summarize(results: list[CityDayResult]) -> dict:
    """Plain descriptive stats of a run (no inference)."""
    n = len(results)
    contracts = sum(r.contracts for r in results)
    pnl = sum(r.pnl_cents for r in results)
    cost = sum(r.cost_cents for r in results)
    fees = sum(r.fee_cents for r in results)
    wins = sum(1 for r in results if r.pnl_cents > 0)
    by_city: dict[str, dict] = {}
    for r in results:
        c = by_city.setdefault(r.city, {"days": 0, "pnl_cents": 0, "contracts": 0})
        c["days"] += 1
        c["pnl_cents"] += r.pnl_cents
        c["contracts"] += r.contracts
    for c in by_city.values():
        c["mean_pnl_per_day_cents"] = round(c["pnl_cents"] / c["days"], 3)
    return {
        "traded_city_days": n,
        "contracts": contracts,
        "pnl_cents": pnl,
        "pnl_usd": round(pnl / 100, 2),
        "cost_cents": cost,
        "fees_cents": fees,
        "roi": round(pnl / (cost + fees), 4) if cost + fees else None,
        "winning_days": wins,
        "mean_pnl_per_day_cents": round(pnl / n, 3) if n else None,
        "mean_pnl_per_contract_cents": round(pnl / contracts, 3) if contracts else None,
        "by_city": by_city,
        "first_date": min(r.event_date for r in results).isoformat() if results else None,
        "last_date": max(r.event_date for r in results).isoformat() if results else None,
    }


def evaluate_gate(
    run: BacktestRun,
    slipped: BacktestRun,
    start: date,
    end: date,
    k: int,
    model_strategy: bool = False,
    daily_scores: list[dict] | None = None,
) -> dict:
    """Evaluate gate criteria 1-6 for a counted variant on the development window.

    Args:
        run: The baseline run (no slippage).
        slipped: The same strategy re-run with +1c slippage.
        start: Window start.
        end: Window end.
        k: Number of pre-registered counted variants (multiple-testing correction).
        model_strategy: Apply the model-only criterion 5 (S4; reported as N/A here).

    Returns:
        Dict with each criterion's value, threshold and pass flag, plus ``passed``.
    """
    results = sorted(run.results, key=lambda r: (r.event_date, r.city))
    alpha = ALPHA_FAMILY / k
    crit: dict[str, dict] = {}

    # 1. Sample size
    per_city: dict[str, int] = {}
    for r in results:
        per_city[r.city] = per_city.get(r.city, 0) + 1
    span_days = (results[-1].event_date - results[0].event_date).days if results else 0
    crit["1_sample_size"] = {
        "traded_city_days": len(results),
        "min_days_per_city": min(per_city.values()) if per_city else 0,
        "span_days": span_days,
        "passed": len(results) >= MIN_TRADED_DAYS
        and len(per_city) == 4
        and min(per_city.values()) >= MIN_DAYS_PER_CITY
        and span_days >= MIN_SPAN_MONTHS * 30,
    }

    # 2. One-sided bootstrap lower bound of mean net P&L per traded city-day
    numer, denom = _per_date_arrays(results, start, end)
    boot = bootstrap_ratio(numer, denom)
    boot = boot[~np.isnan(boot)]
    mean = float(numer.sum() / denom.sum()) if denom.sum() else 0.0
    lower = float(np.quantile(boot, alpha)) if boot.size else float("nan")
    crit["2_mean_pnl_lower_bound"] = {
        "mean_cents": round(mean, 3),
        "alpha_one_sided": alpha,
        "lower_bound_cents": round(lower, 3),
        "passed": bool(boot.size) and lower > 0,
    }

    # 3. Robustness: +1c slippage, and without the best 5% of city-days
    slipped_mean = (
        sum(r.pnl_cents for r in slipped.results) / len(slipped.results) if slipped.results else 0.0
    )
    pnls = sorted((r.pnl_cents for r in results), reverse=True)
    trimmed = pnls[max(1, int(round(len(pnls) * 0.05))) :] if pnls else []
    trimmed_mean = sum(trimmed) / len(trimmed) if trimmed else 0.0
    crit["3_robustness"] = {
        "slippage_1c_mean_cents": round(slipped_mean, 3),
        "without_best_5pct_mean_cents": round(trimmed_mean, 3),
        "passed": bool(results) and slipped_mean >= 0 and trimmed_mean >= 0,
    }

    # 4. Consistency across cities and chronological thirds
    city_means = {
        c: sum(r.pnl_cents for r in results if r.city == c) / n for c, n in per_city.items()
    }
    thirds = []
    if results:
        cut = [results[len(results) * i // 3] for i in range(3)] + [None]
        for i in range(3):
            lo = cut[i].event_date
            hi = cut[i + 1].event_date if cut[i + 1] is not None else date.max
            seg = [r.pnl_cents for r in results if lo <= r.event_date < hi]
            thirds.append(sum(seg) / len(seg) if seg else 0.0)
    crit["4_consistency"] = {
        "city_means_cents": {c: round(v, 3) for c, v in city_means.items()},
        "third_means_cents": [round(v, 3) for v in thirds],
        "passed": sum(1 for v in city_means.values() if v > 0) >= 3
        and sum(1 for v in thirds if v > 0) >= 2,
    }

    # 5. Model-only: edge slope ~ 1 and positive out-of-sample log-loss gain vs market
    if model_strategy:
        crit["5_model_edge"] = model_edge_criterion(results, daily_scores or [], start, end)
    else:
        crit["5_model_edge"] = {"applicable": False, "passed": True}

    # 6. Risk: max drawdown and 5th-percentile 90-day P&L
    dd = max_drawdown([r.pnl_cents for r in results])
    p5 = float(np.quantile(bootstrap_window_sums(numer), 0.05)) if results else 0.0
    crit["6_risk"] = {
        "max_drawdown_cents": round(dd, 1),
        "p5_90day_pnl_cents": round(p5, 1),
        "passed": dd <= MAX_DRAWDOWN_CENTS and p5 >= MIN_P5_90DAY_CENTS,
    }

    passed = all(c["passed"] for c in crit.values())
    return {"criteria": crit, "passed": passed, "k": k}


def edge_slope(results: list[CityDayResult]) -> dict:
    """Regress realized (Y - cost) per contract on predicted edge, SEs clustered by city-day.

    A calibrated edge estimate has slope ~ 1; slope ~ 0 means predicted edge is
    noise (the winner's curse). Contract-weighted OLS with intercept.
    """
    xs, ys, ws, groups = [], [], [], []
    for g, r in enumerate(results):
        for f in r.fills:
            if f.model_probability is None:
                continue
            cost = (f.price_cents + f.fee_cents / f.count) / 100.0
            won = 1.0 if (r.outcomes or {}).get(f.ticker) == f.side else 0.0
            xs.append(f.model_probability - cost)
            ys.append(won - cost)
            ws.append(f.count)
            groups.append(g)
    n = len(xs)
    if n < 10:
        return {"n_fills": n, "slope": None, "ci95": None, "passed": False}
    X = np.column_stack([np.ones(n), np.array(xs)])
    W = np.array(ws, dtype=float)
    y = np.array(ys)
    XtW = X.T * W
    bread = np.linalg.pinv(XtW @ X)
    beta = bread @ (XtW @ y)
    resid = y - X @ beta
    meat = np.zeros((2, 2))
    g_arr = np.array(groups)
    for g in np.unique(g_arr):
        m = g_arr == g
        s_g = (X[m].T * W[m]) @ resid[m]
        meat += np.outer(s_g, s_g)
    cov = bread @ meat @ bread
    se = float(np.sqrt(max(cov[1, 1], 0.0)))
    slope = float(beta[1])
    lo, hi = slope - 1.96 * se, slope + 1.96 * se
    return {
        "n_fills": n,
        "slope": round(slope, 4),
        "ci95": [round(lo, 4), round(hi, 4)],
        "passed": lo <= 1.0 <= hi and lo > 0,
    }


def model_edge_criterion(
    results: list[CityDayResult], daily_scores: list[dict], start: date, end: date
) -> dict:
    """Gate criterion 5 for model strategies (pre-registration §6.5)."""
    slope = edge_slope(results)
    n = (end - start).days + 1
    gain = np.zeros(n)
    count = np.zeros(n)
    for s in daily_scores:
        i = (s["date"] - start).days
        if 0 <= i < n:
            gain[i] += s["ll_market"] - s["ll_model"]
            count[i] += 1
    if count.sum() == 0:
        ll = {"n_city_days": 0, "mean_gain": None, "ci95": None, "passed": False}
    else:
        boot = bootstrap_ratio(gain, count)
        boot = boot[~np.isnan(boot)]
        lo, hi = float(np.quantile(boot, 0.025)), float(np.quantile(boot, 0.975))
        ll = {
            "n_city_days": int(count.sum()),
            "mean_gain": round(float(gain.sum() / count.sum()), 5),
            "ci95": [round(lo, 5), round(hi, 5)],
            "passed": lo > 0,
        }
    return {
        "applicable": True,
        "edge_slope": slope,
        "log_loss_gain": ll,
        "passed": slope["passed"] and ll["passed"],
    }


def evaluate_control(
    strategy_id: str,
    run: BacktestRun,
    start: date,
    end: date,
    real_record_pnl_cents: int | None = None,
) -> dict:
    """Check a control strategy against its pre-registered expected behaviour.

    - C0: no fills and P&L exactly 0.
    - C1: realized mean P&L per contract's 95% bootstrap CI contains the expected
      taker cost -(half spread + fee) computed from the same fills.
    - C2: sign of total P&L equals the sign of v1's real trade record over the window.
    """
    results = sorted(run.results, key=lambda r: (r.event_date, r.city))
    total = sum(r.pnl_cents for r in results)
    if strategy_id == "C0":
        ok = not results and total == 0
        return {"expected": "no trades, P&L 0", "pnl_cents": total, "passed": ok}

    if strategy_id == "C1":
        numer, _ = _per_date_arrays(results, start, end, "pnl_cents")
        contracts, _ = _per_date_arrays(results, start, end, "contracts")
        boot = bootstrap_ratio(numer, contracts)
        boot = boot[~np.isnan(boot)]
        lo, hi = (
            (float(np.quantile(boot, 0.025)), float(np.quantile(boot, 0.975)))
            if boot.size
            else (0.0, 0.0)
        )
        n_contracts = sum(r.contracts for r in results)
        expected = (
            -sum(r.expected_taker_cost_cents for r in results) / n_contracts if n_contracts else 0.0
        )
        return {
            "expected": "mean P&L/contract CI contains -(half spread + fee)",
            "mean_pnl_per_contract_cents": round(total / n_contracts, 3) if n_contracts else None,
            "ci95_cents": [round(lo, 3), round(hi, 3)],
            "expected_cents": round(expected, 3),
            "passed": bool(n_contracts) and lo <= expected <= hi,
        }

    if strategy_id == "C2":
        same_sign = (
            real_record_pnl_cents is not None
            and results
            and (total > 0) == (real_record_pnl_cents > 0)
            and (total < 0) == (real_record_pnl_cents < 0)
        )
        return {
            "expected": "P&L sign matches v1's real trade record",
            "replica_pnl_cents": total,
            "real_record_pnl_cents": real_record_pnl_cents,
            "passed": bool(same_sign),
        }

    return {"expected": "unknown control", "passed": False}


def holdout_mean(run: BacktestRun) -> dict:
    """Gate criterion 7 (holdout, used once): mean net P&L per traded city-day >= 0."""
    n = len(run.results)
    mean = sum(r.pnl_cents for r in run.results) / n if n else 0.0
    return {
        "traded_city_days": n,
        "mean_pnl_per_day_cents": round(mean, 3),
        "passed": n > 0 and mean >= 0,
    }


DEV_WINDOW = (date(2024, 7, 1), date(2026, 6, 30))
HOLDOUT_START = date(2026, 7, 1)


def holdout_window(today: date) -> tuple[date, date]:
    """Holdout = event dates from 2026-07-01 to two days before ``today`` (settled)."""
    return HOLDOUT_START, today - timedelta(days=2)
