# Algorithm & Trading-Logic Changelog

> **Purpose:** A running history of every change to *how the bot predicts and decides trades* — the
> prediction pipeline, probability model, EV/risk logic, and order execution — paired with its
> measured effect on live performance. This is the reference to read before touching prediction or
> trading logic, and before any performance review, so we know what changed and whether it helped.
>
> This is **not** a general release log. Only algo/trading-behavior changes belong here. UI, infra,
> monitoring, and docs changes stay in GitHub Releases.
>
> **Keep this current:** whenever a change alters prediction, probability, EV, sizing, risk, or
> order-execution behavior, add a row. When we run a performance review, append a dated snapshot to
> the *Performance Reviews* section.

## Current state (as of last review 2026-08-21)

- **Deployed version:** v1.9.13 live on the VM; **v1.9.14 built but NOT yet deployed**
- **Balance:** $80.27 (was $78.06 on 2026-08-06)
- **Verdict:** The v1.9.12 bracket-bounds fix is **verified correct and live**, but it did **not**
  restore the edge. Era E (Aug 7-21) is still all-NO and still losing on a small sample.
- **Open problems (in priority order):**
  1. ~~**Calibration was off or half-on for most of Era E**~~ — **FIXED in v1.9.14**
     (mtime-based reload in the `pipeline._get_*` loaders). Not yet deployed.
  2. **`_collect_pairs` treats intraday duplicate predictions as independent samples**
     (~19.5 rows/day/city, so NYC "sample_count 9900" is really ~90 independent days).
     `error_dist.py` and `bias_correction.py` both dedupe per day; calibration does not.
  3. **The AUS calibration curve emits a hard `0.0` probability** on 477/480 recent predictions.
     Latent while AUS is disabled; a landmine if it is re-enabled (a 0-probability bracket makes a
     NO bet look risk-free).
  4. **Per-bracket cap leaks** - `_get_open_bracket_qty` ignores `RESTING`.
- **NOT a problem (retracted):** `error_std` is *not* too tight - see the 2026-08-21 review.
- **Trading scope:** NYC only, `min_ev_threshold_no` 6%, `min_ev_threshold_yes` 12%.

---

## Change history (algo-affecting)

### v1.9.14 — Fix cross-process model-cache staleness (2026-08-21)
- **Files:** `backend/prediction/pipeline.py`
- **What:** the cached loaders in `pipeline.py` (`_get_calibration_curves`,
  `_get_source_weights`, `_get_ml_ensemble`) now compare the **mtime** of their backing file on
  every call and reload when it changes. `reload_models()` still runs after a retrain, but it is
  no longer the only refresh path.
- **Why:** Celery runs a prefork pool (`--concurrency=2`). `reload_models()` clears module-level
  globals **only in the child that ran the training task**, so a sibling child kept serving the
  cache it built at startup until the container restarted. After v1.9.12 invalidated the
  calibration file on 2026-08-06, that meant **0% of predictions were calibrated on Aug 8 and only
  ~47% from Aug 10–15** — the bot ran most of Era E without the layer that made Era C profitable.
  Recovery on Aug 16–17 was accidental (a refit happened to land in the stale child). See the
  2026-08-21 review, "ROOT CAUSE A".
- **Expected effect:** calibration coverage stays at ~100% instead of drifting to ~50% after any
  restart-free deploy that invalidates a model artifact. **No change to the probabilities
  themselves when caches are already fresh** — this restores intended behavior rather than
  altering it, so a clean read of its effect is just "calibrated share per day returns to ~100%".
- **Verify after deploy:** run the isotonic-plateau query from the 2026-08-21 review; the
  calibrated share should be ~100%/day, not ~47%.

