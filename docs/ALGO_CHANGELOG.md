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

## Current state (as of last review 2026-10-07)

- **Deployed version:** v1.9.16, **`trading_mode: manual` since 2026-08-28** (no live trades
  since), 4-source ensemble (NWS:gridpoint, ECMWF, GFS, ICON), NYC only.
- **Balance:** $66.76 (was $71.76 on 2026-08-28; $2.90 of the drop is the last 6 settled trades,
  ~$2.10 is unreconciled against the DB — check Kalshi portfolio history).
- **Verdict (2026-10-07):** the pause was the right call. The 237 queued-but-unexecuted signals
  (a free paper record) would have lost money: capped the way auto mode trades, **106 contracts,
  42.5% WR, −$6.03, −12.3% ROI** vs +6.4% promised. Market Brier 0.209 vs model 0.337 on those
  signals. The loss is concentrated in **bottom catch-all ("X°F or below") NO fades: 4 wins in
  41, −79.9% ROI** — the model's NYC ensemble ran ~1°F warm all of September, so it
  systematically under-prices the cold bracket. Middle-bracket fades were +19.9% ROI on 65
  contracts but only ~20 independent days, and their Brier is a dead heat with the market
  (−0.004) — not evidence of edge. See the 2026-10-07 review.
- **New problems found 2026-10-07:** (a) the "edge gate" metric we planned to watch
  (`/api/accuracy/edge`) is **trade-based, so it has had zero samples since the pause** and can
  never show recovery while paused; (b) that endpoint scores NO trades' `market_probability`
  (stored NO-side) against the YES outcome — a sign bug (small on history: market Brier 0.257 →
  0.248 corrected, 90d); (c) **the VM was down ~4.5 days (Oct 2 00:15 → Oct 6 18:39 UTC)** with
  no alert, because Alertmanager runs on the same VM; NYC settlements for Oct 1–4 are missing.

- **Next: Algo v2** (approved 2026-10-07) — measure-first rebuild: real-price archive + backtester,
  pre-registered strategies and gate, paper trading before any money. See `docs/ALGO_V2_PRD.md`.

### Previous verdict (2026-08-28)

- **Verdict:** **No green day since Aug 14.** Era F (post-v1.9.16, Aug 22→) is 1-for-17, −$7.87.
  The infrastructure fixes all verified, but the model has **no edge over the market** — the
  market's Brier beats the model's in **all four cities even over 90 days**, and every trade the
  bot places is a max-divergence NO clamp (uniform EV 0.064–0.065 is the arithmetic signature).
  The bot's trade filter selects precisely the model's worst errors (adverse selection). See the
  2026-08-28 review.
- **Open problems (in priority order):**
  1. **Negative model edge, structurally selected**: the bot only ever trades brackets where the
     model disagrees with the market by ≥25pp, and on that subset the market is consistently
     right (7d traded-subset Brier: model 0.587 vs market 0.259). No threshold tweak fixes this;
     it needs either a model-accuracy fix or a pause.
  2. **Regime lag**: late-Aug NYC cool-down; the enabled sources over-forecast highs 1–5°F nearly
     every day Aug 21–27. Rolling bias (14d mean) reads +0.29°F — a mean over mixed regimes
     cannot correct a fast regime shift, so the correction did nothing while trades clustered on
     the warm-miss days.
  3. **ECMWF (dropped in v1.9.16) was the best NYC source Aug 22–27** (+0.5..+1.2°F vs
     −1.5..−5.9°F for the enabled three); GFS (kept) was the worst. Re-enabling ECMWF is a
     one-toggle, reversible change backed by continuous accuracy history.
  4. **`_collect_pairs` treats intraday duplicate predictions as independent samples** (unchanged).
  5. **The AUS calibration curve emits a hard `0.0` probability** (latent while AUS disabled).
  6. **Per-bracket cap leaks** - `_get_open_bracket_qty` ignores `RESTING`. (Cap 5 held in Era F,
     but 5 stacked contracts per wrong opinion is still the loss multiplier: 15 of 17 Era F
     trades were 3 brackets × 5 contracts.)
