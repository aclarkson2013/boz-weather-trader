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

## Current state (as of last review 2026-08-06)

- **Deployed version:** v1.9.10 (homelab VM at `10.0.0.175`, live/`auto` mode, not demo)
- **Verdict:** The **per-city probability calibration + Student-t error model (v1.9.6/v1.9.7,
  shipped 2026-05-10)** remains the change that turned the bot profitable (Era C: +6.6% ROI,
  EV gap +0.2pp). However, the **post-Jun-20 era (D) has erased the edge** — see the 2026-08-06
  review. The July watch item is now confirmed as a real problem, not noise.
- **Fix shipped (2026-08-06):** v1.9.12 corrects the bracket-bounds bug behind items 1–3 below and
  resets the calibration layer; trading narrowed to NYC only while we watch the post-fix EV gap.
- **Active problems (2026-08-06, pre-v1.9.12):**
  1. Last 10 days (Jul 27–Aug 5): 34.8% win rate, −$10.78, EV gap −38pp. Model is under-forecasting
     peak summer highs (AUS 100°F+, NYC 83–84°F) and losing NO bets on brackets the market prices
     ~50% that then hit.
  2. The `high` confidence bucket is **anti-predictive** across all of Era D (~38% WR, −$18.85 over
     155 trades) while `medium` is profitable (+$11.22). Under investigation.
  3. 100% of Era D trades are NO-side — the 12% YES threshold + 15% YES floor shut off YES entirely.

---

## Change history (algo-affecting)

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
