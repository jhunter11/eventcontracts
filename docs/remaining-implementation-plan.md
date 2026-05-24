# Remaining Implementation Plan

This plan is based on a source scan of the current
`claude/codebase-structure-audit-rJ4xO` worktree. It includes the uncommitted
audit/schema/Rust scaffold additions. The codebase has a working local research
slice, but most live-facing and model-facing components are still deliberate
interfaces.

## Current Scan Summary

What is already partially implemented:

- Python domain contracts for markets, events, orders, fills, positions, specs,
  features, decisions, latency priority, and serialization.
- Raw ingestion pipeline with an iterable test source.
- Basic raw-to-normalized normalizers for local trade, quote, and book payloads.
- In-memory and Parquet event storage, with DuckDB read-path tests when DuckDB
  is installed.
- Deterministic normalized replay into the runner.
- Strategy registry, built-in example strategy, runner, risk gate, paper
  simulator, latency/queue models, PnL tracker, reports, and a backtest CLI.
- Audit stamp primitives, in-memory audit trail validation, and contract schema
  validation helpers.
- Rust workspace scaffolds for contracts, feature builder, runner, gateway,
  bus, allocator, and parity.
- Strategy plug-in discovery now imports repository-local strategy modules
  dynamically, so new strategies under `eventcontracts.plugins.strategies` do
  not need a central registry edit.
- Conservative dynamic allocation now exists through `InMemorySleeveRegistry`
  and `EqualWeightAllocator`.
- Metadata-only market detection now exists through `InMemoryMarketCatalog` and
  `SubscriptionMarketDetector`.
- Dry-run gateway routing now exists through `InMemoryPriorityScheduler`,
  `InMemoryIdempotencyStore`, and `DryRunVenueGateway`; it records commands but
  cannot place live orders.

What is still mostly interface-only:

- Venue adapters: Kalshi and Polymarket clients are placeholders.
- External data: provider clients and observation-to-envelope mapping are
  placeholders.
- Features and models: schemas exist, but builders, stores, trainers, exporters,
  model runners, and registries are placeholders.
- Artifact bundles: manifest contracts and CLI validation exist, but writer,
  loader, checksum validator, parity validator, and promotion registry are
  placeholders.
- Bus, gateway, OMS, ledger, allocation, observability, compliance, and
  cross-venue normalization are placeholders.
- Rust crates compile but do not yet parse files, serialize records, run parity,
  or execute runtime loops.
- There are 115 `raise NotImplementedError` sites in Python (inventory pinned
  by `tests/test_missing_implementations.py`). Many are valid protocol
  boundaries, but the concrete local implementations listed below should be
  added before live-facing work.

## Immediate Principle

Do not start live order placement yet. The next implementation work should make
data lineage, contract validation, and local replay/paper behavior harder to
misuse. Live venue execution belongs behind a dry-run gateway only after audit,
artifact, parity, OMS, ledger, and reconciliation paths exist.

## Phase A: Lock The Current Scaffold

Goal: make the current branch clean, reproducible, and honest about unfinished
interfaces.

Implementation targets:

- `python/tests/test_missing_implementations.py`: add an explicit inventory of
  allowed `NotImplementedError` sites by module and class. Done.
- `rust/Cargo.toml`: update the stale "Rust workspace stub" comment now that
  crates exist. Done.
- `.github/workflows/quality.yml`: keep Python and Rust jobs separate; add a
  contract validation test command once Phase B lands.
- `python/tests/conftest.py`: centralize optional dependency skip helpers for
  DuckDB, PyArrow, and Polars-backed tests.
- `README.md` and `docs/dataflow-map.md`: keep current package and Rust crate
  descriptions aligned with actual files. In progress as implementations land.

Acceptance criteria:

- `python3 -m compileall -q python/src python/tests` passes.
- `cd python && PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q` passes
  in a lightweight environment, skipping only tests whose backend dependency is
  missing.
- `make rust-check` passes.
- `git diff --check` passes.
- Missing implementations are visible in one test-backed inventory, not hidden
  by scattered placeholders.

## Phase B: Audit Chain Everywhere Data Crosses A Boundary

Goal: every durable or cross-process object can be traced back to input data.

Implementation targets:

- `python/src/eventcontracts/storage/interfaces.py`: add optional or mandatory
  audit metadata to `EventEnvelope`, or add a companion audit mapping that does
  not break current tests.
- `python/src/eventcontracts/normalization/pipeline.py`: make
  `NormalizationResult` include child audit stamps and raw-to-normalized
  `AuditLink` records.
- `python/src/eventcontracts/features/pipeline.py`: make feature vectors require
  audit when written; add an in-memory feature store for tests. First local
  store is done.
- `python/src/eventcontracts/models/pipeline.py`: make `TrainingRun.metrics`
  immutable and require audit for completed runs.
