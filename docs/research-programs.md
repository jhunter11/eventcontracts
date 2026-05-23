# Research Programs

Research programs are named investigations expressed as notebooks plus TOML
configs, not as source code in the framework package. Each program defines a
hypothesis, the data slices needed to test it, and the metrics that decide
whether a strategy graduates to paper deployment.

A research program is run by:

1. A notebook under `notebooks/<program-slug>/`.
2. A TOML config under `configs/research/<program-slug>.toml` describing
   capture windows, replay parameters, sleeve sizing, and metrics.
3. Optional strategy plugins discovered through the
   `eventcontracts.strategies` entry-point group (see
   `docs/strategy-runner-contract.md`).

The three programs below were previously scaffolded as empty Python files
under `src/eventcontracts/research/`. They are recorded here so the intent
is preserved without committing dead code.

## cross-venue-spreads

**Hypothesis.** Persistent price gaps between Kalshi and Polymarket on
equivalent contracts (settled on the same observable event with overlapping
windows) reflect inventory, geo, and credential frictions more than
information differences. After fees and conversion costs, the gap is
sometimes wide enough to fund a market-neutral pair.

**Inputs needed.** Synchronized order books from both venues, contract
mapping table, FX-cost model for USDC vs USD, settlement-rule alignment
checks.

**Metrics.** Realized half-spread after fees, hold time to convergence,
drawdown when one leg gaps without the other, settlement-rule mismatch
incidents.

## crypto-lead-lag

**Hypothesis.** Spot moves on Binance perpetual or mark price lead
short-horizon Kalshi crypto contracts by 50–500 ms during normal hours, and
by more during venue-side congestion. The edge is latency-bounded: missing
the window by 200 ms eliminates expected value after fees.

**Inputs needed.** Binance perp mark and funding stream, Kalshi crypto
market data, latency budget instrumentation, ExecutionPriority routing in
the gateway (priority=FAST per `docs/architecture.md`).

**Metrics.** Decision-to-send latency distribution, hit rate vs lead time,
slippage at FAST tier, fade behavior when underlying reverses.

## weather-event-study

**Hypothesis.** NWS observations published between Kalshi market open and
settlement lag the implied price by enough to predict the residual. The
edge is wider on temperature and snowfall contracts than on rain.

**Inputs needed.** NWS/METAR point-in-time observations, Kalshi weather
market metadata and order books, settlement-rule capture, holiday and
station-outage filter.

**Metrics.** Sharpe by station-contract pair, settlement-rule mismatch
rate, observation-to-settlement lag, regret vs perfect-foresight bound.

## Adding a Program

1. Create `configs/research/<slug>.toml` and `notebooks/<slug>/`.
2. If a new strategy is needed, add it under
   `src/eventcontracts/plugins/strategies/` or ship it in a separate pip
   package exposing the `eventcontracts.strategies` entry point.
3. Run the program via the (future) `eventcontracts backtest` CLI against
   a deterministic replay window.
4. Export an artifact bundle per `docs/artifact-contract.md` when results
   warrant graduation.
