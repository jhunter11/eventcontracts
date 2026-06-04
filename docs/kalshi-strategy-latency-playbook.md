# Kalshi Strategy and Latency Playbook

Date: 2026-06-02

Scope: read-only implementation audit, strategy prioritization, and infrastructure
decision support. No orders were placed. No live-submit path was run.

## Executive Verdict

The current system is strongest for **weather**, **macro/economic ladders**,
**tennis/sharp-reference repricing**, and **audited cross-market scanners**. It is
not yet proven for latency races.

Current measured latency is enough for weather, pre-match sports, macro nowcasts,
politics, entertainment, and slow alternative-data strategies. It is not enough
to justify final-second BTC settlement execution or microstructure scalping until
the repo records source lead and tradable edge at the exact fill moment.

Do not buy a sub-2 ms VPS as a belief purchase. If spending is acceptable, rent a
small Chicago/low-latency instance only as a **measurement probe** for 24-72 hours:
run the same REST/WS latency recorder, c-lead recorder, and fill-simulation
ledger. Keep it only if measured lead or markout improves enough to pay fees,
spread, adverse selection, and the VPS bill.

Implementation note: the first concrete build plan is now in
`docs/kalshi-top3-implementation-plan.md`. It selects weather KXHIGH
distribution/high-so-far, BTC15M timing/c-lead observer, and tennis
sharp-reference/lifecycle as the first three implementation tracks, with
code-along data structures, test commands, ledgers, and promotion gates.

## Current Implementation Inventory

### Architecture Already Present

The repo has a serious framework, not just scripts:

| Layer | Current state | Notes |
|---|---|---|
| Python strategy contract | Implemented in `python/src/eventcontracts/strategy/base.py` | Clean small API: `on_init`, `on_event`, feedback, snapshot/restore. |
| Python runner | Implemented in `python/src/eventcontracts/runner/base.py` | Synchronous runner with risk gate and intent sink. Good for tests, backtests, paper. |
| Risk gates | Implemented in Python and Rust | Checks notional, position, cash, spread, stale data, fees, market order policy, GTC bounds. |
| Fee models | Implemented for Kalshi/Polymarket | Kalshi fee curve is represented; research code must consistently use it. |
| Latency models | Implemented for paper simulation | Constant/lognormal/lookup latency for replay. Needs real measured distributions per venue/host. |
| Kalshi Python adapter | Implemented | REST discovery, order book, trades, authenticated balance reads, WS market-data stream. |
| Rust hot path | Implemented | Live runner, gateway, risk, OMS, venue client, Kalshi REST/WS, model runtime. |
| Live-submit safety | Implemented but forbidden in this workspace | `--live-submit` requires explicit flags, sleeve spec, live cap, confirmation, and reconciliation/cancel-orphans choice. Do not run it here. |
| Weather paper/live paper | Implemented | Python live-paper runner records decisions only; no venue orders. |
| BTC settlement research | Implemented | Kernel, benchmark, gap recorder. Missing c-lead recorder. |

### Strategy Config Inventory

There are 28 strategy TOML specs. The config-level latency classes are:

| Class | Strategy specs | Infrastructure implication |
|---|---|---|
| Critical / sub-20 ms | `microstructure-queue-evader`, `sports-hole-by-hole-pin` | Needs colocated or near-colocated host, push feeds, bounded gateway queue, and measured post-fill markout. Current EC2 may not be enough. |
| Fast / 20-250 ms | `microstructure-obi-scalper`, `arbitrage-cross-venue`, `macro-fed-gnn`, `politics-primary-momentum`, `weather-temperature-arbitrage-live` | VPS can help only if edge half-life is actually this short. Must prove against WS timestamps and executable book. |
| Standard / 250-2000 ms | `macro-nfp-absorber`, `liquidity-tail-risk-insurance`, `aerospace-launch-delay`, base weather | Current EC2 is likely enough; local home may also be enough for non-release windows. |
| Relaxed / seconds to hours | Tennis, weather paper, CPI, courts, crop, energy, entertainment, health, shipping, space weather, tariff, wildfire, politics legislative | Data/model quality matters much more than sub-2 ms networking. |

### Verification Results From This Audit

Commands run from `C:\QWS\eventcontracts` unless noted:

