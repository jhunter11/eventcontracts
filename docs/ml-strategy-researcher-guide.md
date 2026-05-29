# ML Strategy Researcher Guide

This document is the operating contract for researchers building strategies,
features, labels, and models inside this repository. It defines what a
researcher may consume, what they may emit, how labels are constructed, how
strategy code plugs into allocation and execution, and what constraints must
hold before a strategy can move from notebook research to replay, paper,
dry-run, and eventually live capital.

The framework is intentionally strict. A strategy is not a bot. A strategy is a
pure-ish function from point-in-time information to typed decisions. The runner,
risk gate, allocator, paper executor, OMS, gateway, ledger, and reconciliation
systems own everything else.

## Current Readiness Boundary

The current repository is ready for:

- Strategy plug-in development.
- Point-in-time feature design.
- Label design and offline dataset construction.
- Replay and paper-style validation against stored or synthetic data.
- Dry-run gateway routing that records what would have been sent.
- Conservative dynamic allocation in paper/dry-run mode.

The current repository is not ready for:

- Real live order placement.
- Real venue credentials.
- Autonomous live capital.
- Production model promotion without parity fixtures.
- Any strategy that assumes gateway, OMS, ledger, or reconciliation are fully
  implemented.

The researcher should design strategies as if they will eventually run live,
but must validate them through replay, paper, artifact, parity, and dry-run
gates first.

## Core Architecture For Researchers

The strategy boundary is:

```text
(NormalizedEvent, StrategyContext) -> Sequence[StrategyDecision]
```

The strategy receives one normalized event and a read-only context. It returns
typed decisions. It must not mutate external state or call live systems.

The live-shaped flow is:

```text
market/external raw payload
  -> EventEnvelope
  -> NormalizedEvent
  -> FeatureBuilder / FeatureStore
  -> StrategyContext
  -> Strategy.on_event(event, ctx)
  -> StrategyDecision
  -> IntentEnvelope
  -> RiskGate
  -> paper executor or dry-run gateway
  -> OMS / ledger / reconciliation later
```

The researcher works between two boundaries:

- Upstream boundary: point-in-time normalized events, features, context, and
  model predictions.
- Downstream boundary: typed strategy decisions that the runner wraps into
  `IntentEnvelope` records for risk, allocation, paper execution, or dry-run
  gateway routing.

The researcher does not own:

- Venue connectivity.
- Raw data persistence.
- Order placement.
- Position mutation.
- Cash mutation.
- Risk overrides.
- Gateway routing.
- Allocation enforcement.
- Ledger accounting.
- Reconciliation.

## Repository Locations

Strategy modules:

```text
python/src/eventcontracts/plugins/strategies/
```

Feature and model contracts:

```text
python/src/eventcontracts/domain/features.py
python/src/eventcontracts/features/pipeline.py
python/src/eventcontracts/models/pipeline.py
contracts/schemas/feature_schema.schema.json
```

Strategy and sleeve configs:

```text
configs/strategies/
configs/sleeves/
contracts/examples/<bundle>/
```

Backtest, replay, and validation CLIs:

```text
python/src/eventcontracts/cli/backtest.py
python/src/eventcontracts/cli/replay.py
python/src/eventcontracts/cli/validate_bundle.py
```

Allocation, market detection, and gateway boundaries:

```text
python/src/eventcontracts/allocation/capital.py
python/src/eventcontracts/markets/detection.py
python/src/eventcontracts/gateway/base.py
```

## How To Add A Strategy

Create one module under:

```text
python/src/eventcontracts/plugins/strategies/<strategy_name>.py
```

The module must:

1. Define a class that subclasses `StrategyBase` or implements the `Strategy`
   protocol.
2. Read immutable parameters from `StrategySpec`.
3. Implement `on_event(event, ctx)`.
4. Return only `StrategyDecision` variants.
5. Register one factory with `@register("strategy_name")`.
6. Add tests proving it loads with `create_from_spec`.

Minimal shape:

```python
from collections.abc import Sequence
from decimal import Decimal
from uuid import uuid4

from eventcontracts.domain import NoAction, PlaceOrder, StrategyDecision, TradeEvent
from eventcontracts.domain.ids import ClientOrderId
from eventcontracts.domain.models import OutcomeSide
from eventcontracts.domain.orders import OrderSide, OrderType, TimeInForce
from eventcontracts.domain.spec import StrategySpec
from eventcontracts.strategy import StrategyBase, StrategyContext, register


class MyStrategy(StrategyBase):
    def __init__(self, spec: StrategySpec) -> None:
        super().__init__(spec)
        self.buy_below = Decimal(str(spec.parameters["buy_below"]))
        self.size = Decimal(str(spec.parameters["size"]))

    def on_event(self, event, ctx: StrategyContext) -> Sequence[StrategyDecision]:
        if not isinstance(event, TradeEvent):
            return (NoAction(reason="ignored:not_trade"),)

        if event.trade.price >= self.buy_below:
            return (NoAction(reason="price_above_threshold"),)

        return (
            PlaceOrder(
                client_order_id=ClientOrderId(uuid4().hex),
                instrument_id=event.trade.instrument_id,
                outcome_side=OutcomeSide.YES,
                order_side=OrderSide.BUY,
                order_type=OrderType.LIMIT,
                time_in_force=TimeInForce.GTC,
                quantity=self.size,
                price=event.trade.price,
                reason="model_or_rule_edge",
            ),
        )


@register("my_strategy")
def factory(spec: StrategySpec) -> MyStrategy:
    return MyStrategy(spec)
```

Repository-local strategy discovery is automatic. If your module is under
`eventcontracts.plugins.strategies` and registers itself, `create_from_spec`
will load it without a central registry edit.

## Strategy Scope

A strategy may:

- Inspect the current `NormalizedEvent`.
- Read positions, cash, exposure, open orders, features, and predictions through
  `StrategyContext`.
- Read immutable parameters from `StrategySpec.parameters`.
- Read its `StrategySpec.model` and `feature_schema_id`.
- Keep internal deterministic state.
- Emit `PlaceOrder`, `CancelOrder`, `ReplaceOrder`, `Alert`, or `NoAction`.
- Set execution priority hints on order-affecting decisions.
- Snapshot and restore opaque state bytes.

A strategy may not:

- Call venue clients.
- Read or write raw storage.
- Write Parquet directly.
- Publish bus messages directly.
- Place orders directly.
- Bypass the risk gate.
- Change sleeve capital directly.
- Mutate positions or cash.
- Mutate global allocation state.
- Use wall-clock time from `datetime.now()` directly inside decision logic.
- Use future labels, final settlement, revised external data, or hindsight
  reference data as if they were known at decision time.
- Spawn background threads or long-running network calls from `on_event`.

The strategy must be replayable. For a fixed input event stream, fixed context
snapshots, fixed model artifact, fixed feature schema, and fixed parameters, it
must produce the same decisions.

## Allowed Inputs

### Current Event

Strategies consume only `NormalizedEvent` variants:

| Variant | Use |
| --- | --- |
| `QuoteEvent` | Best bid/ask, spread, mid, microstructure signals. |
| `TradeEvent` | Last sale, trade direction, volume, momentum, impact. |
| `OrderBookEvent` | Full depth, imbalance, queue estimates, slippage. |
| `LifecycleEvent` | Market open, pause, close, determined, disputed, finalized. |
| `SettlementResolvedEvent` | Final outcome for training, replay, accounting; not for pre-settlement live decisions. |
| `ExternalSignalEvent` | Point-in-time weather, crypto, macro, or other external inputs. |
| `TimerEvent` | Scheduled evaluation, timeout, rebalance, stale feature checks. |
| `OwnFillEvent` | Own fill feedback. |
| `OwnOrderUpdateEvent` | Own order state feedback. |
| `OwnOrderRejectEvent` | Own reject feedback. |

No venue-native payloads should leak into strategy logic. If a field matters,
promote it into a normalized event, metadata field, feature, or config.

### StrategyContext

`StrategyContext` gives read-only access to:

- `now`: runner clock, event-time in replay or controlled clock in paper/live.
- `strategy_id`
- `sleeve_id`
- `position(instrument_id, side)`
- `positions()`
- `cash(currency)`
- `exposure()`
- `open_orders()`
- `feature(name)`
- `feature_vector()`
- `predict(model_name, features)`

The context is the only sanctioned path for positions, cash, exposure, features,
and predictions.

### StrategySpec

`StrategySpec` defines:

- `strategy_id`
- `name`
- `version`
- `description`
- `subscription`
- `default_execution_priority`
- `parameters`
- `model`
- `feature_schema_id`
- `tags`

`parameters` are immutable for the runner process lifetime. Do not mutate them.
Use parameters for thresholds, horizons, model toggles, min edge, max size, or
feature flags.

### SleeveSpec

`SleeveSpec` defines:

- `sleeve_id`
- `strategy_id`
- `strategy_version`
- `venue`
- `capital_allocation`
- `currency`
- `risk`
- `tags`

The strategy should not inspect or override sleeve limits directly. The risk
gate and allocator enforce them.

### FeatureVector

`FeatureVector` is the model input contract:

- `schema_id`
- `schema_version`
- `instrument_id`
- `timestamp`
- ordered `values`

Feature value order must match `feature_schema.json`. Python and Rust parity
will eventually depend on this exact order.

### Prediction

`Prediction` is the model output contract:

- `model_name`
- `model_version`
- `instrument_id`
- `timestamp`
- `horizon_seconds`
- `value`
- `confidence`
- `extras`

Strategies may call `ctx.predict(model_name, features)` but must treat the
prediction as another point-in-time input. The prediction does not authorize an
order by itself.

## Market Detection

Market detection is metadata filtering, not execution.

The current detector:

```python
from eventcontracts.markets import InMemoryMarketCatalog, SubscriptionMarketDetector

catalog = InMemoryMarketCatalog(markets)
detector = SubscriptionMarketDetector(catalog)
candidates = detector.detect(strategy_spec.subscription)
```

The detector uses:

- `subscription.venues`
- `subscription.instrument_patterns`
- market status
- optional category filters
- max market count

Each `MarketCandidate` includes:

- selected `Market`
- score
- reasons
- detection timestamp
- `AuditStamp`

Researcher constraints:

- Market detection may decide which instruments enter the research universe.
- It may not submit orders.
- It may not override risk limits.
- It must be reproducible from market metadata and subscription config.
- If a strategy needs a market family, encode that in `instrument_patterns` and
  tags, not ad hoc string filters inside trading logic.

## Dynamic Allocation

Dynamic allocation is sleeve-level capital routing. Strategy logic does not own
capital.

Current implementation:

- `InMemorySleeveRegistry`: stores active immutable `SleeveSpec` values.
- `EqualWeightAllocator`: splits configured total capital equally across active
  sleeves, capped by each sleeve's `capital_allocation`.

Researcher constraints:

- A strategy may be run in one or more sleeves.
- A strategy may not call `Allocator.apply`.
- A strategy may not self-increase capital.
- A strategy may expose metrics that future allocator policies can consume, but
  allocator policy must be separate and audited.
- Allocation decisions must be reviewed through `AllocationDecision` records.

Correct mental model:

```text
Strategy proposes trades.
Risk decides whether each intent is allowed.
Allocator decides sleeve capital budgets.
Executor decides simulated or live order effects.
Ledger decides resulting cash/position truth.
```

The researcher may propose allocation features such as rolling drawdown,
Sharpe, hit rate, decay, or capacity, but those features must enter a separate
allocator policy after they are audit-tested. They do not belong inside
strategy order sizing except through approved parameters and context state.

## Executor And Gateway Plug-In Boundary

Strategies emit decisions. The runner wraps decisions in `IntentEnvelope`.

```text
StrategyDecision
  -> IntentEnvelope
  -> RiskGate
  -> PaperIntentSink / DryRunGateway / future live gateway
```

Current downstream implementations:

- Paper execution through `MarketPaperSimulator`.
- Dry-run gateway through `DryRunVenueGateway`.
- Priority routing through `InMemoryPriorityScheduler`.
- Idempotency through `InMemoryIdempotencyStore`.

Execution constraints:

- Strategy code must never instantiate a live gateway.
- Strategy code must never call venue APIs.
- Strategy code must never assume an order filled because it emitted
  `PlaceOrder`.
- Fills arrive later as `OwnFillEvent`.
- Rejections arrive later as `OwnOrderRejectEvent`.
- Open order state is read from context, not strategy-owned mutable globals.

## Decision Types

Allowed outputs:

| Decision | Meaning | Notes |
| --- | --- | --- |
| `PlaceOrder` | Request a new order. | Must include side, instrument, size, order type, TIF, and limit price unless market order. |
| `CancelOrder` | Request a cancel by client order ID. | Protective cancels should use `CRITICAL` priority. |
| `ReplaceOrder` | Request price and/or size replacement. | Must include new price or new quantity. |
| `Alert` | Emit an operational or research alert. | Not an order. |
| `NoAction` | Explicitly record no trade. | Use this to explain ignored events and parity behavior. |

Decision requirements:

- Every order must have a stable `client_order_id`.
- Every order must include a human-readable `reason`.
- Expected edge should be recorded when known.
- Decision metadata may include diagnostic values, but not live credentials,
  raw secrets, or unbounded payloads.
- `priority` is a scheduling hint only. It never bypasses risk.

## Execution Priority

Use `ExecutionPriority` when latency affects expected value:

| Tier | Use |
| --- | --- |
| `RELAXED` | Slow rebalance, slow model signal, non-urgent paper decision. |
| `STANDARD` | Default strategy flow. |
| `FAST` | Short half-life alpha, external-data repricing, book dislocation. |
| `CRITICAL` | Protective cancel, kill-switch, risk-reducing action. |

Optional fields:

- `max_delay_ms`: desired decision-to-send budget.
- `expires_after_ms`: stale order deadline.
- `allow_rate_limit_borrow`: future gateway hint.
- `reason`: why this tier was selected.

Constraint: do not mark everything `FAST` or `CRITICAL`. Overusing priority
destroys scheduler meaning and will be treated as a strategy defect.

## Feature Engineering Contract

A feature is valid only if it is point-in-time correct.

Feature record:

```text
FeatureVector(
  schema_id,
  schema_version,
  instrument_id,
  timestamp,
  values=((name, value), ...)
)
```

Feature schema defines:

- feature names
- dtype
- nullability
- default
- description
- optional target name
- optional target horizon

Allowed feature inputs:

- Current and prior `QuoteEvent`.
- Current and prior `TradeEvent`.
- Current and prior `OrderBookEvent`.
- Current and prior `LifecycleEvent`.
- Point-in-time `ExternalSignalEvent`.
- Current context positions, cash, open orders, exposure when building
  strategy-state features.
- Timer events known at the feature timestamp.

Disallowed feature inputs:

- Future quotes/trades/books.
- Final settlement before it was known.
- Revised external data as if it were original.
- Data corrected after the feature timestamp unless the correction timestamp is
  modeled explicitly.
- The eventual label.
- Current paper fill if it would not yet be known live.
- Any field that only exists because a backtest completed.

Feature values must be deterministic:

- Same input events, same schema, same code version -> same vector.
- Missing values must follow schema defaults.
- Floating point transforms should be documented and tolerance-tested.
- Use `Decimal` for price/probability calculations until explicitly converting
  to model float features.

## Label Construction

Labels are training targets. Labels are not live inputs. A model may train on a
label after the outcome is known, but a live strategy must never read that label
directly.

Each label must specify:

- `label_name`
- prediction horizon
- event-time cutoff
- feature timestamp
- outcome window
- market or instrument universe
- execution assumptions
- fee assumptions
- slippage assumptions
- queue assumptions
- censoring rules
- missing-data rules
- audit parent event IDs or partition checksums

### Label Time Contract

For each feature vector at time `t0`:

```text
feature window:      (-inf, t0]
decision timestamp:  t0
latency floor:       t0 + modeled_latency
label window:        [t0 + latency_floor, t0 + horizon]
```

The model may use only information available at or before `t0`.

The label may look forward into the label window because it is the target, but
the label value itself must never leak back into features.

### Recommended Label Types

| Label | Definition | Use |
| --- | --- | --- |
| `next_mid_change_bps` | Future mid minus current mid, in bps of current mid. | Short-horizon price prediction. |
| `next_markout_bps` | Markout after modeled execution price, net of fees if desired. | Execution-aware alpha. |
| `resolution_value` | Final payout minus current executable price. | Long-horizon event outcome. |
| `binary_profitable_after_fees` | 1 if modeled trade is profitable after fees/slippage. | Classifier target. |
| `fill_probability` | Whether a passive order at a level would fill in the horizon. | Queue/passive execution model. |
| `adverse_selection_bps` | Markout after passive fill. | Maker quality filter. |
| `cancel_urgency` | Whether resting order should have been canceled before adverse move. | Protective strategy model. |
| `spread_capture_bps` | Realized maker spread capture after fees. | Market-making research. |
| `settlement_probability` | Estimated probability of YES payout at settlement. | Fundamental event model. |

### Label Examples

Short-horizon mid label:

```text
t0_mid = midpoint at t0
t1_mid = first midpoint at or before t0 + horizon, after latency floor
label = 10_000 * (t1_mid - t0_mid) / t0_mid
```

Execution-aware buy label:

```text
entry_price = best ask at t0 + latency_assumption
future_mid = midpoint at t0 + horizon
fees = venue_fee_model(entry_price, size, taker)
label = 10_000 * (future_mid - entry_price - fees_per_contract) / entry_price
```

Settlement label:

```text
entry_price = executable price at t0 + latency_assumption
payout = 1.0 if YES resolves true else 0.0
fees = venue_fee_model(entry_price, size, liquidity)
label = payout - entry_price - fees_per_contract
```

Passive fill label:

```text
queue_ahead = estimated queue ahead at t0
traded_through_level = future sell/buy volume at price level over horizon
label = 1 if traded_through_level > queue_ahead else 0
```

### Censoring Rules

Drop or mark censored examples when:

- The market closes before the horizon completes.
- The market pauses before an executable decision could occur.
- Required quote/book data is missing.
- The external data feed was stale at `t0`.
- The label requires settlement but settlement is disputed or absent.
- A venue rule would make the theoretical order ineligible.

Never silently fill censored labels with zero. Zero is a valid economic outcome;
censored means unknown or invalid.

### Label Audit Requirements

Every training dataset must record:

- raw partition identifiers
- normalized event partition identifiers
- feature schema checksum
- label code version
- execution assumptions version
- fee model version
- input time range
- excluded/censored counts
- class balance or label distribution
- random seed if sampling was used

Use `AuditStamp` for dataset and artifact records. The audit chain should be:

```text
raw envelopes
  -> normalized events
  -> feature vectors
  -> labels
  -> training dataset
  -> training run
  -> model artifact
  -> strategy/model bundle
  -> parity cases
```

## Model Training Contract

Python is the research environment. Rust is the parity/runtime environment.
Therefore:

- Train in Python.
- Export immutable artifacts.
- Generate parity cases.
- Validate Python/Rust equivalence before promotion.

The model-family-agnostic pipeline that implements this — ONNX export for
scikit-learn / XGBoost / LightGBM, HuggingFace support, calibrated evaluation,
export-parity verification, and the generic `model-train` CLI — is documented
in [docs/ml-model-pipeline.md](ml-model-pipeline.md). Use
`eventcontracts.models.onnx_export.export_model_onnx` /
`eventcontracts.models.evaluation.evaluate_classification` rather than
hand-rolling export or metric code per strategy.

Training dataset object:

```text
TrainingDataset(
  dataset_id,
  feature_vectors,
  labels,
  created_at,
  audit
)
```

Training run object:

```text
TrainingRun(
  run_id,
  model_name,
  started_at,
  ended_at,
  metrics,
  audit
)
```

