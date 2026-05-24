# Live Rust Runner Implementation Roadmap

This roadmap defines the path from the current research/paper framework to a
live, production-grade event-contract trading system with a Rust hot path.
The target is not a thin live wrapper around the Python runner. The target is a
separate live runtime where Python remains the research and validation
environment, while Rust owns latency-sensitive live event processing, risk
gating, gateway command scheduling, and deterministic runtime parity.

The guiding rule is simple: no live order placement until the system can prove
what it saw, what it decided, what risk allowed, what the venue acknowledged,
what filled, what cash/positions changed, and how the same window replays.

## Current Baseline

Already implemented:

- Python domain contracts for events, decisions, specs, orders, fills,
  positions, latency priority, reports, and serialization.
- Strategy plugin surface and 11 strategy-spec implementations in rules mode.
- Parquet raw and normalized storage.
- DuckDB read path.
- Normalized replay and backtest CLI.
- Paper simulator, fees, queue/latency scaffolds, PnL tracker, and report.
- Kalshi REST market-data client.
- Kalshi WebSocket raw capture client with reconnect/backoff and sequence-gap
  metadata.
- Kalshi native raw normalizer for ticker, trade, orderbook, and lifecycle
  messages.
- `eventcontracts capture` for Kalshi raw capture.
- Subscription planning from active `StrategySpec` values.
- Rust workspace scaffolds for contracts, runner, gateway, bus, feature builder,
  allocator, and parity.

Major live gaps:

- No live order-placement gateway.
- No production OMS.
- No durable double-entry ledger.
- No reconciliation loop.
- No live compliance gate.
- No streaming normalize -> feature -> strategy -> risk -> gateway path.
- No Rust contract readers or parity tests.
- No Rust feature builder, strategy runtime, risk engine, or gateway scheduler.
- No model artifact loading/inference in Rust.
- No production observability, runbooks, deployment, or kill-switch controls.

## Target Production Architecture

The live system should be split into explicit processes. Each process has a
small ownership boundary and writes durable audit records.

```text
venue/external feeds
  -> capture services
  -> raw event log
  -> normalizer
  -> normalized event bus
  -> Rust live runner
      -> feature state
      -> model/strategy runtime
      -> pre-trade risk
      -> intent envelope
  -> gateway command bus
  -> Rust gateway
      -> final risk/compliance/stale checks
      -> idempotency
      -> priority scheduler
      -> venue submit/cancel/replace
  -> OMS
  -> ledger / position / cash
  -> reconciler
  -> observability / audit / incident replay
```

Python remains responsible for:

- Research, exploratory backtests, notebooks, and model development.
- Offline dataset generation.
- Bundle creation and validation.
- Paper-mode analysis and report review.
- Golden parity-case generation.

Rust owns live hot-path responsibilities:

- Typed event ingestion from the normalized bus.
- Event ordering, dedupe, and stale-feed gating.
- Online feature state.
- Native strategy runtime for promoted strategies.
- Model inference for promoted artifacts.
- Pre-trade risk in the live loop.
- Intent serialization.
- Priority scheduling and idempotent gateway command handling.
- Venue gateway implementation.
- Local write-ahead logs for recovery.

## Non-Negotiable Live Invariants

1. Raw payload is persisted before normalization.
2. Every normalized event links back to raw payload checksum, source, channel,
   sequence, exchange timestamp, receipt timestamp, schema version, and
   normalization version.
3. Every feature vector links to the exact event window that produced it.
4. Every prediction links to model artifact checksum, feature schema checksum,
   model version, and input feature vector.
5. Every strategy decision links to event ID, strategy ID, sleeve ID, strategy
   artifact/version, feature vector, prediction when applicable, and prior
   state snapshot hash.
6. Every order-affecting decision passes risk in the runner and again in the
   gateway.
7. The gateway is the only component with live venue credentials.
8. The gateway rejects stale intents, duplicate idempotency keys, unavailable
   markets, paused markets, rate-limit budget breaches, and missing risk
   approval.
9. OMS is the source of order state truth.
10. Ledger is the source of cash, position, fee, realized PnL, and settlement
    truth.
11. Reconciliation can detect and explain venue/local drift.
12. Operators can halt a sleeve and cancel live orders without code changes.
13. A fixed input partition can replay to the same decisions and report.
14. Python and Rust parity cases must match before any strategy runs live.
15. Live credentials, API keys, and private keys never enter strategy code,
    strategy configs, artifacts, reports, or logs.

## Promotion Gates

