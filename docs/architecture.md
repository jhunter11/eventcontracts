# Architecture

The framework is organized as ports and adapters around a typed event-time
core. The main design rule is simple: strategy code is pure with respect to the
framework. A strategy reads state from a context, consumes normalized events,
and returns typed decisions. Everything else is infrastructure.

## End-To-End Flow

```text
venue/external adapters
  -> raw event envelope
  -> storage writer
  -> normalizer
  -> NormalizedEvent stream
  -> StrategyRunner
  -> Strategy.on_event(event, context)
  -> StrategyDecision
  -> IntentEnvelope
  -> RiskGate
  -> IntentSink
  -> paper executor, bus, OMS, gateway, or recorder
```

This same flow should run in historical replay, paper trading, and eventual
live deployment. The source of events and the sink for decisions can change;
the strategy boundary should not.

## Layer Responsibilities

| Layer | Responsibility | Current status |
| --- | --- | --- |
| Domain | Immutable market, order, fill, position, lifecycle, event, decision, feature, and spec types. | Scaffolded. |
| Venue adapters | Translate Kalshi, Polymarket global, and later Polymarket US APIs into raw envelopes and normalized objects. | Stubs. |
| External data | Capture weather, crypto, macro, and other point-in-time signals. | Stubs. |
| Storage | Persist raw envelopes, normalized events, reference data, and replay partitions. | Boundary only. |
| Replay | Produce deterministic event-time streams from persisted data. | Clock exists; engine stubbed. |
| Strategy | Researcher-facing protocol, lifecycle hooks, registry, and context contract. | Scaffolded. |
| Runner | Wires strategies to events, context, state store, risk, and intent sink. | Synchronous reference runner implemented and covered by the vertical-slice and end-to-end tests. |
| Execution | Simulate fills, fees, latency, slippage, queue position, and venue pauses. | Implemented: `MarketPaperSimulator` with queue / latency / fee models and `PnLTracker` (incl. settlement realization). |
| Risk | Pre-trade gates, limits, kill switches, and compliance checks. | `SleeveRiskGate` and `PreTradePolicyService` implemented; eligibility/compliance still placeholder. |
| Bus / gateway | Route typed events and intents between sleeves and the venue-facing process. | Not implemented. |
| Observability | Structured logs, metrics, traces, dashboards, and alerts. | Not implemented. |

## Domain Boundary

The domain layer under `src/eventcontracts/domain` is the shared language of the
system. It is intentionally venue-neutral.

Core objects:

- `InstrumentId`: venue, market id, optional outcome id.
- `Market`, `Quote`, `Trade`, `OrderBook`, `OrderBookLevel`.
- `Order`, `OrderReject`, `Fill`.
- `Position`, `CashBalance`, `Exposure`, `LedgerEntry`.
- `MarketLifecycleEvent`, `SettlementEvent`.
- `FeatureVector`, `Signal`, `Prediction`.
- `StrategySpec`, `SleeveSpec`, `RiskProfile`, `ModelRef`.

Typed IDs use `typing.NewType` wrappers so strategy IDs, sleeve IDs, order IDs,
fill IDs, model versions, and event IDs are not silently swapped in code.

## Event Boundary

Strategies consume only `NormalizedEvent` variants:

- `QuoteEvent`
- `TradeEvent`
- `OrderBookEvent`
- `LifecycleEvent`
- `SettlementResolvedEvent`
- `ExternalSignalEvent`
- `TimerEvent`
- `OwnFillEvent`
- `OwnOrderUpdateEvent`
- `OwnOrderRejectEvent`

Venue-specific payloads should never leak into a strategy. If Kalshi or
Polymarket exposes a new useful field, the adapter can preserve it in metadata,
but strategy-facing behavior should be promoted into a normalized event only
when the field has a clear framework meaning.

## Decision Boundary

Strategies emit only `StrategyDecision` variants:

- `PlaceOrder`
- `CancelOrder`
- `ReplaceOrder`
- `Alert`
- `NoAction`

The runner wraps each decision in an `IntentEnvelope` containing:

- strategy id
- sleeve id
- correlation id
- emitted timestamp
- causal event id
- metadata, including the decision kind

The `IntentEnvelope` is the object risk, paper execution, OMS, gateway, bus, and
audit sinks should handle. Raw strategy decisions should not leave the runner.

## Latency Priority Boundary

Not every edge deserves the same routing priority. A crypto lead-lag signal may
decay in tens or hundreds of milliseconds, while a slower prediction edge may
remain valid even if the order is a second late. This distinction is now part of
the domain contract through `ExecutionPriority` and `LatencyTier`.

Priority tiers:

- `RELAXED`: slower prediction or rebalance flow where a short delay is
  acceptable.
- `STANDARD`: default routing.
- `FAST`: latency-sensitive alpha such as crypto movement or external-feed
  repricing.