Model artifact object:

```text
ModelArtifact(
  name,
  version,
  uri,
  sha256,
  format,
  created_at,
  audit
)
```

Training constraints:

- Use walk-forward validation by default.
- Do not use random train/test shuffles across time.
- Pin random seeds.
- Persist feature schema and code version.
- Record every feature transformation.
- Record every label rule.
- Record censoring rules.
- Evaluate after fees, slippage, latency, queue, and risk rejects.
- Compare to simple baselines.
- Validate capacity and decay.
- Validate per-market-family behavior, not only aggregate metrics.

## Strategy Decision Policy

A model output is not a trade. A model output becomes a trade only after a
policy layer maps it into a typed decision.

The policy should specify:

- minimum expected edge
- minimum confidence
- maximum spread
- maximum staleness
- allowed lifecycle states
- allowed market categories
- order side
- outcome side
- order type
- time in force
- size rule
- cancel/replace rule
- priority rule

Example:

```text
if prediction.edge_bps > min_edge_bps
and prediction.confidence > min_confidence
and spread_bps < max_spread_bps
and feature_age_ms < max_feature_age_ms:
    emit PlaceOrder(...)
else:
    emit NoAction(reason="edge_or_quality_filter_failed")
```

The policy must be testable without a live venue.

## Sizing Rules

Sizing must be deterministic and bounded.

Allowed sizing inputs:

- sleeve risk profile
- current position
- current cash
- current exposure
- model confidence
- expected edge
- current spread/liquidity
- open orders
- static parameters

Disallowed sizing inputs:

- direct capital allocator mutation
- assumptions about future allocation decisions
- hidden leverage
- unbounded Kelly sizing
- side effects outside context

Recommended size formula shape:

```text
raw_size = f(edge, confidence, liquidity)
risk_capped_size = min(raw_size, strategy_max_size, sleeve_max_order_notional / price)
position_capped_size = min(risk_capped_size, remaining_position_budget)
```

The risk gate is still authoritative. A strategy size proposal may be rejected.

## Research Metrics

Minimum model metrics:

- train/validation/test time ranges
- sample count
- censored count
- label distribution
- hit rate
- calibration
- rank correlation
- average markout
- median markout
- tail loss
- turnover
- capacity
- feature staleness distribution

Minimum strategy metrics:

- decisions emitted
- orders requested
- risk rejects
- fills
- fill rate
- maker/taker split
- gross PnL
- net PnL after fees
- drawdown
- exposure
- average latency assumption
- queue assumption sensitivity
- per-market-family PnL
- per-day PnL
- worst replay windows

Minimum paper/dry-run metrics:

- command queue time
- stale drops
- idempotency duplicates
- gateway acks
- dry-run rejects
- simulated vs expected fill quality

## Backtest Requirements

Before review, a strategy must have:

- unit tests for event handling
- strategy registry load test
- deterministic replay test
- risk reject test
- paper execution test
- label construction test
- feature schema validation test
- artifact/bundle validation test once using models
- parity fixture plan

Backtest constraints:

- Use the same `StrategyRunner` path as paper/dry-run.
- Do not special-case strategy behavior for backtest.
- Do not inspect test labels inside strategy code.
- Include realistic fees.
- Include latency assumptions.
- Include queue/slippage assumptions where relevant.
- Report rejected intents, not only filled trades.

## Artifact Bundle Requirements

Every strategy/model bundle should eventually contain:

```text
manifest.toml
strategy_spec.toml
sleeve_spec.toml
feature_schema.json
parity_cases.parquet or equivalent fixture
model.onnx or rules artifact when applicable
README.md
```

Bundle must answer:

- What code produced it?
- What data trained it?
- What labels trained it?
- What features does it require?
- What model artifact does it load?
- What strategy parameters does it use?
- What parity cases prove behavior?
- What checksum proves files did not change?

No mutable bundle edits after promotion.

## Parity Requirements

Parity cases are required before Rust runtime or live promotion.

Each case should include:

- case ID
- input event or event window
- context snapshot
- feature vector
- prediction
- expected decisions
- expected risk result
- expected paper execution effect if applicable
- tolerance