- **NOT a problem (retracted):** `error_std` is *not* too tight - see the 2026-08-21 review.
- **Trading scope:** NYC only, `min_ev_threshold_no` 6%, `min_ev_threshold_yes` 12%.

---

## Change history (algo-affecting)

### v1.14.3 — Faster, smaller deploys: keep the dependency layer cached (2026-10-09) — *infra only*

The v1.14.2 self-update **timed out and never deployed** on 2026-10-09:

- `Dockerfile.backend` copied `VERSION` before `pip install`, so every release busted the layer
  cache and reinstalled every package. That took 33 min on the homelab VM, and writing the
  2.2 GB image took another 38 min, which hit the updater's 1-hour limit.
- Each release had also left a 2.2 GB dangling image behind. The disk grew from 31 to 51 GB (83%)
  in two days, which slowed I/O further.

**Fix:** only `pyproject.toml` feeds the dependency layer. A placeholder `VERSION` satisfies
setuptools during the install, and the real `VERSION` is copied after the code and read at
runtime. A test guards the ordering. The next build reinstalls once; later releases reuse the
cached layer.


### v1.14.2 — S5b: Paper Test card; official B1–B4 verdicts (2026-10-09) — *no trading behavior change*

- **Performance page:** a new **Paper Test** card shows L5 and B5 in plain language: pretend P&L,
  trades, win rate, days running, progress toward the 300-trading-day verdict, and how close each
  strategy is to its automatic shut-off (SPRT / CUSUM).
- **Research report Celery task limit raised** to 55/60 min. The full-window B3/B4 runs took
  ~21 min on the shared worker and finished just past the old 20-min soft limit (results were
  saved intact).

**Official B1–B4 development verdicts** (correct window 2025-07-01 → 2026-06-30; reports 12–15;
reports 8–11 superseded):

| ID | Traded city-days | P&L | ¢/contract | Gate | Failed criteria | Notes |
|---|--:|--:|--:|---|---|---|
| B1 NBM-direct @ D1E | 892 | **+$95.61** | +3.36 | FAIL | **5, 6 only** | profit LB +2.34¢/day; slope −0.18; drawdown $22.38 |
| B2 NBM-direct @ D0M | 807 | −$9.44 | −0.73 | FAIL | 2, 3, 4, 5, 6 | |
| B3 EMOS @ D1E | 862 | **+$87.01** | +3.52 | FAIL | **5, 6 only** | LB +2.73¢/day; robust; slope −0.21 [−0.58, 0.17]; drawdown $14.18; MIA +$49.67 |
| B4 EMOS @ D0M | 730 | −$2.03 | −0.19 | FAIL | 2, 3, 4, 5, 6 | |

**All four** have a positive out-of-sample log-loss gain over the market with a 95% CI above 0
(B1 +0.023, B3 +0.022 nats). The models add information beyond the market price. The D1E
variants make money robustly, but their trade-level edge estimates are not calibrated (criterion
5) and their drawdowns exceed $10 (criterion 6). Consistent with the preliminary run.
Per Amendment 2, **B5 (= B3) is being judged forward on paper trading**, which started
2026-10-09.


### v1.14.1 — Model strategies use their pre-registered evaluation window (2026-10-09)

Also fixes a **v1 risk bug** surfaced by CI running at 22:59 ET.

- **The bug:** `CooldownManager.is_cooldown_active` decided that a cooldown was the
  consecutive-loss "rest of day" kind purely because it ended at about 23:59:59 ET. A 60-minute
  per-loss cooldown started around 22:59 ET also ends then.
- **The effect:** with `enable_consecutive_loss_limit` off, that per-loss cooldown was wrongly
  **cleared**.
- **The fix:** it is now rest-of-day only if the consecutive-loss limit was actually reached.
- **Test:** a regression test freezes the clock at 22:59:53 ET.
- **Live impact:** none while v1 stays in `manual`.

Bug: `create_report` gave B-variants the market-strategy window (2024-07-01 → 2026-06-30) instead of
the pre-registered model window (**2025-07-01 → 2026-06-30**, which reserves 365 days of training
warm-up; pre-registration §1). Official reports **8–11 (B1–B4, run 2026-10-09 02:50Z) used the wrong
window and are SUPERSEDED**. They stay in `research_reports` for the audit trail, and the B1–B4
verdicts are re-run on the correct window after this deploy. For transparency, the superseded
results were:

| ID | P&L | Gate | Failed criteria |
|---|--:|---|---|
| B1 | +$107.63 | FAIL | 2, 3, 5, 6 |
| B2 | −$54.50 | FAIL | 2, 3, 4, 5, 6 |
| B3 | +$119.83 | FAIL | 3, 5, 6 |
| B4 | +$1.36 | FAIL | 2, 3, 5, 6 |


### v1.14.0 — Algo v2 S5a: live paper trading for forward-only L5 and B5 (2026-10-09) — *never places orders*

Starts the forward tests pre-registered in Amendments 1 and 2. **v1 remains paused, and nothing here
can place or queue a real order** (a safety test asserts that the executor and the queue are never
called).

- **`strategy/paper.py` + `paper_tasks.py`:**
  - Every 15 min, each active forward-only strategy (L5: longshot NO fade @ 20:00 local D-1;
    B5: B3 rule @ 17:00 local D-1) acts once per city-day within 90 min of its decision time.
  - Decisions use live Kalshi quotes (`strategy/live.py`, the same `MarketSnapshot` as the
    backtester). Fills are simulated as taker fills with the exact fee and a $4/city-day budget,
    flagged `fill_feasible=False` when the displayed size couldn't absorb them.
  - Every decision is recorded, including "no orders".
  - B5 refreshes the latest NBM/GFS/NAM issuances before deciding and uses only those available at
    the decision time.
- **Settlement + kill switch** (hourly):
  - Paper trades settle on Kalshi's `result`; "scalar" settlements are voided.
  - `strategy/monitor.py` runs an SPRT (stop at ΣΛ ≤ −1.56) and a CUSUM ($10 of unrecovered loss).
  - Either one permanently stops that strategy and sends a push notification.
