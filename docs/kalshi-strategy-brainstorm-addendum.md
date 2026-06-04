# Kalshi Strategy Brainstorm Addendum

Date: 2026-06-02

Scope: new and expanded methods for improving the Kalshi strategy stack. This
is a research/paper-trading document only. It does not authorize orders or
live-submit.

Current public docs used for assumptions:

- Kalshi market data quick start:
  https://docs.kalshi.com/getting_started/quick_start_market_data
- Kalshi WebSocket docs:
  https://docs.kalshi.com/getting_started/quick_start_websockets
- Kalshi rate limits:
  https://docs.kalshi.com/getting_started/rate_limits
- Kalshi order V2 docs:
  https://docs.kalshi.com/api-reference/orders/create-order-v2
- Kalshi fee schedule:
  https://kalshi.com/docs/kalshi-fee-schedule.pdf

## Core Reframe

The sleeve should not be "a model that predicts events." It should be a system
that repeatedly answers four questions:

1. What market is structurally mispriced?
2. What source gives us non-market information or a better aggregation of public
   information?
3. Can we trade at the book before the information decays?
4. Does realized fill quality prove that the edge survives fees, spread, stale
   data, and adverse selection?

This reframe changes the research program. We do not need the fanciest model.
We need the best combination of market selection, source timing, fair-value
construction, execution mode, and proof.

## Current Market Surfaces Worth Watching

A direct public API sample on 2026-06-02 found current two-sided books in:

| Surface | Examples | Why it matters |
|---|---|---|
| Weather daily highs | `KXHIGHNY`, `KXHIGHCHI`, `KXHIGHMIA` | Strongest current modelable surface. High 24h volume and tight books in sampled city brackets. |
| CPI / Core CPI ladders | `KXCPI`, `KXCPICORE` | Real threshold ladders with enough activity for CDF/no-arb work. |
| Fed decisions | `KXFEDDECISION` | Conditional probability graph across meetings and hike/cut sizes. |
| Equity index ranges | `KXINX`, `KXNASDAQ100` | Range-ladder and close-price modeling surface. |
| Brent daily thresholds | `KXBRENTD` | Commodity settlement/threshold modeling, likely underexplored. |
| Tennis match markets | `KXITFMATCH` | Sharp-reference repricing surface, though spreads can be wide. |
| MLB outrights | `KXMLB` | Liquid but long-horizon, high correlation, and capital-duration heavy. |
| BTC15M | `KXBTC15M` | High volume, but decisive edge still depends on c-lead and executable book proof. |

Broad top-volume scans are misleading. They surface multileg sports markets with
historical volume/open interest but weak current books. Market selection must
score current two-sided depth, not just total volume.

## Edge Taxonomy

Every strategy should declare which edge type it claims:

| Edge type | Description | Latency sensitivity | Examples |
|---|---|---:|---|
| Better distribution | Model has a better probability distribution than market | Low to medium | Weather, CPI nowcast, equity range CDF |
| Faster source | We observe a source update before market fully incorporates it | Medium to high | Weather forecast update lag, BTC constituent lead, sports feed events |
| Cross-market inconsistency | Prices violate logical, ladder, or cross-venue constraints | Medium | Range CDF, yes/no parity, mutually exclusive outcomes |
| Liquidity provision | We get paid for providing liquidity when spread is too wide | Medium | Macro pre-release, thin but rule-clear markets |
| Adverse-selection avoidance | We cancel or reprice faster than stale quotes get picked off | High | Queue evader, maker strategies |
| Market anchoring residual | Sharp market is efficient, but Kalshi lags it | Low to medium | Tennis, sports, some politics/elections |
| Resolution-rule expertise | Others misunderstand exact settlement wording/source | Low | Weather station rules, commodity close definitions, court/policy markets |

If a strategy cannot name its edge type, it should stay observe-only.

## Universal Improvements

### 1. Market Selection Engine

Build a market ranker before building more alpha. Score each candidate by:

- two-sided spread,
- L1 bid/ask size,
- recent trade count and 24h volume,
- days/hours to close,
- fee family,
- rule clarity,
- availability of external data,
- availability of historical labels,
- settlement ambiguity,
- expected edge half-life,
- current host latency fit.

Output a daily `market_opportunity_board.jsonl` with:

- `market_id`,
- `series`,
- `strategy_candidates`,
- `book_score`,
- `data_score`,
- `rule_score`,
- `latency_score`,
- `capacity_score`,
- `promotion_status`.

