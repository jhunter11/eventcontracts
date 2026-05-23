# Event Contracts Research Framework

This repository is the Python scaffold for an execution-aware event-contract
research stack covering Kalshi, Polymarket global, and future venue adapters.
It is intentionally built around typed data contracts first: strategies consume
normalized events, read state through a context, and emit typed decisions that a
runner can route through risk, paper execution, or a live gateway later.

The project is not a live trading bot yet. Venue clients, storage, replay, paper
execution, and risk are still mostly stubs. The important foundation now in
place is the object model that lets strategy code plug into a runner without
knowing how data capture, replay, risk, storage, or execution are implemented.

## Dependency Setup

Use `requirements.txt` as the runtime dependency source of truth.

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install -r requirements.txt
```

For tests, linting, and type checking, install the development requirements.
`requirements-dev.txt` includes `requirements.txt`.

```bash
python3 -m pip install -r requirements-dev.txt
```

Optional editable package install for the CLI:

```bash
python3 -m pip install -e .
```

## Verify The Scaffold

```bash
python3 -m compileall -q src tests
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest
```

The `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` flag keeps unrelated globally installed
pytest plugins from affecting this repository.

## Current Package Shape

- `src/eventcontracts/domain`: venue-neutral dataclasses and closed sum types.
- `src/eventcontracts/strategy`: the researcher-facing strategy protocol,lifecycle states, read-only context contract, and registry.
- `src/eventcontracts/strategies`: concrete strategy plugins; currently includes
  `example_threshold`.
- `src/eventcontracts/runner`: reference runner plus in-memory ports for tests and local experiments.
- `src/eventcontracts/venues`: Kalshi and Polymarket adapter boundaries.
- `src/eventcontracts/ingestion`: capture job boundaries.
- `src/eventcontracts/storage`: raw envelope and normalized storage boundaries.
- `src/eventcontracts/normalization`: contract matching and cross-venue rejection logic.
- `src/eventcontracts/replay`: deterministic event-time replay boundary.
- `src/eventcontracts/research`: initial research program scaffolds.
- `src/eventcontracts/execution`: paper execution, fill simulation, and queue modeling boundary.
- `src/eventcontracts/risk`: pre-trade policy, limits, and compliance boundary.
- `configs`: non-secret config examples for venues, storage, and research.
- `docs`: architecture, contracts, roadmap, and development notes.

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
full contract and [src/eventcontracts/strategies/example_threshold.py](src/eventcontracts/strategies/example_threshold.py)
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

1. Create a new module in `src/eventcontracts/strategies/`.
2. Subclass `StrategyBase` or implement the `Strategy` protocol.
3. Match only on `NormalizedEvent` variants your strategy understands.
4. Return `StrategyDecision` values such as `PlaceOrder` or `NoAction`.
5. Register a factory with `@register("strategy_name")`.
6. Add a smoke test that builds a `StrategySpec`, `SleeveSpec`, in-memory event
   source, and `StrategyRunner`.

## Documentation Map

- [docs/architecture.md](docs/architecture.md): layer boundaries and data flow.
- [docs/strategy-runner-contract.md](docs/strategy-runner-contract.md): concrete
  data types and plug-in contract.
- [docs/artifact-contract.md](docs/artifact-contract.md): planned model and
  strategy artifact bundle format.
- [docs/development.md](docs/development.md): local setup, verification, and
  contribution conventions.
- [docs/implementation-roadmap.md](docs/implementation-roadmap.md): gap matrix,
  MVP sequence, and hardening plan.
- [deep-research-report.md](deep-research-report.md): research assessment and
  market-structure rationale behind the roadmap.

## Live Trading Boundary

This project intentionally has no live-ordering implementation yet. The minimum
safe path is:

1. Real venue capture.
2. Raw event persistence.
3. Deterministic replay.
4. Strategy/runner parity in backtest and paper.
5. Paper execution with fees, slippage, latency, queue, pauses, and settlement.
6. Risk, compliance, reconciliation, credentials, and gateway isolation.

Only after those are in place should a live executor be added.