- **API:** `GET /api/paper/strategies`, `GET /api/paper/trades`.
- **Migration 0023:** `paper_trades`, `paper_decisions`, `paper_strategy_state`.
- **Kill switch for the whole test:** `V2_PAPER_ENABLED=false`.
- `PREREGISTERED_K = 10`; B5 is registered as forward-only (the backtest API refuses it).
- **Live smoke** (read-only, 2026-10-08, against the next day's real markets):
  - L5 picked the 1¢ brackets in all four cities.
  - B5 (October fit: α 0.10, β 1.14) found 3 trades (MIA ×2, AUS ×1) and none in NYC/CHI.


### v1.13.0 — Algo v2 S4: model-based strategies B1–B4 (2026-10-08) — *no trading behavior change*

Last pre-registered strategy family (`docs/research/v2-preregistration.md` §4, Amendment 1).

- **Model probabilities** (`research/forecast_models.py`):
  - NBM-direct: Normal(txn, xnd) → brackets.
  - Station EMOS/NGR: μ = a + b1·NBM + b2·MOS with b ≥ 0, log σ = c + d·log(NBM sd), fit by
    closed-form min-CRPS. Refit monthly per city on labels in [t−365, t−2].
  - Features come only from issuances with `available_at ≤ decision` (no lookahead).
- **Benter combination** (`strategy/benter.py`):
  - P = softmax(α·log p_model + β·log q + γ·tail), q = normalized market mids.
  - MAP fit with fixed priors α~N(0,.25²), β~N(1,.25²), γ~N(0,.5²); pooled cities, refit
    monthly, walk-forward. Training-day model probabilities are themselves out-of-sample.
  - Laplace posterior, 200 draws. Trades only if the 10th-percentile edge after the exact fee is
    > 0; fractional Kelly λ=0.1, at least 1 contract, $4/city-day cap.
- **Gate criterion 5 implemented** (`research/gate.py`):
  - Edge slope: contract-weighted OLS with SEs clustered by city-day; the 95% CI must contain 1
    and exclude 0.
  - Paired out-of-sample log-loss gain vs the market: block-bootstrap 95% CI > 0.
- **Registry:**
  - B1 (NBM-direct @ D1E), B2 (NBM-direct @ D0M), B3 (EMOS @ D1E), B4 (EMOS @ D0M).
  - L5 registered as **forward-only**: the backtest API refuses it.
  - `PREREGISTERED_K = 9` (Amendment 1).
- **Fix:** Kalshi "scalar" (fair-price) settlements, e.g. MIA 2026-04-11, are archived as final
  `irregular` days (no more hourly retries) and excluded from backtests and scoring.


### v1.12.0 — Algo v2 S3: as-issued station forecast archive (2026-10-08) — *no trading behavior change*

Inputs for the model-based strategies B1–B4 (slice S4).

- **`weather/forecast_archive.py`:** archives the daily-max forecasts **as issued** by GFS MOS,
  NAM MOS and NBM, plus NBM's spread (`xnd`), for KNYC/KMDW/KMIA/KAUS from 2024-06. Source is the
  Iowa Environmental Mesonet bulk CSV.
  - Max rows (00Z) map to the previous local date.
  - `available_at = run + conservative latency` (GFS 5 h, NAM 4 h, NBM 2 h) for no-lookahead joins.
- **Pacing:** IEM rate-limits hard, so requests are spaced 1 per 15 s with 429 backoff. The
  backfill runs month by month per station and model, is resumable (`forecast_archive_chunks`), and
  re-checks recent months every 6 h.
- **Coverage endpoint:** `GET /api/archive/forecast-coverage`.
- **Migration 0022:** `forecast_issuances`, `forecast_archive_chunks`.
- **Refactor:** the shared upsert helper moved to `common/db_utils.py`.
- **Live smoke test** (AUS, March 2025): GFS 125 runs / 313 rows, NAM 63 / 158, NBM 124 / 310.


### v1.11.0 — Algo v2 S2: real-price backtester + pre-registered verdicts (2026-10-08) — *no trading behavior change*

Second code slice (`docs/ALGO_V2_PRD.md`). v1 is untouched and still in `manual`.

- **Real-price engine** (`backtesting/real_engine.py`): replays a strategy over the Kalshi archive.
  Each fill is a taker fill at the archived bid/ask, with the exact per-order fee (`strategy/fees.py`)
  and a $4 per city-day budget. It settles on Kalshi's recorded result. Snapshots
  (`research/snapshots.py`) use the last hourly candle at or before the decision time (local civil
  time, DST-safe), and outcomes are never visible to the strategy. The legacy synthetic engine is
  marked deprecated.
- **Scoring + gate** (`research/scoring.py`, `research/gate.py`):
  - log loss / Brier / RPS;
  - city-day stationary block bootstrap;
  - gate criteria 1–6 from `docs/research/v2-preregistration.md` at α/K = 0.05/8;
  - control checks C0–C2;
  - holdout usable once per strategy, only after a dev PASS.
- **Strategies** (`strategy/`): C0 null, C1 random taker, C2 v1 replica (v1's own `scan_bracket`
  on stored predictions with `generated_at <= decision`), and L1–L4 longshot NO fade.
- **API:** `POST /api/research/backtest`, `GET /api/research/reports[/{id}]`,
  `GET /api/research/strategies`. Results are stored in `research_reports` (migration 0021).
- **Archive fix:** Kalshi's historical data omits strikes on the winning market for events
  2025-01-16..02-09. They are now inferred from the ticker, and those 100 non-tiling city-days are
  re-archived automatically.
- **Housekeeping:** `kalshi_quotes` older than 30 days are thinned to hourly.

**S1 observe (2026-10-07):** the archive backfill completed:
- 3,311 city-days (774 E1 + 54 E2 per city) and 619k hourly candles (127 MB);
- 100 non-tiling city-days (the Kalshi quirk above);
- average spread 7.7–10.9¢ (2024) → 2.8–4.6¢ (2025) → 1.0–1.4¢ (2026).

**Pre-registered dev-window verdicts (read-only dry run on the live archive, 2026-10-08):**

| ID | Result | Detail |
|---|---|---|
| C0 null | ✅ | P&L 0 |
| C1 random taker | ✅ | −4.09¢/contract vs expected −3.62¢ (95% CI −5.59..−2.68) |
| C2 v1 replica | ✅ | −$41.38 vs v1's real −$92.05 on the same city-days (same sign); **the harness reproduces v1's losses** |

| ID | Traded city-days | Contracts | P&L | ¢/contract | Gate |
|---|--:|--:|--:|--:|---|
| L1 ≤2¢ @17:00 | 1,857 | 7,428 | −$45.74 | −0.62 | **FAIL** (lower bound −5.1¢/day) |
| L2 ≤4¢ @17:00 | 2,360 | 9,440 | −$62.59 | −0.66 | **FAIL** |
| L3 ≤2¢ @20:00 | 1,978 | 7,912 | −$25.06 | −0.32 | **FAIL** |
| L4 ≤4¢ @20:00 | 2,441 | 9,764 | −$17.30 | −0.18 | **FAIL** |

The longshot NO fade's reported +0.5–1.1¢/contract edge **does not replicate** on 2024-07..2026-06
after exact fees. All four variants also fail robustness, consistency and risk (max drawdown
$35–92 vs the $10 limit). Every variant is positive in the **last third** of the window
(+0.9..+2.7¢/day, as spreads tightened), but that is a post-hoc observation. Under the
pre-registration it cannot be acted on without a dated amendment (which increases K) and fresh
out-of-sample data. Market baseline at 17:00 the day before: log loss 1.29, Brier 0.664, RPS 0.097.
Official verdicts are re-run via the API after deploy, which includes the 100 repaired city-days.


### v1.10.0 — Algo v2 S1: Kalshi market archive (2026-10-07) — *no trading behavior change*

First code slice of the algo v2 rebuild (`docs/ALGO_V2_PRD.md`). Adds the data needed to backtest
on **real** prices; v1 trading is untouched (still `manual`).

- `backend/kalshi/public_client.py`: unauthenticated, rate-limited (2 req/s) client for Kalshi's
  public market data, with 429/5xx retry + `Retry-After`.
- `backend/kalshi/archive.py` + tasks: archives every settled KXHIGH* market (and the legacy
  `HIGH*` series, used for events before 2024-10-24) from 2024-07-01: strikes, x.5 bounds, tiling
  flag, outcome (`result` / `expiration_value`), era (E1 NWS CLI / E2 Weather Company from
  2026-08-14), and hourly yes_bid/yes_ask candles. Resumable per city-day; hourly beat +
  self-chaining. Also records live top-of-book quotes every 5 min (`kalshi_quotes`).
- `GET /api/archive/coverage`: per city/month coverage, labels, median spread.
- Migration 0020 (4 new tables). Kill switch: `V2_ARCHIVE_ENABLED=false`.
- Verified live (2026-10-07) on one city-day per era: legacy NYC 2024-07-01 spreads 5–11¢,
  AUS 2025-03-15 2–5¢, MIA 2026-10-01 1¢; bounds tile, winners match `expiration_value`.


### v1.9.16 — Weather-source toggles; default to a 3-source ensemble (2026-08-21)
- **Files:** `backend/common/schemas.py`, `backend/common/models.py`,
  `backend/api/{deps,settings,response_schemas}.py`, `backend/prediction/scheduler.py`,
  `alembic/versions/0019_add_enabled_weather_sources.py`, `frontend/app/settings/page.tsx`
- **What:** new `UserSettings.enabled_weather_sources` (DB column, API field, Settings UI
  toggles), defaulting to the three-source working set **NWS:gridpoint + Open-Meteo:GFS +
  Open-Meteo:ICON**. The prediction scheduler filters forecasts to the enabled set before the
  ensemble. A floor of 2 sources is enforced in the schema, the API (422), and the UI.
- **Why:** the 2026-08-21 source review. NWS and NWS:gridpoint are effectively one feed (error
  correlation 0.983, level correlation 0.9967, identical on 80% of days, mean |diff| 0.32 °F) yet
  held **45.5% of ensemble weight combined**, so NWS was being double-counted. ECMWF was the
  weakest member — dropping it slightly *improved* MAE. Only ICON's removal significantly hurt the
  ensemble (+0.047 °F RMSE, *p* = 0.004). The best 3-source subset beat all five (RMSE 4.098 vs
  4.135).
- **Expected effect:** ensemble RMSE flat-to-slightly-better; NWS no longer double-weighted.
  Because the differences are small (~0.04 °F) and not individually significant, do **not** expect
  a visible P&L signal from this alone — the win is a less redundant, better-understood ensemble.
- **Reversible by design:** disabled sources are still **fetched, stored, and scored** by
  `/api/accuracy/sources`. Only the ensemble skips them, so re-enabling one later is backed by
  continuous history rather than a gap.
- **Deploy note:** requires Alembic migration `0019` (adds `users.enabled_weather_sources` and
  backfills existing rows). The v1.9.15 backend reads that column, so the migration must run
  before the new code serves traffic.

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

### 2026-10-07 — six weeks paused (paper record, source check, gate-metric flaw)

First review run under the "Delivery Pipeline" process (this is stage 10, *Observe*). Bot in
`manual` since 2026-08-28, so the evidence is the **paper record**: every signal the scanner
queued in `pending_trades` (all expired unexecuted), scored against NWS CLI settlements. Data
pulled read-only from the VM Postgres. Nothing on the live bot was changed.

**1. Paper record, Aug 29 → Sep 30 (237 signals, 219 scorable, all NYC, all NO-side).**
Convention note for future reviews: in `pending_trades` (and `TradeSignal`), `price_cents` is
the **YES** price and `market_probability` is the **chosen side's** price — for a NO signal the
stake is `100 − price_cents` and the market's YES probability is `price_cents/100`.

| Slice | Contracts | Win rate | P&L | ROI | Model Brier | Market Brier |
|---|--:|--:|--:|--:|--:|--:|
| Every signal, 1 contract each | 219 | 37.0% | −$19.41 | −20.2% | 0.379 | 0.186 |
| First signal per bracket | 29 | 44.8% | −$1.61 | −11.5% | 0.322 | 0.222 |
| **Capped 5/bracket (≈ what auto would have done)** | **106** | **42.5%** | **−$6.03** | **−12.3%** | 0.337 | 0.209 |
| — bottom catch-all "X°F or below" | 41 | **9.8%** | −$12.62 | **−79.9%** | 0.491 | 0.165 |
| — middle 2°F brackets | 65 | 63.1% | +$6.59 | +19.9% | 0.240 | 0.236 |

Promised EV on every signal was again 0.060–0.065 (184 of 219 exactly 0.065): still 100%
max-divergence clamps. The cold catch-all lost on 8 of 9 distinct days. The middle-bracket
profit is ~20 independent days with Brier parity — luck-compatible, not an edge.

**2. Why the cold bracket keeps hitting: the NYC ensemble runs warm.** Day-ahead (last issue
before the target day), `actual − forecast`, Aug 29 → Oct 6:

| NYC source | n | Bias °F | MAE | Warm-miss days |
|---|--:|--:|--:|--:|
| Ensemble (model) | 33 | −0.94 | **1.48** | 24/33 |
| NWS:gridpoint | 35 | −1.23 | 1.69 | 24/35 |
| ECMWF | 35 | −0.96 | 1.81 | 24/35 |
| ICON | 35 | −0.72 | 1.85 | 20/35 |
| GFS | 35 | −0.93 | 2.09 | 24/35 |

The ensemble is actually the most accurate single forecast by MAE, so this is not "bad
sources". It is a **persistent ~1°F warm bias across every source** that the rolling bias
correction is not removing, and that bias lands exactly on the cold catch-all bracket. Since the
market prices that bracket correctly, every clamp-fade on it is a loss.

**3. ECMWF's NYC lead did not persist.** It was best in the Aug 22–28 week (bias +1.36, only 1
warm miss) but mid-pack since (MAE 1.81, rank 3 of 4). Keeping it on costs nothing; it is not a
fix. Side note: in MIA/AUS the ensemble sits 1.5–2°F *cooler* than the actuals while NWS alone is
near zero bias — and in AUS Aug 22–28 the ensemble (+3.61) was outside the range of *every*
source (max +2.39), i.e. a post-ensemble correction is pushing the wrong way. Must be understood
before any city re-enable.

