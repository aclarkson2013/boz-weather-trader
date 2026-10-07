# Algo v2 — Research Brief (2026-10-07)

> **Pipeline stage:** 01 Idea → research input for the stage-02 PRD.
> **Why:** the v1 decision rule has lost money in every era since June 2026, and the bot has been
> paused since 2026-08-28 (see `docs/ALGO_CHANGELOG.md`, 2026-10-07 review). This brief collects
> the evidence for designing a replacement. Four parallel research passes covered forecasting, same-day
> nowcasting, Kalshi market data and other bots, and decision theory. Constraint: **free data only**.
> Claims marked *(reported)* come from third-party sources and were not independently reproduced here.
> Claims marked *(verified)* were checked live on 2026-10-07.

---

## 1. Headline conclusions

1. **The market beats every free public forecast.** Crosier (arXiv 2609.23969, 7 cities, 7,590
   city-days, 2022 to Aug 2026) reports the market-implied high has 10–11% lower RMSE than the
   National Blend of Models (NBM) and 23–30% lower than GFS MOS, HRRR and ECMWF. The market also
   beats the *best combination* of all public forecasts by 3–4%. Forecasts move toward the
   market, not the reverse. *(reported)*
2. **Independent open-source backtests agree.** k1hbles/kalshi-edge, Schmiedey/kalshi-weather,
   Julesli0217 and SomeGuy966 all find the market's Brier score beats the model's, and every one
   finds that *trading the disagreements loses after fees*. The k1hbles test was 10-point edge,
   taker fills, −2.5¢/contract, t = −2.66. *(reported)* This is exactly v1's failure.
3. **v1's rule is mathematically built to lose.** With `b = 0.3·clamp(p_m) + 0.7·q`, claimed edge
   is at most 0.3 × 0.25 = 0.075. Only clamp-boundary trades clear the 6% bar, so the bot trades
   **only** where it most disagrees with the market. That is the optimizer's curse (Smith & Winkler
   2006) and adverse selection. The 0.3 weight was never fitted.
4. **The edges that survive honest tests are small and structural, not forecasting edges:**
   - **Longshot fade:** buy NO when YES is bid 1–4¢, about 15:00–21:00 local the day before.
     Schmiedey reports +0.5 to +1.1¢/contract after fees over 180 days and 7 cities. The best
     window is 17–19h (90% CI +0.70 to +1.47¢); every city is positive; the edge is gone by
     09:00 on the event day. *(reported)* It is consistent with the favourite–longshot bias
     documented on Kalshi by Bürgi, Deng & Whelan.
   - **Maker vs taker:** across all Kalshi categories, takers lose about 31.5% and makers about
     9.6% on average, and makers buying contracts at ≥50¢ earned about +2.6%. *(reported)* A
     simple weather maker test (k1hbles) came out at −0.7¢, roughly breakeven. Not proven for
     weather.
   - **Market + model combined (Benter-style):** small, not significant. k1hbles: +1.7 to
     +2.2¢. Julesli0217: logit coefficient 0.12 (t = 2.4), out-of-sample Brier 0.1099 → 0.1095.
     *(reported)*
5. **Same-day nowcasting is real but crowded.** About 85% of volume trades on the event day,
   peaking 13–18 ET. The market prices fresh observations within about an hour: on Oct 6 the
   NYC "<63" bracket went 59¢ → 97¢ by 18Z, once the max-so-far was 58°F. What is left is 1–3¢
   on "locked" brackets, which fees mostly consume. SomeGuy966's observation-based LightGBM still
   lost to the market mid-price, worst in the late afternoon. Possible niches: night or
   late-evening highs, whole-°C rounding ambiguity at bracket edges, thin late-evening liquidity.
   *(reported / one-day spot check)*

**Implication.** A better forecast alone very likely will **not** make money. Algo v2 should
(a) **start from the market price**, (b) give the model weight only where history proves it earns
it, (c) harvest small structural edges cheaply (maker orders, longshot pricing), and (d) be
validated on **real historical prices** before risking money. "Don't trade" must be an acceptable
outcome.

---

## 2. Facts that change our assumptions