No strategy reaches live until it passes these gates in order.

1. Research gate:
   - Strategy spec validates.
   - Feature schema validates.
   - Backtest uses point-in-time data only.
   - Labels are built after outcome windows, never used as live inputs.

2. Paper gate:
   - Runs on captured real data.
   - Report is deterministic.
   - Fees, queue, slippage, latency, lifecycle, and settlement assumptions are
     explicit.
   - Drawdown, fill rate, rejection reasons, and exposure metrics are reviewed.

3. Bundle gate:
   - Strategy/model bundle is immutable and checksummed.
   - Feature schema, model artifact, strategy params, sleeve spec, and parity
     cases are included.
   - Audit chain validates.

4. Rust parity gate:
   - Rust loads the same bundle.
   - Rust and Python agree on features, predictions, decisions, risk outcomes,
     and report-critical calculations.
   - Differences are either zero or inside explicit tolerances.

5. Dry-run live gate:
   - Live market data feeds the Rust runner.
   - Gateway records commands but sends no orders.
   - Reconciler can read venue state.
   - Operators can halt/resume/cancel-all in dry-run mode.

6. Tiny-live gate:
   - One venue.
   - One strategy family.
   - One sleeve.
   - Minimal capital.
   - Manual review after every session.

## Phase 0: Repo Hardening And Truthful Inventory

Goal: make the current repository an honest base for live work.

Deliverables:

- Update `docs/implementation-roadmap.md` to reflect current `main`, not old
  branch state.
- Keep `tests/test_missing_implementations.py` as the pinned scaffold
  inventory.
- Add architecture decision records under `docs/adr/` for:
  - Python research vs Rust live split.
  - Event bus choice.
  - Storage layout.
  - Rust strategy plugin approach.
  - Secret management.
  - Live rollout policy.
- Add a live readiness checklist to CI that cannot pass accidentally.

Acceptance criteria:

- `make quality` passes from a clean clone.
- All intentional scaffold boundaries are documented.
- Roadmap and code agree on what is implemented.

## Phase 1: Contract Canonicalization

Goal: make Python and Rust parse the same data exactly.

Deliverables:

- Versioned canonical schemas for:
  - Raw event envelopes.
  - Normalized events.
  - Feature vectors.
  - Predictions.
  - Strategy decisions.
  - Intent envelopes.
  - Risk decisions.
  - Gateway commands and acks.
  - Orders, fills, rejects, ledger entries, settlements.
  - Strategy specs, sleeve specs, model refs, feature schemas.
- Canonical JSON rules:
  - Sorted keys.
  - Decimal values serialized as strings.
  - Timestamps as RFC3339 UTC strings.
  - IDs as strings.
  - No NaN/Infinity.
  - No unordered maps in checksummed payloads unless sorted.
- Python schema validators for all contract examples.
- Rust validators for the same examples.
- Golden contract fixtures under `contracts/examples/` and
  `contracts/parity/`.

Rust work:

- Add `serde`, `serde_json`, `thiserror`, `rust_decimal`, `time`, and strict
  validation helpers to `rust/crates/contracts`.
- Keep external decimals as strings at the contract boundary.
- Parse decimals/timestamps only in validated adapters.
- Add `cargo test` fixtures that load every Python-generated example.

Acceptance criteria:

- Python and Rust load all contract examples.
- Invalid decimal, timestamp, enum, missing field, checksum, and schema-version
  fixtures fail in both languages.

## Phase 2: Raw Capture To Normalized Replay Closure

Goal: make the current Kalshi capture work feed backtests without hand steps.

Deliverables:

- `eventcontracts normalize --data <root> --source kalshi-ws`.
- `eventcontracts capture --normalize`.
- Persist normalization rejects with:
  - raw event checksum
  - source/channel/schema
  - rejection reason
  - received timestamp
  - normalizer version
- Data inspection CLI:
  - raw counts by venue/source/channel/date
  - normalized counts by kind/date
  - first/last timestamps
  - gap counts
  - reject counts
- Capture run manifest:
  - start/end time
  - patterns
  - channels
  - strategy configs used
  - env name
  - code version
  - output partition root

Acceptance criteria:

- One command can capture Kalshi fixture data, normalize it, and backtest a
  strategy over normalized output.
- CI uses recorded fixtures only.
- Real capture can be stopped cleanly and flushes all buffers.

## Phase 3: Live Streaming Data Plane

Goal: replace batch-only capture/normalize/backtest with a durable streaming
data plane.

Deliverables:

- Choose production bus. Recommended:
  - NATS JetStream for command/event streaming.
  - Redpanda/Kafka if long retention and ecosystem tooling matter more.
  - In-memory bus only for tests.
- Typed topics:
  - `raw.kalshi.<channel>`
  - `normalized.<venue>.<kind>`
  - `feature.<schema_id>`
  - `prediction.<model_name>`
  - `intent.<sleeve_id>`
  - `risk.<sleeve_id>`
  - `gateway.command.<venue>`
  - `gateway.ack.<venue>`
  - `oms.order`
  - `oms.fill`
  - `ledger.entry`
  - `ops.alert`
- Backpressure rules per topic.
- Dead-letter queues for decode, validation, normalization, and gateway errors.
- At-least-once ingestion with idempotent consumers.
- Sequence and receipt-time ordering policy.

Rust work:

- Implement `rust/crates/bus`:
  - codec
  - topic validation
  - test in-memory bus
  - JetStream backend behind a feature flag
  - batch pull consumer for runner
  - publisher with backpressure metrics

Acceptance criteria:

- A recorded raw stream can flow through capture -> normalizer -> bus -> Rust
  replay consumer without data loss.
- Duplicate messages do not produce duplicate decisions.

## Phase 4: OMS And Ledger Before Gateway

Goal: make order/cash/position state real before any live order can be sent.

Deliverables:

- `InMemoryOmsStateStore` for tests.
- Durable OMS store:
  - SQLite for local.
  - Postgres for production.
- Finite order state machine:
  - created
  - pending_submit
  - acked/open
  - partially_filled
  - filled
  - pending_cancel
  - canceled
  - replace_pending
  - rejected
  - expired
  - reconciled
- Explicit illegal-transition errors.
- Idempotent fill handling.
- Durable double-entry ledger:
  - cash debit/credit
  - order holds
  - hold releases
  - fills
  - fees
  - settlement payout
  - corrections
- Position keeper derived from ledger/fills.
- Cash keeper derived from ledger.
- Settlement accounting for binary payouts.

Rust work:

- Implement live OMS and ledger types in Rust or implement a Rust client over a
  Postgres schema shared with Python.
- For speed, keep hot-path risk state in Rust memory and append durable events
  asynchronously with write-ahead safety.
- Use event-sourced order transitions so the runner can rebuild state after a
  restart.

Acceptance criteria:

- Duplicate fill fixture applies once.
- Late cancel after fill is handled deterministically.
- Partial fill then replace then cancel is audited.
- Ledger balances per transaction.
- Cash/exposure derives from ledger, not ad hoc state.

## Phase 5: Stateful Risk And Compliance

Goal: make risk a hard stateful barrier, not just a paper-mode helper.

Deliverables:

- Risk state store:
  - sleeve capital
  - open orders
  - positions
  - cash
  - daily realized loss
  - gross/net exposure
  - per-market exposure
  - venue rate budget
  - data freshness state
  - kill-switch state
- Risk checks:
  - max order notional
  - max projected position notional
  - max gross exposure
  - max daily loss
  - max open orders
  - max order rate
  - max cancel rate
  - stale market data reject
  - paused/closed market reject
  - unsupported market category reject
  - missing model/bundle parity reject
  - no live credentials in non-live mode
  - compliance eligibility reject
- Compliance policy:
  - account eligibility
  - country/region restrictions
  - venue-specific account state
  - market-category restrictions
  - manual allowlist for live sleeves
- Risk decision audit records.

Rust work:

- Implement `rust/crates/runner` risk trait with zero-allocation projected
  exposure checks.
- Keep risk snapshots in memory as immutable snapshots swapped atomically.
- Gateway repeats final risk checks from its own local state.

Acceptance criteria:

- Every reject reason has a test.
- Risk decisions are deterministic for fixed state.
- Gateway cannot send an order without a current risk approval.

## Phase 6: Artifact Bundles And Strategy Promotion

Goal: make live strategy code immutable, reviewed, and parity-tested.

Deliverables:

- Bundle writer/loader/validator:
  - `strategy_spec.toml`
  - `sleeve_spec.toml`
  - `feature_schema.json`
  - model artifact
  - strategy runtime artifact
  - parity cases
  - manifest with checksums
  - owner/reviewer/approval metadata
- Promotion registry states:
  - draft
  - candidate
  - paper-approved
  - dry-run-approved
  - live-approved
  - deprecated
  - revoked
- Signed bundle manifests.
- Bundle revocation list.
- Reproducible training metadata.

Rust strategy runtime options:

1. Native Rust strategy crate, statically linked:
   - Fastest.
   - Best for microstructure strategies.
   - Requires Rust implementation and parity with Python research version.

2. Rust DSL/interpreter for decision rules:
   - Safer promotion path for simple rule strategies.
   - Slower than native but still much faster than Python.

3. WASM plugin:
   - Strong sandboxing.
   - Good for untrusted or third-party strategy code.
   - Not fastest; use only when isolation beats latency.

Recommended approach:

- Use native Rust traits for latency-sensitive live strategies.
- Use a constrained declarative rule format for slower macro/weather strategies.
- Keep Python-only strategies in paper/dry-run until ported or compiled into a
  live-approved artifact.

Acceptance criteria:

- No mutable strategy source file is loaded directly in live.
- Live runner loads only approved immutable bundles.
- Bundle checksum appears in every live decision audit record.

## Phase 7: Rust Feature Builder And Model Inference

Goal: remove Python from the live inference path.

Deliverables:

- Rust feature-schema loader.
- Online feature state per strategy/sleeve/instrument.
- Feature warmup from replay window.
- Event-time rolling windows.
- Null/default handling exactly matching schema.
- Leakage-safe feature dependencies.
- Model artifact loader.
- Rules-model runtime.
- ONNX runtime integration for ML models where needed.
- Model output validation:
  - finite numeric output
  - confidence range
  - horizon match
  - model version match
  - stale feature reject

Performance design:

- Preallocate feature buffers.
- Use dense numeric arrays for hot features.
- Use string IDs interned to integer keys.
- Avoid heap allocation in per-event updates.
- Use monotonic timestamps internally after validated parse.
- Separate slow audit serialization from hot feature update.
- Batch model inference when latency budget allows.

Acceptance criteria:

- Rust and Python feature vectors match parity cases.
- Rust and Python model outputs match parity cases.
- Feature update benchmarks meet per-strategy latency budgets.

## Phase 8: Rust Live Runner Hot Path

Goal: implement the fastest safe live runner.

Runtime responsibilities:

- Consume normalized events from bus.
- Validate schema version.
- Dedupe by event ID/source sequence.
- Enforce event ordering policy.
- Maintain per-sleeve strategy state.
- Update feature state.
- Run model/strategy.
- Create intent envelopes.
- Run pre-trade risk.
- Publish approved intents and rejected risk records.
- Snapshot state.
- Expose metrics and health.

Hot-path architecture:

```text
bus batch poll
  -> decode into borrowed/raw contract record
  -> validate schema/version
  -> intern IDs
  -> update feature state
  -> strategy runtime
  -> risk projection
  -> intent encode
  -> publish batch
```

Performance requirements:

- No Python FFI in live hot path.
- No blocking disk I/O in event processing.
- No network calls from strategy code.
- No unbounded queues.
- No per-event dynamic schema lookup after warmup.
- No logging on every event except sampled counters and structured anomalies.
- No allocation in the common strategy path after warmup for microstructure
  strategies.
- No panics on malformed external input; return typed errors and dead-letter.

Rust implementation details:

- Tokio multi-thread runtime for I/O-bound services.
- Dedicated runner worker per sleeve or per latency class.
- Bounded MPSC channels between bus consumer and runner workers.
- `bytes::Bytes` for message payload sharing.
- `smallvec` or fixed arrays for small decision lists.
- `ahash` or `rustc_hash` only for non-consensus internal maps where
  deterministic ordering is not required.
- `BTreeMap` or sorted vectors for canonical serialization.
- `parking_lot` or lock-free atomics only where profiling justifies it.
- `tracing` spans with low-cardinality fields.
- `criterion` benchmarks for runner loop, feature update, strategy decision,
  risk check, serialization, and bus publish.
- Flamegraph profiling on representative feeds.

State snapshots:

- Snapshot per sleeve at fixed event counts and time intervals.
- Snapshot includes:
  - strategy state hash
  - feature state hash
  - risk state version
  - last processed event cursor
  - bundle checksum
- Recovery replays from last snapshot plus event log tail.

Acceptance criteria:

- Runner benchmark has a documented p50/p95/p99 budget by strategy class.
- Replay from snapshot produces identical decisions.
- Runner survives malformed event, duplicate event, out-of-order event, and
  stale feed fixtures.

## Phase 9: Rust Gateway

Goal: isolate venue access and make live order transmission deterministic,
idempotent, rate-limited, and observable.

Responsibilities:

- Consume approved intents.
- Revalidate bundle/risk/compliance/staleness.
- Convert intent to venue-native order request.
- Reserve idempotency key.
- Schedule by priority and rate budget.
- Submit/cancel/replace via venue API.
- Persist command and ack.
- Publish order update/reject/fill events.
- Run cancel-all and halt commands.

Kalshi gateway deliverables:

- Authenticated submit order.
- Authenticated cancel order.
- Authenticated amend/replace if available; otherwise cancel-replace policy.
- Open order sync.
- Fill/order private stream or polling fallback.
- Venue reject mapping.
- Rate-limit budget tracking.
- Idempotency persistence.
- Kill-switch path that bypasses normal alpha queues but does not bypass final
  safety checks.

Performance design:

- Separate critical cancel queue from normal submit queue.
- Strict bounded queues by priority.
- Deadline-aware stale-intent dropping.
- Connection pools for REST.
- Private WS fill stream handled separately from public market-data stream.
- No strategy code inside gateway.
- Backpressure to runner when gateway queue approaches risk limits.

Acceptance criteria:

- Dry-run gateway and live gateway share command semantics.
- Gateway can run in record-only mode.
- Every venue ack/reject maps to OMS transition.
- Duplicate command fixture sends once.
- Cancel-all works in sandbox/dry-run before live.

## Phase 10: Reconciliation And Self-Healing

Goal: catch drift before it compounds.

Deliverables:

- Order reconciliation:
  - local open orders vs venue open orders
  - missing local
  - missing venue
  - status mismatch
  - quantity/price mismatch
- Fill reconciliation:
  - venue fills not applied locally
  - local fills absent at venue
  - duplicate fills
- Cash and position reconciliation where venue exposes enough state.
- Break records with severity.
- Auto-actions:
  - halt sleeve on severe break
  - cancel all on unknown live exposure
  - force ledger correction only with explicit operator approval
- Reconciliation CLI:
  - `eventcontracts reconcile --venue kalshi`
  - `eventcontracts inspect-order <client_order_id>`
  - `eventcontracts inspect-audit <object_id>`

Acceptance criteria:

- Reconciliation breaks are test fixtures, not only log messages.
- Unknown live exposure halts new order placement.
- Operators can reproduce every break from persisted records.

## Phase 11: Observability And Operations

Goal: make live operation inspectable and controllable.

Telemetry:

- Structured logs:
  - run ID
  - service
  - strategy ID
  - sleeve ID
  - venue
  - market ID
  - event ID
  - correlation ID
  - bundle checksum
  - audit ID
  - severity
- Metrics:
  - feed lag
  - sequence gaps
  - normalization rejects
  - event throughput
  - runner latency
  - feature latency
  - model latency
  - risk latency
  - gateway queue depth
  - gateway send latency
  - venue ack latency
  - fills
  - rejects
  - cancel rate
  - PnL
  - drawdown
  - exposure
  - reconciliation breaks
  - kill-switch state
- Traces:
  - raw event -> normalized event -> feature -> decision -> risk -> gateway
    command -> ack/fill -> ledger.

Operator controls:

- halt sleeve
- resume sleeve
- cancel all by venue/sleeve/strategy
- rotate credentials
- reconcile now
- replay incident window
- drain gateway queue
- inspect raw/normalized partition
- inspect bundle
- revoke bundle

Runbooks:

- feed outage
- sequence gap storm
- normalizer rejects spike
- venue outage
- stale market data
- gateway reject spike
- reconciliation break
- runaway strategy
- drawdown breach
- credential compromise
- data corruption
- disaster restore

Acceptance criteria:

- On-call can answer "why did this order happen?" from CLI.
- On-call can stop trading without developer access.
- Every alert has a runbook and severity.

## Phase 12: Deployment, Security, And Environments

Goal: make live deployment repeatable and secure.

Environments:

- local
- fixture CI
- paper
- dry-run live data
- sandbox if venue supports it
- production live

Deployment:

- Container images for each service.
- Reproducible builds.
- SBOM and dependency scans.
- Signed artifacts.
- Config separated from code.
- Secrets mounted by reference, not stored in repo or artifact bundles.
- Infrastructure-as-code for production services.
- Blue/green deployment for gateway and runner.
- Rollback procedures.

Security:

- Least-privilege venue keys.
- Separate read-only and trading credentials.
- Hardware-backed or managed secret storage for private keys.
- Credential rotation drills.
- Audit log immutability.
- Network egress restrictions.
- No live order credentials in research machines.

Acceptance criteria:

- Production live can be redeployed from scratch from pinned artifacts.
- Credential compromise runbook has been tested.
- Live trading cannot start from an unapproved environment.

## Phase 13: Limited Live Rollout

Goal: expose capital only after dry-run is operationally boring.

Rollout sequence:

1. Fixture replay in Rust runner.
2. Captured real data replay in Rust runner.
3. Paper trading on live data.
4. Dry-run gateway on live data.
5. Shadow orders reviewed manually.
6. Tiny live sleeve with strict max notional.
7. Daily manual reconciliation review.
8. Gradual capital increases after multiple clean sessions.

Initial live constraints:

- One venue: Kalshi.
- One low-latency path first only after dry-run proves stable.
- One strategy family.
- One sleeve.
- Minimal order notional.
- No cross-venue live arb until both venue gateways and reconciliation loops are
  production-grade.
- No fully autonomous model promotion.

Hard stop conditions:

- unexplained reconciliation break
- stale data used for a decision
- missing audit link for an order-affecting decision
- gateway idempotency failure
- risk/gateway disagreement
- parity drift
- operator cancel-all failure
- unexpected live credential path
- unbounded queue growth
- unknown exposure

## Rust Performance Program

Performance is a workstream, not a final polish step.

Benchmarks from day one:

- contract decode
- normalized event validation
- feature update
- model inference
- strategy decision
- risk projection
- intent encode
- scheduler enqueue/dequeue
- gateway command build
- end-to-end event-to-intent latency

Recommended benchmark classes:

- macro/weather slow path: p99 under 50 ms
- standard strategy path: p99 under 5 ms
- fast strategy path: p99 under 500 us after event decode
- microstructure cancel path: p99 under 100 us inside runner/gateway excluding
  venue network

Implementation rules:

- Measure before optimizing.
- Treat allocation count as a benchmark metric.
- Keep canonical serialization off the critical path unless required for send.
- Keep audit data as compact IDs/checksums on the hot path; expand in async
  audit writers.
- Use bounded queues everywhere.
- Prefer static dispatch for native Rust strategies.
- Avoid global locks.
- Avoid per-event config parsing.
- Avoid string matching after warmup; intern IDs and enums.
- Separate latency classes into separate worker pools.
- Use CPU affinity only after profiling shows scheduler noise.
- Use production-like recorded feeds for benchmarks, not synthetic tiny events
  only.

CI performance gates:

- `cargo bench` baseline checked manually at first.
- Later, fail CI on large regressions for critical benchmarks.
- Store benchmark reports as CI artifacts.

## Recommended Implementation Order

1. Close the data loop:
   - `normalize` CLI
   - `capture --normalize`
   - data inspection CLI

2. Implement audit and artifact bundles:
   - checksummed bundle writer/loader/validator
   - promotion registry
   - parity case format

3. Build Rust contracts:
   - serde readers
   - strict validators
   - Python/Rust golden tests

4. Build OMS and ledger:
   - order state machine
   - durable store
   - double-entry ledger
   - reconciliation reports

5. Build stateful risk/compliance:
   - live-deny default
   - explicit allowlist for approved sleeves
   - full projected exposure checks

6. Build Rust runner MVP:
   - normalized event source
   - feature state
   - native rules strategy
   - risk gate
   - intent sink
   - benchmarks

7. Build dry-run gateway:
   - priority scheduler
   - idempotency
   - command recorder
   - no live send

8. Add live Kalshi gateway behind feature flag:
   - submit/cancel/replace
   - private fills
   - open-order reconciliation
   - live disabled by default

9. Add observability and operator controls:
   - halt/resume/cancel-all/reconcile
   - metrics/logs/traces
   - runbooks

10. Run dry-run live data until boring:
    - no unexplained rejects
    - no reconciliation breaks
    - no stale decisions
    - clean operator drills

11. Tiny live rollout:
    - one sleeve
    - explicit approval
    - minimal capital
    - manual review after every run

## First Concrete Build Batch

The next practical batch should not touch live order placement.

Implement:

- `eventcontracts normalize`.
- `eventcontracts capture --normalize`.
- `eventcontracts inspect-data`.
- Normalization reject Parquet partitions.
- Capture run manifest.
- Tests using the existing Kalshi fixtures.

Why this first:

- It closes the Phase 2 raw-to-backtest gap.
- It gives real data lineage for strategy research.
- It creates the batch parity cases Rust needs.
- It avoids prematurely building a gateway before data correctness is strong.

After that, implement artifact bundles and Rust contract readers.