**4. The planned edge gate can't work as specified.** The Aug 28 decision said "resume only if
`/api/accuracy/edge` turns positive". That endpoint reads **settled trades only** → zero samples
while paused, so it can never turn positive. It also has a side bug: for NO trades it compares the
stored NO-side `market_probability` with the bracket-hit (YES) outcome. Recomputed over 90 days
(287 trades) market Brier moves 0.257 → 0.248 corrected — the verdict ("market outperforming")
was right, just understated. Any gate must score *signals* (or all predicted brackets vs market
snapshots), not only executed trades, and use YES-side probabilities consistently.

**5. Ops: silent 4.5-day outage.** No predictions/forecasts from Oct 2 ~00:15 to Oct 6 18:39 UTC
(VM boot time). Prometheus/Alertmanager live on the same VM, so nothing alerted. NYC settlements
for Oct 1–4 are missing from the `settlements` table.

**6. Money.** Balance $66.76. DB accounts for −$2.90 since the Aug 28 snapshot (last 6 settled
trades incl. the Aug 29 NO-78°F position, −$0.42); ~$2.10 is unreconciled.

> Next review: only after a code change ships. Track the paper-record ROI split by bracket type,
> the ensemble's NYC day-ahead bias, and (once fixed) the signal-based edge metric.