### 2. Market-Implied Prior Everywhere

Treat market mid as the prior, not the enemy. Most false edges come from an
overconfident model. For every predictive strategy:

```text
fair = w * model_probability + (1 - w) * market_probability
```

Where `w` is learned by bucket:

- source age,
- market liquidity,
- bracket distance from center,
- model uncertainty,
- historical CLV,
- regime,
- time to expiry.

This prevents the model from fighting a liquid market when it has weak evidence.

### 3. Meta-Labeling For Tradeability

Separate "is model edge positive?" from "is this edge tradable?"

Primary model:

- predicts probability or fair value.

Meta model:

- predicts whether taking this apparent edge leads to positive CLV or PnL.

Meta features:

- spread,
- L1 depth,
- quote age,
- source age,
- model confidence,
- market volume,
- time to close,
- recent market move,
- side,
- passive/taker mode,
- historical bucket CLV.

This should become the default promotion gate. Many models can predict outcomes
but not tradable fills.

### 4. Full-Ladder Pricing

For threshold/range markets, never price contracts independently. Fit one
latent distribution, then map all strikes/ranges to probabilities.

Useful surfaces:

- CPI and Core CPI thresholds,
- S&P/Nasdaq ranges,
- Brent daily thresholds,
- weather high brackets,
- Fed meeting path markets.

Advantages:

- catches monotonicity violations,
- reduces noise,
- enforces probability mass consistency,
- turns multiple thin markets into one stronger inference problem.

### 5. Linear Programming Arbitrage Scanner

Build a deterministic Dutch-book scanner for any event group. Inputs are all
YES/NO bids/asks and payoff vectors. Solve:

- cheapest portfolio with guaranteed payoff,
- highest guaranteed return under budget,
- dominated contract detection,
- mutually exclusive outcome sum violations,
- complementary yes/no parity violations,
- range ladder mass violations.

This is not glamorous, which is exactly why it belongs in the always-on stack.

### 6. Bitemporal Data Store

Every external source must be recorded with:

- `source_event_time`,
- `source_published_time`,
- `received_at`,
- `normalized_at`,
- `used_in_decision_at`,
- payload version,
- fetch URL or WS channel,
- content hash.

This prevents timestamp leakage in backtests and lets us measure source lead.

### 7. Execution Mode As A Model Feature

Do not pool:

- taker IOC,
- passive maker at bid/ask,
- passive midpoint,
- laddered orders,
- re-entry after partial fill,
- exit/liquidation orders.

Each has different fill quality and adverse selection. Every paper ledger row
should include `execution_mode`, `liquidity_role`, `order_lifetime_ms`, and
`post_fill_markout`.

### 8. Capacity From Book Depth, Not Capital

Size by:

```text
max_size = min(
  L1_size * participation_limit,
  edge_decay_capacity,
  event_group_limit,
  drawdown_limit,
  Kelly_fraction_limit,
  venue_rate_limit_capacity
)
```

Capital is last, not first.

## Strategy-Specific Brainstorms

## Weather: Daily Highs, Rain, Snow, Hurricanes

### Signals

1. High-so-far plus remaining forecast:
   - final high = max(observed high so far, remaining-hours stochastic high).
   - This is stronger than using a static forecast snapshot.

2. Lead-aware residuals:
   - fit bias/sigma by station, month, lead time, hour of day, and bracket
     distance.
   - Do not trade lead buckets whose calibration is unproven.

3. Regime classifier:
   - sea breeze,
   - lake effect,
   - frontal passage,
   - convective storm day,
   - cloud-cover cap,
   - heat dome,
   - high wind mixing.

4. Observation nowcast:
   - METAR/ASOS current temp,
   - nearest station cross-check,
   - dewpoint,
   - cloud cover,
   - radar/cloud satellite proxy,
   - NWS forecast discussion NLP.

5. Market reaction lag:
   - record each forecast update and market mid at +1/+5/+15/+30 minutes.
   - trade only update types where market reprices slowly.

### How To Trade

- Prefer taker only when model edge clears fees plus half-spread plus stale
  penalty.
- Prefer passive only after adverse-selection tests prove fills are not toxic.
- Trade ladder residuals, not isolated brackets.
- Use "center-safe, tail-cautious" sizing: near-mean brackets get larger max
  size than sparse tail buckets.

### New Variants

- `weather_ladder_cdf`: one station-day distribution, trade all mispriced
  brackets coherently.