| Check | Result |
|---|---|
| BTC settlement sanity | `.venv\Scripts\python.exe -m pytest python\tests\test_btc_settlement.py -q` -> exit 0, `7 passed`. |
| Strategy/risk/live-readiness slice | 73 tests -> exit 0, `73 passed`. |
| Full Python pytest | `.venv\Scripts\python.exe -m pytest python\tests -q` -> exit 0, `432 passed`. |
| Python ruff | `.venv\Scripts\python.exe -m ruff check python\src python\tests` -> exit 0, all checks passed. |
| Python mypy | exit 1, 12 existing errors in 6 files. See Hygiene Gaps. |
| Rust tests | `cargo test --workspace` from `rust/` -> exit 0, workspace tests passed. |
| Rust fmt | `cargo fmt --all -- --check` -> exit 1, formatting drift in `rust/crates/runner/src/lib.rs`. |
| Rust clippy | `cargo clippy --workspace --all-targets -- -D warnings` -> exit 1, large enum variants in `rust/crates/live-runner/src/main.rs`. |

## Measured Latency

### Local Windows Route

Existing script:

```powershell
.venv\Scripts\python.exe python\scripts\btc_settlement_bench.py --net-samples 9 --net-pause 0.35
```

Results:

| Leg | Min | Median | P90 | Max |
|---|---:|---:|---:|---:|
| Coinbase REST ticker | 30 ms | 39 ms | 68 ms | 68 ms |
| Kalshi REST markets, read proxy | 32 ms | 34 ms | 58 ms | 58 ms |
| Pricing compute | 0.61 us mean | | | |

Interpretation: local compute is irrelevant; network dominates by about 65,000x.

### EC2 Host Route

The EC2 host `ubuntu@18.191.175.183` is reachable, but `~/eventcontracts` is not
currently deployed there. I ran an inline read-only benchmark instead of syncing
the dirty tree.

| Endpoint | Min | Median | P90 | Max |
|---|---:|---:|---:|---:|
| Coinbase REST ticker | 13.66 ms | 16.23 ms | 28.77 ms | 49.02 ms |
| `api.elections.kalshi.com` REST markets | 10.26 ms | 12.27 ms | 16.36 ms | 33.43 ms |
| `external-api.kalshi.com` REST markets | 7.77 ms | 10.56 ms | 15.14 ms | 18.06 ms |

Interpretation: EC2 materially beats the local route, especially to Kalshi. Use
the recommended `external-api.kalshi.com` host where possible. The Kalshi REST
number is a read-RTT proxy only. It does not prove matching-engine latency, order
submit latency, queue priority, or authenticated write behavior.

### QuantVPS / Sub-2 ms Question

Public vendor material claims Chicago infrastructure can see about 0-1 ms or
around 1.14 ms to Kalshi API endpoints. Treat that as self-reported API endpoint
latency, not independent proof of matching-engine or fill latency.

The value of upgrading from EC2 median 10-12 ms to a true sub-2 ms path is:

| Strategy family | Is sub-2 ms likely worth it? | Reason |
|---|---|---|
| BTC 15m settlement arb | Maybe, but only after c-lead is proven | If synthetic index leads official RTI by only 5-15 ms, the host matters. If lead is zero or negative, VPS is irrelevant. |
| Microstructure OBI / queue evasion | Yes, if the edge survives fees | These configs declare 20-50 ms budgets and are directly adverse-selection sensitive. |
| Cross-venue arb | Maybe | Benefit depends on slow leg, not just Kalshi. If Polymarket/other venue is slower, sub-2 ms to Kalshi alone may not clear leg risk. |
| Fed/CME propagation | Maybe | Chicago helps if signal source is CME-adjacent and Kalshi route is also better. Need release-time tests. |
| Weather KXHIGH | No for core edge | Signal half-life is minutes/hours. Data quality, station rules, and stale-book filters dominate. |
| Tennis pre-match | No | Current thesis is sharp-reference repricing, not microsecond execution. |
| CPI/PPI nowcasts | No pre-release; yes only for release sniping | Pre-release model does not need it. Release sniping needs institutional feed and still may be unwinnable. |
| Politics/entertainment/alternative data | No | Edge half-life is usually seconds to hours; bottleneck is source quality and resolution ambiguity. |