### 2026-08-28 — "why no green day?" (Era F: the losing mechanism, fully traced)

Prompted by the user noticing no green day in a while. Live data from the VM: 1,000 most recent
settled trades, `/api/accuracy/edge`, `/api/accuracy/trends` per source, `/api/accuracy/calibration`,
and the PREDICTION log stream. v1.9.16 is deployed (all Aug 21 fixes live).

**1. Last green day: Aug 14.** Daily P&L since: Aug 16 −$0.50, Aug 18 −$2.09, (no trades
Aug 19–22), Aug 23 −$2.70, Aug 24 −$2.18, Aug 25 −$0.51, Aug 26 −$1.98, Aug 27 −$0.50.
Trailing 7 days: **1 win / 16 losses, −$7.69**. August month-to-date: −$20.64. Balance $71.76.

| Era | Trades | Win rate | P&L | ROI | avg promised EV |
|---|--:|--:|--:|--:|--:|
| E Aug 7 – Aug 21 (post-bounds-fix) | 19 | 21.1% | −$5.22 | −49.9% | +6.4% |
| **F Aug 22 → (v1.9.16 live)** | **17** | **5.9%** | **−$7.87** | **−92.7%** | **+6.4%** |

All 36 trades NO-side, all NYC. Era F is 3 brackets × 5 stacked contracts + 2 singles.