### v1.9.12 — Fix bracket-bounds parsing + calibration reset (2026-08-06)
- **Files:** `backend/kalshi/markets.py`, `backend/prediction/probability_calibration.py`
- **What:** `parse_bracket_from_market` now emits **continuous half-degree bounds** matching
  Kalshi's shared-boundary integer-strike convention: middles cover two integer temps
  ([88.5, 90.5) for "89° to 90°"), the bottom cap and top floor are exclusive shared boundaries,
  and the top catch-all label is corrected (+1: floor=96 → "97°F or above", matching Kalshi's
  display). `parse_event_markets` warns if the parsed ladder ever stops tiling (guard against
  future convention changes). Calibration: curves are stamped with `bounds_version`; files fitted
  pre-fix are ignored on load (identity until refit), and the fit skips stored predictions with
  pre-fix 1°F-wide brackets so curves retrain only on clean data.
- **Why:** Root cause of the Era D bleed — raw strikes passed straight to the CDF made middle
  brackets 1°F wide with phantom gaps, roughly halving middle-bracket model probabilities and
  driving perpetual "model ~20% vs market ~50%" NO bets. See the 2026-08-06 review below.
- **Expected effect:** middle-bracket probabilities roughly double → most fade-the-favorite NO
  signals stop clearing the 6% EV threshold; trade count should drop sharply and the promised-EV
  vs realized-ROI gap should close. Also deployed alongside: active_cities reduced to NYC only
  (the one city where the model beats the market per `/api/accuracy/edge`).

### v1.9.7 — Student-t error distribution + full-pipeline error std (2026-05-10)
- **Files:** `backend/prediction/error_dist.py`, `backend/prediction/brackets.py`,
  `backend/prediction/pipeline.py`
- **What:** Bracket CDF switched from Normal to **Student's t (df=10)** for heavier tails at the same
  scale. `error_dist.py` now measures the std of the *full-pipeline* output
  (`Prediction.ensemble_mean_f` — the blended ensemble + ML + bias-corrected value) vs realized highs,
  instead of a narrower upstream signal. Fallback stds retained for the first ~30 days (bootstrap).
- **Why:** Normal tails under-priced surprise outcomes; measuring error on the actual blended output
  makes the CDF spread reflect the variance the brackets truly face.

### v1.9.6 — Per-city probability calibration layer (2026-05-10)
- **Files:** `backend/prediction/probability_calibration.py` (new, ~292 lines),
  `pipeline.py`, `train_models.py`
- **What:** Fits a **non-parametric isotonic regression** curve per city mapping raw predicted bracket
  probabilities → actual historical hit rates, learned from joined `Prediction × Settlement` rows.
  Applied to bracket probabilities before they leave the pipeline, then renormalized to sum to 1.0.
  Persisted to `probability_calibration.json` (in the `modeldata` volume, alongside
  `source_weights.json` / `ml_weights.json`). Refit weekly during `train_all_models` (Sun 3 AM ET)
  and on every manual `/api/training/trigger`.
- **Why:** At v1.9.5 the 0.7–0.9 probability buckets were firing roughly *half* as often as predicted
  — the model was systematically overconfident. This is the core fix behind the profitability flip.

### v1.9.5 — "Stop the bleed": calibration prep (2026-05-09)
- **What:** Preparatory fix laying groundwork for the calibration layer (diagnostics + plumbing to
  join predictions against settlement outcomes).

### (local commit, untagged) — ML acceptance threshold 5.0 → 7.0°F RMSE (2026-04-07)
- **Files:** `backend/prediction/ml_models.py`
- **What:** Raised the RMSE bar at which a trained ML sub-model (XGBoost/RF/Ridge) is accepted into
  the ensemble from 5.0 to 7.0°F, letting more models contribute rather than falling back to stats.

### v1.9.4 — Rolling bias correction for ensemble predictions (2026-03-31)
- **What:** Applies a rolling per-city bias offset (measured over a trailing ~14-day window) to the
  ensemble mean before bracket probabilities are computed, correcting persistent directional error.

### Execution changes (affect fills, not prediction)
- **v1.9.10 (2026-06-20)** — `cancel_order` migrated to Kalshi v2 endpoint.
- **v1.9.9 (2026-06-20)** — Cancel stale resting orders to restore 14-min auto-expiry.
- **v1.9.8 (2026-06-20)** — Order placement migrated to Kalshi v2 endpoint.