- `weather_update_lag`: only trade after forecast/observation updates with
  historically slow market reaction.
- `weather_station_pair`: use correlated station/city forecasts to detect
  inconsistent market moves.
- `hurricane_track_ladder`: NHC cone and ensemble track probabilities for
  landfall/city impact markets.

## Macro: CPI, Core CPI, PPI, NFP, Fed

### Signals

1. Component nowcast:
   - gasoline,
   - shelter/rent,
   - used cars,
   - food,
   - healthcare,
   - airfares,
   - wages,
   - freight,
   - import prices.

2. Survey distribution, not point consensus:
   - use economist survey dispersion as uncertainty.
   - trade thresholds where market-implied tail differs from survey/nowcast
     distribution.

3. Fed path graph:
   - model target range as a hidden Markov path.
   - enforce consistency across current meeting, next meeting, and year-end
     markets.

4. Pre-release liquidity premium:
   - quote wider when market maker liquidity withdraws.
   - stop before release unless the release feed is institutional and measured.

5. Post-release stale ladders:
   - after official release, some far thresholds or back-month conditional
     markets may update slower than front thresholds.

### How To Trade

- Pre-release: patient limit orders, small size, distribution edge only.
- Release moment: observe-only unless source timestamps and latency prove lead.
- Post-release: scan all thresholds for stale or internally inconsistent prices.
- Always trade the ladder, not single direction.

### New Variants

- `macro_cpi_cdf_arb`: implied CDF vs nowcast distribution.
- `macro_release_stale_ladder`: post-release stale far-threshold scanner.
- `macro_fed_path_consistency`: transition-matrix no-arb across Fed meetings.
- `macro_liquidity_premium`: maker strategy around expected spread widening.

## BTC / ETH 15m Settlement

### Signals

1. Synthetic CF-style index:
   - Coinbase,
   - Kraken,
   - other US-accessible constituents if available,
   - per-feed timestamp and age.

2. Official/proxy lead:
   - record official RTI if obtainable.
   - if not, label the proxy honestly.

3. Final-window partial sum:
   - once inside settlement window, track observed partial average and remaining
     variance.

4. Vol regime:
   - realized vol,
   - exchange spread,
   - trade intensity,
   - cross-exchange dispersion,
   - outage/degradation flags.

5. Book freshness:
   - Kalshi book update age,
   - constituent age,
   - decision-to-send latency,
   - candidate edge expiry.

### How To Trade

- No execution until c-lead is proven.
- Taker only. Passive orders in final seconds are likely toxic unless you are
  explicitly market-making with cancels.
- Use edge expiry: drop any intent older than a few milliseconds in the final
  window.
- Size tiny until lead, fill rate, and markout are proven.

### New Variants

- `btc_clead_recorder`: mandatory measurement build.
- `btc_final_window_pin`: only price within final 60 seconds with partial sum.
- `btc_pre_window_vol_misprice`: compare market-implied sigma to realized sigma
  before the final window.
- `crypto_outage_detector`: trade only when one constituent lags or degrades and
  Kalshi follows the stale source.

## Equity Index Ranges

### Signals

1. Futures fair value:
   - ES/NQ front future,
   - ETF premium/discount,
   - cash index fair-value adjustment.

2. Close model:
   - Brownian bridge into 4pm,
   - realized intraday vol,
   - final-hour trend,
   - auction imbalance if accessible.

3. Range ladder CDF:
   - enforce mass across all ranges.
   - detect impossible or dominated ranges.

4. Volatility surface:
   - compare Kalshi range probabilities to option-implied distribution.

### How To Trade

- Midday: CDF residual trades, slower.
- Final 30 minutes: Brownian bridge and range ladder.
- Final minutes: only if market data feed and route prove low latency.
- Prefer ranges with tight book and near-center mass.

### New Variants

- `equity_close_range_cdf`: one close distribution, all ranges.
- `equity_option_implied_residual`: option-implied distribution vs Kalshi.
- `equity_close_auction_lag`: final imbalance reaction if source is accessible.

## Brent / Commodity Daily Thresholds

This surface was not emphasized enough before. Current public sampling showed
two-sided `KXBRENTD` markets.

### Signals

1. Settlement mapping:
   - first verify exact Brent source and close window.
   - no model until settlement wording is parsed.

2. Futures/spot lead:
   - ICE Brent futures if accessible,
   - public delayed source only for research,
   - broker/API source for live if licensed.

