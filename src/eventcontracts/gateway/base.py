"""Venue-facing live gateway scaffolds.

The gateway owns credentials, rate limits, idempotency, and final venue-state
checks. Strategy code and runners should only pass typed intent envelopes into
this boundary.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from eventcontracts.domain.decisions import IntentEnvelope
from eventcontracts.domain.ids import ClientOrderId, CorrelationId, VenueOrderId
from eventcontracts.domain.models import Venue
from eventcontracts.domain.orders import Order, OrderReject
from eventcontracts.domain.validation import require_aware_datetime, require_non_empty
from eventcontracts.execution.simulator import OrderIntent


class GatewayCommandKind(str, Enum):
    SUBMIT = "submit"
    CANCEL = "cancel"
    REPLACE = "replace"
    CANCEL_ALL = "cancel_all"
    HALT = "halt"


@dataclass(frozen=True)
class GatewayCommand:
    kind: GatewayCommandKind
    venue: Venue
    correlation_id: CorrelationId
    intent: OrderIntent | None = None
    client_order_id: ClientOrderId | None = None
    envelope: IntentEnvelope | None = None

    def __post_init__(self) -> None:
        require_non_empty(str(self.correlation_id), "correlation_id")


@dataclass(frozen=True)
class GatewayAck:
    command: GatewayCommand
    accepted: bool
    acknowledged_at: datetime
    venue_order_id: VenueOrderId | None = None
    reject: OrderReject | None = None
    reasons: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        require_aware_datetime(self.acknowledged_at, "acknowledged_at")


@dataclass(frozen=True)
class RateLimitBudget:
    venue: Venue
    priority_tier: str
    requests_remaining: int
    reset_at: datetime

    def __post_init__(self) -> None:
        require_non_empty(self.priority_tier, "priority_tier")
        require_aware_datetime(self.reset_at, "reset_at")


class CredentialProvider:
    """Resolve venue credentials without exposing raw secrets to strategies."""

    def credential_ref(self, venue: Venue) -> str:
        raise NotImplementedError

    def load_signing_material(self, credential_ref: str) -> bytes:
        raise NotImplementedError


class IdempotencyStore:
    """Track command keys that have already been sent to a venue."""

    def reserve(self, key: str, correlation_id: CorrelationId) -> bool:
        raise NotImplementedError

    def mark_complete(self, key: str, ack: GatewayAck) -> None:
        raise NotImplementedError

    def lookup(self, key: str) -> GatewayAck | None:
        raise NotImplementedError


class PriorityScheduler:
    """Order gateway commands by execution priority and expiry."""

    def enqueue(self, envelope: IntentEnvelope) -> None:
        raise NotImplementedError

    def next_batch(self, *, now: datetime, limit: int) -> Sequence[IntentEnvelope]:
        raise NotImplementedError

    def drop_stale(self, *, now: datetime) -> Sequence[IntentEnvelope]:
        raise NotImplementedError


class VenueGateway:
    """Venue-facing command executor."""

    def submit(self, command: GatewayCommand) -> GatewayAck:
        raise NotImplementedError

    def cancel(self, command: GatewayCommand) -> GatewayAck:
        raise NotImplementedError

    def replace(self, command: GatewayCommand) -> GatewayAck:
        raise NotImplementedError

    def cancel_all(self, venue: Venue, reason: str) -> Sequence[GatewayAck]:
        raise NotImplementedError


class GatewayReconciler:
    """Compare venue state against local OMS state."""

    def fetch_open_orders(self, venue: Venue) -> Sequence[Order]:
        raise NotImplementedError

    def reconcile(self, venue: Venue) -> Sequence[str]:
        raise NotImplementedError