Python and Rust must agree on:

- schema parsing
- feature order
- feature values
- prediction outputs
- decision kind
- instrument
- side
- size
- price
- priority
- risk reasons

If parity drifts, the artifact is not promotable.

## Constraints Between Strategy, Allocator, And Executor

The researcher owns strategy/model logic. The framework owns allocation and
execution enforcement.

Allowed interaction:

```text
StrategySpec.subscription
  -> MarketDetector selects markets
  -> Runner feeds events
  -> Strategy returns decisions
  -> RiskGate accepts/rejects
  -> Allocator sets sleeve capital outside strategy
  -> Executor simulates or dry-runs accepted intents
```

Forbidden interaction:

```text
Strategy calls Allocator.apply
Strategy calls VenueGateway.submit
Strategy mutates CashBalance
Strategy mutates Position
Strategy writes LedgerEntry
Strategy reads final label during decision
Strategy bypasses RiskGate
```

The strategy can suggest urgency, expected edge, and size. The allocator,
risk gate, and executor decide whether that request is feasible.

## Live Promotion Gates

A strategy is not eligible for live capital until all gates pass:

1. Feature schema validates.
2. Label code is reviewed for leakage.
3. Dataset audit chain is complete.
4. Walk-forward validation is positive after costs.
5. Replay is deterministic.
6. Paper execution is positive after fees, slippage, latency, queue, and
   lifecycle constraints.
7. Risk rejects are understood.
8. Artifact bundle validates.
9. Parity cases pass.
10. Dry-run gateway produces expected command stream.
11. OMS and ledger paths are implemented and reconciled.
12. Reconciliation catches order, fill, cash, and position drift.
13. Kill switch and cancel-all are tested.
14. Human review approves the exact sleeve and capital limit.

Until these gates pass, strategies may run only in research, replay, paper, or
dry-run mode.

## Required Strategy Review Packet

For each strategy, provide:

- strategy name and version
- researcher owner
- hypothesis
- market universe
- data sources
- feature schema
- label definition
- model type
- validation periods
- metrics table
- known failure modes
- risk assumptions
- execution assumptions
- allocation assumptions
- expected capacity
- expected latency sensitivity
- parity case location
- artifact bundle location
- paper/dry-run results

## Naming Conventions

Strategy module:

```text
python/src/eventcontracts/plugins/strategies/<family>_<variant>.py
```

Strategy registration name:

```text
<family>_<variant>
```

Strategy ID:

```text
<family>-<variant>-v<major>
```

Feature schema ID:

```text
<family>_<variant>_features
```

Model name:

```text
<family>_<variant>
```

Sleeve ID:

```text
<family>-<venue>-<mode>-<letter>
```

Examples:

```text
weather_threshold
weather-threshold-v1
weather_threshold_features
weather-threshold-kalshi-paper-a
```

## Researcher Checklist

Before opening a PR:

- [ ] Strategy module exists under `plugins/strategies`.
- [ ] Factory is registered with `@register`.
- [ ] Strategy spec TOML loads.
- [ ] Sleeve spec TOML loads.
- [ ] Feature schema validates.
- [ ] Labels are point-in-time and documented.
- [ ] Censoring rules are documented.
- [ ] Unit tests cover event handling.
- [ ] Replay test is deterministic.
- [ ] Risk rejects are tested.
- [ ] Paper execution path is tested.
- [ ] No strategy code calls venue/storage/gateway/allocation mutators.
- [ ] No live credentials are referenced.
- [ ] No future data is used in features.
- [ ] Model artifact has checksum and audit record.
- [ ] Dry-run command stream is reviewed before live consideration.

## Current Framework Gaps The Researcher Must Respect

The following are still under implementation:

- real venue capture
- real live execution gateway
- OMS state persistence
- double-entry ledger
- live reconciliation
- production artifact writer/loader
- Rust parity runner
- production feature store
- production model registry
- compliance enforcement

Design strategies so they will fit these boundaries, but do not assume those
systems are production-ready today.
