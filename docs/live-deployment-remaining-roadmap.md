# Remaining Live Deployment Roadmap

This document is the implementation backlog from the current repository state
to live trading. It intentionally starts from what is already working now:
Kalshi production WebSocket capture, historical weather paper tests, observed
book-depth probes, capital-aware weather sizing, and IOC/FOK-safe paper
execution.

Live order placement remains disabled until every deployment gate below is
green. The system should first run as a no-trade live paper stack, then as a
tiny-live stack with manual review, and only later as a Rust hot-path live
runner.

## Current Verified Baseline

As of May 26, 2026, the repo has these working pieces:

- Python research and paper stack:
  - typed events, decisions, specs, orders, fills, positions, reports
  - strategy plugin registry
  - Parquet raw and normalized storage
  - deterministic normalized replay
  - paper simulator with fee, latency, queue, lifecycle, settlement, IOC, and
    FOK handling
  - PnL tracker with settlement payouts
  - `backtest`, `capture`, `normalize`, `inspect-data`, `weather-*`, and
    sports-golf research CLIs
- Kalshi data path:
  - authenticated production REST works with the configured key
  - production WebSocket read-only capture works
  - Kalshi WS order book snapshots/deltas normalize into `OrderBookEvent`
  - raw payloads are persisted before normalization
  - normalization rejects are persisted
- Weather research path:
  - historical Kalshi weather market discovery
  - historical Kalshi candles
  - Open-Meteo historical forecast signals
  - weather strategy replay through paper execution
  - depth-sensitivity retests using live-observed depth caps
- Quality gate:
  - `make PYTHON=.venv/bin/python quality` passes
  - latest known count after IOC/FOK fix: 193 Python tests passing, ruff clean,
    mypy clean, Rust workspace `cargo check` clean

Known live-data observations:

- A 3-minute production WS probe for open `KXTEMPNYCH` markets at overnight
  New York time observed much smaller immediate depth than the historical
  synthetic stress setting:
  - YES ask depth median `0`, mean about `4.76`, p90 `10`, max `25`
  - NO ask depth median `0`, mean about `19.91`, p90 `157`, max `157`
- Historical weather sensitivity on the same event set:
  - depth `10`: `+1,949.90` PnL, `28.75` path max drawdown
  - depth `25`: `+4,881.92` PnL, `71.75` path max drawdown
  - depth `50`: `+9,766.46` PnL, `143.49` path max drawdown
  - depth `100`: `+19,537.41` PnL, `286.87` path max drawdown
  - depth `250`: `+48,815.51` PnL, `717.19` path max drawdown
- The `250` depth setting is a stress case, not a current realistic default.

## Non-Negotiable Live Invariants

No live order placement until all of these are implemented and tested:

1. Strategy code cannot access credentials, venue clients, storage writers, or
   gateway APIs directly.
2. Raw venue and external payloads are persisted before normalization.
3. Every normalized event has raw lineage, source, channel, schema version,
   exchange time, receipt time, and normalizer version.
4. Every signal, feature vector, prediction, decision, risk verdict, command,
   ack, fill, ledger entry, and settlement can be replayed or audited.
5. Live risk runs in the runner and again in the gateway.
6. The gateway owns credentials, idempotency, stale-intent rejection,
   rate-limit budgets, command scheduling, and final venue submit/cancel.
7. OMS is the source of order state truth.
8. Ledger is the source of cash, position, fees, realized PnL, and settlement
   truth.
9. Reconciliation can explain venue/local drift.
10. Operators can halt a sleeve and cancel all live orders without code edits.
11. Fixed input partitions replay deterministically.
12. Python and Rust agree on promoted strategy decisions before Rust live
    execution is enabled.

## Deployment Stages

### Stage 0: Current Paper Research

Purpose: run historical and captured-data experiments only.

Allowed:

- REST historical tests
- WS read-only capture
- normalization
- replay/backtest
- dry-run diagnostics

Blocked:

- live order placement
- live gateway credentials in strategy process
- unattended production processes

Exit criteria:

- weather and sports paper CLIs documented
- generated data ignored by git
- all current tests pass
- roadmap accepted as implementation backlog

### Stage 1: No-Trade Live Paper

Purpose: run the real-time pipeline for minutes or hours with zero venue
orders.

Required capabilities:

- live Kalshi WS capture service
- live external signal service for weather forecasts
- streaming normalizer
- event multiplexer that merges market data and external signals by event time
  and receipt time
- strategy runner operating continuously
- live risk evaluation
- dry-run sink that records order intents and risk verdicts but cannot submit
  to the venue
- durable run manifest, raw payloads, normalized events, signals, decisions,
  risk verdicts, simulated fills, and report snapshots

Acceptance tests:

- run for at least 3 minutes with no order gateway imported
- run for at least 60 minutes with no memory leak and no uncaught reconnect
  failure
- produce replayable event lake
- replay of the captured partition produces the same decisions
- stale weather signals produce no orders
- stale market data produces no orders
- operator halt stops new decisions

### Stage 2: Live Paper With Reconciliation

Purpose: compare local paper state against venue state without trading.

Required capabilities:

- authenticated read-only venue state polling:
  - account balance
  - open orders
  - positions
  - fills or trade history
- local OMS state store
- local ledger state store
- reconciliation reports:
  - local dry-run intended exposure
  - venue actual exposure
  - cash and positions
  - unexpected live orders
  - unexpected fills
- alert if venue has orders or positions not owned by the test harness

Acceptance tests:

- reconciliation runs on a schedule
- reconciliation writes durable reports
- reconciliation fails closed if venue APIs return 401, 429, timeout, or schema
  changes
- dry-run commands do not create venue orders
- cancel-all dry-run command records what it would cancel

### Stage 3: Tiny-Live Manual Session

Purpose: permit very small real orders with an operator present.

Hard restrictions:

- one venue: Kalshi
- one strategy family
- one sleeve
- tiny capital cap
- low max contracts per order
- low max active contracts
- no unattended runtime
- operator must start, monitor, and stop the session

Required capabilities:

- real Kalshi order gateway
- real submit/cancel/replace/cancel-all
- idempotency store
- final gateway risk gate
- live OMS transitions
- live ledger entries
- venue ack/reject handling
- fill handling
- settlement reconciliation
- emergency halt and cancel-all

Acceptance tests:

- unit tests for every gateway command
- recorded fixture tests for venue acks, rejects, partial fills, cancels,
  duplicate client order IDs, and rate limits
- sandbox or dry-run parity before production
- production tiny-live session can submit one intentionally tiny IOC order,
  reconcile it, and halt cleanly
- no stale-intent order reaches the gateway

### Stage 4: Limited Live Paper Automation

Purpose: run longer sessions with real market data and dry-run decisions, and
short supervised tiny-live windows.

Required capabilities:

- scheduled live paper sessions
- automatic restart from checkpoint
- persistent state snapshots
- alerting
- runbook-driven incidents
- day/time book-depth profiles
- capacity curves by market family, side, price bucket, and time to close

Acceptance tests:

- 1 to 2 weeks of paper sessions
- no unexplained state drift
- no stale-signal orders
- no risk-budget breaches
- replay reports match live paper decisions
- operator runbooks executed in drills

### Stage 5: Rust Hot-Path Runtime

Purpose: move latency-sensitive live runtime out of Python after parity is
proven.

Rust responsibilities:

- contract parsing and validation
- normalized event ingestion
- event ordering and dedupe
- online feature state
- promoted strategy runtime
- model inference for promoted artifacts
- live risk engine
- gateway command scheduling
- idempotency
- local write-ahead log
- recovery from checkpoint

Python remains responsible for:

- research
- historical backtests
- model training
- artifact bundle creation
- parity case generation
- operational analysis

Acceptance tests:

- Rust loads Python-generated contract fixtures
- Rust and Python agree on:
  - normalized event parsing
  - feature vectors
  - model outputs
  - strategy decisions
  - risk verdicts
  - PnL/report-critical calculations
- Rust runner can replay the same captured partition and produce matching
  decisions
- Rust gateway scheduler passes failure and rate-limit simulations

## Workstream A: Live Market Data

Deliverables:

- production `eventcontracts capture-live` or equivalent long-running service
- heartbeat and reconnect telemetry
- sequence-gap counters promoted to alerts
- subscription expansion from active strategy specs
- capture manifest with code version, env, patterns, tickers, channels, and
  credentials reference only
- data-quality summary for each run

Required fixes:

- normalize Kalshi `ticker` messages that carry zero quantity without treating
  them as fatal domain errors
- add a normalizer for `subscribed` acks or classify them as accepted control
  messages outside market-data normalization
- add capture timeout and bounded-run options for live probes
- add day/time book-depth probe CLI

Acceptance criteria:

- 3-minute, 1-hour, and multi-hour live capture runs complete without manual
  cleanup
- every raw file can be normalized or has an explainable reject
- data-quality report includes message counts, gaps, reconnects, rejects,
  stale periods, and top-of-book depth stats

## Workstream B: Live Weather Signal Service

Deliverables:

- periodic Open-Meteo/NWS forecast polling
- provider timestamp and receipt timestamp capture
- market-to-location and market-to-target-time mapper
- forecast snapshot cache
- external signal event writer
- stale-signal filter
- provider fallback policy

Required behavior:

- signal events must include:
  - provider
  - request time
  - provider publication/update time if available
  - market ticker
  - target time
  - threshold
  - direction
  - model family/version
  - implied probability
  - uncertainty features
- strategy must refuse to trade when:
  - forecast is older than configured max age
  - market target time cannot be mapped
  - current book is stale
  - provider error rate exceeds threshold

Acceptance criteria:

- no-trade live paper can produce weather signals and strategy decisions from
  live market data
- replay of captured signals produces identical decisions
- missing or stale forecasts produce `NoAction`

## Workstream C: Capacity, Depth, And Execution Realism

Deliverables:

- live depth probe CLI
- depth profiles by:
  - venue
  - market family
  - ticker root
  - side
  - price bucket
  - time of day
  - time to close
  - day of week
- historical replay depth cap sourced from observed profiles
- fill diagnostics comparing requested quantity, visible quantity, filled
  quantity, and unfilled IOC remainder

Implementation details:

- Replace static `--synthetic-candle-depth` defaults with profile-driven caps.
- Keep `10` as conservative overnight default for NYC temperature markets until
  better daytime data exists.
- Treat `250` as a stress parameter only.
- Add marketable fill reports that separate:
  - full IOC fills
  - partial IOC fills
  - zero-fill IOC cancellations
  - FOK cancellations
  - GTC passive remainders

Acceptance criteria:

- depth-sensitive backtest reports include capacity assumptions
- no paper result can be reported without its depth profile or depth cap
- live probes can be scheduled without writing secrets or placing orders

## Workstream D: Live Risk State

Deliverables:

- stateful runner-side risk context
- gateway-side final risk context
- shared risk state snapshots
- daily loss ledger
- open order exposure
- active notional
- gross and net exposure by:
  - sleeve
  - strategy
  - market
  - event/ladder
  - outcome side
- kill switch
- stale-data halt
- provider-health halt
- max order rate
- max cancel rate

Acceptance criteria:

- every `PlaceOrder` has a recorded risk verdict
- risk rejects are persisted with reason codes
- risk can be replayed from state snapshots
- kill switch blocks new orders and can optionally trigger cancel-all
- gateway rejects any command missing runner risk approval

## Workstream E: OMS

Deliverables:

- durable order state machine
- client order ID allocator
- venue order ID mapping
- order update ingestion
- cancel/replace tracking
- terminal state handling
- duplicate update handling
- pending timeout handling

States:

- created
- risk accepted
- gateway queued
- submitted
- acknowledged
- open
- partially filled
- filled
- canceled
- rejected
- expired
- unknown/reconcile required

Acceptance criteria:

- all state transitions are explicit and tested
- invalid transitions fail
- OMS can restore from durable state
- OMS can reconcile against venue open orders

## Workstream F: Ledger And Settlement

Deliverables:

- durable cash ledger
- fee ledger
- position ledger
- realized PnL ledger
- settlement ledger
- daily PnL and daily loss views
- venue statement reconciliation

Acceptance criteria:

- every fill creates cash, fee, and position entries
- every settlement creates payout and realized PnL entries
- no position can disappear without fill, cancel, expiry, or settlement
- ledger can reproduce `PnLTracker` results from durable entries
- venue/local cash and positions reconcile

## Workstream G: Kalshi Live Gateway

Deliverables:

- submit order
- cancel order
- replace order
- cancel all
- idempotency key store
- rate-limit budget
- retry policy
- stale-intent reject
- final risk check
- command write-ahead log
- ack/reject parser
- private state polling or private stream integration

Safety requirements:

- gateway is disabled by default
- live mode requires explicit config flag
- production credentials are only read by gateway process
- per-session capital cap required
- per-session order count cap required
- dry-run mode and live mode use the same command path up to the final submit
  call

Acceptance criteria:

- gateway fixture suite covers acks, rejects, partial fills, rate limits,
  disconnects, duplicate command IDs, and cancel-all
- dry-run and live command logs have identical schema
- tiny-live session can be replayed and reconciled

## Workstream H: Reconciliation

Deliverables:

- scheduled reconciler
- manual reconciler CLI
- venue balance snapshot
- venue positions snapshot
- venue open orders snapshot
- venue fills snapshot
- local OMS snapshot
- local ledger snapshot
- drift report
- severity classification

Drift classes:

- missing local order
- missing venue order
- status mismatch
- fill mismatch
- quantity mismatch
- fee mismatch
- cash mismatch
- position mismatch
- unknown venue order

Acceptance criteria:

- reconciler can run before any live order placement
- any unknown live order triggers alert
- drift report links to raw venue payloads and local audit records
- severe drift can trigger halt and cancel-all

## Workstream I: Observability And Operations

Deliverables:

- structured JSON logs
- metrics
- traces for decision-to-order lifecycle
- dashboards
- alerts
- run manifests
- incident replay CLI
- operator runbooks