---

## Performance Reviews

### 2026-08-21 — Era E two-week check (fix verified, edge NOT restored)

Two-week check-in on v1.9.12/v1.9.13 (deployed 2026-08-06, Era E starts event date 2026-08-07).
Analysis of **3,000 settled trades** pulled from `GET /api/trades?status=SETTLED`, plus live
`/api/logs`, `/api/accuracy/calibration`, and `/api/training/reports`.

**1. The bracket-bounds fix works — verified numerically.** Recomputed a live NYC log line
(mean 76.6, std 2.08, df 10, brackets 76-or-below … 85-or-above) under both hypotheses:

| Hypothesis | Probabilities | Max abs error vs logged |
|---|---|--:|
| **Fixed (2°F-wide, half-degree)** | 0.4813, 0.3275, 0.1461, 0.0363, 0.0071, 0.0017 | **0.0023** |
| Buggy (1°F-wide raw strikes) | 0.5996, 0.2580, 0.1095, 0.0260, 0.0050, 0.0018 | 0.1160 |

The fixed hypothesis matches; the buggy one is decisively rejected. Trade volume also fell as
predicted: **8.5/day (Era D last 30d) → 3.8/day (Era E)**, trading on only 5 of 14 days.

**2. But performance did not recover.** Era E is a small sample — treat P&L as weak evidence:

| Era | Trades | Win rate | P&L | ROI | EV gap |
|---|--:|--:|--:|--:|--:|
| C May 10 – Jun 19 (calibration) | 655 | 60.2% | +$23.86 | +6.6% | +0.2pp |
| D Jun 20 – Aug 6 (bounds bug) | 492 | 48.2% | −$10.95 | −4.6% | −11.0pp |
| **E Aug 7 – Aug 21 (post-fix)** | **19** | **21.1%** | **−$4.88** | **−57.1%** | **−63.5pp** |

Model-vs-market Brier edge got *worse*, not better: Era D NYC −0.0586 → **Era E NYC −0.2103**.
All 19 Era E trades are still **NO-side**. The structural findings below are much stronger evidence
than these 19 trades.

**3. ROOT CAUSE A - calibration was off, then half-on, for most of Era E (Celery prefork).**
*(This supersedes an earlier draft of this review that claimed calibration was entirely off. It is
not - see the correction note below.)* Counting isotonic plateaus (two brackets sharing an exactly
equal probability, which a raw t-CDF essentially never produces) in the stored `predictions` rows:

| Day | Predictions | Calibrated |
|---|--:|--:|
| Aug 1-6 (pre-fix) | 384/day | 268-354 (70-92%) |
| Aug 7 | 384 | 125 |
| **Aug 8** | 384 | **0** |
| Aug 9 | 384 | 91 |
| **Aug 10-15** | 384/day | **~180 (~47%)** |
| Aug 16 | 384 | 257 |
| Aug 17-22 | 384/day | 348-367 (~91%) |

The ~47% plateau is the signature: the worker runs `--concurrency=2` and has been **Up 2 weeks**.
`train_all_models` calls `pipeline.reload_models()`, which resets *module-level globals in the
calling process only*. v1.9.12 rejected the stale file on Aug 6, so both children cached
"no calibration"; each subsequent refit (Aug 9, Aug 16) healed whichever child happened to run it.
Recovery was accidental, not designed - and a restart-free deploy will reproduce it.

**Observability trap that caused the initial misdiagnosis:** the INFO log
`"Bracket probabilities calculated"` reports **pre-calibration** probabilities. The calibrated
values appear only in the DEBUG line `"Probability calibration applied"` and in `brackets_json`.
Verified on the 2026-08-21 15:05 NYC cycle - logged `[0.4836, 0.3262, ...]` vs stored
`[0.5131, 0.2915, ...]`. Anyone debugging calibration from INFO logs will conclude it is off.

