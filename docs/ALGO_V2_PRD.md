# Algo v2 — Product Requirements ("measure first, then trade")

> **Status:** approved 2026-10-07 · **Pipeline stage:** 02 (PRD) · **Owner:** aclarkson
> **Inputs:** `docs/ALGO_CHANGELOG.md` (2026-10-07 review) ·
> `docs/research/2026-10-07-algo-v2-research.md` (research brief) ·
> `docs/research/v2-preregistration.md` (pre-registered strategies, gate, holdout)

## 1. Problem

**Who:** the operator of a small ($66.76) self-hosted Kalshi weather-trading bot.

**What happens today:**
- The v1 decision rule has lost money in every era since June 2026, and auto-trading has been
  paused since 2026-08-28.
- v1 blends model and market (0.3 / 0.7, ±0.25 clamp). Its only qualifying trades are
  max-divergence NO fades, i.e. it trades exactly where the model is most wrong (adverse
  selection).
- **We cannot tell whether any strategy has an edge before risking money:**
  - the backtester uses synthetic prices;
  - the edge metric only counts executed trades, so it is blind while paused;
  - no market-price history is stored.

**What changes:** every candidate strategy gets a fee-exact, walk-forward verdict on **real**
Kalshi bid/ask history across all 4 cities. A survivor must then hold up in live paper trading
before any real money is used.

**How we'll know it worked:** `GET /api/research/reports` shows each pre-registered strategy's
pass/fail against the gate in §6. "No strategy passes" is an acceptable outcome — it saves money.

## 2. Goals

1. Archive real Kalshi market data (markets, outcomes, hourly bid/ask candles, live quotes) and
   as-issued station forecasts.
2. Score strategies and models against the market with no lookahead.
3. Paper-trade gate survivors on live quotes.
4. Promote to real money only through the gates in §6.

## 3. Non-goals (v2.0)

- No live orders from v2 code.
- No change to v1 decision code. v1 stays paused and is retired once v2 is live.
- No paid data — free sources only.
- No same-day nowcasting. That is v2.1, once this infrastructure exists.
- No neural nets, no Polymarket.
- No frontend beyond a single paper-trading card.
- No Python 3.12 upgrade: the scoring rules are hand-rolled instead of using `scoringrules`.
- No fixes to v1 bugs here. They are tracked separately (§9).

## 4. User stories and acceptance criteria

Each slice is one branch, one PR and one deploy. The Given/When/Then criteria become tests.

### S1 — Kalshi market archive + live quote recorder

*As the operator, I can see how much real market history is archived per city and month.*

- **Given** Kalshi returns 429 with `Retry-After`, **when** the archive fetches, **then** it
  retries after the delay and does not raise.
- **Given** the same settled page is ingested twice, **then** row counts are unchanged.
- **Given** a market that settled before `/historical/cutoff`, **then** it is fetched from
  `/historical/...` and tagged `source=historical`.
- **Given** `HIGHNY-24JUL01-B80.5` and `KXHIGHNY-26OCT06-T63`, **then** both parse to NYC with
  the correct event date and x.5 bounds.
- **Given** `event_date` 2026-08-14, **then** `era=E2`.
- **Given** a backfill interrupted mid-chunk, **when** it re-runs, **then** it resumes with no
  gaps or duplicates.

### S2 — Snapshots, real-price backtester, market scoring, market-only strategies

*As the operator, I can backtest a strategy on real prices and see a pass/fail verdict.*

- **Given** a 17:00 decision with candles ending 16:00 and 18:00, **then** the snapshot uses the
  16:00 candle.
- **Given** a bracket with bid=0 or ask=100, **then** it is flagged and cannot be filled.
- **Given** `kalshi_fee_cents(97,1)`, `(97,4)` and `(50,10)`, **then** the results are 1, 1 and
  18.
- **Given** the null strategy, **then** P&L is 0. **Given** random taker trades, **then** mean
  P&L ≈ −(½ spread + fee) within the CI.
- **Given** the v1 replica over v1's live window, **then** the sign of its P&L matches the real
  trade record. This validates the harness.
- **Given** a D-0 decision on a DST transition day, **then** the UTC timestamp and LST event
  mapping are correct.

### S3 — As-issued forecast archive (IEM MOS / NBM)

- **Given** a model run, **then** `available_at ≥ run_ts + the model's conservative latency`.
- **Given** a max valid at 00Z the next day, **then** it maps to the correct LST `valid_date`.
- **Given** a re-ingest, **then** row counts are unchanged.
- **Given** a missing run, **then** a WARN log and a coverage gap are recorded, with no crash.

### S4 — Model vs market scoring + Benter-style strategy

- **Given** "poisoned" future rows (a label dated after t−2, or a forecast with `available_at`
  after the decision), **then** neither is ever used.
- **Given** a model with no information, **then** β̂ ≈ 0 and no trades are placed.
- **Given** synthetic data with a real edge, **then** the gate passes.
- **Given** a Gaussian forecast, **then** the closed-form CRPS equals numerical integration to
  within 1e-6.

### S5 — Live paper trading (shadow)

- **Given** any trading mode, **then** v1's executor and trade queue are never called (safety
  test).
- **Given** a settled market, **then** the paper trade settles from Kalshi `result`.
- **Given** the cumulative SPRT statistic ≤ −1.56, **then** the strategy auto-disables and the
  user is notified.
- **Given** the recorded ask size < the order count, **then** the trade is marked
  `fill_infeasible`.

## 5. Data model (new tables)

