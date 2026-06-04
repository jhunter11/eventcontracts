# Video Transcript Ideas: Strategy Map

Date: 2026-06-02

Source: user-provided transcript about using AI agents to improve Polymarket
weather and tennis bots, plus a Hyperliquid liquidation-map freshness issue.

Scope: adapt the useful ideas to this Kalshi/eventcontracts repo. No orders or
live-submit are authorized.

## Useful Ideas From The Transcript

The transcript is informal, but several ideas are directly valuable:

1. Use multiple independent "agents" or research lanes to improve each strategy:
   signal, execution, and risk/failure modes.
2. Weather should price the **full probability distribution**, not a single point
   forecast.
3. Weather daily highs need **intraday high-so-far anchoring**: once the observed
   station high has crossed a threshold, lower buckets are impossible.
4. Close the PnL loop: every bot needs fill, CLV, settlement, and fee-net PnL
   tracking.
5. Tennis needs a fair-value model, not blind "stink bids."
6. Tennis live context matters: set score, game score, and whether the favorite is
   in genuine trouble.
7. Entries should be laddered; exits should be active, not always hold-to-expiry.
8. Stale listings are dangerous: the transcript's bot found already-finished
   matches still listed by the market source.
9. API freshness and rate limits matter: use one central cached data service for
   expensive calls, and distinguish snapshot data from fresh detail calls.
10. Small-size incubation is the right path after backtests, but only after the
    paper/CLV gates are clean.

## Where This Helps Existing Strategies

## Weather KXHIGH

This is the most directly useful part of the transcript.

### Idea: Full Ensemble Distribution

The transcript proposes using 51 or 119 ensemble members and calculating the
fraction that lands in each weather bucket. That maps cleanly to Kalshi KXHIGH:

```text
P(bucket F-C) = count(round(member_daily_high) in [F, C]) / member_count
P(high > T)   = count(round(member_daily_high) > T) / member_count
P(high < T)   = count(round(member_daily_high) < T) / member_count
```

Repo impact:

- Extend `weather/temperature.py` or `weather/calibration.py` to accept ensemble
  member highs, not only point forecasts.
- Add a `weather_ladder_cdf` research path that prices all same-city brackets from
  one distribution.
- Keep station/month/lead calibration on top of the ensemble, because raw ensemble
  probabilities can still be miscalibrated.

Why this matters:

- It avoids overconfident point-estimate bets.
- It naturally prices every bracket in a ladder.
- It gives a real uncertainty estimate for sizing.

### Idea: Intraday High-So-Far Anchor

This is probably the highest EV weather improvement.

For a daily high market:

```text
final_high = max(high_so_far_at_official_station, remaining_day_high_distribution)
```

If Central Park has already printed 77 F, then NYC buckets below 77 are dead,
subject to exact Kalshi settlement rules and observation rounding.

Repo impact:

- Add official-station observation ingestion for each KXHIGH city.
- Compute `high_so_far_f` and inject it into KXHIGH probability calculations.
- Add a stale-observation kill switch. A stale observation is worse than no
  observation because it manufactures "free money."
- Add a time-of-day rule: this signal gets stronger after late morning and
  strongest near the end of the local temperature day.

Important caveat:

- Do not use generic city weather. Use the exact settlement station/source already
  documented in `docs/weather-kxhigh-validation-and-edge-spec.md`.

### Idea: PnL Loop

The transcript explicitly says "close the loop P&L tracking." This is exactly
the right promotion standard.

Weather ledger should contain:

- model fair,
- market bid/ask/mid,
- entry price,
- execution mode,
- source age,
- quote age,
- high-so-far,
- forecast distribution,
- CLV at +1/+5/+15/+30 minutes,
- near-close mid,
- actual high,
- settlement,
- fee-net realized PnL.

This should be promoted from "nice to have" to "required."

## Tennis / Sports

The transcript's current WTA bot is primitive: flat $10 stink bids 30% below best
bid, refreshed every 15 minutes, hold to expiration. The useful lesson is not the
bot itself; it is the list of leaks.

### Idea: ELO Fair Value

ELO can help, but in this repo it should **not** replace sharp-reference pricing.
Our existing tennis research found the standalone winner model is dominated by
the closing line.

Best use:

- ELO as a fallback when sharp odds are missing.
- ELO as a sanity check against bad market data.
- ELO residual over sharp fair value, not standalone fair value.

Proposed formula:

```text
logit(fair) = logit(sharp_consensus) + residual(ELO, surface, form, fatigue)
```

If sharp consensus is unavailable:

```text
fair = shrink(ELO_probability, market_mid, reliability_weight)
```

### Idea: Live Set/Game Context

The transcript notes that a favorite down a set should not be treated the same
as a favorite cruising. This maps to a new observe-only research strategy:

`tennis_panic_dip_reversion`

Signal:

- pre-match sharp fair,
- current Kalshi price,
- set score,
- game score,
- serving state if available,
- implied probability drop,
- whether the favorite is down a set, down a break, or just suffered a temporary
  price panic.

Trade thesis:

- The market overreacts to a temporary favorite dip.
- But if the favorite is truly structurally impaired, do not bid.

Needed data:

- official or reliable tennis live score feed,
- source timestamp,
- match lifecycle validation,
- retirement/rules handling.

### Idea: Active Exits

The transcript's "sell 50% when price doubles" and "cut if favorite down in the
third" should become a paper-tested exit module, not a hardcoded rule.

Candidate exits:

- take partial profit when market price converges to fair,
- exit when sharp fair collapses,
- exit when live-score state invalidates the thesis,
- stop-loss when post-fill markout is sharply negative,
- hold to settlement only if exit liquidity is bad and model still supports it.

Repo impact:

- Add exit-policy fields to strategy specs.
- Extend ledgers to separate entry edge from exit edge.
- Never pool hold-to-expiry and active-exit results.

### Idea: Finished-Match Guard

The transcript's bot tried to trade an already-finished match. That is a critical
failure mode.

Add hard guards:

- market must be active/open,
- event must not be settled, determined, closed, or stale,
- current score feed must say match is live or pre-match,
- no trade if official match status and market lifecycle disagree,
- no trade if source result exists before quote timestamp.