Decision: **do not commit to QuantVPS as production infrastructure yet**. First
deploy the benchmark and c-lead recorders to EC2 and a trial VPS, compare 24-72
hours of distributions, and keep the VPS only if it changes an executable edge
metric: fill rate, slippage, stale drops, positive CLV, or source lead minus
submit RTT.

## Strategy Ranking and How To Make Each Work

### 1. Weather Temperature / KXHIGH

Current repo state:

- `weather/calibration.py`, `weather/kxhigh.py`, `weather/temperature.py`.
- `plugins/strategies/weather_temperature_arbitrage.py`.
- `scripts/weather_kxhigh_paper.py`, `scripts/weather_settlement_reconcile.py`.
- `docs/weather-kxhigh-validation-and-edge-spec.md`.
- Live-paper runner can emit weather signals and record decisions.

Why it is high priority:

- Modelable, rule-based, less HFT-dependent.
- Current docs already reconcile KXHIGH station/settlement concerns.
- Best fit for a sustainable sleeve.

What must improve:

1. Lead-aware calibration: fit bias/sigma by station, month, lead time, bracket
   distance, and time of day.
2. High-so-far conditioning: daily high probability should condition on observed
   max so far plus remaining-hours forecast distribution.
3. Ensemble spread: use NOAA/NBM/HRRR/Open-Meteo multiple sources as uncertainty,
   not only point forecast.
4. Liquidity gate: reject wide spreads, tiny L1 size, stale quotes, no recent
   trades, and markets near resolution ambiguity.
5. CLV ledger: for every paper entry, log entry mid, model fair, entry fill,
   mid at +1/+5/+15/+30 minutes, near-close mid, settlement, fees, and PnL.
6. Passive adverse-selection analysis: maker fills are not free. Log mid before
   and after fill and separate maker/taker performance.
7. Station kill switches: halt on station settlement-source mismatch, negative
   CLV by bracket bucket, stale forecast, or calibration drift.

Latency requirement:

- Core model: 1-60 seconds is fine.
- Forecast-update reaction: 250 ms to 5 seconds is enough.
- Sub-2 ms VPS is unnecessary.

Capital:

- Dry/paper: $0.
- Micro live outside this workspace: $500-$2,000.
- Pilot: $5k-$25k.
- Mature cap: $25k-$150k, limited by book depth, city/event correlation, and
  station-specific drawdown.

Promotion gate:

- At least 100+ paper candidates with positive CLV after fee/spread stress.
- Positive settled PnL by station and bracket bucket.
- No single station or single weather regime explains all PnL.

### 2. Macro CPI/PPI/Fed/NFP

Current repo state:

- Strategy specs exist: `macro-cpi-predictor`, `macro-fed-gnn`,
  `macro-nfp-absorber`.
- Implementations are mostly generic external-signal/quote/timer engines.
- No mature data pipeline equivalent to weather/tennis was observed in this pass.

Best approaches:

1. Threshold ladder CDF: treat all strikes as one implied distribution, not
   independent binaries.
2. Nowcast ensemble: Cleveland Fed, market inflation swaps where accessible,
   component-level CPI/PCE, gasoline, shelter, used cars, wages.
3. Release calendar discipline: pre-release trades only unless you have a
   timestamped institutional release feed and proof of faster processing.
4. Cross-month Fed propagation: build transition matrix over meetings and rate
   paths; compare to CME Fed Funds/OIS probabilities.
5. Ladder no-arb scanner: monotonicity violations across `> threshold` contracts
   are often cleaner than directional nowcasts.

Latency requirement:

- Pre-release: seconds to minutes.
- Release sniping: sub-10 ms plus data vendor timestamp proof. Current EC2 is
  probably not enough for a pure release race.
- Fed/CME propagation: trial Chicago VPS only if CME-derived signal lead is proven.

Capital:

- Research: $0.
- Pilot: $5k-$25k.
- Mature: $50k-$250k, but event concentration is severe. Hard cap by release.

Promotion gate:

- Walk-forward event study over many releases.
- Real entry-price replay, not closing candles.
- No post-release timestamp leakage.
- Stress against one-tick, two-tick, and no-fill assumptions.

### 3. Tennis / Sharp Reference Repricing

Current repo state:

- `sports_tennis_xgboost` is one of the most developed sleeves.
- Docs show plain winner model is dominated by closing line.
- Existing strategy can consume external probabilities and live quotes.
- Runbook identifies odds-feed and schema-version gates.