**2. The losing trade, mechanically.** Every losing trade has the same shape: NYC bottom/low
bracket ("79°F or below" etc.) priced ~50¢ by the market, model probability 0.13–0.28, bot sells
it (NO) — and the bracket **hits**. Note the promised-EV column: **every trade lands at exactly
0.064–0.065**. That is not a coincidence, it is arithmetic. With guardrails
`max_model_market_divergence = 0.25` and `model_weight = 0.4`
(`ev_calculator.apply_guardrails`), any model prob ≥25pp below market clamps to
`market − 0.25`, blends to `market − 0.10`, and at a 50¢ price that yields ~+6.5% EV after fees —
the maximum the pipeline can emit. **A uniform 0.064–0.065 EV column means literally every trade
the bot places is a max-divergence clamp**: the bot now *only* trades when the model maximally
disagrees with the market.

**3. And on that subset, the market is right — the model has negative edge everywhere.**
`/api/accuracy/edge` (Brier, lower is better):

| Window | Model Brier | Market Brier | Edge | Verdict |
|---|--:|--:|--:|---|
| 7 days (n=17, the Era F trades) | 0.587 | 0.259 | **−0.328** | Market outperforming |
| 90 days NYC (n=222) | 0.310 | 0.267 | −0.043 | Market outperforming |
| 90 days MIA / AUS / CHI | 0.309/0.331/0.322 | 0.264/0.266/0.273 | −0.045/−0.064/−0.049 | Market outperforming |

The 30-day NYC calibration table sharpens this into **adverse selection**: across *all* brackets
the model's 0.1–0.2 bin is nearly calibrated (predicted 0.136, actual 0.143, n=2756) — but the
mid-bins where model and market disagree are badly off (predicted 0.46 → actual 0.22; predicted
0.55 → actual 0.28). The trade filter (EV threshold + divergence) samples exactly the
disagreement region, i.e. the model's worst errors. Aggregate calibration looks fine while every
*traded* probability is wrong.