This matters for all sports, not just tennis.

## BTC / Crypto Settlement

The Hyperliquid example in the transcript is about stale snapshot data versus a
fresh detail API call. That lesson maps directly to BTC settlement:

- Broad ticker snapshots may lag.
- Detail/source feed may be fresher.
- Rate limits force careful caching.
- Freshness must be recorded per source.

For `btc_clead_recorder.py`, add:

- `source_snapshot_ts`,
- `source_detail_ts`,
- `received_at`,
- `source_age_ms`,
- `cache_age_ms`,
- `rate_limit_backoff`,
- `constituent_health`,
- `used_for_decision`.

This helps avoid the exact stale-`c` bug already called out in AGENTS.md.

New possible crypto strategy from the transcript:

`crypto_liquidation_pressure_to_event_contracts`

Thesis:

- Large liquidation clusters on crypto perps can create short-horizon spot
  pressure that affects Kalshi BTC/ETH minute markets.

Caveats:

- This is not proven.
- Hyperliquid or perp data may have access/rate-limit constraints.
- US accessibility and data terms must be checked.
- It should start as an observe-only feature in the BTC c-lead ledger, not as a
  trading strategy.

## Macro / Equity / Brent Thresholds

The transcript's ensemble idea generalizes to all ladder markets.

For CPI, equity ranges, and Brent thresholds:

- build a full distribution,
- map it to every threshold/range,
- compare to the market-implied CDF,
- trade only residuals that survive fees and spread.

This reinforces the `full-ladder CDF engine` recommendation:

| Market | Distribution input |
|---|---|
| CPI/Core CPI | component nowcast + survey dispersion |
| Fed decisions | path transition model |
| S&P/Nasdaq | futures/ETF fair value + vol/Brownian bridge |
| Brent | futures/settlement source + intraday vol |
| Weather | ensemble + station calibration + high-so-far |

## Workflow Improvements

The transcript's strongest operational idea is not "use AI"; it is splitting
improvement work into independent lanes.

For every strategy improvement, run three lanes:

1. Signal agent:
   - What new information can improve fair value?
   - What source timestamp proves it is point-in-time?

2. Execution agent:
   - How should we enter, exit, size, cancel, and avoid stale orders?
   - Is the edge half-life compatible with host latency?

3. Skeptic/risk agent:
   - How is this fake?
   - Where is leakage?
   - What settlement/source/rate-limit/lifecycle bug would manufacture edge?

Then build only the idea that survives all three.

## New Strategy Ideas Inspired By Transcript

### 1. `weather_high_so_far_anchor`

Use official station observations to kill impossible buckets and reprice the
remaining daily-high distribution.

Priority: highest.

Latency: seconds to minutes.

### 2. `weather_ensemble_ladder_cdf`

Use ensemble member daily highs to price the entire city ladder.

Priority: high.

Latency: low.

### 3. `tennis_panic_dip_reversion`

Buy temporary overreaction dips only when sharp fair and live-score state say
the favorite is still undervalued.

Priority: medium, data-dependent.

Latency: medium if live score feed is fast.

### 4. `tennis_active_exit_policy`

Paper-test partial take-profit, fair-value convergence exits, and live-state
invalidations.

Priority: high for tennis if quote capture continues.

Latency: low to medium.

### 5. `crypto_snapshot_freshness_guard`

Not a standalone alpha: a guard/feature layer that prevents stale crypto source
data from creating fake edge.

Priority: high for BTC settlement work.

### 6. `liquidation_pressure_feature`

Use public perp liquidation clusters as a feature for BTC/ETH short-horizon
markets.

Priority: low/experimental.

Latency: high if used for trading; start observe-only.

### 7. `api_freshness_router`

Central service that caches expensive data, refreshes detail endpoints only for
candidate markets/traders/sources, and records cache age.

Priority: medium, useful across strategies.

## What To Copy Versus Avoid

Copy:

- full distribution instead of point estimate,
- high-so-far anchoring,
- active exits,
- laddered entries,
- lifecycle/finished-event guards,
- API freshness architecture,
- small-size incubation after paper proof,
- independent idea-generation lanes.

Avoid:

- blind flat-size stink bids,
- "hold everything to expiration,"
- trading already-finished or status-ambiguous events,
- treating ELO as enough against sharp markets,
- building many bots faster than we can validate them,
- assuming a backtest means future profitability,
- ignoring fees/spreads/rate limits.

## Best Immediate Application

The best concrete next build from this transcript is:

1. Add `high_so_far_f` to KXHIGH weather pricing.
2. Add ensemble ladder pricing for KXHIGH.
3. Add a tennis finished-match/lifecycle guard audit.
4. Add active-exit paper logic for tennis.
5. Add API/source/cache-age fields to BTC c-lead recorder.

These ideas align with our existing playbook and improve strategies that are
already close to research usefulness.

## Second Transcript Addendum: Backtest Discipline And Liquidation Ideas

The second transcript is less about Polymarket/Kalshi weather and more about the
research loop: generate many ideas, backtest them, throw away losers, robustness
test survivors, then incubate small. That maps well to this repo because our
main risk is not lack of ideas; it is promoting a false edge.

### Useful Ideas

1. Research, backtest, incubate is the right sequence.
2. Run competing hypotheses side by side instead of defending intuition.
3. Liquidation cascades often continue rather than mean revert, at least in the
   transcript's crypto tests.
4. Long-only and long-short variants can behave very differently.
5. Eye-popping returns are usually overfit until proven otherwise.
6. Robustness tests matter more than headline Sharpe.
7. Bad data can manufacture fake edge. The transcript's "broken low column"
   invalidated headline results because stops were not firing.
8. Use walk-forward, out-of-sample, Monte Carlo, parameter sensitivity, and raw
   unoptimized results before trusting any strategy.
9. Sortino can be useful for asymmetric crypto strategies, but it cannot rescue
   a bad or overfit backtest.
10. A simple existing backtesting framework is fine for research; the custom
    engineering should go into data correctness, labels, costs, and replay.

### How This Helps Existing Strategies

#### BTC / Crypto Settlement