**4. RETRACTED - `error_std` is NOT too tight.** An earlier draft of this review claimed the CDF
was too narrow, comparing live `error_std` against ML test RMSE of 2.9-4.3 F. **That comparison was
invalid**: those RMSEs pool all four seasons and all forecast horizons, while `error_std` is
season- and day-of-specific. Measuring the actual day-of weighted source-ensemble error for
**summer only** (n~81/city, the same slice `error_dist` uses) gives the opposite result:

| City | Measured summer day-of sigma | Live `error_std` | |
|---|--:|--:|---|
| NYC | 1.55 | 2.08 | live is 1.34x **wider** |
| CHI | 1.07 | 1.70 | 1.59x wider |
| MIA | 1.01 | 1.19 | 1.18x wider |
| AUS | 1.01 | 1.47 | 1.46x wider |

`error_dist.calculate_error_std` is behaving correctly and conservatively. Its per-day averaging
(`func.avg` + `group_by date`) - flagged as a bug in the earlier draft - is the **correct** pattern,
matching `bias_correction.calculate_rolling_bias`.

**4b. The real calibration defect: sample independence.** `_collect_pairs` does *not* dedupe by day,
unlike `error_dist` and `bias_correction`. It feeds every intraday prediction to the isotonic fit as
an independent observation. For the Aug 16 refit, NYC reported `sample_count = 9900` - but that is
**1,757 prediction rows over only 90 distinct days** (~19.5 near-identical rows per day). So
`MIN_SAMPLES_PER_CITY = 200` is satisfied by roughly ten days of data, and the isotonic curve is far
less constrained than its sample count suggests. Consequences visible in production:

- **AUS emits a hard `0.0`** on **477 of 480** predictions since Aug 17 (curve `y_range` starts at
  0.0). A zero-probability bracket makes a NO bet look risk-free to the EV calculator. AUS is
  currently disabled, so this is latent - but it is the same cell that produced Era D's worst losses.
- CHI/MIA curves were fitted with `y_range` capping at 0.50 / 0.556.
- NYC is the healthiest (no zeros, max prob 0.9546) - fortunate, as it is the only city trading.

**5. Weather sources - five is more than the accuracy justifies.**
Day-of forecast vs settled actual, latest fetch per city/day, 446 city-days with all five present:

| Source | MAE | RMSE | Bias | Live weight |
|---|--:|--:|--:|--:|
| NWS | 2.49 | 4.29 | -0.02 | 0.229 |
| NWS:gridpoint | 2.41 | 4.27 | -0.09 | 0.226 |
| Open-Meteo:ECMWF | 3.25 | 4.57 | -0.97 | 0.175 |
| Open-Meteo:GFS | 2.67 | 4.28 | -0.29 | 0.176 |
| Open-Meteo:ICON | 2.75 | 4.21 | -0.81 | 0.193 |

- **NWS and NWS:gridpoint are effectively one source**: error correlation **0.983**, level
  correlation **0.9967**, identical on **80%** of days, mean absolute difference **0.32 F**. They
  jointly hold **45.5%** of ensemble weight, so the ensemble is really "NWS twice + three others".
- All five error series correlate **0.86-0.98**, so diversification gains are inherently small.
- **Leave-one-out (paired t-test on squared error):** only **ICON** matters - dropping it costs
  +0.047 F RMSE, *p* = 0.004. Dropping NWS (+0.001, *p* = 0.97), gridpoint (+0.013, *p* = 0.62)
  or GFS (+0.012, *p* = 0.38) is not significant, and dropping **ECMWF improves** MAE by 0.100
  (*p* = 0.55).
- **Best 3-source subset (gridpoint + GFS + ICON) beats all five**: RMSE 4.098 vs 4.135.