- `python/src/eventcontracts/artifacts/bundle.py`: add local bundle checksum
  validation using streamed SHA-256 and `AuditTrailValidator`.
- `python/tests/test_audit.py`: extend from simple parent-chain tests to raw ->
  normalized -> feature -> model/artifact chains.

Acceptance criteria:

- Audit chain validation fails if any parent stamp, link, checksum, or schema
  version is missing.
- Normalization rejects retain enough audit data to explain why data did not
  become a strategy-facing event.
- Feature/model/artifact records cannot be promoted without audit stamps.

## Phase C: Contract And Artifact Bundle Implementation

Goal: make bundle validation and promotion real enough for model/strategy
iteration.

Implementation targets:

- `python/src/eventcontracts/contracts/schema.py`: finish the current schema
  subset used by all top-level schemas, or replace it with `jsonschema` if we
  intentionally accept the dependency.
- `python/src/eventcontracts/artifacts/bundle.py`: implement
  `LocalArtifactBundleWriter`, `LocalArtifactBundleLoader`,
  `LocalArtifactBundleValidator`, and `InMemoryPromotionRegistry`.
- `python/src/eventcontracts/cli/validate_bundle.py`: validate all referenced
  files, detect bundle path escapes, and reject non-placeholder checksum drift.
- `contracts/examples/weather_threshold/manifest.toml`: replace placeholder
  file checksums with real checksums once the writer exists.
- `contracts/parity/weather_threshold/`: add a small JSON or Parquet parity
  fixture with expected features and decisions.

Acceptance criteria:

- A bundle can be written, loaded, validated, and promoted locally without hand
  editing TOML.
- File checksums are deterministic and streamed, not loaded into memory as one
  blob.
- The validator rejects missing files, changed files, path escapes, unknown
  schema versions, and invalid event kinds.

## Phase D: Point-In-Time Features And Python Model Harness

Goal: support Python-first ML and financial model experiments without leakage.

Implementation targets:

- `python/src/eventcontracts/features/pipeline.py`: implement
  `InMemoryFeatureStore` and a base deterministic `FeatureBuilder` helper.
- `python/src/eventcontracts/models/pipeline.py`: implement in-memory model
  registry, deterministic training dataset metadata, and a rules-model runner
  before adding ONNX.
- `contracts/schemas/feature_schema.schema.json`: align dtype enum with Python
  `FeatureDType` or extend Python to support `int32` and `string`.
- `python/tests/test_contract_schema_validation.py`: assert schema enums match
  Python enums for event kinds and feature dtypes.
- `python/tests/test_model_pipeline.py`: test dataset checksum, seed metadata,
  feature schema checksum, and prediction audit lineage.

Acceptance criteria:

- Feature rows are generated only from event-time ordered inputs.
- Training datasets carry feature schema checksum, input event checksums, seed,
  and label-window metadata.
- A simple non-ML/rules model and a simple ML-ready interface can both be
  exported into artifact bundles.

## Phase E: Replay And Paper Execution Hardening

Goal: make paper results reproducible enough for strategy iteration.

Implementation targets:

- `python/src/eventcontracts/storage/parquet_store.py`: finish normalized
  deserialization for own fill, own order update, and own order reject events.
- `python/src/eventcontracts/replay/engine.py`: add a concrete replay engine
  with deterministic tie-breakers and optional checkpoint windows.
- `python/src/eventcontracts/execution/market_simulator.py`: add audit hooks for
  simulated fills, stale order handling, and explicit lifecycle reject records.
- `python/src/eventcontracts/cli/backtest.py`: output full
  `BacktestReport.from_run(...)`, not only a short JSON summary.
- `python/tests/test_determinism.py`: include audit checksum comparison and real
  report serialization.

Acceptance criteria:

- The same stored input partition produces byte-identical reports.
- Paper fills, rejects, fees, queue assumptions, and latency assumptions are
  explicit in the report.
- Backtest output can be traced to data partitions and strategy/model bundle
  checksums.

## Phase F: Real Capture For One Narrow Slice

Goal: capture real point-in-time data for one venue and one external source.

Implementation targets:

- `python/src/eventcontracts/adapters/venues/kalshi/client.py`: implement
  read-only market discovery, order book snapshot, and trade/book stream
  capture with auth, retry, heartbeat, rate-limit, and sequence-gap reporting.
- `python/src/eventcontracts/external/base.py`: add a concrete NWS/METAR client
  and envelope mapper for weather observations.
- `python/src/eventcontracts/ingestion/pipeline.py`: add checkpoint and
  data-quality counters without changing raw payloads.
- `python/src/eventcontracts/normalization/basic.py`: add venue-specific
  Kalshi weather payload mappers beside the generic local normalizers.