The liquidation material is potentially relevant, but only as a feature, not as
a standalone permission to trade.

Possible feature:

```text
liquidation_pressure = signed liquidation notional near current BTC/ETH price
```

Hypothesis variants:

- momentum: liquidation cascade continues and pushes spot further;
- mean reversion: liquidation shock exhausts and reverses;
- volatility-only: liquidation cluster widens short-term distribution but has no
  directional edge;
- no-trade filter: liquidation regime makes BTC settlement prices too unstable
  for stale Kalshi orders.

Best repo application:

- add liquidation-pressure fields to `btc_clead_recorder.py` as observe-only
  features,
- compare forward spot move after liquidation clusters,
- compare Kalshi BTC15M markout by liquidation regime,
- do not trade until the c-lead and tradable-gap gates are already green.

Data concerns:

- source must be US-accessible and allowed by terms,
- source timestamps must be reliable,
- rate limits must be measured,
- liquidation data must be joined point-in-time,
- exchange-specific liquidation clusters may not map to CF Benchmarks RTI.

#### Macro / Equity / Brent

The key idea is not liquidation; it is hypothesis competition.

For every ladder strategy, explicitly test:

- momentum after source update,
- mean reversion after market overreaction,
- volatility expansion without direction,
- liquidity-premium capture,
- no-arb correction.

Example for CPI:

- momentum: post-release front threshold move propagates to far thresholds;
- mean reversion: first post-release Kalshi move overshoots;
- no-arb: threshold ladder violates monotonicity after release;
- liquidity: spreads widen pre-release more than realized outcome risk.

Example for Brent:

- momentum after large futures move;
- mean reversion after inventory/news spike;
- threshold CDF repricing lag;
- volatility-only widening before settlement.

#### Weather

Apply robustness testing to weather before adding stations:

- walk-forward by year,
- hold out entire stations,
- hold out months/seasons,
- hold out weather regimes,
- perturb forecast by +/-1 F,
- perturb high-so-far by source rounding uncertainty,
- stress fees/spread/fill assumptions,
- compare raw ensemble, calibrated ensemble, high-so-far anchor, and market-shrunk
  versions.

The lesson from the broken-low-column bug is especially relevant: if the official
station high or day boundary is wrong, weather backtests can look amazing and be
entirely fake.

#### Tennis

For tennis, test strategy families, not one bot:

- pre-match sharp-reference residual,
- live favorite dip reversion,
- favorite dip momentum against injured/tilting favorite,
- active exits versus hold to settlement,
- laddered entry versus single stink bid,
- long favorites only versus both sides.

Robustness:

- walk-forward by season,
- hold out tournaments,
- hold out surfaces,
- hold out player popularity buckets,
- remove all matches without sharp odds,
- apply real Kalshi quote spreads,
- drop finished/status-ambiguous matches.

### Backtest Robustness Checklist

Every strategy result should include:

| Test | Purpose |
|---|---|
| Raw unoptimized parameters | Prevent optimizer mirage. |
| Walk-forward split | Prevent future leakage. |
| Out-of-sample holdout | Confirm generalization. |
| Parameter grid sensitivity | Check if result exists only at one magic parameter. |
| Monte Carlo trade shuffle | Estimate path/drawdown fragility. |
| Cost stress | Fees, 1-2 ticks slippage, half fills, no fills. |
| Data perturbation | Check whether small data errors erase edge. |
| Regime breakdown | Avoid one-regime-only alpha. |
| Capacity stress | Check if available book depth supports size. |
| Timestamp audit | Ensure signals existed before tradable quotes. |

### Backtest Red Flags

Treat these as automatic "do not promote":

- returns are huge but Sharpe/Sortino are mediocre,
- one data bug explains the whole result,
- result only works after heavy optimization,
- long-short loses but long-only wins without explanation,
- all PnL comes from one day, one event, one station, or one player,
- no fee/spread/slippage applied,
- labels use close/settlement data not known at decision time,
- strategy trades events that were already finished,
- no drawdown or trade-count reporting,
- no actual entry-price replay.

### New Build Ideas From This Transcript

#### `research_robustness_harness`

A reusable CLI that runs:

- walk-forward,
- parameter perturbation,
- Monte Carlo trade shuffle,
- cost stress,
- regime breakdown,
- output as markdown plus JSON.

Use it for weather, tennis, macro, Brent, equity, and BTC candidate ledgers.

#### `data_integrity_audit`

Before any backtest result is accepted, validate:

- OHLC columns are sane,
- high >= low,
- open/close within high/low,
- no fabricated lows/highs,
- timestamps monotone,
- no duplicate event IDs,
- no future-known labels in features,
- source age is available.

This directly addresses the transcript's corrupted-low-column failure.

#### `hypothesis_tournament`

For each market family, run momentum, mean-reversion, no-arb, liquidity, and
volatility-only hypotheses on the same point-in-time dataset and rank them by
post-cost CLV, not just return.

#### `liquidation_pressure_observer`

Observe-only crypto feature module:

- liquidation cluster distance from spot,
- signed notional,
- cascade direction,
- distance to next cluster,
- forward spot returns at 1s/5s/30s/60s,
- Kalshi BTC15M quote response.

Promotion gate: only becomes a trading feature if it improves BTC c-lead or
gap-recorder markout out of sample.

### Best Immediate Application

From this second transcript, the most useful immediate build is not a liquidation
bot. It is a **robustness harness** plus **data integrity audit** for the
strategies we already care about.

Priority order:

1. `data_integrity_audit` for all research datasets.
2. `research_robustness_harness` for weather and tennis first.
3. `hypothesis_tournament` for BTC, Brent, CPI, and equity ladders.
4. observe-only liquidation features inside the BTC recorder.

This turns the transcript's useful lesson into process alpha: fewer false
positives, faster idea rejection, and cleaner promotion decisions.

## Third Transcript Addendum: Anchored VWAP

The third transcript is useful, but not because anchored VWAP should be bolted
onto every strategy. Its best lesson is more precise:

> Anchored price context can explain who is in profit since a known event, but a
> mean-reversion tool can hurt a momentum/cascade strategy if used as an exit.

