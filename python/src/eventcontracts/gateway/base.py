"""Venue-facing live gateway scaffolds."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum

from eventcontracts.audit import AuditStamp, audit_stamp_for
from eventcontracts.domain.decisions import IntentEnvelope
from eventcontracts.domain.ids import ClientOrderId, CorrelationId, VenueOrderId
from eventcontracts.domain.latency import LatencyTier
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
    audit: AuditStamp
    intent: OrderIntent | None = None
    client_order_id: ClientOrderId | None = None
    envelope: IntentEnvelope | None = None

    def __post_init__(self) -> None:
        require_non_empty(str(self.correlation_id), "correlation_id")
        if self.kind is GatewayCommandKind.SUBMIT and self.intent is None:
            raise ValueError("submit command requires intent")
        if (
            self.kind in (GatewayCommandKind.CANCEL, GatewayCommandKind.REPLACE)
            and self.client_order_id is None
        ):
            raise ValueError(f"{self.kind.value} command requires client_order_id")


@dataclass(frozen=True)
class GatewayAck:
    command: GatewayCommand
    accepted: bool
    acknowledged_at: datetime
    audit: AuditStamp
    venue_order_id: VenueOrderId | None = None
    reject: OrderReject | None = None
    reasons: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        require_aware_datetime(self.acknowledged_at, "acknowledged_at")
        object.__setattr__(self, "reasons", tuple(self.reasons))


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


class InMemoryIdempotencyStore(IdempotencyStore):
    """Local idempotency store for paper and dry-run gateway tests."""

    def __init__(self) -> None:
        self._reserved: dict[str, CorrelationId] = {}
        self._acks: dict[str, GatewayAck] = {}

    def reserve(self, key: str, correlation_id: CorrelationId) -> bool:
        require_non_empty(key, "idempotency key")
        if key in self._reserved:
            return False
        self._reserved[key] = correlation_id
        return True

    def mark_complete(self, key: str, ack: GatewayAck) -> None:
        require_non_empty(key, "idempotency key")
        if key not in self._reserved:
            raise KeyError(f"idempotency key was not reserved: {key}")
        self._acks[key] = ack

    def lookup(self, key: str) -> GatewayAck | None:
        require_non_empty(key, "idempotency key")
        return self._acks.get(key)


class PriorityScheduler:
    """Order gateway commands by execution priority and expiry."""

    def enqueue(self, envelope: IntentEnvelope) -> None:
        raise NotImplementedError

    def next_batch(self, *, now: datetime, limit: int) -> Sequence[IntentEnvelope]:
        raise NotImplementedError

    def drop_stale(self, *, now: datetime) -> Sequence[IntentEnvelope]:
        raise NotImplementedError


class InMemoryPriorityScheduler(PriorityScheduler):
    """Deterministic priority scheduler for dry-run and paper gateway flow."""

    _TIER_RANK = {
        LatencyTier.CRITICAL: 0,
        LatencyTier.FAST: 1,
        LatencyTier.STANDARD: 2,
        LatencyTier.RELAXED: 3,
    }

    def __init__(self) -> None:
        self._queue: list[IntentEnvelope] = []

    def enqueue(self, envelope: IntentEnvelope) -> None:
        self._queue.append(envelope)

    def next_batch(self, *, now: datetime, limit: int) -> Sequence[IntentEnvelope]:
        require_aware_datetime(now, "now")
        if limit < 0:
            raise ValueError("limit must be non-negative")
        self.drop_stale(now=now)
        ordered = sorted(self._queue, key=self._sort_key)
        batch = tuple(ordered[:limit])
        selected = {id(envelope) for envelope in batch}
        self._queue = [envelope for envelope in self._queue if id(envelope) not in selected]
        return batch

    def drop_stale(self, *, now: datetime) -> Sequence[IntentEnvelope]:
        require_aware_datetime(now, "now")
        stale: list[IntentEnvelope] = []
        fresh: list[IntentEnvelope] = []
        for envelope in self._queue:
            expires_after_ms = envelope.priority.expires_after_ms
            if expires_after_ms is None:
                fresh.append(envelope)
                continue
            expires_at = envelope.emitted_at + timedelta(milliseconds=expires_after_ms)
            if now > expires_at:
                stale.append(envelope)
            else:
                fresh.append(envelope)
        self._queue = fresh
        return tuple(stale)

    def _sort_key(self, envelope: IntentEnvelope) -> tuple[int, datetime, str]:
        return (
            self._TIER_RANK[envelope.priority.tier],
            envelope.emitted_at,
            str(envelope.correlation_id),
        )


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


class DryRunVenueGateway(VenueGateway):
    """Venue gateway that records commands but never sends network orders."""

    def __init__(
        self,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.clock = clock
        self.commands: list[GatewayCommand] = []
        self.acks: list[GatewayAck] = []

    def submit(self, command: GatewayCommand) -> GatewayAck:
        return self._record(command, reasons=("dry_run_submit",))

    def cancel(self, command: GatewayCommand) -> GatewayAck:
        return self._record(command, reasons=("dry_run_cancel",))

    def replace(self, command: GatewayCommand) -> GatewayAck:
        return self._record(command, reasons=("dry_run_replace",))

    def cancel_all(self, venue: Venue, reason: str) -> Sequence[GatewayAck]:
        require_non_empty(reason, "reason")
        return ()

    def _record(self, command: GatewayCommand, *, reasons: tuple[str, ...]) -> GatewayAck:
        now = self.clock()
        require_aware_datetime(now, "gateway clock")
        self.commands.append(command)
        audit = audit_stamp_for(
            {
                "command_kind": command.kind.value,
                "venue": command.venue.value,
                "correlation_id": str(command.correlation_id),
                "accepted": True,
                "reasons": reasons,
            },
            object_id=f"gateway-ack:{command.correlation_id}:{now.isoformat()}",
            object_kind="gateway_ack",
            schema_version="gateway-v1",
            produced_at=now,
            producer="dry_run_gateway",
            parent_ids=(command.audit.object_id,),
        )
        ack = GatewayAck(
            command=command,
            accepted=True,
            acknowledged_at=now,
            audit=audit,
            reasons=reasons,
        )
        self.acks.append(ack)
        return ack


class GatewayReconciler:
    """Compare venue state against local OMS state."""

    def fetch_open_orders(self, venue: Venue) -> Sequence[Order]:
        raise NotImplementedError

    def reconcile(self, venue: Venue) -> Sequence[str]:
        raise NotImplementedError
