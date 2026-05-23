# Strategy Runner Contract

This document defines the plug-in surface for strategies. A researcher should
be able to add a strategy by implementing this contract and adding a spec; the
runner and downstream infrastructure should not need custom code per strategy.

## Core Rule

A strategy is a pure-ish event handler:

```text
(NormalizedEvent, StrategyContext) -> Sequence[StrategyDecision]
```

The strategy receives normalized events and a read-only context. It returns
typed decisions. It does not directly call venue clients, write storage, publish
bus messages, mutate positions, or place orders.

## Input: Normalized Events

`NormalizedEvent` is a closed sum type in `eventcontracts.domain.events`.

| Variant | Purpose |
| --- | --- |
| `QuoteEvent` | Best bid/ask style quote update. |
| `TradeEvent` | Venue trade print or normalized public trade. |
| `OrderBookEvent` | Full local book snapshot or reconstructed book state. |
| `LifecycleEvent` | Market listed/opened/paused/resumed/closed/determined/disputed/finalized. |
| `SettlementResolvedEvent` | Resolution and payout information. |
| `ExternalSignalEvent` | Point-in-time non-venue data such as weather, crypto, or macro signals. |
| `TimerEvent` | Synthetic timer event from replay or live scheduler. |
| `OwnFillEvent` | Fill belonging to this account/sleeve/strategy. |
| `OwnOrderUpdateEvent` | Update to an order owned by this strategy. |
| `OwnOrderRejectEvent` | Rejection for an order owned by this strategy. |

Use `event_kind(event)` for stable logging, metrics, filters, and topic names.

## State Access: StrategyContext

The strategy context is a protocol, not a concrete class. Production can back it
with feature stores, position keepers, model servers, or in-process state. Tests
can use `InMemoryContext`.

Available reads:

- `now`: current clock time.
- `strategy_id`: current strategy id.
- `sleeve_id`: current sleeve id.
- `position(instrument_id, side)`: single position lookup.
- `positions()`: all known positions for the sleeve.
- `cash(currency)`: cash and availability by currency.
- `exposure()`: current exposure snapshot.
- `open_orders()`: open orders owned by the sleeve/strategy.
- `feature(name)`: one feature value.
- `feature_vector()`: full feature vector, if available.
- `predict(model_name, features)`: model prediction hook.

The context should be treated as read-only. Any state change must be expressed
as a returned `StrategyDecision`.

## Output: Strategy Decisions

`StrategyDecision` is a closed sum type in `eventcontracts.domain.decisions`.

| Variant | Purpose |
| --- | --- |
| `PlaceOrder` | Request a new order intent. |
| `CancelOrder` | Request cancellation by client order id. |
| `ReplaceOrder` | Request price and/or size replacement. |
| `Alert` | Emit a structured operational or risk alert. |
| `NoAction` | Explicitly record that the strategy chose not to act. |

Use `NoAction` when a strategy intentionally ignores an event and the decision
log should show why. This is useful in replay, debugging, and parity cases.

Order-affecting decisions can optionally include `priority:
ExecutionPriority`. If omitted, the runner applies
`StrategySpec.default_execution_priority`.

Latency tiers:

- `RELAXED`: acceptable to be late by roughly a second or more.
- `STANDARD`: normal default routing.
- `FAST`: latency-sensitive alpha such as crypto lead-lag or fast external data.
- `CRITICAL`: protective risk flow, cancels, and kill-switch actions.

The future live gateway should prioritize faster tiers when allocating gateway
workers, venue rate-limit budget, retries, and low-latency routes. Priority must
not bypass risk checks.

## IntentEnvelope

The runner wraps each returned decision in an `IntentEnvelope` before it leaves
the strategy boundary.

Envelope fields:

- `decision`: the original `StrategyDecision`.
- `strategy_id`: source strategy.
- `sleeve_id`: source sleeve.
- `correlation_id`: unique id for downstream tracing.
- `emitted_at`: runner clock timestamp.
- `priority`: computed execution priority for gateway scheduling.
- `triggered_by_event_id`: causal event id when present.
- `metadata`: decision kind and other runner metadata.