In event contracts, AVWAP should be treated as an **event-anchored context
feature**, not a standalone edge. The core question is not "did price reclaim
AVWAP?" It is:

> Does anchored price context improve the probability distribution of the event
> payout at the tradable moment, after fees, spread, stale-data gates, and fill
> assumptions?

### What AVWAP Actually Adds

Anchored VWAP answers:

- since this anchor event, what is the volume-weighted average entry price?
- are post-anchor buyers or sellers mostly in profit?
- is current price extended from the post-anchor participant cost basis?
- did price reclaim, reject, or compress around that cost basis?
- is the move momentum continuation, mean reversion, or exhaustion?

That is valuable for markets whose settlement variable is price-like:

- BTC and ETH spot or index settlement,
- equity index levels,
- Brent or crude oil prices,
- rates or futures reaction after macro releases,
- any threshold ladder where the underlying follows a traded price series.

It is weak or indirect for weather, CPI, politics, and entertainment unless we
translate the idea into "anchored market reaction" rather than literal VWAP.

### BTC Settlement-Arb Application

For `KXBTC15M`, AVWAP belongs first inside the c-lead and gap-recorder workflow
as observe-only features:

- AVWAP anchored to the 15-minute Kalshi contract open,
- AVWAP anchored to the 60-second settlement averaging window open,
- AVWAP anchored to a liquidation cascade start,
- AVWAP anchored to a large Coinbase/Kraken constituent impulse,
- AVWAP anchored to a funding/open-interest shock if that data is available,
- distance from spot/index to AVWAP in dollars and basis points,
- AVWAP slope over 1s/5s/15s/60s,
- volume since anchor,
- reclaim/rejection state,
- time since last anchor.

Use cases:

- **Regime filter:** when spot is far above anchored VWAP after a liquidation-up
  impulse, treat mean-reversion and continuation hypotheses separately.
- **Basis warning:** if Coinbase spot moves far from post-anchor AVWAP while
  synthetic CFB constituents do not, the apparent Kalshi gap may be an exchange
  basis artifact.
- **Volatility prior:** distance from AVWAP and post-anchor volume can update
  short-horizon sigma in the settlement kernel.
- **No-trade filter:** if price is oscillating around AVWAP and Kalshi spread is
  wide, the edge may be too noisy to pay fees.
- **Continuation detector:** if price reclaims AVWAP with rising post-anchor
  volume after a failed liquidation flush, model continuation rather than
  immediate reversion.

Avoid using AVWAP as a naive exit for liquidation momentum trades. The
transcript's own tests found that AVWAP exits hurt violent liquidation/cascade
strategies because they cut winners before the cascade paid. For BTC, test these
as separate hypotheses:

| Hypothesis | Expected Role | Promotion Gate |
|---|---|---|
| AVWAP mean reversion | Fade extension from post-anchor cost basis | Positive post-cost markout when Kalshi is stale or overreacted |
| AVWAP continuation | Follow reclaim/rejection with volume confirmation | Positive CLV and no early exit problem |
| AVWAP no-trade filter | Skip noisy chop around fair post-anchor cost | Improves risk-adjusted return by reducing bad trades |
| AVWAP volatility prior | Improve settlement distribution width | Better calibration and Brier/log-loss |
| AVWAP basis detector | Distinguish Coinbase-only moves from index moves | Fewer false BTC gap flags |

### Equity, Nasdaq, And Brent Thresholds

For equity index and commodity ladders, anchored VWAP is more naturally useful
than in many non-price event contracts.

Useful anchors:

- market open,
- cash open,
- futures session open,
- FOMC/CPI/NFP release timestamp,
- EIA inventory release for oil,
- OPEC/headline timestamp,
- prior swing high/low,
- start of a high-volume directional impulse,
- opening range breakout or failure.

Features:

- underlying price distance from anchor VWAP,
- slope and curvature of anchor VWAP,
- realized volatility since anchor,
- volume concentration since anchor,
- reclaim/rejection around anchor VWAP,
- distance to Kalshi threshold bins,
- probability mass crossing each threshold if drift is conditioned on AVWAP
  state.

How it should change models:

- shift Brownian-bridge or terminal CDF drift only when the anchored state has
  out-of-sample predictive value,
- widen the distribution during high-volume AVWAP rejection/reclaim regimes,
- prefer ladder/no-arb trades when AVWAP state says the market-implied ladder is
  internally inconsistent,
- do not trade from chart signals directly unless the chart signal improves
  Kalshi contract markout.

### Tennis, Weather, And Non-Price Markets

Do not force literal AVWAP where there is no traded underlying volume series.
Use the analogy instead: anchor to the moment new information arrived and track
how the market repriced from that point.

For tennis:

- anchor to match start,
- anchor to a break of serve,
- anchor to injury/medical timeout/news,
- anchor to a large sharp-odds move,
- track Kalshi price versus anchored sharp fair price,
- detect overreaction or delayed reaction after state changes.

For weather:

- anchor to major forecast cycle updates,
- anchor to official observation prints,
- anchor to high-so-far updates,
- track Kalshi mid change since anchor,
- detect stale markets after forecast/observation updates.

For CPI/Fed/macro:

- anchor to release timestamp,
- anchor to first Treasury/futures reaction print,
- anchor to second-wave revision after the first liquidity vacuum,
- measure whether Kalshi repricing lags the most relevant liquid proxy.

### Anchor Selection Rules

The easiest way to fake AVWAP edge is to choose anchors with hindsight. All
anchors must be ex-ante and timestamped before the quote being evaluated.

Allowed anchor types:

- scheduled event timestamps,
- market/session opens,
- contract opens,
- official data releases,
- mechanically detected swings using only past data,
- mechanically detected volume/liquidation impulses using only past data,
- first quote after a known news timestamp.

Disallowed anchor types:

- best-looking swing low selected after the move,
- anchor chosen because the backtest worked,
- anchors based on future high/low labels,
- anchors that depend on final settlement outcome,
- manually curated chart points without a reproducible rule.

### New Build Ideas

#### `event_anchor_feature_builder`

A generic feature builder that accepts point-in-time anchor events and emits:

- anchored mean or VWAP,
- distance to anchor mean/VWAP,
- slope,
- volume since anchor,
- reclaim/rejection state,
- max favorable/adverse excursion since anchor,
- feature age,
- anchor provenance.

This should work for BTC/ETH, equity indexes, Brent, and market-price analogues
such as tennis sharp odds or Kalshi mids.

#### `crypto_avwap_observer`

An observe-only BTC/ETH module for:

- Coinbase/Kraken AVWAP by anchor,
- synthetic-index AVWAP by anchor,
- liquidation-cluster AVWAP,
- settlement-window AVWAP,
- post-anchor volatility and volume,
- Kalshi quote response after each anchor.

Promotion gate: it only becomes part of execution if it improves BTC c-lead,
gap-recorder markout, or calibration out of sample.

#### `commodity_equity_event_anchor_cdf`

For `KXINX`, `KXNASDAQ100`, `KXBRENTD`, and similar markets, condition the
terminal CDF on anchored state:

- above/below anchor VWAP,
- distance to anchor VWAP,
- reclaim/rejection,
- post-anchor realized volatility,
- post-anchor volume impulse.

This is not a chart-pattern bot. It is a probability-distribution adjustment
that must beat the unconditioned model after spread and fees.

#### `anchor_selection_audit`

A leakage audit that verifies:

- every anchor timestamp precedes every decision timestamp,
- anchor rules are deterministic,
- no settlement labels are used in anchor selection,
- anchors are available in live mode,
- backtest and live code produce identical anchors from the same input stream.

### Immediate Priority

Use this transcript in this order:

1. Add AVWAP-like anchored features to BTC c-lead/gap ledgers as observe-only
   columns.
2. Add anchored event features to equity, Nasdaq, and Brent threshold CDF
   research.
3. Add anchor-selection checks to the robustness harness.
4. Use anchored-market-reaction analogues for tennis and weather, not literal
   VWAP.
5. Do not use AVWAP as a default exit for liquidation momentum. Test
   continuation, mean reversion, no-trade filtering, and volatility-prior
   variants separately.

The strongest takeaway is that anchored features can improve our understanding
of **when the market is stale, extended, or absorbing a shock**, but the payoff
model still has to be Kalshi-specific. AVWAP is context; the edge remains
post-cost event-contract mispricing.

## Fourth Transcript Addendum: Sports Bot Factory And In-Play Data Latency

This transcript is noisy, but it contains several useful ideas for our Kalshi
research stack:

- use schedule/market discovery to find many candidate events,
- understand the actual game structure before modeling,
- rank data sources by latency before assuming an edge,
- incubate many small hypotheses,
- aggressively enforce order/position lifecycle invariants,
- treat one lucky win as noise, not validation.

The dangerous part is the "build it and trade it immediately" energy. For this
repo, the translation is stricter:

> Build many read-only observers and paper strategies, but promote nothing until
> point-in-time data, fair-value model, quote replay, fill simulation, and
> lifecycle safety are proven.

### Sports Data-Latency Lesson

The best part of the transcript is the tennis data hierarchy:

| Data Source | Typical Role | Edge Implication |
|---|---|---|
| Venue observer / courtside feed | Fastest score source | Compliance and terms risk; do not assume usable |
| Licensed scout feed | Professional-grade live data | Potential edge if allowed and faster than market |
| Premium API | Practical systematic source | Measure delay versus exchange quotes |
| TV/video stream | Often very delayed | Usually too stale for in-play scalping |
| Public scoreboard | Easy but variable | Good for research, often weak for latency edge |

For Kalshi, the key is the same as the BTC c-lead task: measure lead before
building execution. A sub-2 ms VPS does not matter if the sports score source is
5-30 seconds behind the market. The binding latency is usually **data source
freshness**, not our local compute.

Decision gate:

> If the score/news/official-data feed is not reliably ahead of Kalshi quote
> updates by more than fees, spread, and execution latency, there is no in-play
> latency edge.

### Tennis: Directly Relevant To Existing Work

This one maps cleanly onto the existing tennis path.

The transcript's usable tennis ideas:

- tennis is year-round, so data volume is attractive,
- every point changes fair value,
- no draw makes settlement clean,
- server state creates predictable conditional probabilities,
- WTA and ATP can have different volatility/upset profiles,
- best-of-three and best-of-five are different model regimes,
- live markets may not support point-by-point trading even if the sport does.

For our current tennis model, the next research layer should be:

- `tour`: ATP/WTA/Challenger/ITF if available,
- `format`: best-of-three versus best-of-five,
- `surface`,
- `server`,
- `score_state`: set/game/point score,
- `break_point`, `set_point`, `match_point`,
- `retirement_or_walkover_risk`,
- sharp odds before match,
- sharp odds live if available,
- Kalshi/market liquidity and spread,
- quote age and match status age.

The most promising tennis strategy families:

| Strategy | Signal | Latency Sensitivity | Risk |
|---|---|---|---|
| Pre-match selective model | Existing odds-enriched tennis model | Low | Model calibration and stale/finalized events |
| Live score lag | Official score ahead of market quote | High | Data feed rights, stale score, thin books |
| Favorite dip reversion | Strong favorite loses early game/set but model still favors them | Medium | Injury, retirement, real regime shift |
| WTA volatility premium | Market under/overprices higher break/upset variance | Low-medium | Needs tour-specific calibration |
| Best-of-five favorite comeback | ATP slam favorite down early but still structurally favored | Medium | Available markets and liquidity |

Immediate improvement:

> Add tennis lifecycle/status features and sport-format features before any
> live-score strategy. The previous odds/confidence gate is already the right
> deployment shape; live score should be an incremental gated feature, not a
> replacement.

### Passive "Stink Bid" Translation

The transcript builds a simple passive-bid idea: find favorites, bid far below
the current price, cancel after a cutoff, and avoid duplicate orders.

The useful abstraction is not "bid 30% low." The useful abstraction is:

> Passive liquidity provision around a fair-value model, with strict lifecycle
> controls.

A Kalshi-safe research version:

- discover liquid two-sided markets,
- compute fair value from a model,
- place **paper** passive bids only when model edge exceeds spread + fee +
  adverse-selection buffer,
