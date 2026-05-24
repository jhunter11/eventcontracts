# Live Readiness Roadmap

For the detailed no-shortcuts production plan with a Rust hot path, see
`docs/live-rust-runner-roadmap.md`. This file remains the broader historical
roadmap; the Rust-live document is the implementation plan for taking the
system to live.

This roadmap starts from the current scaffold on the
`claude/codebase-structure-audit-rJ4xO` branch. The repository now has a typed
Python research scaffold, a dependency-light Rust workspace scaffold,
cross-language contracts, a deterministic replay/paper-execution test path, and
audit/model extension points. It is still not ready for live trading.

The goal is not to make the system "bulletproof" in the abstract. The goal is
to make every live decision reproducible, bounded by risk, attributable to exact
input data, and testable in both Python and Rust before capital is exposed.

## Current State Snapshot

| Area | Current state | Live-readiness gap |
| --- | --- | --- |
| Python domain contracts | Scaffolded with market, order, fill, lifecycle, feature, model, decision, spec, and priority types. | Tighten validation, schema versioning, and serialization compatibility guarantees. |
| Python vertical slice | Partially implemented: Parquet-backed normalized replay, strategy runner, risk gate, paper simulator, PnL, deterministic report tests. | Replace synthetic fixtures with real captured venue data and richer execution assumptions. |
| Auditability | `AuditStamp` and scaffold contracts exist for features, models, artifacts, bus, gateway, OMS, and ledger. | Wire audit stamps through every durable and cross-process handoff, then test chain completeness. |
| Venue adapters | Kalshi and Polymarket client/fee boundaries exist; Kalshi fee model is tested. | Real REST/WS auth, rate limits, reconnects, gap detection, historical pulls, and sandbox/live separation. |
| External data | Provider boundaries exist. | NWS/METAR, crypto, macro, and provider provenance capture need real implementations. |
| Storage | Parquet/DuckDB boundaries and tests exist for local replay. | Raw capture persistence, partition contracts, schema evolution, object-store layout, and ops state store are incomplete. |
| Replay | Determinism path exists for normalized fixtures. | Event-time engine must handle raw-to-normalized replay, order book reconstruction, checkpoints, and point-in-time features. |
| Features | Feature schema/builder/store contracts exist. | Offline and online builders, leakage checks, feature versioning, and Python/Rust parity cases are missing. |
| Models | Training/export/registry contracts exist. | Training harness, walk-forward validation, artifact signing, ONNX/export parity, and champion/challenger flow are missing. |
| Rust | Workspace scaffolds exist for contracts, features, runner, gateway, bus, allocator, and parity. | Serialization, contract validation, parity readers, runtime loops, benchmarks, and fault handling are missing. |
| Execution | Paper simulator, latency, queue, PnL, and report modules exist. | Venue-calibrated queue/slippage, lifecycle handling, OMS integration, settlement accounting, and reconciliation are incomplete. |
| Gateway / OMS | Contract scaffolds exist. | No live venue-facing process, credentials, idempotency store, priority scheduler implementation, or reconciliation loop. |
| Risk / compliance | Risk gate and policy scaffolds exist. | Pre-trade risk must become stateful, audited, configurable, and enforced at runner and gateway boundaries. |
| Observability | Telemetry contracts exist. | Structured logs, metrics, traces, dashboards, alerts, and incident replay tooling are missing. |
| CI/CD | Python and Rust quality targets are scaffolded. | Full dependency install, parity jobs, coverage thresholds, artifact validation, and container builds are not complete. |

## Non-Negotiable Live Invariants

Live order placement must not be implemented or enabled until these invariants
are true and tested:

1. Raw payloads are persisted before normalization.
2. Every normalized event has source, channel, exchange time, receipt time,
   schema version, and raw-event lineage.
3. Every feature vector, prediction, strategy decision, risk result, order
   command, venue ack, fill, ledger entry, and model artifact has an audit
   record or a checksum link to one.
4. Replay of a fixed input partition is deterministic.
5. Python and Rust agree on schema parsing, feature values, model outputs, and
   decisions for golden parity cases.
6. Strategy code cannot call venues, storage, credentials, the bus, or execution
   APIs directly.
7. Risk gates run before paper execution and again inside the live gateway.
8. Gateway owns credentials, rate limits, idempotency keys, stale-intent
   dropping, and final pre-trade checks.
9. Reconciliation catches order, fill, cash, position, and ledger drift.
10. Operators can halt a sleeve, cancel all live orders, reconcile state, and
    replay an incident window without changing code.

