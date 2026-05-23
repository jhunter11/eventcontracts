# Implementation Roadmap

This roadmap starts from the current Python scaffold: domain dataclasses,
normalized event variants, strategy decision variants, strategy protocol,
registry, reference runner, in-memory ports, and smoke tests. Most production
infrastructure is still missing.

Legend: blocking = required before meaningful strategy iteration; live gate =
required before live trading; later = useful after the core loop works.

## Gap Matrix By Layer

| Layer | Current state | Missing |
| --- | --- | --- |
| Domain models | Market, quote, trade, order book, orders, fills, positions, cash, lifecycle, settlement, features, specs, IDs. | Lots, full double-entry ledger semantics, settlement-aware capital accounting. |
| Venue: Kalshi | Stub client and fee boundary. | blocking: REST auth/retry/rate-limit, WS auth/heartbeat/reconnect/gap detection, historical pull. live gate: FIX, queue-position integration. |
| Venue: Polymarket | Stub client and fee boundary. | blocking: Gamma discovery, CLOB REST/WS, price history, Polygon RPC log parsing, onchain fill joins, resolution rule capture. |
| External data | Weather, macro, crypto client boundaries. | blocking: NWS/METAR, Binance mark/funding. live gate: FRED/BLS/CME FedWatch. |
| Storage | Raw envelope and DuckDB boundaries. | blocking: Parquet writer, partitioning, S3/MinIO abstraction, DuckDB read path, reference-data store. live gate: Postgres ops state. |
| Replay | Replay clock exists; engine boundary. | blocking: event-time engine, order book reconstruction, point-in-time queries, determinism harness. |
| Features | `FeatureVector`, `Signal`, `Prediction`; surveillance feature boundary. | blocking: shared Python/Rust feature schema, offline pipeline, online builder. later: feature store. |
| Training | Not implemented. | blocking: training harness, ONNX exporter, parity case generator. live gate: walk-forward CV. later: Optuna, MLflow/W&B. |
| Model registry | Not implemented. | blocking: artifact bundle format, object-store layout, version pinning. live gate: champion/challenger flow. |
| Strategy abstraction | Python strategy protocol, base class, context, registry, example strategy. | blocking: TOML loader into specs, plugin discovery beyond eager imports, Rust trait parity, state persistence adapters. |
| Runner | Reference synchronous runner and in-memory ports. | blocking: replay-backed event source, paper intent sink, metrics, graceful halt semantics, typed bus sink. |
| Execution: paper | Boundaries and decision types. | blocking: fill simulator with queue, latency, slippage, fees, cancel/replace, venue pause handling. |
| Execution: live | Execution priority types exist; live executor not implemented. | live gate: OMS state machine, smart order router, priority scheduler for latency-sensitive edges, credential isolation, idempotency keys, reconciliation. |
| Cash and positions | Position/cash/exposure dataclasses. | blocking: double-entry ledger, position keeper, settlement-aware capital usage. live gate: tax lots. |
| Risk | Risk gate protocol and policy stubs. | blocking: pre-trade gates, exposure limits, eligibility checks, kill switches. live gate: circuit breakers, correlation monitor. |
| Sleeves and allocation | `SleeveSpec` dataclass. | blocking: sleeve config schema, sleeve registry, allocator service. live gate: rebalance scheduler, PnL attribution. |
| Bus / IPC | Not implemented. | blocking: NATS JetStream or Redis Streams, typed topic contracts, schema registry. |
| Gateway | Not implemented. | blocking: single venue-facing process, credential vault integration, priority-aware rate-limit aggregator, final pre-trade risk check. |
| Containerization | Not implemented. | blocking: Python/Rust Dockerfiles and docker-compose dev stack. later: k8s/Nomad manifests, Helm charts. |
| Config | TOML loader and example configs. | live gate: sleeve-scoped schema, env overlay, secret references. |
| Observability | Not implemented. | blocking: structured logging schema, Prometheus metrics, OpenTelemetry traces, Grafana dashboards. live gate: Alertmanager rules. |
| CI/CD | Not implemented. | blocking: test workflow, Rust/Python parity job. live gate: container build/push. |
| Tests | Import tests plus strategy/runner smoke tests. | blocking: unit coverage per module, replay determinism tests, venue sandbox integration tests. later: chaos and load tests. |
| Ops tooling | CLI has `check-config` only. | live gate: halt sleeve, cancel all, reconcile, replay incident, DR runbook. |
| Compliance | Compliance stub. | live gate: geo enforcement, account eligibility, audit log, trade-report export. |

## Minimum Framework For Strategy Iteration

The smallest version that lets a researcher mostly focus on strategies is this
vertical slice:

1. Kalshi capture path for one market family: REST discovery, WS market data,
   reconnect, gap detection, raw envelopes to Parquet.
2. Deterministic replay from Parquet to `NormalizedEvent`.
3. Strategy specs and sleeve specs loaded from TOML.
4. Strategy registry and runner using the same code path in backtest and paper.
5. Paper executor with fee, latency, slippage, queue, cancel/replace, and pause
   handling.
6. Priority-aware intent scheduling contract so latency-sensitive edges can be
   routed ahead of slower prediction edges without bypassing risk.
7. Feature schema and offline feature builder.
8. Artifact bundle exporter: ONNX model, strategy spec, feature schema, parity
   cases, and signed manifest.
