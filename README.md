# Event Contracts Research Framework

This repository is an execution-aware event-contract research stack covering
Kalshi, Polymarket global, and future venue adapters. It is intentionally built
around typed data contracts first: strategies consume normalized events, read
state through a context, and emit typed decisions that a runner can route
through risk, paper execution, or a live gateway later.

The project is not a live trading bot yet. The current production-shaped use
case is market-edge research: capture real venue data, normalize it, replay it
through strategy specs, and estimate fills with book/trade-based paper
execution before any live order path exists.

## Repository Layout

This is a polyglot workspace. Python code lives under `python/`, future
Rust crates under `rust/`, and the cross-language file-format contracts
that both consume under `contracts/`. The Python/Rust seam is files on
disk (TOML specs, JSON schemas, Parquet parity cases, ONNX models), not
in-process imports.

## Dependency Setup

Use `python/requirements.txt` as the runtime dependency source of truth.

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install -r python/requirements.txt
```

For tests, linting, and type checking, install the development requirements.
`python/requirements-dev.txt` includes `python/requirements.txt`.

```bash
python3 -m pip install -r python/requirements-dev.txt
```

Optional editable package install for the CLI:

```bash
python3 -m pip install -e ./python
```

## Verify The Scaffold

From the repo root, the Makefile delegates into `python/` for you:

```bash
make quality
```

Or run the underlying commands directly:

```bash
cd python
python3 -m compileall -q src tests
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest
```

The `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` flag keeps unrelated globally installed
pytest plugins from affecting this repository.

## Current Package Shape

All Python source lives under `python/src/eventcontracts/`:

- `domain`: venue-neutral dataclasses and closed sum types.
- `strategy`: the researcher-facing strategy protocol, lifecycle states, read-only context contract, and registry.
- `plugins/strategies`: concrete strategy plugins; currently includes `example_threshold`.
- `runner`: reference runner. In-memory ports for tests live under `testing/`.
- `testing`: in-memory ports and other test doubles. Not for production imports.
- `adapters/venues`: Kalshi and Polymarket adapter boundaries.
- `ingestion`: capture job boundaries.
- `storage`: raw envelope and normalized storage boundaries.
- `normalization`: contract matching and cross-venue rejection logic.
- `replay`: deterministic event-time replay boundary.
- `execution`: paper execution, fill simulation, and queue modeling boundary.
- `risk`: pre-trade policy, limits, and compliance boundary.
- `features`: offline/online feature schema, builder, and feature-store contracts.
- `models`: Python-first training, model-family-agnostic ONNX export (sklearn/XGBoost/LightGBM/HuggingFace), calibrated evaluation, export-parity, inference, and model-registry contracts.
- `artifacts`: immutable strategy/model bundle writer, loader, validator, and promotion contracts.
- `bus`: typed IPC topic, message, codec, publisher, and subscriber contracts.
- `gateway`: live-shaped venue command, idempotency, priority scheduling, dry-run gateway, and reconciliation contracts.
- `oms`: order ticket, transition, state-machine, and reconciliation contracts.
- `ledger`: ledger, cash keeper, position keeper, and settlement accounting contracts.
- `allocation`: sleeve registry and capital allocation contracts.
- `observability`: structured logging, metrics, tracing, alerts, and health-check contracts.
- `external`: point-in-time weather, crypto, macro, and other provider contracts.
- `contracts`: dependency-free validation helpers for the cross-language schema files in top-level `contracts/`.
- `markets`: subscription-based market detection and in-memory reference catalog for paper/dry-run mode.

Other top-level directories:

- `python/tests`: pytest suite.
- `rust/`: Rust workspace with compileable low-latency scaffold crates:
  `contracts`, `feature-builder`, `runner`, `gateway`, `bus`, `allocator`,
  and `parity`.
- `contracts/`: cross-language file-format contracts (JSON schemas, TOML examples, parity cases).
- `configs/`: non-secret config examples for venues, storage, and research programs.
- `docs/`: architecture, contracts, roadmap, research programs, and development notes.
- `notebooks/`: researcher exploration surface.

## Core Data Flow

```text
raw venue/external payload
  -> normalized event
  -> Strategy.on_event(event, context)
  -> strategy decision
  -> intent envelope
  -> risk gate
  -> paper executor, bus, OMS, or gateway
```

Strategies do not call venue clients, storage, the bus, or execution APIs
directly. They return values. The runner owns lifecycle, provenance,
state restore/save, risk evaluation, and dispatch.

## REST-Based Predictive Edge Tests

For predictive edges that do not depend on sub-second latency, start with Kalshi
REST polling. This captures repeated order-book snapshots and deduped recent
trades, then normalizes them into the same replay path used by backtests.

```bash
eventcontracts capture \
  --venue kalshi \
  --transport rest \
  --patterns "KXHIGHNY-*" \
  --max-polls 120 \
  --poll-interval-seconds 30 \
  --trades-limit 100 \
  --normalize \
  --out data/edge-tests/kalshi-highny-rest

eventcontracts inspect-data \
  --data data/edge-tests/kalshi-highny-rest \
  --source kalshi-rest

