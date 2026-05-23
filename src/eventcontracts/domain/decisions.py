"""Strategy outputs.

A strategy never mutates anything directly — it returns ``StrategyDecision``
values from its callbacks. The runner wraps each decision in an
``IntentEnvelope`` that carries provenance (strategy id, sleeve id, correlation
id, causal event id) and pushes it through the risk gate and onto the bus.

Keeping decisions as a closed sum type means every downstream component
(risk, OMS, gateway, recorder) handles the same finite set of shapes.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, TypeAlias

from eventcontracts.domain.ids import (
    ClientOrderId,
    CorrelationId,
    EventId,
    SleeveId,
    StrategyId,
)
from eventcontracts.domain.latency import DEFAULT_EXECUTION_PRIORITY, ExecutionPriority
from eventcontracts.domain.metadata import FrozenMap, freeze_mapping
from eventcontracts.domain.models import InstrumentId, OutcomeSide
from eventcontracts.domain.orders import OrderSide, OrderType, TimeInForce
from eventcontracts.domain.validation import (
    require_non_empty,
    require_optional_aware_datetime,
    require_positive_decimal,
    require_probability_decimal,
)


class AlertSeverity(str, Enum):
    INFO = "info"
    WARN = "warn"
    ERROR = "error"


@dataclass(frozen=True)
class PlaceOrder:
    client_order_id: ClientOrderId
    instrument_id: InstrumentId
    outcome_side: OutcomeSide
    order_side: OrderSide
    order_type: OrderType
    time_in_force: TimeInForce
    quantity: Decimal
    price: Decimal | None = None
    expires_at: datetime | None = None
    reason: str = ""
    expected_edge_bps: Decimal | None = None
    priority: ExecutionPriority | None = None
    metadata: Mapping[str, Any] = field(default_factory=FrozenMap)

    def __post_init__(self) -> None:
        require_non_empty(str(self.client_order_id), "client_order_id")
        require_positive_decimal(self.quantity, "quantity")
        require_optional_aware_datetime(self.expires_at, "expires_at")
        if self.price is None and self.order_type is not OrderType.MARKET:
            raise ValueError("price is required for non-market orders")
        if self.price is not None:
            require_probability_decimal(self.price, "price")
        object.__setattr__(self, "metadata", freeze_mapping(self.metadata))


@dataclass(frozen=True)
class CancelOrder:
    client_order_id: ClientOrderId
    reason: str = ""
    priority: ExecutionPriority | None = None

    def __post_init__(self) -> None:
        require_non_empty(str(self.client_order_id), "client_order_id")


@dataclass(frozen=True)
class ReplaceOrder:
    client_order_id: ClientOrderId
    new_price: Decimal | None = None
    new_quantity: Decimal | None = None
    reason: str = ""
    priority: ExecutionPriority | None = None

    def __post_init__(self) -> None:
        require_non_empty(str(self.client_order_id), "client_order_id")
        if self.new_price is None and self.new_quantity is None:
            raise ValueError("replace order requires new_price or new_quantity")
        if self.new_price is not None:
            require_probability_decimal(self.new_price, "new_price")
        if self.new_quantity is not None:
            require_positive_decimal(self.new_quantity, "new_quantity")


@dataclass(frozen=True)
class Alert:
    severity: AlertSeverity
    message: str
    tags: Mapping[str, str] = field(default_factory=FrozenMap)

    def __post_init__(self) -> None:
        require_non_empty(self.message, "message")
        object.__setattr__(self, "tags", freeze_mapping(self.tags))


@dataclass(frozen=True)
class NoAction:
    reason: str = ""


StrategyDecision: TypeAlias = PlaceOrder | CancelOrder | ReplaceOrder | Alert | NoAction


@dataclass(frozen=True)
class IntentEnvelope:
    """A strategy decision wrapped with provenance for risk + gateway + the bus."""

    decision: StrategyDecision
    strategy_id: StrategyId
    sleeve_id: SleeveId
    correlation_id: CorrelationId
    emitted_at: datetime
    priority: ExecutionPriority = DEFAULT_EXECUTION_PRIORITY
    triggered_by_event_id: EventId | None = None
    metadata: Mapping[str, str] = field(default_factory=FrozenMap)

    def __post_init__(self) -> None:
        require_non_empty(str(self.strategy_id), "strategy_id")
        require_non_empty(str(self.sleeve_id), "sleeve_id")
        require_non_empty(str(self.correlation_id), "correlation_id")
        require_optional_aware_datetime(self.emitted_at, "emitted_at")
        object.__setattr__(self, "metadata", freeze_mapping(self.metadata))


def decision_kind(decision: StrategyDecision) -> str:
    """Stable tag for a decision variant."""
    match decision:
        case PlaceOrder():
            return "place_order"
        case CancelOrder():
            return "cancel_order"
        case ReplaceOrder():
            return "replace_order"
        case Alert():
            return "alert"
        case NoAction():
            return "no_action"


def decision_priority(
    decision: StrategyDecision,
    default: ExecutionPriority = DEFAULT_EXECUTION_PRIORITY,
) -> ExecutionPriority:
    """Return the scheduling priority for a strategy decision."""
    match decision:
        case PlaceOrder(priority=priority) | CancelOrder(priority=priority) | ReplaceOrder(
            priority=priority
        ):
            return priority or default
        case _:
            return default