**4. The proximate weather cause: a late-August cool regime the sources keep missing warm.**
Per-source day-of error for NYC (`actual − forecast`, °F; negative = forecast too warm):

| Source | 08-21 | 08-22 | 08-23 | 08-24 | 08-25 | 08-26 | 08-27 |
|---|--:|--:|--:|--:|--:|--:|--:|
| NWS:gridpoint (enabled) | −2.0 | −1.6 | −1.7 | −1.8 | −2.6 | −1.3 | −4.9 |
| Open-Meteo:GFS (enabled) | −2.1 | +1.4 | −3.5 | −2.8 | −2.9 | −4.1 | −5.9 |
| Open-Meteo:ICON (enabled) | −1.9 | −1.2 | −1.6 | −0.8 | −0.9 | −0.0 | −3.1 |
| **Open-Meteo:ECMWF (dropped in v1.9.16)** | −1.5 | **+2.2** | **+0.5** | **+0.7** | **+1.2** | **+1.1** | **−1.8** |

Every enabled source ran warm nearly every day; the ensemble kept predicting ~81–82°F highs while
actuals came in ≤79, so the model kept assigning ~13–27% to the cool bracket the market (rightly)
priced at ~50%. Two aggravators:

- **Rolling bias correction did nothing**: `calculate_rolling_bias` returned **+0.29°F**
  (14-day mean) on Aug 28 — the warm misses of Aug 23–27 are averaged against the cool-biased
  days of Aug 18/20, so a windowed mean structurally cannot catch a fast regime flip. Trades
  cluster on exactly the days the mean misses.
- **v1.9.16 dropped the one source that had the regime right.** ECMWF was removed from the
  default ensemble (justified on 446-day pooled stats) but was the *best* NYC source in the week
  after the switch, while GFS — the worst performer this week at −2.8..−5.9 — stayed. Small
  sample, and Era E was already losing with all 5 sources, so this is an aggravator, not the root
  cause.

**5. What this rules out.** The Aug 21 infrastructure worries are clear: calibration is loading
(v1.9.14 mtime reload live; identical model probs across cycles show a stable, applied pipeline),
bounds are correct (verified Aug 21), the bracket cap held at 5 in Era F. The remaining problem is
not plumbing — **the strategy "fade the market when the model disagrees by ≥25pp" has had negative
expectancy in every era since June**, and after v1.9.12 cut volume, those max-divergence fades are
the *only* trades left.

**Options discussed:** (a) pause live trading until the model demonstrates positive edge
out-of-sample; (b) re-enable ECMWF (one Settings toggle, reversible); (c) tighten
`max_model_market_divergence` (e.g. 0.25 → 0.15) and/or cut `model_weight` so max-divergence
fades can no longer clear the 6% NO threshold — note this likely reduces trade count to ~zero,
which is (a) by another name; (d) make the EV threshold scale with recent realized model-vs-market
edge so the bot self-throttles when the market is beating it.

**Decision (2026-08-28): APPLIED & VERIFIED.** (a) + (b) — trading mode → `manual` and
`Open-Meteo:ECMWF` re-enabled (4-source ensemble), both flipped by the user via the Settings UI
and confirmed live via `GET /api/settings`. Edge gate (d) deferred — revisit at the next review. Rationale for skipping (c): with `model_weight` 0.3 the clamp arithmetic means
tightening divergence to 0.15 caps attainable EV at ~2.5%, below the 6% NO threshold — zero trades
ever fire, i.e. it is a disguised pause. Note the open position at decision time: 1 contract NO on
`KXHIGHNY-26AUG29-T79` @ 40¢ market — the model's own current Aug 29 forecast (78.4°F mean) gives
that bracket ~even odds, so the position contradicts the bot's latest view; left to the user.

> Next review: EV gap remains the key health metric, plus (a) the model-vs-market Brier edge at
> 7/30 days — the bot should not be trading while it is negative; (b) whether ECMWF's recent NYC
> advantage persisted; (c) AUS 0.0-probability curve before any city re-enable.

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