Downstream systems should consume `IntentEnvelope`, not raw decisions. This
keeps audit, risk, gateway, and replay tooling tied to the same provenance.

## Lifecycle

The runner owns strategy lifecycle transitions.

```text
created -> initializing -> ready -> running -> draining -> disposed
```

Halt transitions are allowed from initialization, warmup, ready, running, and
draining states where appropriate. Strategies observe lifecycle indirectly
through context and callbacks:

- `on_init(ctx)`
- `on_event(event, ctx)`
- `on_shutdown(ctx)`
- `snapshot()`
- `restore(state)`

`snapshot` and `restore` return and accept opaque bytes. The runner does not
inspect strategy state.

## Runner Ports

`StrategyRunner` depends only on protocols:

| Port | Responsibility |
| --- | --- |
| `EventSource` | Yield `NormalizedEvent` values. |
| `ContextProvider` | Build/provide the latest `StrategyContext`. |
| `RiskGate` | Accept or reject an `IntentEnvelope`. |
| `IntentSink` | Emit allowed envelopes to paper, bus, gateway, or recorder. |
| `StateStore` | Save and load strategy snapshots. |
| `Clock` | Provide current event or wall time. |

The in-memory implementations are for tests and local experiments:

- `InMemoryEventSource`
- `InMemoryIntentSink`
- `InMemoryStateStore`
- `InMemoryClock`
- `InMemoryContext`
- `StaticContextProvider`
- `AllowAllRiskGate`

## Minimal Strategy Example

```python
from collections.abc import Sequence
from decimal import Decimal

from eventcontracts.domain import (
    NoAction,
    PlaceOrder,
    StrategyDecision,
    TradeEvent,
    OutcomeSide,
)
from eventcontracts.domain.ids import ClientOrderId
from eventcontracts.domain.orders import OrderSide, OrderType, TimeInForce
from eventcontracts.strategy import StrategyBase, StrategyContext
from eventcontracts.strategy.registry import register


class MyStrategy(StrategyBase):
    def on_event(
        self, event, ctx: StrategyContext
    ) -> Sequence[StrategyDecision]:
        if not isinstance(event, TradeEvent):
            return (NoAction(reason="ignored event kind"),)

        if event.trade.price > Decimal("0.50"):
            return (NoAction(reason="price too high"),)

        return (
            PlaceOrder(
                client_order_id=ClientOrderId("client-id"),
                instrument_id=event.trade.instrument_id,
                outcome_side=OutcomeSide.YES,
                order_side=OrderSide.BUY,
                order_type=OrderType.LIMIT,
                time_in_force=TimeInForce.GTC,
                quantity=Decimal("1"),
                price=event.trade.price,
                priority=None,
                reason="threshold crossed",
            ),
        )


@register("my_strategy")
def factory(spec):
    return MyStrategy(spec)
```

The complete reference example is
`src/eventcontracts/strategies/example_threshold.py`.

## Test Wiring Pattern

A strategy smoke test should build the whole plug path:

1. `StrategySpec`
2. `SleeveSpec`
3. concrete strategy through `create("name", spec)`
4. `InMemoryEventSource`
5. `InMemoryContext`
6. `StaticContextProvider`
7. `AllowAllRiskGate` or a test rejection gate
8. `InMemoryIntentSink`
9. `StrategyRunner`

Then assert:

- events processed
- decisions emitted
- intents dispatched/rejected
- rejection reasons
- envelope provenance
- decision payloads
- final lifecycle state

See `tests/test_strategy_runner.py`.

## Compatibility Rules

- Adding a new `NormalizedEvent` variant is a cross-cutting change.
- Adding a new `StrategyDecision` variant is a cross-cutting change.
- Metadata may carry venue-specific extra fields, but core behavior should be
  promoted into first-class dataclasses before strategies depend on it.
- Strategy specs should be immutable for a runner process lifetime.
- Every strategy should be replayable with deterministic input events.
- Backtest, paper, and live should differ by ports, not strategy code.