3. Threshold ladder:
   - price all `above X` contracts as one distribution.

4. Intraday vol:
   - realized oil vol,
   - inventory/news shocks,
   - time-to-close Brownian bridge.

### How To Trade

- Treat like equity range/threshold markets.
- Avoid if data is delayed or settlement source is ambiguous.
- Good candidate for a non-HFT distribution strategy if source quality is high.

### New Variants

- `commodity_brent_threshold_cdf`.
- `commodity_settlement_source_arb`.
- `commodity_news_vol_filter`.

## Tennis And Sports

### Tennis Signals

1. Sharp-reference fair value:
   - Pinnacle or sharp consensus,
   - de-vigged,
   - source timestamped.

2. Line-move lag:
   - record sharp move time and Kalshi reaction time.
   - trade only if Kalshi lags by enough.

3. Match integrity filters:
   - retirement rules,
   - tournament level,
   - player injury/withdrawal,
   - liquidity/spread,
   - odds-source coverage.

4. Model fallback:
   - anchored residual model, not standalone winner model.

### Sports Signals Beyond Tennis

1. MLB/NBA/NFL/NHL moneyline repricing:
   - sharp sportsbook consensus vs Kalshi.
   - de-vig, align rules, record lag.

2. Outrights:
   - sportsbook futures, team strength, schedule simulation.
   - capital-duration penalty matters.

3. In-play:
   - official feed only.
   - source timestamp proof mandatory.

4. Golf:
   - cut-line distribution,
   - tee-time weather wave,
   - live scoring source lag,
   - hole-by-hole only if feed is truly fast.

### How To Trade

- Pregame: fair-value residual, taker only when edge clears costs.
- In-play: observe-only until feed lag is measured.
- Outrights: small size, long duration, portfolio correlation caps.
- Avoid wide books unless explicitly earning spread as maker and adverse
  selection is measured.

### New Variants

- `sports_sharp_lag_repricing`.
- `tennis_line_move_lag`.
- `mlb_outright_futures_residual`.
- `golf_cutline_weather_wave`.

## Cross-Market And Portfolio Arbitrage

### Signals

1. Within-event mass:
   - mutually exclusive outcomes should not sum above/below rational bounds
     after fees.

2. Range ladders:
   - adjacent ranges and above/below thresholds imply a CDF.

3. Conditional markets:
   - if A implies B, prices must respect that relation.

4. Cross-venue:
   - only when resolution rules are identical.

5. Same event, different wrappers:
   - multivariate legs versus singles,
   - yes/no complement,
   - event-level order book versus market-level contract if supported.

### How To Trade

- Prefer deterministic no-arb portfolios over directional single legs.
- For cross-venue, cap by leg-risk and settlement mismatch risk.
- Use IOC only when both legs are simultaneously executable.
- If one leg fails, immediately record hedge-miss and do not hide it in PnL.

### New Variants

- `kalshi_dutch_book_scanner`.
- `range_ladder_noarb`.
- `conditional_probability_graph`.
- `multileg_single_leg_consistency`.

## Politics, Courts, Policy, Regulation

### Signals

1. Official-source first:
   - court docket,
   - bill calendar,
   - committee schedule,
   - agency filing,
   - election office data.

2. Market reaction lag:
   - source publish time versus Kalshi quote move.

3. Polling/state model:
   - Bayesian poll aggregation,
   - turnout priors,
   - cross-market consistency.

4. NLP only as triage:
   - headlines generate candidates,
   - official source confirms tradeable state.

### How To Trade

- Mostly passive or patient limit orders.
- Taker only after official source and rule match.
- Size small due to resolution ambiguity and long duration.

### New Variants

- `court_docket_official_lag`.
- `legislative_calendar_hazard`.
- `election_cross_market_consistency`.
- `policy_headline_false_positive_filter`.

## Entertainment, Awards, Box Office

### Signals

1. Box office:
   - presales,
   - Thursday previews,
   - theater count,
   - competitor releases,
   - review/social momentum,
   - studio estimates and revision behavior.

2. Awards:
   - precursor awards,
   - critic guilds,
   - betting odds where legal,
   - nomination shortlists.

3. Streaming/media:
   - platform rank changes,
   - public chart updates,
   - social velocity.

### How To Trade

- Slow, patient, small capacity.
- Rule ambiguity is the main risk.
- Trade only markets with exact data source in rules.

### New Variants