**Conclusion:** the 4th and 5th sources buy no measurable accuracy. The defensible reason to keep
them is **failure tolerance**, which is not hypothetical - Aug 18-21 logged 104 x Open-Meteo 503 and
86 x "Missing temp_max". Recommended: keep ICON (the only significant contributor) + one NWS feed +
GFS as the working ensemble, retain the rest as hot spares, and stop giving two copies of NWS 45% of
the weight.

**6. Per-bracket position cap leaks.** `_get_open_bracket_qty` counts only
`Trade.status == TradeStatus.OPEN`, ignoring `RESTING`. On Aug 10 the bot accumulated **10 contracts
on one bracket** ("88F or below") against `max_contracts_per_bracket = 5` - 5 fills at 02:45-03:45
and 5 more at 15:45-17:00. All 10 lost (-$4.69, the bulk of Era E's loss).

**7. Data-quality degradation (Aug 18-21).** 104 x "Open-Meteo returned 503, retrying" and 86 x
"Missing temp_max in Open-Meteo response", plus 10 Kalshi WebSocket reconnects. Intermittent, not
down - all 5 sources were present in the Aug 21 cycles.

> Next review: re-check (a) the calibrated-prediction share per day (the plateau query above - it
> should be ~100%, not ~47%), (b) whether AUS still emits `0.0` probabilities, (c) whether any
> YES-side trades appear. The EV gap remains the key health metric. Note: read calibrated values
> from `brackets_json`, **not** from the `"Bracket probabilities calculated"` INFO log.

### 2026-08-06 — last-month deep dive (Period D watch item CONFIRMED)

Analysis of **2,957 settled trades**; focus on Jul 7 – Aug 5. Codebase unchanged since Jun 20
(v1.9.10), so all drift below happened on constant code. Balance fell $97.27 → $78.06 since Jul 15.

| Window | Trades | Win rate | P&L | ROI | EV gap |
|---|--:|--:|--:|--:|--:|
| Jul 7 – Jul 26 | 177 | 50.8% | +$1.50 | +1.8% | −4.7pp |
| **Jul 27 – Aug 5 (last 10 days)** | 69 | **34.8%** | **−$10.78** | **−32.0%** | **−38.4pp** |
| Era C (May 10 – Jun 19, calibration) | 655 | 60.2% | +$23.86 | +6.6% | +0.2pp |
| Era D (Jun 20 – now, updated) | 468 | 48.7% | −$7.63 | −3.4% | −9.8pp |

**Findings:**
1. **Losing-trade signature:** NO bets at ~50¢ where model says ~20% but market says ~50%, and the
   bracket **hits** (e.g. Aug 4 AUS 100–101°F: model 18%, market 54%, actual 100°F). The model is
   under-forecasting peak summer highs; the market is right on these coin-flips.
2. **Confidence label inverted (all of Era D):** `high` = 155 trades, ~38% WR, −$18.85;
   `medium` = 313 trades, ~54% WR, +$11.22. Skipping `high` trades would have made Era D profitable.
3. **All 468 Era D trades are NO-side.** YES thresholds (12% EV + 15% market floor) shut YES off.
4. AUS worst recently (last 10d: −$7.68 @ 29.7% WR); NYC also flipped negative.

**Root-cause investigation (same day):**

1. **BUG FOUND — bracket bounds parsing (`kalshi/markets.py: parse_bracket_from_market`).**
   Kalshi sends *integer* cap strikes for these markets (e.g. floor 89.0 / cap 90.0 for
   "89° to 90°F", which covers integer temps 89 **and** 90 ≈ continuous [88.5, 90.5)). The parser
   passes floor/cap straight through as CDF bounds, so middle brackets are treated as **1°F wide
   instead of 2°F** with phantom gaps between brackets. Verified numerically: recomputing the live
   NYC log line (mean 90.1, std 1.94, df 10) with the buggy 1°-wide bounds reproduces the logged
   probabilities almost exactly ([0.252, 0.230, 0.320, 0.153, 0.035] vs logged
   [0.248, 0.227, 0.316, 0.151, 0.034]); no plausible correct-bounds distribution does.
   Effect: middle-bracket model probabilities are roughly **halved** before normalization → the
   engine sees "model ~20% vs market ~50%" on nearly every mid-priced bracket → all-NO strategy
   fading the market favorite. Feb 24 commit `bb84e9c` fixed the *label* for integer caps but never
   the CDF bounds. The bug is **chronic** (weekly avg model-prob on these trades has been 17–26%
   all along) — Era C's profit came from volatile spring weather where fading the favorite won
   anyway; stable summer weather turned the same structural bet into a bleed.
2. **Confidence inversion explained — it's one cell.** Era D high-conf: AUS N=94 WR 31% −$18.38;
   all other high-conf cells ≈ breakeven (NYC −$0.02, CHI +$0.03, MIA −$0.48). AUS-medium is the
   *best* cell (+$7.39, 70% WR). "High" = tight forecast spread + low summer std → concentrates on
   stable AUS extreme-heat days, where the distorted model keeps fading 100°F+ brackets that hit.
   The label is a regime proxy, not a causal defect.
3. **Refits ARE running.** TrainingReports #105–107 (Jul 31, Aug 2, Aug 6) all completed: 3 models
   accepted, source weights updated, calibration refit in-task (`train_models.py` Step 4b) and
   caches invalidated (Step 6). Rolling bias live (+0.92°F NYC on Aug 6). Stored predictions show
   isotonic plateaus (e.g. three brackets at 0.0076) — calibration is being applied. Not the cause.
4. **Model edge report** (`/api/accuracy/edge`, N=1199): market Brier beats model in MIA (−0.056),
   AUS (−0.059), CHI (−0.015); model beats market only in NYC (+0.052).

### 2026-07-15 — first tracked review (baseline)

Analysis of **2,781 settled trades** (Feb 20 – Jul 14), bucketed by the algo era in force at each
trade's event date. Source: `GET /api/trades?status=SETTLED` on the live VM.

| Period | Change in force | Trades | Win rate | P&L | ROI | Promised EV | Realized ROI | Gap |
|---|---|--:|--:|--:|--:|--:|--:|--:|
| A Feb 20–Mar 30 | baseline | 985 | 50.1% | −$57.99 | −10.6% | +9.4% | −10.6% | −20.0pp |
| B Mar 31–May 9 | bias + ML threshold | 849 | 59.4% | −$34.40 | −6.5% | +6.3% | −6.5% | −12.8pp |
| **C May 10–Jun 19** | **calibration + t-dist** | 655 | 60.2% | **+$23.86** | **+6.6%** | +6.4% | +6.6% | **+0.2pp** |
| D Jun 20–Jul 14 | Kalshi-v2 execution | 292 | 51.0% | +$1.57 | +1.1% | +6.5% | +1.1% | −5.4pp |

Monthly P&L: Feb −$34.10, Mar −$13.58, Apr −$30.17, May −$11.70, **Jun +$12.46, Jul +$10.13**
(June & July are the first two profitable months; July running ~+15% ROI).

**Takeaways:**
1. The **calibration + t-distribution overhaul (May 10) is the inflection point** — it closed a 20pp
   overconfidence gap and flipped the bot to profitable. Strongest evidence: promised-EV vs
   realized-ROI gap collapsed from −20pp/−12.8pp to +0.2pp.
2. Trade volume fell each era (985→849→655→292) — the bot became **more selective**, as intended.
3. Still **net-negative cumulatively** (dug a ~−$67 hole in Periods A–B) but climbing since May.
4. **Watch Period D:** win rate fell to 51% and the EV gap reopened to −5.4pp after the execution
   migration. Small sample (292 trades / 24 days) — likely noise, but could be worse fills from the
   v2 order path. Re-check next review.

> Next review: pull `GET /api/trades?status=SETTLED`, page through all, bucket by event date against
> the eras above, and compare win rate / ROI / (promised EV − realized ROI) gap per era. The EV gap
> is the key health metric — it should stay near zero.