- cancel when event state changes,
- cancel when the quote/feed is stale,
- cancel after a sport-specific time cutoff,
- never maintain duplicate open orders for the same side/market,
- never accidentally bid both sides without explicit inventory logic,
- stop when status is in-play phase where model is invalid,
- cap exposure by market, event, sport, and correlated cluster.

The transcript exposed a real bug class: open orders remained while the bot also
had a position, and the favorite flipped, causing both-side confusion. That is
exactly the kind of issue our live-runner/risk layer must catch.

Required invariants:

- one active intent per market/outcome unless explicitly allowed,
- open orders and positions share one canonical exposure view,
- side flips cancel stale orders before placing new ones,
- filled orders update exposure before the next decision,
- market status and event status are both checked,
- all cancels and quote ages are logged,
- no order is considered live unless the exchange ack says it is live,
- paper mode uses the same lifecycle state machine as live mode.

### Baseball

Baseball can be modeled, but "favorite plus passive bid until fifth inning" is
too crude.

Useful state variables:

- inning,
- top/bottom,
- score differential,
- base/out state,
- pitcher,
- bullpen availability,
- home/away,
- park factor,
- weather,
- pregame moneyline,
- live moneyline,
- run expectancy,
- leverage index,
- lineup strength.

Safer research strategies:

- stale-market lag after scoring plays,
- passive bids around a fair live win-probability model,
- bullpen fatigue mispricing,
- starting-pitcher removal repricing lag,
- weather/wind-total interaction if related markets exist,
- late-game no-trade zones where variance/adverse selection spikes.

Cutoff logic should be model-driven. "Cancel after the fifth" might make sense
for one passive pregame-style strategy, but live baseball state is nonlinear:
a one-run game with bases loaded is not the same as a five-run game with no one
on base.

### Cricket

The transcript notices that cricket structure is unfamiliar, then tries to map
it onto "half the game." That is a good instinct but not enough.

For T20/IPL-style markets, model:

- innings number,
- overs remaining,
- wickets lost,
- current run rate,
- required run rate,
- target score,
- batting order depth,
- toss result,
- venue scoring environment,
- team strength,
- chase versus defend dynamics.

Potential strategies:

- market underreacts to wicket clusters,
- market overreacts to early run-rate bursts,
- chase model disagreement after first innings target is set,
- liquidity-provider bids only before predefined model-valid phases,
- no-trade once public score feed is stale or innings phase is ambiguous.

Cricket is globally liquid in sports betting, but event-contract liquidity must
be measured directly. Do not assume global fanbase equals usable Kalshi depth.

### Mention, Tweet, And News Markets

The transcript's "Elon tweet bot" and "mention markets" idea maps to a broad
news/event-detection sleeve.

Useful markets:

- whether a person/company is mentioned,
- whether a public figure posts,
- whether a company announces something,
- whether a product/event occurs by a deadline,
- awards/media/social outcomes.

Research architecture:

- source collectors: X/Twitter, official RSS, SEC/press releases, YouTube,
  company blogs, government pages,
- entity extraction and alias matching,
- source credibility scoring,
- duplicate/rumor filtering,
- timestamped first-seen ledger,
- market quote response ledger,
- label resolver tied to exchange settlement rules.

Failure modes:

- false positives from parody or screenshots,
- ambiguous wording versus market rules,
- deleted posts,
- rate limits,
- API outages,
- news already priced before public source arrival,
- unofficial sources that do not count for settlement.

This is potentially high edge if the market lags public official sources, but
latency must be measured source-by-source.

### Tokenomics And Buyback Narratives

The Hyperliquid/Pump-style buyback discussion is not directly a Kalshi sports
edge, but the framework is useful for crypto/company event markets:

- fee revenue,
- buyback amount,
- burn versus treasury accumulation,
- float/circulating supply,
- buyback yield,
- market cap,
- revenue multiple,
- unlock schedule,
- insider/team wallet behavior,
- narrative velocity.

For Kalshi, this belongs in a **fundamental event/reaction model**, not a blind
price bot. It can help with markets tied to crypto protocols, company news,
ETF/stock outcomes, or public-company proxy exposure.

### Multi-Bot Incubation Is Good, But Only With Clean Measurement

The transcript's most robust workflow idea is to generate many small hypotheses
quickly. That is powerful if the measurement layer is strict.

Bad version:

- build three bots,
- trade live immediately,
- count a lucky first win as evidence,
- lose track of overlapping exposure,
- manually inspect PnL,
- change rules midstream.

Good version:

- build many observers,
- run paper only,
- log every decision and skipped decision,
- mark each with model fair, quote, spread, fee, data age, and reason code,
- evaluate CLV and markout,
- kill weak hypotheses quickly,
- promote only after out-of-sample incubation.

### New Build Ideas

#### `sports_market_discovery`

Read-only scanner that outputs:

- active sports/event markets,
- sport/league/tournament,
- start time,
- event phase,
- liquidity,
- spread,
- volume,
- available outcome types,
- matched external schedule event,
- model availability.

This prevents blindly running a bot on markets with no depth or unclear event
status.

#### `sports_state_model_lab`

Research module for tennis/baseball/cricket:

- canonical game-state schema,
- sport-specific win-probability models,
- sharp odds comparison,
- event-contract fair-value projection,
- quote replay,
- calibration and CLV reports.

Start with tennis because this repo already has a deployed odds-enriched tennis
path.

#### `paper_passive_bid_simulator`

A paper-only simulator for passive market making/stink-bid ideas:

- fair-value thresholding,
- queue/fill assumptions,
- cancel/replace rules,
- stale quote and stale score gates,
- no duplicate orders,
- no accidental both-side exposure,
- sport-specific phase cutoffs,
- adverse-selection markout.

Promotion requires profitable post-cost markout under conservative fill
assumptions, not just "the favorite won."

#### `order_lifecycle_invariant_tests`

Shared tests for the runner/live-runner state machine:

- duplicate order prevention,
- position plus open-order exposure,
- side-flip cancellation,
- stale quote cancellation,
- market close cancellation,
- paper/live parity of lifecycle events,
- crash/restart recovery from open-order snapshots.