| Fact | Status | Impact |
|---|---|---|
| **Settlement source changed.** KXHIGH events up to 2026-08-13 settle on the NWS CLI. From **2026-08-14** they settle on "The Weather Company" (weather.com/kalshi), same station (CLINYC etc.). Only "the first official non-preliminary report" counts; revisions after expiration (10:00 ET) are ignored. | **verified** via the public API rules text and `series.settlement_sources` | CLAUDE.md and the docs are out of date. Values matched the CLI on 671/672 city-days in one test (the exception was MIA Aug 29: Kalshi 90 vs CLI 85); our Oct 6 NYC also matched (60). Unlikely to explain the losses, but labels must come from Kalshi's `expiration_value`. |
| **Free historical prices exist.** `api.elections.kalshi.com/trade-api/v2` needs no auth for market data. Candlesticks carry OHLC for **yes_bid and yes_ask** at 1-min/1-h/1-day granularity. Settled markets older than `GET /historical/cutoff` move to the `/historical/...` endpoints. | verified (endpoints); granularity/depth *(reported)* | The backtester can finally use **real** prices instead of synthetic ones. Quotes are usable from about mid-2024 (spreads were 79¢ in 2023 and 2¢ in 2026). Tickers are `HIGHNY-` up to 2024 and `KXHIGHNY-` from 2025. |
| **Fees.** Taker fee = `roundup_to_cent(0.07 × C × P × (1−P))` **in dollars per order**; maker fee uses 0.0175, charged only on fill. KXHIGH series: `fee_type: quadratic, fee_multiplier: 1`. | verified (series fields); maker applicability to weather **unverified** | Rounding up per order kills 1-contract trades on cheap or expensive contracts (1 contract at 97¢: true fee 0.2¢, charged 1¢). Order size has to account for this. |
| **Spreads** are usually 1¢ on middle brackets, widening to 2–3¢ at 13–17 ET. | *(reported)* | Taker cost is about ½¢ of spread plus the fee. |
| **Contracts are fractional** (`count_fp` such as "299.88"). Bracket labels now read "between 80-81°". | *(reported)* | Check the order and parse code. |
| **Market close is local-standard midnight** (05Z NY/MIA, 06Z CHI/AUS); trading runs through the whole LST day. | *(reported)* | Same-day trading is possible right up to the end of the measurement window. |

---

## 3. Forecasting: what a v2 probability model should use

**Free station-level probabilistic sources** (all free, no keys):

| Source | Access | History (for backtests) | Notes |
|---|---|---|---|
| **NBM text bulletins (NBP/NBS)** | AWS `noaa-nbm-grib2-pds/blend.YYYYMMDD/HH/text/blend_nbptx.tHHz` | from 2020-05; percentiles from about 2021 | Station blocks for KNYC/KMDW/KMIA/KAUS: `TXNMN` mean, `TXNSD` SD, `TXNP1..P9` percentiles. Hourly cycles. TXN window is 12Z to 06Z (≠ LST day). |
| **IEM MOS/NBM JSON** | `mesonet.agron.iastate.edu/api/1/mos.json?station=KNYC&model={GFS,NAM,MEX,NBS,NBE,LAV}&runtime=…` | GFS MOS 2003, NAM 2008, NBS 2018, NBE 2020 | Station-bias-corrected and **as issued**, which suits fair backtests. |
| Open-Meteo Ensemble API | `ensemble-api.open-meteo.com/v1/ensemble` | about 3 months, **not as-issued** | Members: ECMWF 51, GEFS 31, ICON-EPS 40. Live use only; **don't backtest on it**. |
| Open-Meteo Previous Runs | `previous-runs-api.open-meteo.com` (`temperature_2m_previous_day1`) | 2021–2024 onward | Deterministic, gridded, as issued. |

**Likely cause of our NYC warm bias:** raw gridpoint forecasts with a 14-day mean correction,
against a station that MOS/NBM are fitted to *by construction*. Central Park is reported to read
cool because of tree canopy (domain claim, unverified).

**Recommended model: station NGR/EMOS** (Gneiting et al. 2005) fitted by minimum CRPS on 2+ years
of as-issued pairs:
- μ = a + b₁·NBM_mean + b₂·GFSMOS + b₃·NAMMOS (+ ECMWF ensemble mean, live only), with **b ≥ 0**.
  The non-negative weights keep μ inside the range of the sources, which fixes the MIA/AUS
  correction that pushed μ outside every source.
