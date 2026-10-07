# Algo v2 — Pre-registration (frozen 2026-10-07, before any archived data was examined)

> **Purpose:** fix in writing, *before* looking at results, every strategy variant we will test,
> how it is scored, and what counts as passing. This limits multiple-testing and
> garden-of-forking-paths bias. **Changing anything below requires a dated amendment at the
> bottom of this file, and the amendment increases K. Nothing is deleted.**

## 1. Data windows

| Window | Event dates | Use |
|---|---|---|
| Excluded | < 2024-07-01 | Spreads too wide (median widest spread 79¢ in 2023) |
| **Development** | 2024-07-01 → 2026-06-30 | All strategy evaluation and tuning |
| **Holdout** | ≥ 2026-07-01 | Includes all of era E2 (≥ 2026-08-14, Weather Company settlement). Evaluated **once per strategy**, only after its development verdict is PASS |

- **Model-based strategies** need a 365-day training warm-up, so their development evaluation
  window is **2025-07-01 → 2026-06-30**.
- **Labels:** Kalshi `result` / `expiration_value` only.
- **Label lag:** a decision for event day t may only use labels with `event_date ≤ t−2`.

## 2. Decision times (local civil time at the station, via `ZoneInfo`)

- **D1E** = 17:00 the day before the event
- **D1L** = 20:00 the day before the event
- **D0M** = 10:00 on the event day

Each snapshot uses the latest hourly candle whose `end_ts ≤ decision_ts`, and only markets
with `open_time ≤ decision_ts`.

## 3. Execution model (identical for every strategy)

- **Taker fills:**
  - buy YES at `yes_ask`;
  - buy NO at `100 − yes_bid`;
  - fee per order = `ceil(7·count·p·(100−p)/10000)` cents, where p is the price paid.
- **Not fillable:** no fill if bid = 0, ask = 100, or the quote is missing.
- **Sizing cap:** total cost plus fees ≤ **$4.00 per city-day** across all orders in that
  city-day. Integer contracts only.
- **Maker fills** (fee rate 1.75; fill only if a later candle in the same market trades
  strictly through the limit price): **reported but not eligible to pass the gate.**

## 4. Strategy variants

### Controls (not counted in K, never eligible)

| ID | Description | Expected result |
|---|---|---|
| C0 `null` | Never trades | P&L exactly 0 |
| C1 `random_taker` | 1 random bracket and side per city-day at D1E, 1 contract | Mean ≈ −(½ spread + fee) |
| C2 `v1_replica` | v1 rule on stored `predictions` (`generated_at ≤ decision_ts`), v1 settings | Same P&L sign as the real v1 record over the same window |

**If any control fails its expected result, the harness is considered broken. Nothing else is
evaluated until it is fixed.**

### Counted variants (K = 8)

**Market-only: longshot NO fade.** Buy NO on every bracket whose YES bid is ≤ X¢ and > 0, at
decision time T. Spread $4 across qualifying brackets, cheapest-risk first.

| ID | X | T |
|---|---|---|
| L1 | 2¢ | D1E |
| L2 | 4¢ | D1E |
| L3 | 2¢ | D1L |
| L4 | 4¢ | D1L |

**Model-based: Benter-style market-anchored combination.**
- Combination: `P_i = softmax_i(α·log p_m,i + β·log q_i + γ·tail_i)`, where
  - q = mid prices normalised across the event's brackets;
  - p_m = the model's bracket probabilities.
- Fitting: walk-forward on [t−365, t−2], with fixed priors α ~ N(0, 0.25²), β ~ N(1, 0.25²),
  γ ~ N(0, 0.5²).
- Uncertainty: Laplace posterior, 200 draws.
- Trade rule: trade bracket i on side s only if the 10th percentile of
  `edge = P − cost − fee/contract` is > 0.
- Sizing: fractional Kelly λ = 0.1, capped by the $4 rule. **No tuning of the priors.**

| ID | Model p_m | T |
|---|---|---|
| B1 | NBM-direct: N(NBS `txn`, NBS `xnd`) → brackets | D1E |
| B2 | NBM-direct | D0M |
| B3 | Station EMOS (NGR, min-CRPS, weights ≥ 0) on NBS + GFS MOS + NAM MOS | D1E |
| B4 | Station EMOS | D0M |

**Significance level:** α_family = 0.05 one-sided, so **per-variant α = 0.05 / 8 = 0.00625**.

## 5. Scoring

- **Unit of analysis:** the city-day.
- **Uncertainty:** stationary block bootstrap over city-days, mean block length 7 days, 5,000
  resamples.
- **Model and market scores** on every bracket: log loss, Brier, RPS.
  - Market probability = mid prices normalised across the event's brackets.
  - Model vs market is compared as a paired difference per city-day.
- **Edge slope:** regress realised `(Y − cost)` per traded contract on predicted edge, with
  standard errors clustered by city-day.

## 6. Gate — a counted variant PASSES only if every item holds on the development window

1. ≥ 300 traded city-days, ≥ 40 in each city, spanning ≥ 6 calendar months.
2. Bootstrap lower bound (one-sided, α = 0.00625) of mean net P&L per traded city-day is > 0.
3. The mean is still ≥ 0 under each of:
   - +1¢ slippage on every fill;
   - removing the best 5% of city-days.
4. The mean is > 0 in ≥ 3 of 4 cities, and in ≥ 2 of 3 chronological thirds of the window.
5. **B-variants only:**
   - the 95% CI of the edge slope contains 1 and excludes 0;
   - the 95% CI of the paired log-loss gain over the market excludes 0.
6. Simulated max drawdown ≤ $10, and the bootstrap 5th-percentile 90-day P&L ≥ −$5.
7. **Then, once:** the holdout mean net P&L per city-day is ≥ 0.

A variant that passes 1–7 is eligible for paper trading (S5). Promotion to live needs the paper
criteria in `docs/ALGO_V2_PRD.md` §6.

## 7. Amendments

*(none)*