- `CRITICAL`: protective actions such as risk-reducing cancels or kill-switch
  flow.

Order-affecting decisions can carry explicit priority. `StrategySpec` also has
`default_execution_priority` so a whole strategy can opt into a latency class
without setting it on every order. The runner computes the final envelope
priority, and the future gateway should use that priority to schedule:

- gateway worker queues
- venue rate-limit budget
- retry windows
- stale intent dropping via `expires_after_ms`
- colocated or low-latency routes when available

Priority is a scheduling hint, not an override. Every intent still passes
through pre-trade risk, compliance, idempotency, and venue-state checks.

## Strategy Boundary

The strategy protocol lives in `src/eventcontracts/strategy/base.py`.

```python
class Strategy(Protocol):
    spec: StrategySpec

    def on_init(self, ctx: StrategyContext) -> None: ...
    def on_event(self, event: NormalizedEvent, ctx: StrategyContext) -> Sequence[StrategyDecision]: ...
    def on_shutdown(self, ctx: StrategyContext) -> None: ...
    def snapshot(self) -> bytes: ...
    def restore(self, state: bytes) -> None: ...
```

The `StrategyContext` protocol is read-only from the strategy's perspective. It
provides current time, positions, cash, exposure, open orders, features, and
model prediction access. Strategies request changes by returning decisions; they
do not mutate framework state.

## Runner Boundary

`StrategyRunner` depends on ports:

- `EventSource`: yields `NormalizedEvent` values.
- `ContextProvider`: provides a fresh `StrategyContext`.
- `RiskGate`: accepts or rejects an `IntentEnvelope`.
- `IntentSink`: receives allowed envelopes.
- `StateStore`: optional snapshot persistence.
- `Clock`: current event or wall time.

The reference runner is synchronous and single-threaded by design. Concurrency
belongs around it: NATS can deliver one event at a time, the gateway can batch
or net intents, and allocators can run as separate services.

## Storage Boundary

Raw events should be stored before normalization. Each raw event envelope should
preserve:

- source venue or provider
- source channel
- exchange timestamp when available
- local receipt timestamp
- raw payload
- schema version

Normalized data should be separately persisted for replay. The eventual Parquet
layout should partition by venue, event kind, date, and possibly market family.
DuckDB should be a read path over Parquet, not the only source of truth.

## Replay Boundary

Replay should produce `Iterator[NormalizedEvent]` ordered by exchange time with a
deterministic tie-breaker for missing or duplicate timestamps. Backtests, paper
replay, and parity tests should use the same strategy and runner interfaces as
live sleeves.

Replay must support:

- point-in-time event ordering
- fake clock control
- order book reconstruction
- deterministic output for a fixed input dataset
- strategy snapshot restore/save checkpoints

## Execution Boundary

Execution is downstream from the strategy boundary. A strategy says what it
wants; paper or live execution decides what actually happens.

The paper executor should model:

- venue-specific fee formulas and rounding
- queue position and passive fill probability
- taker slippage through the local book
- latency draws
- cancel/replace semantics
- venue pauses, restarts, and market lifecycle restrictions
- settlement-aware capital usage

The live executor should not live inside strategies. It should be isolated in a
gateway process that owns credentials, rate limits, idempotency keys,
reconciliation, and final pre-trade risk checks.

## Configuration Boundary

`StrategySpec` describes the strategy implementation and immutable parameters.
`SleeveSpec` describes a deployment sleeve: venue, capital, currency, and risk
profile. This separation allows one strategy to run in multiple sleeves with
different capital allocations or venue constraints.

Future TOML config should map directly onto these dataclasses:

- `strategy_spec.toml` for strategy identity, parameters, subscription, model,
  and feature schema.
- sleeve config TOML for sleeve id, venue, allocation, and risk limits.
- environment overlay for non-secret environment-specific values.
- secret references for credentials, never raw secrets in repo configs.

## Cross-Language Boundary

Python is the research and export environment. Rust is planned for low-latency
online feature building, sleeve runners, gateway, allocator, and bus-facing
services. The shared contract should be file formats, not Python imports:

- `feature_schema.json`
- `strategy_spec.toml`
- `manifest.toml`
- `parity_cases.parquet`
- ONNX model artifacts

The CI parity job should load the same bundle in Python and Rust and fail if
features, model outputs, decisions, or PnL diverge beyond explicit tolerances.

## Design Invariants

- Store raw payloads before normalization.
- Preserve exchange time and receipt time.
- Strategy code does not call venues, storage, the bus, or execution directly.
- Strategies emit typed decisions, not side effects.
- Every decision leaving a runner has strategy, sleeve, correlation, and causal
  event provenance.
- Risk gates run before any order intent is dispatched.
- Backtest, paper, and live sleeves share the same strategy boundary.
