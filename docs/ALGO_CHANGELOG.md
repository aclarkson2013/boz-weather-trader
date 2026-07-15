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

## Current state (as of last review 2026-07-15)

- **Deployed version:** v1.9.10 (homelab VM at `10.0.0.175`, live/`auto` mode, not demo)
- **Verdict:** The **per-city probability calibration + Student-t error model (v1.9.6/v1.9.7,
  shipped 2026-05-10)** is the change that turned the bot from consistently losing to profitable.
  It fixed a systematic **overconfidence** problem: pre-calibration the model promised +9% EV while
  realizing −11% ROI (a 20pp gap); post-calibration promised EV ≈ realized ROI within 0.2pp.
- **Watch item:** After the Kalshi-v2 execution migration (v1.9.8–v1.9.10, 2026-06-20) the realized-
  vs-promised EV gap re-widened to −5.4pp on a small sample. Possibly fill quality; re-check.

---

## Change history (algo-affecting)

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
