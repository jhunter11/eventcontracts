"""Venue-facing live gateway scaffolds."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import Enum

from eventcontracts.audit import AuditStamp, audit_stamp_for
from eventcontracts.domain.decisions import IntentEnvelope, PlaceOrder
from eventcontracts.domain.ids import ClientOrderId, CorrelationId, VenueOrderId
from eventcontracts.domain.latency import LatencyTier
from eventcontracts.domain.models import InstrumentId, MarketSnapshot, OutcomeSide, Venue
from eventcontracts.domain.orders import Order, OrderReject, OrderSide, OrderType
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
        if self.kind in (GatewayCommandKind.CANCEL, GatewayCommandKind.REPLACE) and self.client_order_id is None:
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


@dataclass(frozen=True)
class GatewayLastLookPolicy:
    """Send-time checks against the latest executable BBO."""

    max_quote_age_ms: int = 1000
    max_price_movement: Decimal = Decimal("0.02")
    max_spread: Decimal = Decimal("0.05")
    max_slippage: Decimal = Decimal("0.00")

    def __post_init__(self) -> None:
        if self.max_quote_age_ms <= 0:
            raise ValueError("max_quote_age_ms must be positive")
        if self.max_price_movement < 0:
            raise ValueError("max_price_movement must be non-negative")
        if self.max_spread <= 0:
            raise ValueError("max_spread must be positive")
        if self.max_slippage < 0:
            raise ValueError("max_slippage must be non-negative")


@dataclass(frozen=True)
class GatewayLastLook:
    """Lookup + policy object used immediately before venue submission."""

    snapshot_for: Callable[[InstrumentId, OutcomeSide], MarketSnapshot | None]
    policy: GatewayLastLookPolicy = field(default_factory=GatewayLastLookPolicy)

    def evaluate(self, command: GatewayCommand, *, now: datetime) -> tuple[str, ...]:
        require_aware_datetime(now, "now")
        if command.kind is not GatewayCommandKind.SUBMIT:
            return ()
        if command.envelope is None:
            return ("missing_intent_envelope",)
        decision = command.envelope.decision
        if not isinstance(decision, PlaceOrder):
            return ()
        latest = self.snapshot_for(decision.instrument_id, decision.outcome_side)
        return validate_gateway_last_look(
            decision,
            latest,
            now=now,
            policy=self.policy,
        )


def validate_gateway_last_look(
    order: PlaceOrder,
    latest: MarketSnapshot | None,
    *,
    now: datetime,
    policy: GatewayLastLookPolicy,
) -> tuple[str, ...]:
    """Reject an order if the current BBO no longer matches its decision BBO."""

    require_aware_datetime(now, "now")
    decision_snapshot = order.market_snapshot
    if decision_snapshot is None:
        return ("last_look_missing_decision_snapshot",)
    if latest is None:
        return ("last_look_missing_latest_snapshot",)

    reasons: list[str] = []
    if latest.instrument_id != order.instrument_id:
        reasons.append("last_look_instrument_mismatch")
    if latest.side is not order.outcome_side:
        reasons.append("last_look_side_mismatch")
    if latest.exchange_ts is None:
        reasons.append("last_look_missing_exchange_ts")
    if latest.source_sequence is None:
        reasons.append("last_look_missing_sequence")
    if latest.sequence_gap:
        reasons.append("last_look_sequence_gap")
    if latest.bid is None or latest.ask is None:
        reasons.append("last_look_missing_bbo")
    else:
        spread = latest.ask.price - latest.bid.price
        if spread < 0:
            reasons.append("last_look_crossed_bbo")
        elif spread > policy.max_spread:
            reasons.append("last_look_spread_too_wide")

    age_ms = latest.age_ms(now)
    if age_ms < 0:
        reasons.append("last_look_snapshot_from_future")
    elif age_ms > policy.max_quote_age_ms:
        reasons.append("last_look_stale_snapshot")

    movement = _snapshot_movement(decision_snapshot, latest)
    if movement is None:
        reasons.append("last_look_missing_decision_bbo")
    elif movement > policy.max_price_movement:
        reasons.append("last_look_price_moved")

    reasons.extend(_price_collar_reasons(order, latest, policy))
    return tuple(reasons)


def _snapshot_movement(
    decision_snapshot: MarketSnapshot,
    latest: MarketSnapshot,
) -> Decimal | None:
    if decision_snapshot.bid is None or decision_snapshot.ask is None or latest.bid is None or latest.ask is None:
        return None
    return max(
        abs(latest.bid.price - decision_snapshot.bid.price),
        abs(latest.ask.price - decision_snapshot.ask.price),
    )


def _price_collar_reasons(
    order: PlaceOrder,
    latest: MarketSnapshot,
    policy: GatewayLastLookPolicy,
) -> tuple[str, ...]:
    if order.order_type is OrderType.MARKET:
        return ("last_look_unpriced_market_order",)
    if order.price is None:
        return ("last_look_missing_limit_price",)
    if latest.bid is None or latest.ask is None:
        return ()
    if order.order_side is OrderSide.BUY and order.price > latest.ask.price + policy.max_slippage:
        return ("last_look_buy_price_above_collar",)
    if order.order_side is OrderSide.SELL and order.price < latest.bid.price - policy.max_slippage:
        return ("last_look_sell_price_below_collar",)
    return ()


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

    def __init__(
        self,
        *,
        max_entries: int = 10000,
        ttl: timedelta = timedelta(hours=1),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.max_entries = max_entries
        self.ttl = ttl
        self.clock = clock
        self._reserved: OrderedDict[str, tuple[CorrelationId, datetime]] = OrderedDict()
        self._acks: OrderedDict[str, tuple[GatewayAck, datetime]] = OrderedDict()

    def reserve(self, key: str, correlation_id: CorrelationId) -> bool:
        require_non_empty(key, "idempotency key")
        self._evict()
        if key in self._reserved:
            return False
        self._reserved[key] = (correlation_id, self.clock())
        self._reserved.move_to_end(key)
        self._evict()
        return True

    def mark_complete(self, key: str, ack: GatewayAck) -> None:
        require_non_empty(key, "idempotency key")
        self._evict()
        if key not in self._reserved:
            raise KeyError(f"idempotency key was not reserved: {key}")
        self._acks[key] = (ack, self.clock())
        self._acks.move_to_end(key)
        self._evict()

    def lookup(self, key: str) -> GatewayAck | None:
        require_non_empty(key, "idempotency key")
        self._evict()
        found = self._acks.get(key)
        if found is None:
            return None
        self._acks.move_to_end(key)
        return found[0]

    def cache_size(self) -> int:
        self._evict()
        return len(self._reserved) + len(self._acks)

    def _evict(self) -> None:
        now = self.clock()
        cutoff = now - self.ttl
        for mapping in (self._reserved, self._acks):
            for key, (_, updated_at) in list(mapping.items()):
                if updated_at < cutoff:
                    mapping.pop(key, None)
            while len(mapping) > self.max_entries:
                mapping.popitem(last=False)


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
        last_look: GatewayLastLook | None = None,
    ) -> None:
        self.clock = clock
        self.last_look = last_look
        self.commands: list[GatewayCommand] = []
        self.acks: list[GatewayAck] = []

    def submit(self, command: GatewayCommand) -> GatewayAck:
        now = self.clock()
        require_aware_datetime(now, "gateway clock")
        if self.last_look is not None:
            reject_reasons = self.last_look.evaluate(command, now=now)
            if reject_reasons:
                return self._record(
                    command,
                    reasons=reject_reasons,
                    accepted=False,
                    now=now,
                )
        return self._record(command, reasons=("dry_run_submit",), now=now)

    def cancel(self, command: GatewayCommand) -> GatewayAck:
        return self._record(command, reasons=("dry_run_cancel",))

    def replace(self, command: GatewayCommand) -> GatewayAck:
        return self._record(command, reasons=("dry_run_replace",))

    def cancel_all(self, venue: Venue, reason: str) -> Sequence[GatewayAck]:
        require_non_empty(reason, "reason")
        return ()

    def _record(
        self,
        command: GatewayCommand,
        *,
        reasons: tuple[str, ...],
        accepted: bool = True,
        now: datetime | None = None,
    ) -> GatewayAck:
        now = now or self.clock()
        require_aware_datetime(now, "gateway clock")
        self.commands.append(command)
        audit = audit_stamp_for(
            {
                "command_kind": command.kind.value,
                "venue": command.venue.value,
                "correlation_id": str(command.correlation_id),
                "accepted": accepted,
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
            accepted=accepted,
            acknowledged_at=now,
            audit=audit,
            reject=(
                OrderReject(
                    client_order_id=command.client_order_id,
                    reason=",".join(reasons),
                    rejected_at=now,
                    venue_code="last_look",
                )
                if not accepted and command.client_order_id is not None
                else None
            ),
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