- `box_office_preview_velocity`.
- `awards_precursor_graph`.
- `streaming_rank_update_lag`.

## Execution Improvements

### Taker Logic

Use taker only when:

```text
edge > fee + half_spread + stale_penalty + adverse_selection_penalty
```

Add per-strategy stale penalty:

- BTC final window: very high.
- Weather: low if forecast source is fresh.
- Tennis line moves: medium.
- CPI release: extreme at release time.

### Passive Maker Logic

Maker is not automatically better. It is better only when:

- fills are not followed by negative markout,
- cancel path is fast enough,
- market does not select against stale quotes,
- maker fee/rebate terms are favorable for that series,
- order lifetime is capped.

Every passive strategy needs:

- cancel-on-source-update,
- cancel-on-book-age,
- cancel-on-market-lifecycle-change,
- cancel-on-model-fair-change,
- cancel-on-spread-collapse,
- inventory skew.

### Laddered Execution

For slower markets, replace one all-or-nothing order with a ladder:

- small taker at best ask if edge is very large,
- passive at mid if edge is medium,
- passive at bid if edge is weak but persistent.

Record each tier separately.

### Inventory-Aware Quoting

If holding inventory:

- skew fair value away from adding more same-side risk,
- widen quotes near event-group exposure caps,
- prefer risk-reducing exits during drawdown,
- block correlated additions.

### Exit Logic

Most current strategy thinking focuses on entry. Add exit policies:

- CLV take-profit,
- model-fair convergence,
- source invalidation,
- stale-source exit,
- event risk de-risk before release,
- trailing stop for sports/tennis only where markout supports it,
- hold-to-settlement only when expected payout dominates exit liquidity.

## Signal Generation Methods To Add

### Distributional Modeling

Use for:

- weather highs,
- CPI/PPI,
- equity/commodity ranges,
- Fed paths.

Methods:

- normal/logistic CDF baseline,
- skewed distributions,
- mixture models,
- quantile regression,
- conformal intervals,
- Bayesian hierarchical shrinkage,
- Brownian bridge near close,
- hidden Markov model for policy paths.

### Market-Anchored Residual Modeling

Use when another market is sharper than our raw model:

- tennis,
- sports,
- Fed,
- equities,
- commodities.

Method:

```text
logit(fair) = logit(sharp_market) + residual_model(features)
```

This should replace standalone classifiers for efficient markets.

### Source-Lag Modeling

Use when edge is "we see it first":

- BTC constituent lead,
- weather update lag,
- sharp sportsbook move lag,
- official data release lag,
- court docket updates.

Metrics:

- source publish to receive,
- receive to market move,
- receive to decision,
- decision to ack,
- fill to markout.

### Regime Models

Add a regime layer before fair value:

- weather regime,
- macro volatility regime,
- sports liquidity regime,
- crypto exchange-stress regime,
- election/news cycle regime.

The regime decides whether the base model is trusted, shrunk, or disabled.

### Anomaly Detection

Use for:

- stale books,
- bad external source,
- one constituent exchange stuck,
- market rules mismatch,
- abnormal spread/depth,
- sudden market move without source confirmation.

Anomaly result should often be `NoAction`, not a trade.

## Validation From All Angles

Before any strategy is called "good," prove all of these:

| Angle | Required proof |
|---|---|
| Outcome model | Calibrated out of sample, no leakage. |
| Market comparison | Beats market at actual tradable quotes. |
| Execution | Fill simulation with real book and latency. |
| Costs | Kalshi fee curve, spread, slippage, settlement fees if relevant. |
| Source timing | Point-in-time data with source/receive timestamps. |
| Rule correctness | Market resolution source and wording parsed. |
| Capacity | Uses real depth and fill rates, not capital desire. |
| Robustness | Positive by bucket, not one event or one station. |
| Operations | Kill switches, stale-source behavior, reconciliation. |
| Infrastructure | Host latency fits edge half-life. |

## Highest-Value Build Queue

1. `market_opportunity_board.py`: rank current markets by book/data/rule/latency
   fit.
2. `btc_clead_recorder.py`: decisive BTC lead measurement.
3. Weather high-so-far plus lead-aware calibration.
4. Full-ladder CDF engine for weather/CPI/equity/Brent.
5. Kalshi quote capture for tennis versus sharp odds.
6. Dutch-book/no-arb scanner.
7. Generic source-lag ledger.
8. Meta-labeler for tradeability and CLV.
9. Passive adverse-selection analyzer.
10. Host A/B latency profiler for EC2 versus trial VPS.