eventcontracts backtest \
  --strategy configs/strategies/weather-temperature-arbitrage.toml \
  --sleeve configs/sleeves/weather-kalshi-paper-a.toml \
  --data data/edge-tests/kalshi-highny-rest \
  --latency-ms 250 \
  --queue-fraction 1.0 \
  --starting-equity 10000 \
  --out artifacts/reports/weather-rest-edge.json
```

Fill realism in this mode is deliberately conservative: marketable orders walk
the captured opposite book, while passive orders join a configurable fraction
of visible same-price depth and only fill when later captured trades consume
that queue. Repeated REST trade polls are deduped so fill volume is not
artificially multiplied.

## Strategy Plug-In Contract

Every strategy implements:

- `on_init(ctx)`: one-time setup.
- `on_event(event, ctx)`: receive a normalized event and return decisions.
- `on_shutdown(ctx)`: clean exit hook.
- `snapshot()` / `restore(state)`: optional opaque state persistence.

Strategies are registered by name:

```python
from eventcontracts.strategy.registry import register

@register("my_strategy")
def factory(spec):
    return MyStrategy(spec)
```

The runner resolves a `StrategySpec` through the registry, feeds it
`NormalizedEvent` values, wraps returned `StrategyDecision` values into
`IntentEnvelope` objects, and passes them through a `RiskGate` before emitting.

See [docs/strategy-runner-contract.md](docs/strategy-runner-contract.md) for the
full contract and [src/eventcontracts/plugins/strategies/example_threshold.py](src/eventcontracts/plugins/strategies/example_threshold.py)
for the smallest working example.

## Implemented Type Scaffolding

The domain layer now includes:

- Market data: `Market`, `Quote`, `Trade`, `OrderBook`, `OrderBookLevel`.
- Identity: `StrategyId`, `SleeveId`, `RunId`, order/fill/event/model IDs.
- Orders and fills: `Order`, `OrderReject`, `Fill`, side/type/status enums.
- State: `Position`, `CashBalance`, `Exposure`, `LedgerEntry`.
- Lifecycle: `MarketLifecycleEvent`, `SettlementEvent`.
- Features: `FeatureVector`, `Signal`, `Prediction`.
- Execution priority: `ExecutionPriority`, `LatencyTier`.
- Specs: `StrategySpec`, `SleeveSpec`, `RiskProfile`, `ModelRef`.
- Inbound event variants: quote, trade, book, lifecycle, settlement, external,
  timer, own fill, own order update, own order reject.
- Strategy decision variants: place, cancel, replace, alert, no action.

Order-affecting decisions can carry latency hints. A crypto lead-lag strategy
can emit `ExecutionPriority(tier=LatencyTier.FAST, max_delay_ms=100)`, while a
slower weather or macro prediction edge can use the default `STANDARD` or
`RELAXED` priority. The future gateway should use this for scheduling and
rate-limit allocation, but never to bypass risk checks.

## Adding A Strategy

1. Create a new module in `src/eventcontracts/plugins/strategies/`.
2. Subclass `StrategyBase` or implement the `Strategy` protocol.
3. Match only on `NormalizedEvent` variants your strategy understands.
4. Return `StrategyDecision` values such as `PlaceOrder` or `NoAction`.
5. Register a factory with `@register("strategy_name")`.
6. Add a smoke test that builds a `StrategySpec`, `SleeveSpec`, in-memory event
   source, and `StrategyRunner`.

## Documentation Map

- [docs/architecture.md](docs/architecture.md): layer boundaries and data flow.
- [docs/dataflow-map.md](docs/dataflow-map.md): current partial implementation
  functions and the data types handed between layers.
- [docs/strategy-runner-contract.md](docs/strategy-runner-contract.md): concrete
  data types and plug-in contract.
- [docs/artifact-contract.md](docs/artifact-contract.md): planned model and
  strategy artifact bundle format.
- [docs/ml-model-pipeline.md](docs/ml-model-pipeline.md): model-family-agnostic
  ONNX/HuggingFace training, export, parity, and serving pipeline with reference
  results.
- [docs/development.md](docs/development.md): local setup, verification, and
  contribution conventions.
- [docs/implementation-roadmap.md](docs/implementation-roadmap.md): gap matrix,
  MVP sequence, and hardening plan.
- [docs/deep-research-report.md](docs/deep-research-report.md): research assessment and
  market-structure rationale behind the roadmap.

## Live Trading Boundary

A gated live-ordering path now exists: the Rust `rust/crates/live-runner` with
`--live-submit` routes intents through risk + the Kalshi gateway/venue client to
place real orders. It is **off by default** and guarded (explicit `KALSHI_ENV`,
mandatory `--max-live-orders`, startup reconciliation, a confirmation prompt, a
file kill switch, and a toxicity breaker). Everything else runs paper/backtest.

The safe path each strategy must clear before real capital:

1. Real venue capture.
2. Raw event persistence.
3. Deterministic replay.
4. Strategy/runner parity in backtest and paper.
5. Paper execution with fees, slippage, latency, queue, pauses, and settlement.
6. Risk, compliance, reconciliation, credentials, and gateway isolation.
7. Cross-language parity: any model/feature/risk change must agree Python↔Rust.

Read `docs/runbooks/kalshi-live-runner.md` before enabling real orders. Note the
two risk gates are **not** fully identical (e.g. `max_daily_loss` latches in
Python but not in Rust) — the Rust gate is authoritative for live.