- log σ = c + d·log(NBM_SD) (+ ensemble SD).
- Season: a ±30–45-day calendar window pooled across years, or sin/cos day-of-year terms. Plus a
  small decaying recent-error term (weight 0.05–0.1).
- Lang et al. 2020: multi-year adaptive training beats short sliding windows for 2 m temperature.
- Gaussian CRPS closed form: `σ[z(2Φ(z)−1) + 2φ(z) − 1/√π]`. Check the PIT histogram and switch
  to Student-t if the tails are heavy; real tails are reportedly about 2× Gaussian.
- **Baseline to beat:** NBM percentiles used directly (interpolated quantile function). If v2
  can't beat that on CRPS/RPS, don't use it.

**Bracket probabilities:** the settled value is an integer °F. P("74–75") = F(75.5) − F(73.5),
P(≤L) = F(L+0.5), P(≥U) = 1 − F(U−0.5). Read `strike_type` from the API.

**Time windows don't line up:** CLI/TWC is midnight–midnight **LST** (1 AM–1 AM during DST); NBM
TXN is 12Z–06Z; MOS max is daytime; Open-Meteo daily aggregation follows clock time. Add a
"midnight-high" correction from hourly guidance. Crosier reports 4.6% of days peak outside the
daytime window.

---

## 4. Same-day nowcasting

**How the official max is formed:** ASOS produces 1-min whole-°F values and a rolling 5-min
average (some sources say 2-min). The daily max is the highest such average in the LST day.
- METAR hourly T-group (0.1 °C) → `round(1.8T+32)` usually recovers the °F value.
- **Whole-°C values (5-min METAR, MADIS) are ambiguous by ±1°F.**
- 6-h (`1snTTT`) and 24-h (`4snTTTsnTTT`) max groups convert back to °F exactly and capture peaks
  between obs.
- The afternoon CLI (about 4 PM LST) gives the official max-so-far.
- CLI equals the METAR-derived max on 97.2% of days, is higher 2.4% and lower 0.5% (QC).
  *(reported, SomeGuy966, 19,549 station-days)*

**Free real-time feeds** (latency measured 2026-10-07):
- **aviationweather.gov METAR API** (`/api/data/metar?ids=…&format=json`): about 3 min. Primary.
- **IEM** (`asos.py`, `json/current.py`): minutes; decades of history, so the training source.
- **api.weather.gov observations:** gappy (missed 3 hourly obs that day). Fallback only.
- **CLI text** via `api.weather.gov/products/types/CLI/locations/{NYC,MDW,MIA,AUS}`: PM issue 20–22Z.
- **LAMP** (`model=LAV`, hourly) and **HRRR** via Open-Meteo, for the remaining-hours forecast.

**Method:**
1. Lower bound L_t = max over the LST day so far of the METAR T-group °F, the 6/24-h groups and
   the PM CLI.
2. Model Δ = final − L_t with ordinal/multiclass gradient boosting. Features: LST hour, month,
   station, (max of remaining-hours LAMP/HRRR − L_t), current temp − L_t, 1–2 h trend,
   clouds/precip, wind direction (sea breeze at NYC/MIA).
3. Keep about 0.5% mass at Δ = −1 for QC drops, and use the empirical +1 rate.
4. Train by replaying IEM METAR history hour by hour with as-of joins.

**Evidence of edge:** weak. The market reprices within about an hour (section 1, item 5).

---

## 5. Decision layer: combining the model with the market

**Benter (1994)** is the canonical solution to "my model is worse than the public but has
information". Fit a second-stage conditional logit across the outcomes of each event:

`P_i = exp(α·log p_m,i + β·log q_i + γ·tail_i) / Σ_j exp(α·log p_m,j + β·log q_j + γ·tail_j)`

- Benter's reported fit (pseudo-R²): public odds alone 0.1237, model alone 0.1016, combined
  0.1327. The model was **worse than the public alone** yet added information. *(reported,
  secondary summary)*
- The softmax across the 6 brackets absorbs the overround, so no separate de-vig step is needed.
  `γ·tail` captures the favourite–longshot bias.
- **Binary test version:** fit `y ~ a + b1·logit(q) + b2·logit(p_m)` with standard errors
  clustered by city-day. If the market is efficient, b1 ≈ 1 and b2 ≈ 0. **Trade a segment only if
  b2 > 0 out-of-sample.**