- `python/tests/fixtures/`: add recorded payload fixtures so CI never depends
  on live network calls.

Acceptance criteria:

- Raw venue and external payloads are persisted before normalization.
- Capture failures, sequence gaps, stale feeds, and normalization rejects are
  surfaced as data-quality records.
- CI uses recorded fixtures only.

## Phase G: OMS, Ledger, Allocation, And Reconciliation

Goal: make order, fill, cash, and position state auditable before gateway work.

Implementation targets:

- `python/src/eventcontracts/oms/state.py`: implement `InMemoryOmsStateStore`
  and deterministic `OrderStateMachine` transitions.
- `python/src/eventcontracts/ledger/accounting.py`: implement in-memory ledger,
  position keeper, cash keeper, and settlement accounting for event-contract
  yes/no payouts.
- `python/src/eventcontracts/allocation/capital.py`: implement
  `InMemorySleeveRegistry` and conservative equal-weight allocator.
- `python/tests/test_oms.py`, `python/tests/test_ledger.py`,
  `python/tests/test_allocation.py`: add transition, double-entry, and
  allocation invariants.

Acceptance criteria:

- Every order transition is finite, audited, and replayable.
- Ledger entries balance per transaction.
- Positions, cash, and PnL derive from fills/ledger events instead of ad hoc
  mutable state.
- Reconciliation reports explicit local/venue mismatches.

## Phase H: Dry-Run Gateway And Bus

Goal: isolate live venue interaction behind typed commands without sending
orders.

Implementation targets:

- `python/src/eventcontracts/bus/contracts.py`: implement in-memory schema
  registry, canonical message codec, publisher, and subscriber for tests.
- `python/src/eventcontracts/gateway/base.py`: implement in-memory
  idempotency store, priority scheduler, and dry-run gateway.
- `python/src/eventcontracts/risk/compliance.py`: implement explicit
  placeholder-deny compliance rules for live mode.
- `python/src/eventcontracts/cli/main.py`: add operator commands for
  validate-audit-chain, dry-run-gateway, halt-sleeve, and reconcile once stores
  exist.

Acceptance criteria:

- Dry-run gateway records exactly what would have been sent and why.
- Idempotency prevents duplicate command sends.
- Priority scheduling honors critical/fast/standard/relaxed without bypassing
  risk.
- No code path can place live orders yet.

## Phase I: Rust Parity And Runtime Foundations

Goal: make Rust a parity-checked runtime path, not a second source of truth.

Implementation targets:

- `rust/crates/contracts`: add serde-based readers for contract examples and
  strict validators for IDs, timestamps-as-strings, decimals-as-strings, and
  SHA-256 strings.
- `rust/crates/parity`: implement a CLI or test harness that loads the same
  parity cases as Python.
- `rust/crates/feature-builder`: implement one deterministic feature builder
  matching the Python weather-threshold fixture.
- `rust/crates/gateway`: implement priority scheduler and idempotency store
  once Python semantics are pinned.
- `.github/workflows/quality.yml`: add a Python/Rust parity job after fixtures
  exist.

Acceptance criteria:

- Rust and Python load the same bundle fixture and produce matching feature and
  decision outputs within explicit tolerances.
- Rust never silently parses ambiguous decimals or timestamps.
- Parity drift fails CI.

## Phase J: Observability And Ops

Goal: make failures visible before any live capital is exposed.

Implementation targets:

- `python/src/eventcontracts/observability/telemetry.py`: implement in-memory
  structured logger, metrics recorder, tracer, and health check aggregators.
- Add Prometheus/OpenTelemetry adapters later, behind the same interfaces.
- Add runbook docs for capture outage, bad data, venue outage, kill switch,
  credential rotation, and restore drill.
- Add CLI commands for incident replay and audit-chain inspection.

Acceptance criteria:

- Every long-running service can report health.
- Capture, replay, strategy, risk, gateway, OMS, ledger, and reconciliation
  events share correlation IDs and audit IDs.
- Operators can inspect why an order-affecting decision happened without
  reading raw logs manually.

## Review Recommendation

Implement in this order:

1. Phase A, because it makes the scaffold honest and CI-friendly.
2. Phase B, because auditability should be threaded before more records exist.
3. Phase C, because bundles are the bridge between research, replay, Rust, and
   eventual dry-run/live deployment.
4. Phase E, because paper results need to be reproducible before real capture
   or model promotion becomes useful.
5. Phase D and F in parallel only after audit and artifact gates are in place.
6. Phase G and H before any live order placement.
7. Phase I after the first Python bundle/parity fixture exists.

After review, the next concrete implementation batch should be Phase A plus the
first half of Phase B. That is small enough to complete and test in one pass
without accidentally starting live-facing behavior.