Minimum metrics:

- feed connected
- feed reconnects
- sequence gaps
- raw messages per second
- normalization rejects
- event lag
- forecast age
- strategy decisions
- risk rejects by reason
- order commands
- gateway rejects
- fills
- PnL
- drawdown
- exposure
- reconciliation drift

Runbooks:

- start no-trade live paper
- stop live paper
- rotate keys
- stale feed
- provider outage
- Kalshi 401
- Kalshi 429
- sequence gap
- risk breach
- halt sleeve
- cancel all
- reconcile drift
- replay incident

Acceptance criteria:

- operator can diagnose a failed live paper run without reading source code
- every alert has an owner action
- every production session has a manifest and end-of-run report

## Workstream J: Artifact And Promotion Flow

Deliverables:

- strategy bundle writer
- strategy bundle validator
- model bundle writer
- model bundle validator
- feature schema checksum
- parameter checksum
- parity cases
- promotion registry
- rollback registry

Promotion states:

- draft
- research accepted
- paper candidate
- dry-run candidate
- tiny-live candidate
- live limited
- disabled
- revoked

Acceptance criteria:

- no strategy config can be promoted without validation
- no model can be promoted without reproducible training metadata
- promoted bundle can be loaded by Python and Rust
- rollback is a registry change, not a code edit

## Workstream K: Rust Hot Path

Deliverables:

- `rust/crates/contracts`:
  - parse all promoted contract schemas
  - canonical validation
  - golden fixture tests
- `rust/crates/feature-builder`:
  - online feature state
  - parity with Python feature fixtures
- `rust/crates/runner`:
  - event loop
  - strategy runtime
  - decision emission
  - risk handoff
- `rust/crates/gateway`:
  - command scheduler
  - idempotency
  - final risk
  - Kalshi submit/cancel/replace client
- `rust/crates/parity`:
  - Python vs Rust fixture runner
  - report mismatches
- benchmarks:
  - event parse latency
  - book update latency
  - feature update latency
  - decision latency
  - gateway scheduling latency

Acceptance criteria:

- Rust can replay a captured Python event lake and match decisions
- Rust can run no-trade live paper with the same dry-run outputs as Python
- Rust gateway dry-run passes the same command fixture suite
- performance targets are measured, not guessed

## Workstream L: Security And Secret Management

Deliverables:

- credential provider interface
- local key path validation
- production key rotation runbook
- no secrets in logs
- no secrets in reports
- no secrets in strategy configs
- no secrets in artifact bundles
- production vs demo environment guard
- live trading enable flag

Acceptance criteria:

- `.env` can configure local research, but production deployment reads secrets
  from the chosen secret manager
- accidental demo/prod mismatch fails clearly
- key path and key ID diagnostics print only presence and shape, not values
- CI checks prevent committing `.env`, private keys, and generated live data

## Workstream M: CI/CD And Deployment

Deliverables:

- Python quality job
- Rust quality job
- contract fixture parity job
- bundle validation job
- Docker images or deployment artifacts for:
  - capture
  - normalizer
  - live paper runner
  - gateway
  - reconciler
- deployment config templates
- database/object-store migrations if used

Acceptance criteria:

- clean clone can run quality
- CI never touches live network
- live deployment requires explicit manual approval
- deployment artifact includes code version and contract version

## Recommended Implementation Order

1. Ignore generated live data and commit the current weather/IOC baseline.
2. Add live depth probe CLI from the ad hoc probe.
3. Fix Kalshi ticker normalizer zero-size handling.
4. Add live weather signal service.
5. Add no-trade live paper runner.
6. Persist decisions, risk verdicts, and dry-run command records.
7. Add live paper replay parity.
8. Add stateful live risk context.
9. Add OMS state machine.
10. Add ledger/reconciliation.
11. Add dry-run gateway command path around the existing `DryRunVenueGateway`.
12. Add real Kalshi gateway behind disabled-by-default live flag.
13. Run 1 to 2 weeks of no-trade live paper.
14. Run tiny-live manual session.
15. Build Rust contract/parity layer.
16. Move runner/gateway hot path to Rust only after parity is green.

## First Concrete Milestone

Milestone: one-hour no-trade live paper weather session.

Scope:

- Kalshi production WS market data
- Open-Meteo/NWS live forecast polling
- weather strategy decisions
- live risk verdicts
- dry-run command records
- no venue order placement
- replayable event lake and report

Definition of done:

- one command starts the session
- one command inspects the run
- one command replays the captured run
- no order gateway submit method is invoked
- replay decisions match live dry-run decisions
- report includes depth profile, forecast age, decisions, rejected decisions,
  simulated fills, PnL, drawdown, and exposure