## What Not To Do

- Do not add more standalone ML classifiers before proving real-price CLV.
- Do not treat calibration as edge.
- Do not trade release sniping without institutional source timestamps.
- Do not buy permanent sub-2 ms hosting before a measured edge needs it.
- Do not size long-duration markets by nominal capital without duration and
  correlation penalties.
- Do not trust broad top-volume scans.
- Do not pool maker and taker results.
- Do not call a cross-venue gap arbitrage unless rules match exactly.

## Implementation Status (2026-06-02)

What from this addendum is now runnable (configs + a registered runtime, validated
+ `on_event`-smoked), vs deferred. "Runnable" follows the v7 rule: it emits correct
decisions when fed its trigger events; signal-driven ones still need their external
producer for *live* decisions (see `docs/v7-live-test-ready-strategy-specs.md`).

| Variant | Status | How |
|---|---|---|
| Full-ladder CDF engine | **Implemented (runtime)** | new `ladder_cdf` plugin: one latent distribution → coherent per-bracket probs |
| `weather_ladder_cdf` | **Implemented** | `weather-ladder-cdf.toml` → `ladder_cdf` |
| `macro_cpi_cdf_arb` | **Implemented** | `macro-cpi-cdf.toml` → `ladder_cdf` (pre-release) |
| `equity_close_range_cdf` | **Implemented** | `equity-close-range-cdf.toml` → `ladder_cdf` |
| `commodity_brent_threshold_cdf` | **Implemented** | `commodity-brent-threshold-cdf.toml` → `ladder_cdf` (new surface) |
| `sports_sharp_lag_repricing` | **Implemented** | `sports-sharp-lag-repricing.toml` → `external_edge` |
| `mlb_outright_futures_residual` | **Implemented** | `mlb-outright-residual.toml` → `external_edge` |
| `range_ladder_noarb` | **Implemented** | `range-ladder-noarb.toml` → `kalshi_noarb_scanner` (cumulative) |
| `kalshi_dutch_book_scanner` | **Partial** | `kalshi_noarb_scanner` does sum-lock + monotonicity; full LP Dutch-book deferred |
| `awards_precursor_graph`, `box_office_preview_velocity` | Covered (v7) | `entertainment-awards` / `entertainment_box_office` |
| `tennis_line_move_lag`, `macro_liquidity_premium` | Covered | v7 tennis sharp-ref re-spec / `macro_nfp_absorber` |
| `*_official_lag`, `legislative_calendar_hazard`, `policy_headline_*` | Overlaps existing | `court_docket_timing` / `politics_legislative_cascade` / `tariff_headline_gap_fader` |
| `golf_cutline_weather_wave` | Overlaps existing | `sports_cut_line_shifter` / `sports_frl_weather_arb` |
| `btc_final_window_pin`, `equity_close_auction_lag` | **Excluded** | latency-bound (violates the ~100ms-irrelevant brief) |
| `weather_update_lag`, `weather_station_pair`, `hurricane_track_ladder` | Deferred | need source-lag / NHC producers (host on `external_edge`/`ladder_cdf`) |
| `macro_release_stale_ladder`, `macro_fed_path_consistency` | Deferred | post-release scanner / Fed transition-matrix no-arb |
| `equity_option_implied_residual`, `commodity_settlement_source_arb`, `commodity_news_vol_filter` | Deferred | need option-implied / settlement-parse / anomaly producers |
| `btc_pre_window_vol_misprice`, `crypto_outage_detector` | Deferred | non-latency crypto vol/anomaly variants |
| `conditional_probability_graph`, `multileg_single_leg_consistency` | Deferred | extends the no-arb scanner |
| `election_cross_market_consistency`, `streaming_rank_update_lag` | Deferred | producer-bound |
| `market_opportunity_board`, source-lag ledger, meta-labeler, adverse-selection analyzer, bitemporal store, `btc_clead_recorder` | Deferred (infra) | tools/recorders, not strategy runtimes — the build-queue items |

**Net new this pass:** the `ladder_cdf` runtime + 7 strategy configs + 7 paper
sleeves + 7 parity stubs; all `validate-config`-clean, `ladder_cdf` ruff/mypy-clean
and `on_event`-smoked. No new Rust. The common remaining blocker for the
signal-driven sleeves is the same one v7 names: their external-signal **producer**.