Best approach:

1. Do not trade the standalone winner model.
2. Fair value should be sharp-reference consensus, preferably Pinnacle or a
   robust multi-book sharp blend, de-vigged.
3. Use XGBoost as fallback/residual, not primary alpha.
4. Capture real Kalshi tennis quotes and compare to sharp fair value at the
   exact tradable moment.
5. Extend runtime odds feed so missing odds fail loudly.

Latency requirement:

- Pregame: 1-30 seconds is enough.
- Injury/withdrawal/news shocks: 100-500 ms helps, but source quality dominates.
- Sub-2 ms VPS is not the main lever.

Capital:

- Paper: $0.
- Micro live: $500-$2k.
- Pilot: $5k-$25k.
- Mature: $25k-$100k if real Kalshi entry-price CLV is positive.

Promotion gate:

- Positive CLV vs sharp close at actual Kalshi fill prices.
- Separate performance by odds-source presence, tournament level, surface, and
  liquidity bucket.
- Confirm no model feature leakage or schema mismatch.

### 4. BTC / ETH 15-Minute Settlement

Current repo state:

- Settlement kernel is implemented and tested.
- Gap recorder exists and has stale-spot safeguards.
- Bench script proves compute is free and REST polling is too slow for final
  seconds.
- Missing decisive build: `btc_clead_recorder.py`.

Best approach:

1. Build c-lead recorder before execution.
2. Use Coinbase and Kraken public WS trades/ticker/order book.
3. Document CF Benchmarks constituents and US accessibility.
4. Compute synthetic index with source timestamps and received timestamps.
5. If official RTI print is not freely obtainable, label the best proxy as a
   proxy. Do not fabricate lead.
6. Record `c_synth`, constituent ages, gap to Kalshi book, quote age, spread,
   and whether an order could have arrived before edge expiry.

Latency requirement:

- Measurement: EC2 is fine.
- Execution: likely sub-10 ms, possibly sub-2 ms if the measured lead is only
  a few ms.

Capital:

- Measurement: $0.
- Micro live only after proof: $500-$2k.
- Mature: probably capped below $50k-$200k unless book depth and lead are robust.

Promotion gate:

- Synthetic index lead over official/proxy settlement print is reliably positive.
- Lead minus submit RTT remains positive.
- Candidate gap clears fee, spread, stale input, and adverse selection.
- Edge persists out of sample across many windows.

### 5. Equity Index Daily Close / Range Ladders

Current repo state:

- Configs and fee support exist; no mature equity close model was observed.
- Kalshi fee schedule has lower fees for S&P/Nasdaq products.

Best approach:

1. Model all range contracts as a terminal distribution.
2. Use ES/NQ futures, SPY/QQQ, fair-value adjustments, and official close timing.
3. Add Brownian bridge / intraday volatility model near close.
4. Add close-auction imbalance if accessible.
5. Use no-arb ladder constraints: adjacent ranges must sum sensibly, and CDF
   must be monotone.

Latency requirement:

- Midday range pricing: 1-10 seconds.
- Final 1-5 minutes: 10-100 ms.
- Final seconds/auction imbalance: sub-10 ms may help, but data source is more
  important than Kalshi route alone.

Capital:

- Pilot: $5k-$25k.
- Mature: $50k-$200k if liquidity supports it.

Promotion gate:

- Real quote replay in final 30 minutes.
- Positive CLV by minute-to-close bucket.
- Separate range-center from wing behavior.

### 6. Cross-Venue / Logical Arbitrage

Current repo state:

- `arbitrage_cross_venue.py` exists as a deterministic quote-pair strategy.
- Normalization and cross-venue modules exist.

Best approach:

1. Focus first on logical no-arb within Kalshi: yes/no parity, CDF monotonicity,
   range ladder consistency, mutually exclusive outcomes summing to one.
2. Then add cross-venue only where rules match exactly.
3. Track leg risk explicitly: quote time, decision time, first-leg fill, second
   leg availability, hedge miss.
4. Never treat cross-venue spread as risk-free without rule identity and fill proof.

Latency requirement:

- Internal scanner: 100 ms to seconds.
- Cross-venue taker arb: sub-10 to 100 ms depending on venue pair.
- A sub-2 ms Kalshi path helps only if the other leg is also fast.