This directly addresses the transcript's observed bug where an open order and
existing position coexisted in a confusing way.

#### `mention_news_observer`

Observe-only module for mention/tweet/news markets:

- official-source polling,
- social-source polling,
- NLP alias matching,
- confidence scoring,
- first-seen timestamp,
- exchange quote response,
- settlement-rule matching.

No trading until false-positive rate and market reaction lag are measured.

#### `hypothesis_incubator_dashboard`

A dashboard/report that ranks paper strategies by:

- decisions,
- fills under conservative simulation,
- skipped reasons,
- CLV,
- 5m/30m/settlement markout,
- drawdown,
- parameter sensitivity,
- data-staleness rejects,
- lifecycle rejects,
- correlation with other sleeves.

This turns "launch lots of bots" into controlled research rather than chaos.

### Immediate Priority

Use this transcript in this order:

1. Extend the existing tennis research with ATP/WTA/format/status features.
2. Build `sports_market_discovery` read-only before any sport-specific bot.
3. Build `paper_passive_bid_simulator` for passive-bid ideas.
4. Add `order_lifecycle_invariant_tests` for duplicate order/position bugs.
5. Build `mention_news_observer` for tweet/mention/news markets.
6. Treat baseball/cricket as later research unless live data and market depth
   are confirmed.

The punchline: this transcript is not strong because of the specific bots. It is
strong because it points to a reusable **market discovery -> state model ->
paper incubation -> lifecycle-safe execution** pipeline. That pipeline is useful
for Kalshi; live improvisation is not.

## Fifth Transcript Addendum: Whale Flow, 0DTE Logic, And Trapped Traders

This transcript is mostly about Polymarket whale scanning, liquidation
backtests, and 0DTE options. It is not directly a Kalshi strategy recipe, but it
has a strong transferable frame:

> The most interesting short-horizon edges cluster around position asymmetry,
> volatility mispricing, and execution timing.

That maps well to event contracts because many Kalshi markets are effectively
short-dated binary options. The danger is also the same as 0DTE options:
apparent returns can be enormous, but one bad fill, stale input, overfit
parameter, or compounding bug can fabricate the entire result.

### The Three-Edge Framework

| 0DTE Edge Concept | Kalshi Translation | Best Markets |
|---|---|---|
| Position asymmetry | Who is trapped, crowded, overhedged, or forced to react? | BTC15M, equity index, Brent, sports live, politics/news |
| Volatility mispricing | Market-implied distribution is too wide or too narrow | BTC15M, equity ladders, Brent, CPI/Fed, weather |
| Execution timing | Edge exists only at specific seconds/minutes around state changes | BTC settlement window, macro releases, market open/close, weather obs, live sports |

This is a cleaner organizing principle than asking "what bot should we build?"
For each candidate market, the research question becomes:

1. Is there a forced-position or stale-reaction mechanism?
2. Is the market's implied probability distribution wrong?
3. Does the edge exist at a moment where our data arrives before the quote
   adjusts?
4. Can we enter and exit at prices that preserve the edge after fees and spread?

If one of those answers is missing, the idea stays in observe-only mode.

### Position Asymmetry

The transcript's "trapped traders" idea is useful, especially for crypto and
short-dated index markets.

For BTC15M:

- liquidation clusters above/below spot,
- forced perp flow,
- funding imbalance,
- large Coinbase/Kraken impulse,
- order-book imbalance near Kalshi threshold,
- Kalshi quote movement lagging synthetic spot/index movement,
- whether the final 60-second settlement window creates forced repricing.

For equity index and Brent:

- open-range failure after one-sided positioning,
- crowded breakout that reverses,
- large ETF/futures move versus stale Kalshi ladder,
- macro-release first move that gets faded,
- threshold pinning or threshold panic near expiry.

For sports:

- favorite/underdog side flips,
- live score changes before market adjustment,
- retirement/injury/news repricing,
- lopsided public favorite pricing versus sharp odds.

For news/mention markets:

- large holder or market maker inventory visible through quote behavior,
- rumor already priced before official confirmation,
- official source posts while market still reflects pre-news odds.

Promotion gate:

> Position asymmetry only matters if it predicts Kalshi quote markout, not merely
> underlying price movement.

### Volatility Mispricing

0DTE options are dominated by IV versus realized move. Kalshi analogues are
market-implied event distributions versus realized or model-implied
distributions.

Use this in:

- `KXBTC15M`: implied short-horizon sigma versus Coinbase/Kraken realized sigma
  and settlement-kernel sigma.
- Equity index ladders: market CDF width versus futures/ETF realized vol and
  event-time vol.
- Brent/oil: ladder width versus realized energy futures vol and inventory/OPEC
  event regimes.
- Weather: Kalshi ladder width versus forecast ensemble uncertainty and
  high-so-far constraints.
- CPI/Fed: market bins versus nowcast dispersion, economist dispersion, and
  release-day historical surprises.

Required diagnostics:

- implied distribution width,
- realized distribution width,
- calibration by market-implied vol bucket,
- edge after 1/2/3 tick cost stress,
- Brier/log-loss improvement versus market,
- CDF no-arb consistency.

This should feed the `hypothesis_tournament` and `research_robustness_harness`
instead of becoming one-off scripts.

### Execution Timing

The transcript makes a good point: in very short-dated instruments, timing can
matter more than directional opinion.

Kalshi timing windows to study:

- BTC15M contract open,
- BTC final 60-second averaging window,
- BTC settlement boundary,
- equity market open,
- equity power hour and close,
- CPI/Fed/NFP release second,
- weather official observation updates,
- forecast model publication times,
- live sports score updates,
- official news/source publication timestamp.

For each window, log:

- source timestamp,
- first local receipt timestamp,
- first Kalshi quote update timestamp,
- best bid/ask before and after,
- spread,
- book depth,
- model fair before and after,
- whether a passive or aggressive fill would plausibly occur,
- 1s/5s/30s/5m/settlement markout.

The right product here is not a bot first. It is a **timing ledger**.

### Whale Scanner Translation

A Polymarket whale scanner does not transfer directly unless Kalshi exposes
enough public trade/order-book information. The useful version is a public
flow-and-inventory observer:

- large trade prints,
- sudden quote size changes,
- repeated quote replenishment,
- lopsided depth,
- spread tightening/widening,
- market maker pullback after news,
- price movement without underlying movement,
- underlying movement without Kalshi movement.

Features:

- signed print imbalance,
- depth imbalance,
- quote replenishment rate,
- cancel/replace intensity,
- price impact per dollar,
- time since last large print,
- market-level concentration proxy,
- cross-market ladder consistency.

Promotion gate:

> Flow features become tradable only if they predict event-contract markout
> after controlling for the underlying/model signal.

### 0DTE Capitulation Analogy

The transcript's "buy capitulation off the open" idea maps to Kalshi as an
opening-shock reversal/continuation observer.

Markets to test:

- equity index daily threshold ladders,
- Nasdaq/S&P close-above or range markets,
- Brent daily/range markets,
- BTC after large overnight or open-session impulse,
- macro-release ladders immediately after first print.

Hypotheses:

| Hypothesis | Description | Failure Mode |
|---|---|---|
| Opening panic reversal | First move is overdone and mean reverts | Trend day crushes fades |
| Opening breakout continuation | First move reveals real regime | Choppy open fakes out |
| Threshold panic | Market overpays near a round threshold | Underlying actually crosses |
| Quote pullback | Market makers widen/pull quotes after shock | No fill or toxic fill only |
| Vol overpricing | Kalshi distribution too wide after shock | Realized vol remains elevated |

This should be tested as a regime classifier, not a fixed "fade every open"
rule.

### Venue And Routing Lesson

The transcript compares brokers/venues for 0DTE execution. The Kalshi version:

- compare API endpoints and websocket freshness,
- measure rate-limit behavior,
- measure authenticated read/write latency in paper where possible,
- model fee drag,
- model spread and depth,
- distinguish maker/passive from taker/aggressive execution,
- track cancel reliability,
- track order ack and book reflection time,
- keep venue-specific assumptions out of shared strategy configs.

For non-Kalshi research, broker choice matters because payment for order flow,
rebates, routing, options permissions, margin, and paper/live behavior can
change the effective edge. For Kalshi, the analogous question is whether the
market has enough depth and API reliability for the strategy's execution style.

### Backtest Artifact Warnings

The transcript explicitly mentions stale artifacts and a compounding artifact.
That is more valuable than the huge reported returns.

Add these checks to the robustness harness:

- every report includes git commit, data hash, config hash, and code hash,
- stale combined CSVs are invalidated when sizing or cost assumptions change,
- annualized return is recomputed from raw trade ledger,
- compounded and uncompounded returns are both shown,
- trade count and average holding period are always shown,
- Sharpe/Sortino are calculated from the same equity curve as return,
- max drawdown is calculated from marked equity, not closed trades only,
- parameter search results keep train/validation/test separated,
- no strategy can optimize on the final holdout,
- any result above a sanity threshold gets automatic rerun from raw inputs.

Red flags from this transcript:

- 5,000%+ returns with Sharpe below 1,
- "agent improved it until target return" without a locked holdout,
- old CSVs mixed with new sizing assumptions,
- compounding bugs,
- selecting only winning assets/data sources,
- manual trade anecdotes treated as model evidence.

### Agent Gauntlet, But With Guardrails

The multi-agent iteration pattern can be useful if constrained.

Allowed:

- multiple agents propose hypotheses,
- each hypothesis gets a written mechanism,
- each agent uses the same data split,
- each agent reports failures as well as wins,
- the champion is evaluated on untouched holdout data,
- results are ranked by post-cost CLV/markout and Sharpe, not headline return.

Not allowed:

- optimizing until a desired return appears,
- deleting weak variants,
- changing splits mid-run,
- reading future labels,
- promoting a strategy because one manual live trade won,
- treating paper broker fills as live fills without slippage checks.

### New Build Ideas

#### `trapped_trader_pressure_observer`

Observe-only module for BTC, equity, and Brent:

- liquidation clusters,
- funding/perp imbalance,
- open-range shock state,
- threshold distance,
- Kalshi depth imbalance,
- large print/replenishment features,
- post-shock markout.

Output should join the BTC c-lead/gap ledger and equity/Brent CDF research.

#### `event_vol_mispricing_lab`

Research module that compares:

- Kalshi-implied distribution,
- model-implied distribution,
- realized historical distribution,
- event-regime conditional distribution.

It should produce calibration curves, edge histograms, and cost-stressed
markout reports for BTC, equity, Brent, weather, and macro ladders.

#### `timing_edge_ledger`

A generic ledger for latency-sensitive events:

- scheduled source timestamp,
- observed source timestamp,
- local receipt timestamp,
- Kalshi quote timestamp,
- model update timestamp,
- decision timestamp,
- paper fill timestamp,
- markout timestamps.

This is the measurement layer that decides whether a low-latency VPS is useful.

#### `backtest_artifact_guard`

CI/research guard that fails or warns when:

- result files are older than input files,
- config hash changed but report was not regenerated,
- combined CSV mixes incompatible run IDs,
- annualized return is inconsistent with raw trades,
- compounding assumptions are unstated,
- returns are extreme but trade count is tiny,
- Sharpe/drawdown are missing.

#### `agent_hypothesis_gauntlet`

A controlled multi-agent research harness:

- pre-register hypotheses,
- enforce train/validation/test splits,
- run parameter perturbations,
- run cost stress,
- rank by holdout markout,
- archive both winners and losers,
- generate a markdown report with all variants.

### Immediate Priority

Use this transcript in this order:

1. Add `timing_edge_ledger` to BTC and macro/sports observers.
2. Add `event_vol_mispricing_lab` for BTC, equity, Brent, weather, and macro.
3. Add `backtest_artifact_guard` before trusting any giant-return transcript
   idea.
4. Add `trapped_trader_pressure_observer` as observe-only for BTC first.
5. Only then consider execution changes, and only in paper mode until markout
   proves edge.

The useful idea is not 0DTE trading itself. The useful idea is that Kalshi
short-dated markets can be analyzed through the same lens: **who is trapped,
what distribution is mispriced, and exactly when does the market update?**
