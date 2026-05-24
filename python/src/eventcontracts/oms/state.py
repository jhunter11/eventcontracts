"""OMS state-machine scaffolds."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from eventcontracts.audit import AuditStamp
from eventcontracts.domain.decisions import IntentEnvelope
from eventcontracts.domain.fills import Fill
from eventcontracts.domain.ids import ClientOrderId, CorrelationId
from eventcontracts.domain.orders import Order, OrderReject, OrderStatus
from eventcontracts.domain.validation import require_aware_datetime, require_non_empty
from eventcontracts.execution.simulator import OrderIntent


class OrderEventKind(str, Enum):
    CREATED = "created"
    ACKED = "acked"
    REJECTED = "rejected"
    PARTIAL_FILL = "partial_fill"
    FILLED = "filled"
    CANCELED = "canceled"
    EXPIRED = "expired"
    RECONCILED = "reconciled"


@dataclass(frozen=True)
class OrderTicket:
    envelope: IntentEnvelope
    intent: OrderIntent
    created_at: datetime
    audit: AuditStamp

    def __post_init__(self) -> None:
        require_aware_datetime(self.created_at, "created_at")


@dataclass(frozen=True)
class OrderTransition:
    client_order_id: ClientOrderId
    correlation_id: CorrelationId
    kind: OrderEventKind
    from_status: OrderStatus | None
    to_status: OrderStatus
    occurred_at: datetime
    audit: AuditStamp
    reason: str = ""

    def __post_init__(self) -> None:
        require_non_empty(str(self.client_order_id), "client_order_id")
        require_non_empty(str(self.correlation_id), "correlation_id")
        require_aware_datetime(self.occurred_at, "occurred_at")


@dataclass(frozen=True)
class ReconciliationReport:
    generated_at: datetime
    audit: AuditStamp
    missing_local: tuple[Order, ...] = field(default_factory=tuple)
    missing_venue: tuple[Order, ...] = field(default_factory=tuple)
    mismatched: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        require_aware_datetime(self.generated_at, "generated_at")


class OmsStateStore:
    """Durable order/fill state store."""

    def put_order(self, order: Order, audit: AuditStamp) -> None:
        raise NotImplementedError

    def get_order(self, client_order_id: ClientOrderId) -> Order | None:
        raise NotImplementedError

    def put_fill(self, fill: Fill, audit: AuditStamp) -> None:
        raise NotImplementedError

    def open_orders(self) -> Sequence[Order]:
        raise NotImplementedError

    def append_transition(self, transition: OrderTransition) -> None:
        raise NotImplementedError


class OrderStateMachine:
    """State transition policy for gateway and paper order updates."""

    def create(self, ticket: OrderTicket) -> Order:
        raise NotImplementedError

    def ack(self, order: Order, *, occurred_at: datetime) -> OrderTransition:
        raise NotImplementedError

    def reject(self, order: Order, reject: OrderReject) -> OrderTransition:
        raise NotImplementedError

    def fill(self, order: Order, fill: Fill) -> OrderTransition:
        raise NotImplementedError

    def cancel(self, order: Order, *, occurred_at: datetime, reason: str) -> OrderTransition:
        raise NotImplementedError

    def reconcile(self, local: Sequence[Order], venue: Sequence[Order]) -> ReconciliationReport:
        raise NotImplementedError