## Phase 0: Lock The Scaffold

Purpose: make the current scaffold reliable enough to build on.

Acceptance criteria:

- `make quality` has stable Python and Rust targets.
- Python imports do not require optional heavy dependencies unless a backend is
  actually used.
- Rust workspace compiles with `cargo check --workspace`.
- Current scaffold packages have import tests.
- The architecture, dataflow, artifact, and roadmap docs agree with the code.
- Unimplemented functions raise explicit `NotImplementedError` or are typed
  protocols/abstract methods, never silent placeholders.

Implementation tasks:

- Finish wiring package exports for new scaffold modules.
- Add tests proving audit, model, feature, bus, gateway, OMS, ledger, and
  allocation contracts import without side effects.
- Add a "missing implementation" inventory test or script that reports
  intentional stubs.
- Keep `rust/Cargo.lock` committed for reproducible local and CI checks.
- Make CI run Python quality and Rust quality as separate jobs.

Status: mostly scaffolded, but not yet committed on this branch.

## Phase 1: Audit-First Data Contracts

Purpose: define the exact data that can cross process, language, and storage
boundaries before adding real feeds.

Acceptance criteria:

- Raw envelope, normalized event, feature schema, strategy spec, sleeve spec,
  manifest, and parity case schemas are versioned under `contracts/`.
- Python serializers produce canonical bytes for checksums.
- Rust contract structs can load and validate the same files without lossy
  timestamp, decimal, ID, or checksum parsing.
- Audit links can reconstruct:
  - raw payload -> normalized event
  - normalized event window -> feature vector
  - feature vector -> prediction
  - prediction/strategy state -> decision
  - decision -> risk result -> order command -> venue ack/fill
  - fill -> ledger and PnL updates
- Schema migrations are append-only unless a breaking version is explicitly
  introduced.

Implementation tasks:

- Add JSON schema validation in Python for all files under `contracts/schemas`.
- Add Rust schema readers with strict validation and golden-file tests.
- Add audit-chain validators in Python.
- Make `AuditStamp` mandatory in feature, model, artifact, bus, gateway, OMS,
  and ledger records where the scaffold currently makes it optional or
  protocol-level.
- Add fixture bundles that intentionally fail validation for missing lineage,
  bad checksums, time drift, and decimal rounding.

## Phase 2: Real Data Capture And Storage

Purpose: replace synthetic fixtures with reproducible point-in-time data.

Acceptance criteria:

- Kalshi market discovery and WebSocket capture run against a configured
  account without live order permissions.
- Raw venue payloads are stored before normalization.
- Capture jobs handle auth refresh, retries, rate limits, heartbeats,
  reconnects, and sequence gaps.
- Raw and normalized Parquet partitions have documented layout and retention.
- DuckDB can query partitions without rewriting source files.
- Data quality monitors report gaps, stale feeds, out-of-order events, bad
  schema versions, and normalization rejects.
- At least one external source, preferably NWS/METAR for weather contracts, is
  captured with provider timestamps and receipt timestamps.

Implementation tasks:

- Implement Kalshi REST discovery and authenticated WebSocket capture.
- Implement raw Parquet writer path and partitioning.
- Implement normalization reject storage with reason codes.
- Implement first external provider client and point-in-time mapper.
- Add CLI commands for capture, normalize, inspect partitions, and summarize
  data quality.
- Add integration tests using recorded fixtures, not live network calls.

## Phase 3: Replay, Backtest, And Paper Baseline

Purpose: make research results reproducible before adding models or live paths.

Acceptance criteria:

- Replay can run from raw or normalized storage into the same runner interface.
- Ordering is deterministic by exchange time, receipt time, sequence, and
  event ID tie-breakers.
- Order book reconstruction is deterministic and test-covered.
- Strategy state snapshots can save and restore.
- Backtest CLI emits a signed or checksummed report.
- Paper execution models fees, slippage, latency, queue position, cancel/replace
  behavior, venue pauses, lifecycle restrictions, and settlement timing.
- Reports include PnL, drawdown, exposure, fee, fill-rate, reject, queue, and
  latency metrics by strategy and sleeve.

Implementation tasks:

- Extend replay engine from simple normalized streams to full partition replay.
- Add order book reconstruction fixtures.
- Calibrate Kalshi fee, queue, and lifecycle rules from venue data.
- Add `backtest` CLI that runs replay -> strategy -> risk -> paper -> report.
- Add determinism tests against stored real-data fixtures.
- Add daily summary notebooks or scripts for researcher review.