Capital:

- Pilot: $1k-$10k.
- Mature: $10k-$50k; capacity usually decays quickly.

Promotion gate:

- Rule-matching audit per market pair.
- Fill-aware replay with both books.
- Leg-risk loss distribution.

### 7. Sports Live / Golf Hole-By-Hole / Cut-Line

Current repo state:

- Golf primitives and strategy specs exist.
- Hole-by-hole and cut-line strategy modules exist.
- These depend heavily on source availability and timestamp quality.

Best approach:

1. Use official/scored feeds with source timestamps, not scraped delayed pages.
2. Separate pre-event, in-play, and post-event market types.
3. For hole-by-hole, measure feed publication delay vs market move.
4. For cut-line, model full field distribution and weather wave effects.
5. Avoid markets where sportsbook/Kalshi participants reprice faster than your
   data feed.

Latency requirement:

- Cut-line: seconds to minutes.
- Hole-by-hole: 10-50 ms after source event, but only if feed is truly fast.
- Sub-2 ms host may matter for hole-by-hole but not for cut-line.

Capital:

- Pilot: $1k-$10k.
- Mature: $10k-$75k depending on liquidity.

Promotion gate:

- Feed-lag study.
- CLV by source delay bucket.
- Rule-resolution audit.

### 8. Politics / Courts / Policy / Tariffs

Current repo state:

- Several generic external-signal strategies exist.
- They are config/logic scaffolds more than proven alphas.

Best approach:

1. Build event-specific resolution-rule parser.
2. Use source hierarchy: official docket/calendar/bill tracker first, news second,
   social third.
3. Time-stamp source publication and market response.
4. Prefer market-making or patient limit orders over taker trades.
5. Use Bayesian state models, not sentiment-only classifiers.

Latency requirement:

- Usually seconds to hours.
- Sub-2 ms VPS is not needed.

Capital:

- Pilot: $1k-$10k.
- Mature: $10k-$100k only after rule ambiguity is controlled.

Promotion gate:

- Resolution-rule audit.
- Post-cost CLV and settlement PnL.
- Headline false-positive tracking.

### 9. Entertainment / Box Office / Awards

Current repo state:

- `entertainment_box_office.py` exists and has tests/parity artifacts.
- Still likely alternative-data constrained.

Best approach:

1. Focus on measurable releases: box office grosses, streaming ranks, awards
   nominations.
2. Use source timestamp and revision tracking.
3. Model market reaction lag, not just final outcome probability.
4. Avoid ambiguous resolution markets.

Latency requirement:

- Seconds to hours.
- Sub-2 ms VPS is not needed.

Capital:

- Pilot: $500-$5k.
- Mature: $5k-$25k.

Promotion gate:

- Positive CLV after official/source timestamp.
- Settlement ambiguity checklist.

## Cross-Cutting Improvements

### P0: Build the Missing BTC c-Lead Recorder

Create `python/scripts/btc_clead_recorder.py` with:

- `--no-network` self-test path.
- Coinbase and Kraken public WS subscription.
- Synthetic index ledger.
- Official RTI if obtainable; otherwise clearly labeled proxy.
- Per-source received timestamp, exchange timestamp, message age, and stale flag.
- JSONL output under `live-test/`.
- Summary statistics: lead median/p10/p90, source age, update rate, sequence gaps.

Gate: do not build a BTC execution thread until this shows positive lead greater
than residual submit RTT.

### P0: Make Latency a First-Class Ledger

Add a generic latency recorder that every paper/live run writes:

| Field | Meaning |
|---|---|
| `host_profile` | local, EC2 region, VPS provider/location. |
| `venue_endpoint` | Kalshi host and environment. |
| `source_exchange_ts` | timestamp inside message, if present. |
| `received_at` | local monotonic/wall timestamp. |
| `normalized_at` | after parser/normalizer. |
| `decision_at` | after strategy. |
| `risk_done_at` | after risk gate. |
| `gateway_enqueue_at` | if emitted. |
| `ack_at` | paper or venue ack. |
| `stale_drop` | whether gateway refused it as stale. |

This is the only way to decide whether EC2 or QuantVPS is better for a given
strategy.

### P0: Fix Quality-Gate Failures

Current failures to fix before any promotion:

- Python mypy exit 1:
  - `python/tests/test_tennis_pipeline.py:80`
  - `python/tests/test_sports_tennis_xgboost_strategy.py:165,179`
  - `python/src/eventcontracts/research/tennis_v2.py:851`
  - `python/src/eventcontracts/cli/live_paper.py:513`
  - `python/tests/test_weather_kxhigh.py:151,155`
  - `python/tests/test_tennis_v2_research.py:226,299,334`
- Rust fmt exit 1:
  - formatting drift in `rust/crates/runner/src/lib.rs`.
- Rust clippy exit 1:
  - large enum variants in `rust/crates/live-runner/src/main.rs`; box
    `LiveInput::External` and/or `WsLoopResult::Input`.

### P1: Use Current Kalshi Hosts and Rate-Limit Introspection

Kalshi docs recommend:

- REST production: `https://external-api.kalshi.com/trade-api/v2`.
- WS production: `wss://external-api-ws.kalshi.com/trade-api/ws/v2`.

Update scripts that still use `api.elections.kalshi.com` unless compatibility is
being tested. Add `/account/limits` introspection for authenticated reads where
available, and replace static write-throttle assumptions with token-bucket logic.

### P1: Promotion Gate Per Strategy

Every strategy should have a promotion packet with:

1. Market universe and exact resolution rules.
2. Data sources, source latency, and source failure behavior.
3. Feature schema and nullability.
4. Label construction and censoring.
5. Backtest with real fee curve.
6. Paper replay with real quote/order-book data.
7. CLV and settled PnL.
8. Execution mode attribution: maker, taker, passive-mid, IOC.
9. Capacity estimate from actual top-of-book depth.
10. Kill switches and exposure caps.

No strategy should graduate on calibration alone.

### P1: Market Discovery and Liquidity Scoring

Create a scanner that ranks markets by:

- two-sided executable spread,
- top-of-book size,
- recent trade activity,
- open interest,
- time to close,
- fee family,
- rule clarity,
- historical capture availability.

This prevents the top-volume trap observed in open-market queries: many multileg
sports markets show historical volume/open interest but no current two-sided
executable book.

### P1: Strategy-Specific Dashboards

Minimum per sleeve:

- active markets,
- events processed,
- source age,
- quote age,
- decision counts by reason,
- risk rejects by reason,
- hypothetical fills,
- CLV,
- PnL after fees,
- stale drops,
- sequence gaps,
- kill-switch state.

## Recommended Capital Policy

Use capital only after measurement gates:

| Stage | Capital | Rules |
|---|---:|---|
| Research | $0 | Record data, no orders. |
| Paper | $0 | Decision and fill simulation ledgers only. |
| Micro live outside this workspace | $500-$2k | Validate venue semantics, fees, fills, and cancels. |
| Pilot | $5k-$25k | Only if paper CLV and settled PnL are positive. |
| Sleeve | $25k-$250k | Only with strategy-specific drawdown and capacity proof. |

Never allocate by thesis. Allocate by realized fill quality, CLV, drawdown, and
capacity.

## Infrastructure Decision Tree

1. If strategy half-life is minutes or longer, use EC2 or any stable always-on
   host. Spend on data, validation, and monitoring.
2. If half-life is 100 ms to seconds, EC2 us-east may be enough. Measure WS
   source-to-decision latency and stale drops.
3. If half-life is 10-100 ms, run A/B on EC2 versus Chicago/QuantVPS for 24-72h.
4. If half-life is below 10 ms, do not trade unless colocated measurements prove
   source lead, gateway latency, and positive post-fill markout.
5. If the strategy depends on an official data release, source-feed latency
   dominates host latency. A faster VPS cannot fix a slow or delayed source.

## Immediate Next Steps

1. Fix the quality gates: mypy, Rust fmt, Rust clippy.
2. Deploy or inline-run the benchmark on EC2 and any trial VPS daily; write to
   `live-test/latency_ledger.jsonl`.
3. Build `btc_clead_recorder.py`.
4. Continue weather KXHIGH paper capture until settled PnL and CLV are meaningful.
5. Capture Kalshi tennis quotes and compare to sharp-reference fair value at
   actual entry prices.
6. Build market liquidity scanner before adding more strategies.
7. Keep live-submit disabled in this workspace until explicit promotion packets
   prove edge and operational safety.