| Table | Slice | Purpose |
|---|---|---|
| `kalshi_markets` | S1 | One row per market: ticker PK, event/series/city, LST `event_date`, strike fields and x.5 bounds, `tiles_ok`, open/close, `result`, `expiration_value`, `era`, `source` |
| `kalshi_candles` | S1 | Hourly yes_bid / yes_ask OHLC, last price, volume, OI. PK (ticker, period_min, end_ts) |
| `kalshi_quotes` | S1 | Live top-of-book snapshots every 5 min (bid, ask, sizes) for fill-realism checks |
| `research_reports` | S2 | Backtest and scoring outputs: strategy, params, config hash, version, window, eras, K, results JSON, `gate_passed` |
| `forecast_issuances` | S3 | As-issued station forecasts: city, model, `run_ts`, `available_at`, `valid_date`, `tmax_f`, `tmax_sd_f`, extras |
| `paper_trades` | S5 | Shadow trades at the real ask, with fee, posterior edge, result and P&L |

## 6. Success metrics and gates

The full pre-registered definition is in `docs/research/v2-preregistration.md`. Summary:

**Backtest gate**
- Rules:
  - taker fills at the ask, with exact per-order fees;
  - size limited to **$4 per city-day**;
  - α/K multiple-testing correction;
  - development window only.
- Requirements:
  - ≥300 traded city-days, ≥40 per city, spanning ≥6 months;
  - the block-bootstrap lower bound of net P&L per city-day is > 0;
  - still ≥0 with +1¢ slippage, and ≥0 without the best 5% of days;
  - positive in ≥3 of 4 cities and ≥2 of 3 time-thirds;
  - model strategies: the edge-slope CI contains 1 and excludes 0, and the log-loss-gain CI
    excludes 0;
  - max drawdown ≤ $10, and the 5th-percentile 90-day P&L ≥ −$5;
  - holdout mean ≥ 0, used once.

**Paper → live**
- ≥6 weeks and ≥40 traded city-days across the 4 cities;
- paper mean ≥ the backtest's 90% lower bound;
- the SPRT never crossed −1.56 and the CUSUM never alarmed;
- ≥90% of fills feasible at the recorded ask size;
- ≥14 days without task alerts.

**Live (S7, separate approval)**
- fractional Kelly λ = 0.1;
- $4 per city-day cap, and ≤ $10 total exposure for the first 30 days;
- an SPRT kill switch that raises `TradingHaltedError` and reverts to manual.

## 7. Cross-cutting requirements

- **Two clocks:** `event_date` is the LST date. Markets close at local-standard midnight (05Z
  NY/MIA, 06Z CHI/AUS). Decision times are local civil times via `ZoneInfo` (D-1 17:00,
  D-0 10:00), stored as UTC. DST transition days are tested.
- **Tickers:**
  - Reuse `parse_market_date_from_ticker` and `parse_bracket_from_market`
    (`backend/kalshi/markets.py`).
  - Add legacy `HIGHNY`-style series (before 2025).
  - Exclusions are explicit flags, never silent drops.
- **Eras:**

  | Era | Event dates | Settlement source | Use |
  |---|---|---|---|
  | E0 | < 2024-07-01 | — | Excluded (spreads too wide) |
  | E1 | 2024-07-01 → 2026-08-13 | NWS CLI | Development data |
  | E2 | ≥ 2026-08-14 | The Weather Company | Part of the holdout |

  Labels come only from Kalshi `result` / `expiration_value`. Label lag: a fit for day t uses
  `event_date ≤ t−2`.
- **Fees:** `kalshi_fee_cents(price_cents, count, maker=False) = ceil(rate·count·p·(100−p)/10000)`,
  with rate 7 (taker) or 1.75 (maker).
- **Layout:**
  - `backend/kalshi/` for the public client and archive;
  - `backend/weather/` for the forecast archive;
  - new `backend/strategy/` for strategies, fills and fees, shared by the backtest and paper
    trading;
  - new `backend/research/` for snapshots, scoring, EMOS and reports;
  - shared types in `backend/common/schemas.py`.
- **Celery:** backfills run in ~120 s chunks that re-queue themselves (300 s soft limit,
  concurrency 2). New task modules are added to `celery_app.conf.include`.
- **Flags:** env flags `V2_ARCHIVE_ENABLED` and `V2_PAPER_ENABLED`.
- **Repo rules:** structured logs, metrics in `common/metrics.py`, external APIs mocked in
  tests, a VERSION minor bump per code slice, and a `docs/ALGO_CHANGELOG.md` entry per slice.

## 8. Risks

| Risk | Mitigation |
|---|---|
| VM capacity (4 cores / 4.8 GB, shared with the live bot) | Hourly candles only (~0.8M rows, ~200 MB); quotes downsampled after 90 days |
| Rate limits | ~5 req/s, chunked, away from trading-cycle minutes |
| Lookahead | As-of joins, t−2 label lag, `available_at`, poisoned-row tests |
| Survivorship | Archive every market; flags rather than deletes |
| Fee rounding at small size | Gate evaluated at the $4/city-day feasible size |
| Optimistic maker fills | Report-only; confirmed against recorded quotes in paper trading |
| Overfitting / multiple testing | Pre-registration, α/K, single-use holdout, fixed priors |

## 9. Out of scope — known v1 issues (tracked separately)

- Manual queue approval skips the risk check and the bracket cap (`backend/api/queue.py`).
- `_load_user_settings` drops `fee_estimate_mode`, `enable_per_loss_cooldown` and `demo_mode`.
- `/api/accuracy/edge` scores NO-side market probabilities against the YES outcome.
- No off-VM uptime alert (the 2026-10-02 → 10-06 outage went unnoticed).
- `best_yes_price_from_orderbook` treats bids as asks.

## 10. Open questions

1. Does the maker fee apply to KXHIGH? Check the order ticket or fee page before trusting any
   maker result.
2. Same-day nowcasting (v2.1): go or no-go once S5 is running.