Paper-ready gate:

- A researcher can run the same strategy over the same stored data twice and
  get identical reports.
- All inputs to the report can be traced by checksum and audit lineage.
- Strategy changes can be compared against a baseline without changing data,
  feature, or execution assumptions.

## Phase 4: Python Model Research And Artifact Export

Purpose: support ML and financial models in Python while preserving exact
runtime portability.

Acceptance criteria:

- Training datasets are generated only from point-in-time replay windows.
- Feature definitions have names, dtypes, windows, dependencies, versions, and
  leakage checks.
- Model training records include dataset checksum, feature schema checksum,
  code version, parameters, metrics, and random seeds.
- Walk-forward validation is the default evaluation mode.
- Model artifacts are exported into immutable bundles with checksummed files.
- Optional ONNX or another portable runtime format is produced when applicable.
- Parity cases are generated from real replay windows and included in bundles.
- Model registry supports draft, candidate, champion, challenger, deprecated,
  and revoked states.

Implementation tasks:

- Implement `FeatureBuilder` and `FeatureStore` for offline replay.
- Implement a training harness with deterministic splits and seed control.
- Add leakage tests for event-time joins, future labels, settlement data, and
  revised external data.
- Implement `ArtifactBundleWriter`, loader, validator, and promotion registry.
- Add model-output parity tests in Python.
- Add examples for a non-ML financial model and a simple ML model so both
  plugin styles are supported.

Model gate:

- No model can be promoted unless its bundle validates, its training data is
  reproducible, and parity cases pin expected feature and prediction outputs.

## Phase 5: Rust Runtime Parity And Speed Path

Purpose: move latency-sensitive runtime pieces to Rust without changing
business results.

Acceptance criteria:

- Rust contract readers load the same `contracts/` examples as Python.
- Rust feature builder produces byte-for-byte or tolerance-bound equivalent
  feature values for parity cases.
- Rust runner and risk traits can process intent flow without venue access.
- Rust gateway contracts preserve audit lineage, priority, idempotency, and
  stale-intent behavior.
- Rust benchmarks cover feature building, runner loop overhead, gateway queue
  scheduling, and serialization.
- Rust code treats ambiguous parsing as an error, not a guess.
- Rust runtime crates have no uncontrolled panics in normal error paths.

Implementation tasks:

- Add `serde`, validation, and golden fixture tests to `eventcontracts-contracts`.
- Add Rust parity CLI for bundle validation.
- Implement online feature builders behind explicit schemas.
- Implement priority scheduler and idempotency store.
- Implement benchmark suite with realistic event batches.
- Add CI job that runs Python and Rust against the same parity bundle.

Rust-live gate:

- Rust must not become the source of different financial answers. If Rust and
  Python disagree outside explicit tolerances, CI fails and the bundle cannot be
  promoted.

## Phase 6: Stateful Risk, OMS, Ledger, And Allocation

Purpose: ensure orders, fills, cash, and capital cannot drift silently.

Acceptance criteria:

- Pre-trade risk checks include order notional, projected exposure, open order
  count, daily loss, sleeve capital, market eligibility, account eligibility,
  geo/compliance restrictions, and kill switches.
- OMS state transitions are finite, validated, audited, and replayable.
- Ledger is double-entry and settlement-aware.
- Cash, positions, exposure, and PnL derive from ledger/fill events, not ad hoc
  mutable state.
- Reconciliation compares internal state with venue state and creates explicit
  break records.
- Allocator can reserve, release, rebalance, and attribute capital by sleeve.

Implementation tasks:

- Implement OMS state machine and transition persistence.
- Implement ledger store and position/cash keepers.
- Add reconciliation reports for orders, fills, cash, and positions.
- Add allocation service with conservative equal-weight policy first.
- Add policy tests for every reject reason and state transition.
- Add incident fixtures for duplicate fills, late cancels, partial fills, and
  venue rejects.

## Phase 7: Bus, Gateway, And Dry-Run Live Path

Purpose: isolate live venue interaction behind one controlled process.

Acceptance criteria:

- Typed bus topics exist for normalized events, features, predictions, intents,
  risk results, order commands, acks, fills, ledger updates, and alerts.
- Gateway is the only component that can access live venue credentials.
- Gateway validates idempotency keys, stale intent deadlines, venue lifecycle,
  risk approval, and rate-limit budget before sending.