- Shrink toward the market with priors α ~ N(0, 0.25²) and β ~ N(1, 0.25²). Allow α to vary by
  segment (D-1 vs D-0 lead, city).
- **Bet on uncertainty-adjusted edge:** sample (α, β, γ) from the Laplace posterior about 200
  times. edge_i = P_i − cost_i, where cost = ask (or 1 − bid for NO) plus the per-contract fee.
  Trade only if the **10th percentile of edge > 0**. This replaces the clamp, the 0.3 blend and
  the 6%/12% thresholds.
- Linear pools (v1's blend) are provably miscalibrated (Ranjan & Gneiting 2010); log pools with
  fitted weights are not.

**Sizing:**
- Joint log-utility across YES/NO on all 6 brackets, with fee-inclusive prices. Use cvxpy, or the
  Smoczynski–Tomkins / Kelly closed form for the YES-only case.
- Then **fractional Kelly λ = 0.1–0.25**. Shrinkage is optimal when probabilities are estimated
  (Baker & McHale 2013; Metel 2017).
- Cap total exposure at 1–2% of bankroll per city-day.
- Whelan 2025: joint multi-outcome Kelly loses *faster* than simple rules when there is no real
  edge, which is another reason to enforce the gate.

---

## 6. Validation protocol (non-negotiable before real money)

1. **Data:** backfill Kalshi bid/ask candles (live + `/historical`), as-issued forecasts (IEM
   MOS/NBM, NBM AWS), and labels = Kalshi `expiration_value`. Split results before and after
   2026-08-14.
2. **Walk-forward:** for each day t, fit on [t−365, t−1], predict day t at the real decision
   timestamp, fill at the ask with the exact rounded fee (or a conservative maker-fill model).
3. **Primary metric is scoring rules, not ROI:** log loss, Brier and RPS vs the market on *every*
   bracket, compared with a paired test per city-day (block bootstrap or Diebold–Mariano). On the
   traded subset, regress realized (Y − cost) on predicted edge: **slope ≈ 1 required** (≈ 0 means
   the winner's curse is still there). Deflate for multiple variants tried.
4. **Why not ROI?** Detecting +3% ROI at 80% power and one-sided α = 0.05 needs about 1,200
   independent bets at 85¢ contracts, about 6,900 at 50¢ and about 27,500 at 20¢. The independent
   unit is the city-day (about 1,460/yr across 4 cities).
5. **Stages:** backtest gate → 4–8 weeks of shadow/paper trading (must land inside the backtest
   CI) → live at λ = 0.1 → scale.
6. **Kill switch:** a Wald SPRT on live trades. Λ per trade =
   `y·log(P/q) + (1−y)·log((1−P)/(1−q))`. Stop if ΣΛ ≤ −1.56; scale up if ΣΛ ≥ 2.77 (α = 0.05,
   β = 0.2). Run a CUSUM as the continuous monitor. On v1's numbers it would have fired within a
   few dozen trades.

---

## 7. Honest expectations

- Bankroll is $66.76. The best-evidenced edge (longshot fade) is about +1¢/contract on contracts
  costing about 96–99¢: roughly 1% on capital at risk, with rare full losses when a "1–4¢"
  bracket hits. At this bankroll that is cents per day. **The realistic goal of v2 is "stop losing,
  and prove or disprove an edge cheaply"**, not income.
- Most of the value is in the **infrastructure** (real-price backtester, as-issued forecast
  archive, scoring vs the market, kill switch). That infrastructure is also what makes this
  open-source project credible.

---

## 8. Reusable open-source code

| Repo / library | Use |
|---|---|
| [SomeGuy966/weather-bot](https://github.com/SomeGuy966/weather-bot) (MIT) | IEM CLI/METAR/MOS downloaders, as-of snapshot pipeline, running-max nowcast anchor |
| [k1hbles/kalshi-edge](https://github.com/k1hbles/kalshi-edge) | Most rigorous walk-forward test on real bid/ask; methodology template |
| [Schmiedey/kalshi-weather](https://github.com/Schmiedey/kalshi-weather) | Longshot-fade study |
| [Julesli0217/weather_prediction_markets](https://github.com/Julesli0217/weather_prediction_markets) | NYC market-vs-model logistic test |
| [Weather-Capital-Markets/kalshi-weather](https://github.com/Weather-Capital-Markets/kalshi-weather) | Order-book/trade logger, NBM byte-range ingestion |
| [scoringrules](https://github.com/frazane/scoringrules) | CRPS/Brier/RPS/log score. **Needs Python ≥3.12** (project is 3.11+) |
| [penaltyblog](https://github.com/martineastwood/penaltyblog) | `multiple_kelly_criterion` for mutually exclusive outcomes |
| [Met Office IMPROVER EMOS](https://improver.readthedocs.io/en/stable/improver.calibration.emos_calibration.html) | Reference EMOS implementation (heavy; read, don't import) |
| [suislanchez/polymarket-kalshi-weather-bot](https://github.com/suislanchez/polymarket-kalshi-weather-bot) | Popular (≈777★) but no backtest; profit claims self-reported |
| [NorthLake postmortem](https://www.northlakelabs.com/max/blog/kalshi-weather-postmortem-and-pivot/) | 0-for-32 lessons: fat tails, fees on cheap contracts, slow polling |

---

## 9. Open questions for the PRD

1. Which edge(s) to pursue first: market-anchored Benter gate, longshot fade, maker orders,
   same-day nowcast, or "build the measuring infrastructure first and let data decide"?
2. Run v2 **beside** v1 (shadow) or replace v1's decision path?
3. Python 3.12 upgrade (for `scoringrules`) or hand-roll the scores?
4. Acceptable capital at risk for the longshot fade (tail loss is about 97¢/contract)?
5. Does the maker fee apply to KXHIGH? (Check the order ticket or fee page.)

---

## Sources

**Papers & docs:**
- Crosier 2026, arXiv 2609.23969: https://arxiv.org/abs/2609.23969
- Bürgi, Deng & Whelan, *Makers and Takers*: https://www.karlwhelan.com/Papers/Kalshi.pdf · https://cepr.org/voxeu/columns/economics-kalshi-prediction-market
- Benter 1994: https://www.gwern.net/doc/statistics/decision/1994-benter.pdf (summary: https://oddspapi.io/blog/?p=3174)
- Smith & Winkler 2006, *The Optimizer's Curse*: https://ideas.repec.org/a/inm/ormnsc/v52y2006i3p311-322.html
- Baker & McHale 2013: https://pubsonline.informs.org/doi/10.1287/deca.2013.0271
- Metel 2017: https://arxiv.org/abs/1701.02814
- Whelan 2025, multi-outcome Kelly: https://www.karlwhelan.com/?p=2411
- Gneiting et al. 2005, EMOS: https://sites.stat.washington.edu/raftery/Research/PDF/gneiting2005.pdf
- Lang et al. 2020: https://npg.copernicus.org/articles/27/23/2020/
- Rasp & Lerch 2018: https://arxiv.org/pdf/1805.09091

**Kalshi:**
- Historical data: https://docs.kalshi.com/getting_started/historical_data
- Rate limits: https://docs.kalshi.com/getting_started/rate_limits
- Weather help: https://help.kalshi.com/en/articles/13823837-weather-markets
- Contract terms: https://assets.kalshi.com/contract_terms/GLOBALTEMPERATURE.pdf
- Fee schedule: https://kalshi.com/docs/kalshi-fee-schedule.pdf
- Weather Company partnership: https://news.kalshi.com/p/kalshi-weather-company-partnership

**Weather data:**
- NBM text card: https://vlab.noaa.gov/web/mdl/nbm-textcard-v4.3
- NBM AWS bucket: https://noaa-nbm-grib2-pds.s3.amazonaws.com/
- IEM MOS: https://mesonet.agron.iastate.edu/mos/
- IEM CLI JSON: https://mesonet.agron.iastate.edu/json/cli.py?station=KNYC&year=2026
- IEM CLI audit: https://mesonet.agron.iastate.edu/nws/cli-audit/
- Open-Meteo Ensemble: https://open-meteo.com/en/docs/ensemble-api
- Open-Meteo Previous Runs: https://open-meteo.com/en/docs/previous-runs-api
- NWS high-res ASOS: https://www.weather.gov/psr/HiResASOS
- NWS ASOS temperature: https://www.weather.gov/lox/asostemperature
- NWS directive 10-1004: https://www.weather.gov/media/directives/010_pdfs/pd01010004curr.pdf