9. Backtest CLI that runs replay -> strategy -> paper executor -> metrics.
10. Model/strategy parity tests in CI.
11. Observability baseline for runner, paper executor, and capture jobs.

Do not start with a broad infrastructure platform. Build this vertical slice
for one simple strategy and one venue category, then expand.

## Recommended First Vertical Slice

Use the weather-threshold idea because it has objective settlement sources and
clear external data.

1. Capture Kalshi weather market metadata and order books.
2. Capture NWS/METAR observations point-in-time.
3. Normalize venue data into `QuoteEvent`, `TradeEvent`, `OrderBookEvent`, and
   `LifecycleEvent`.
4. Write raw and normalized events to local Parquet.
5. Replay the events into the existing `StrategyRunner`.
6. Extend `example_threshold` or add a weather-specific strategy.
7. Route decisions into a paper executor instead of the in-memory sink.
8. Compare realized paper fills against simulated fills.
9. Export an artifact bundle and run parity cases.

This slice forces real decisions about event schemas, feature schemas, strategy
specs, replay ordering, order intent semantics, and metrics without building
every future service first.

## Phase 1: Contracted Data Lake

Acceptance criteria:

- Raw envelopes are persisted before normalization.
- Every raw event includes source, channel, exchange timestamp, receipt
  timestamp, raw payload, and schema version.
- Normalized events cover quote, trade, book, lifecycle, settlement, external
  signal, timer, and own-account updates.
- Parquet partitioning is documented and stable.
- DuckDB can query local partitions without rewriting files.
- A replay fixture built from stored events produces deterministic output.

Implementation tasks:

- Implement Kalshi market discovery and authenticated WebSocket capture.
- Implement raw Parquet writer and partitioning.
- Add gap detection and reconnect policy to market-data capture.
- Add the first external data client for NWS/METAR.
- Add replay fixtures and determinism tests.

## Phase 2: Strategy Loop

Acceptance criteria:

- TOML specs load into `StrategySpec` and `SleeveSpec`.
- Strategies can be discovered and instantiated by registry name.
- Backtest uses the same `StrategyRunner` interface as paper replay.
- Each strategy decision becomes an `IntentEnvelope` with provenance.
- Risk gate decisions are recorded with rejection reasons.
- Strategy snapshots save and restore through a state store.
- Intent envelopes include execution priority so live gateways can schedule
  latency-sensitive strategies ahead of tolerant strategies.

Implementation tasks:

- Add config schemas and loaders for strategy and sleeve specs.
- Add replay-backed `EventSource`.
- Add paper `IntentSink`.
- Add metrics around events, decisions, rejects, and runner lifecycle.
- Add metrics around intent priority, queue time, stale drops, and
  decision-to-send latency.
- Expand smoke tests into table-driven event/decision tests.

## Phase 3: Paper Execution

Acceptance criteria:

- Fees are calculated per fill with venue-specific rounding.
- Passive and taker fills are separated.
- Queue and latency assumptions are explicit and testable.
- Cancel/replace semantics preserve or forfeit queue position correctly.
- Venue pauses and lifecycle states block or cancel orders as configured.
- Capital usage includes settlement timing.

Implementation tasks:

- Implement fee models from venue rules.
- Implement a simple queue model, then validate against Kalshi queue APIs.
- Implement order-book-walk slippage for marketable orders.
- Add fill and order update events back into the runner stream.
- Report PnL, drawdown, exposure, fill rates, and reject reasons by sleeve.

## Phase 4: Artifact And Parity

Acceptance criteria:

- A strategy/model bundle can be exported to an object-store layout.
- The bundle includes `manifest.toml`, `strategy_spec.toml`,
  `feature_schema.json`, `parity_cases.parquet`, and optional `model.onnx`.
- Python and Rust loaders agree on features, model outputs, and decisions for
  the same parity cases.
- CI fails on parity drift.

Implementation tasks:

- Implement the artifact bundle writer.
- Add feature schema validation.
- Add ONNX export from the training harness.
- Add parity case generation from replay windows.
- Add CI job for Python tests, lint, type checks, and parity.

## Phase 5: Live Gate

Do not add live order placement until all of the following are true:

- A paper strategy family is positive after fees, slippage, latency, queue
  modeling, and paper-trading decay.
- Simulated fills and realized paper fills are close enough to trust the
  execution model.
- Risk gates include exposure, position, account, geo, market-category, and
  eligibility checks.
- Gateway owns venue credentials, rate limits, idempotency keys, and final
  pre-trade checks.
- Gateway scheduling honors `ExecutionPriority`: crypto or other fast-decay
  edges can use `FAST`, slower prediction flow can use `RELAXED`, and protective
  cancel/kill-switch flow can use `CRITICAL`.
- Reconciliation catches order, fill, position, and cash drift.
- Observability covers PnL, fills, rejects, errors, latency, and data gaps per
  sleeve.
- Manual ops commands exist for halt sleeve, cancel all, reconcile, and replay
  incident windows.

## Later Production Hardening

- Polymarket adapter and cross-venue normalization.
- NATS JetStream typed topics and schema registry.
- Rust sleeve runner, gateway, and allocator.
- Credential vault integration.
- Double-entry ledger and tax lots.
- Compliance audit logs and trade-report export.
- Smart allocation: equal weight, risk parity, then bandit.
- DR backups, restore drills, chaos tests, and load tests.