- Priority scheduling honors `CRITICAL`, `FAST`, `STANDARD`, and `RELAXED`
  without bypassing risk.
- Dry-run mode records exactly what would have been sent, with no network order
  placement.
- Sandbox or read-only mode reconciles venue state without order placement.
- Every gateway command and ack has audit lineage.

Implementation tasks:

- Choose and implement the first bus backend for local/dev and production.
- Implement gateway command processor and dry-run sink.
- Implement credential provider with secret references, not repo secrets.
- Implement idempotency persistence.
- Add stale-intent dropping and queue-time metrics.
- Add gateway replay tests from recorded intent streams.

Dry-run gate:

- The system can run against live market data with order placement disabled,
  producing the same audited command stream that would be sent live.

## Phase 8: Observability, Ops, And Incident Response

Purpose: make failures visible and recoverable before capital is exposed.

Acceptance criteria:

- Structured logs include run ID, strategy ID, sleeve ID, event ID, correlation
  ID, audit checksum, and severity.
- Metrics cover feed health, replay lag, decision latency, queue time, order
  latency, fills, rejects, PnL, exposure, data gaps, reconciliation breaks, and
  risk kill-switch state.
- Traces connect raw event ingestion through strategy decision and gateway ack.
- Alerts exist for stale feeds, dropped intents, risk rejects spikes,
  reconciliation breaks, failed captures, gateway errors, and drawdown limits.
- Operator CLI supports halt sleeve, resume sleeve, cancel all, reconcile,
  inspect audit chain, replay incident window, and validate bundle.
- Runbooks exist for feed outage, bad data, venue outage, runaway strategy,
  credential rotation, and disaster restore.

Implementation tasks:

- Implement structured logger, metrics recorder, tracer, and health checks.
- Add Grafana/Prometheus/OpenTelemetry dev stack.
- Add operator CLI commands.
- Add backup and restore procedures for data, bundles, state, and configs.
- Add load, soak, and chaos tests before live rollout.

## Phase 9: Limited Live Rollout

Purpose: expose minimal capital only after paper and dry-run evidence is strong.

Preconditions:

- Paper strategy is profitable after fees, slippage, latency, queue, settlement,
  and paper-trading decay.
- Dry-run command stream has been reviewed and reconciled against venue state.
- Risk, OMS, ledger, reconciliation, observability, and ops commands are live.
- Kill switch has been tested.
- Credentials are isolated and revocable.
- Capital allocation is tiny, sleeve-scoped, and explicitly approved.

Rollout sequence:

1. Read-only live market data with paper execution.
2. Dry-run gateway with command capture only.
3. Shadow live mode: compare paper orders against what gateway would send.
4. One venue, one strategy family, one sleeve, minimal capital.
5. Manual review after each session.
6. Gradual size increases only after reconciliation and PnL attribution are
   clean over multiple sessions.

Hard stop conditions:

- Any unexplained reconciliation break.
- Any audit-chain gap for an order-affecting decision.
- Any Python/Rust parity drift for promoted bundles.
- Any stale feed used for an order-affecting decision.
- Any risk or gateway reject reason that cannot be explained.
- Any operator unable to halt sleeve and cancel orders quickly.

## Phase 10: Scale And Hardening

Purpose: expand only after the first live sleeve is operationally boring.

Expansion tasks:

- Add Polymarket adapter and cross-venue normalization.
- Add macro, crypto, and additional external signal providers.
- Add multi-sleeve allocator policies beyond equal weight.
- Add tax-lot and reporting workflows if needed.
- Add container build, deployment manifests, and environment promotion.
- Add object-store backed artifact registry and immutable data retention.
- Add security review, dependency scanning, and secret rotation checks.
- Add disaster-recovery drills and restore-time objectives.
- Add larger performance benchmarks and Rust runtime tuning.

## Recommended Immediate Next Steps

1. Commit the current scaffold additions once the branch validates locally.
2. Finish Phase 0 by making `make quality` reliable in a clean environment.
3. Implement Phase 1 audit-chain validation before adding more live-facing code.
4. Build one Kalshi weather capture path and one NWS/METAR external data path.
5. Run the weather-threshold strategy through real captured data in paper mode.
6. Export the first artifact bundle with parity cases.
7. Add Rust readers for that bundle and fail CI on parity drift.

The fastest safe path is still narrow: one venue, one market family, one
strategy family, one paper/live sleeve. Breadth comes after replay, audit,
parity, risk, and reconciliation are proven on that slice.
