"""Capital allocation scaffolds."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from typing import cast

from eventcontracts.audit import AuditStamp, audit_stamp_for
from eventcontracts.domain.decisions import IntentEnvelope, PlaceOrder
from eventcontracts.domain.ids import ClientOrderId, SleeveId
from eventcontracts.domain.metadata import FrozenMap, freeze_mapping
from eventcontracts.domain.orders import OrderSide
from eventcontracts.domain.spec import SleeveSpec
from eventcontracts.domain.validation import (
    require_aware_datetime,
    require_currency,
    require_non_empty,
    require_non_negative_decimal,
    require_positive_decimal,
)


@dataclass(frozen=True)
class CapitalSnapshot:
    as_of: datetime
    total_capital: Decimal
    allocated_capital: Decimal
    currency: str
    audit: AuditStamp

    def __post_init__(self) -> None:
        require_aware_datetime(self.as_of, "as_of")
        require_non_negative_decimal(self.total_capital, "total_capital")
        require_non_negative_decimal(self.allocated_capital, "allocated_capital")
        require_currency(self.currency, "currency")


@dataclass(frozen=True)
class AllocationDecision:
    sleeve_id: SleeveId
    target_capital: Decimal
    reason: str
    audit: AuditStamp

    def __post_init__(self) -> None:
        require_non_empty(str(self.sleeve_id), "sleeve_id")
        require_non_negative_decimal(self.target_capital, "target_capital")
        require_non_empty(self.reason, "reason")


@dataclass(frozen=True)
class PortfolioReservation:
    client_order_id: ClientOrderId
    sleeve_id: SleeveId
    event_group_id: str
    amount: Decimal
    created_at: datetime
    audit: AuditStamp

    def __post_init__(self) -> None:
        require_non_empty(str(self.client_order_id), "client_order_id")
        require_non_empty(str(self.sleeve_id), "sleeve_id")
        require_non_empty(self.event_group_id, "event_group_id")
        require_positive_decimal(self.amount, "amount")
        require_aware_datetime(self.created_at, "created_at")


class IntentOutcome(str, Enum):
    ACCEPTED = "accepted"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"
    EXPIRED = "expired"


@dataclass
class PortfolioRiskAllocator:
    """Atomic capital reservations across sleeves and event groups."""

    total_capital: Decimal
    currency: str
    sleeve_budgets: Mapping[SleeveId, Decimal]
    group_budgets: Mapping[str, Decimal] = field(default_factory=dict)
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    reservations: dict[ClientOrderId, PortfolioReservation] = field(default_factory=dict)

    def __post_init__(self) -> None:
        require_positive_decimal(self.total_capital, "total_capital")
        require_currency(self.currency, "currency")
        for sleeve_id, budget in self.sleeve_budgets.items():
            require_non_empty(str(sleeve_id), "sleeve_id")
            require_non_negative_decimal(budget, "sleeve budget")
        for group_id, budget in self.group_budgets.items():
            require_non_empty(group_id, "event_group_id")
            require_non_negative_decimal(budget, "group budget")
        # `freeze_mapping` returns `FrozenMap`, which is a `Mapping` —
        # explicit cast keeps mypy happy on the declared field types.
        self.sleeve_budgets = cast(
            Mapping[SleeveId, Decimal], freeze_mapping(self.sleeve_budgets)
        )
        self.group_budgets = cast(Mapping[str, Decimal], freeze_mapping(self.group_budgets))

    def reserve(
        self,
        envelope: IntentEnvelope,
        *,
        event_group_id: str | None = None,
    ) -> tuple[PortfolioReservation | None, tuple[str, ...]]:
        decision = envelope.decision
        if not isinstance(decision, PlaceOrder) or decision.order_side is not OrderSide.BUY:
            return None, ()
        amount = (decision.price if decision.price is not None else Decimal("1")) * decision.quantity
        if amount <= 0:
            return None, ()

        client_order_id = decision.client_order_id
        if client_order_id in self.reservations:
            return self.reservations[client_order_id], ()

        group_id = self._event_group_id(envelope, decision, event_group_id)
        reasons: list[str] = []
        if self.reserved_total() + amount > self.total_capital:
            reasons.append("portfolio_budget")
        sleeve_budget = self.sleeve_budgets.get(envelope.sleeve_id)
        if sleeve_budget is None:
            reasons.append("sleeve_budget_missing")
        elif self.reserved_for_sleeve(envelope.sleeve_id) + amount > sleeve_budget:
            reasons.append("sleeve_budget")
        group_budget = self.group_budgets.get(group_id)
        if group_budget is not None and self.reserved_for_group(group_id) + amount > group_budget:
            reasons.append("event_group_budget")
        if reasons:
            return None, tuple(reasons)

        now = self._now()
        reservation = PortfolioReservation(
            client_order_id=client_order_id,
            sleeve_id=envelope.sleeve_id,
            event_group_id=group_id,
            amount=amount,
            created_at=now,
            audit=audit_stamp_for(
                {
                    "client_order_id": str(client_order_id),
                    "sleeve_id": str(envelope.sleeve_id),
                    "event_group_id": group_id,
                    "amount": str(amount),
                    "currency": self.currency,
                },
                object_id=f"portfolio-reservation:{client_order_id}",
                object_kind="portfolio_reservation",
                schema_version="allocation-v1",
                produced_at=now,
                producer="portfolio_risk_allocator",
                parent_ids=(str(envelope.correlation_id),),
            ),
        )
        self.reservations[client_order_id] = reservation
        return reservation, ()

    def release(self, client_order_id: ClientOrderId) -> PortfolioReservation | None:
        return self.reservations.pop(client_order_id, None)

    def on_intent_outcome(
        self,
        client_order_id: ClientOrderId,
        outcome: IntentOutcome | str,
    ) -> PortfolioReservation | None:
        outcome_value = outcome if isinstance(outcome, IntentOutcome) else IntentOutcome(str(outcome))
        if outcome_value in {
            IntentOutcome.FILLED,
            IntentOutcome.CANCELED,
            IntentOutcome.REJECTED,
            IntentOutcome.EXPIRED,
        }:
            return self.release(client_order_id)
        return None

    def reserved_total(self) -> Decimal:
        return sum((reservation.amount for reservation in self.reservations.values()), Decimal("0"))

    def reserved_for_sleeve(self, sleeve_id: SleeveId) -> Decimal:
        return sum(
            (reservation.amount for reservation in self.reservations.values() if reservation.sleeve_id == sleeve_id),
            Decimal("0"),
        )

    def reserved_for_group(self, event_group_id: str) -> Decimal:
        return sum(
            (
                reservation.amount
                for reservation in self.reservations.values()
                if reservation.event_group_id == event_group_id
            ),
            Decimal("0"),
        )

    def _event_group_id(
        self,
        envelope: IntentEnvelope,
        decision: PlaceOrder,
        explicit: str | None,
    ) -> str:
        raw = (
            explicit
            or decision.metadata.get("event_group_id")
            or envelope.metadata.get("event_group_id")
            or decision.instrument_id.market_id
        )
        return str(raw)

    def _now(self) -> datetime:
        now = self.clock()
        require_aware_datetime(now, "allocator clock")
        return now


class SleeveRegistry:
    """Registry of deployable sleeves and immutable specs."""

    def register(self, sleeve: SleeveSpec) -> None:
        raise NotImplementedError

    def get(self, sleeve_id: SleeveId) -> SleeveSpec:
        raise NotImplementedError

    def list_active(self) -> Sequence[SleeveSpec]:
        raise NotImplementedError


class InMemorySleeveRegistry(SleeveRegistry):
    """Deterministic sleeve registry for local paper runs and tests."""

    def __init__(self, sleeves: Sequence[SleeveSpec] = ()) -> None:
        self._sleeves: dict[SleeveId, SleeveSpec] = {}
        for sleeve in sleeves:
            self.register(sleeve)

    def register(self, sleeve: SleeveSpec) -> None:
        current = self._sleeves.get(sleeve.sleeve_id)
        if current is not None and current != sleeve:
            raise ValueError(f"conflicting sleeve spec: {sleeve.sleeve_id}")
        self._sleeves[sleeve.sleeve_id] = sleeve

    def get(self, sleeve_id: SleeveId) -> SleeveSpec:
        try:
            return self._sleeves[sleeve_id]
        except KeyError as exc:
            raise KeyError(f"sleeve not registered: {sleeve_id}") from exc

    def list_active(self) -> Sequence[SleeveSpec]:
        return tuple(sorted(self._sleeves.values(), key=lambda sleeve: str(sleeve.sleeve_id)))


class Allocator:
    """Capital allocation policy across sleeves."""

    def snapshot(self) -> CapitalSnapshot:
        raise NotImplementedError

    def propose(self, sleeves: Sequence[SleeveSpec]) -> Sequence[AllocationDecision]:
        raise NotImplementedError

    def apply(self, decisions: Sequence[AllocationDecision]) -> None:
        raise NotImplementedError


@dataclass
class EqualWeightAllocator(Allocator):
    """Conservative allocator that splits capital equally across active sleeves.

    This is intentionally simple and deterministic. It gives the framework a
    real dynamic-allocation path for paper and dry-run mode without introducing
    optimizer risk before audited PnL, drawdown, and reconciliation data exist.
    """

    total_capital: Decimal
    currency: str
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    allocations: Mapping[SleeveId, Decimal] = field(
        default_factory=lambda: cast(Mapping[SleeveId, Decimal], FrozenMap())
    )

    def __post_init__(self) -> None:
        require_non_negative_decimal(self.total_capital, "total_capital")
        require_currency(self.currency, "currency")
        object.__setattr__(self, "allocations", freeze_mapping(self.allocations))

    def snapshot(self) -> CapitalSnapshot:
        now = self._now()
        allocated = sum(self.allocations.values(), Decimal("0"))
        return CapitalSnapshot(
            as_of=now,
            total_capital=self.total_capital,
            allocated_capital=allocated,
            currency=self.currency,
            audit=audit_stamp_for(
                {
                    "total_capital": str(self.total_capital),
                    "allocated_capital": str(allocated),
                    "currency": self.currency,
                },
                object_id=f"capital-snapshot:{now.isoformat()}",
                object_kind="capital_snapshot",
                schema_version="allocation-v1",
                produced_at=now,
                producer="equal_weight_allocator",
            ),
        )

    def propose(self, sleeves: Sequence[SleeveSpec]) -> Sequence[AllocationDecision]:
        active = tuple(sorted(sleeves, key=lambda sleeve: str(sleeve.sleeve_id)))
        if not active:
            return ()
        for sleeve in active:
            if sleeve.currency != self.currency:
                raise ValueError(
                    f"sleeve {sleeve.sleeve_id} currency {sleeve.currency} "
                    f"does not match allocator currency {self.currency}"
                )

        now = self._now()
        base = self.total_capital / Decimal(len(active))
        decisions: list[AllocationDecision] = []
        for sleeve in active:
            target = min(base, sleeve.capital_allocation)
            decisions.append(
                AllocationDecision(
                    sleeve_id=sleeve.sleeve_id,
                    target_capital=target,
                    reason="equal_weight_capped_by_sleeve_allocation",
                    audit=audit_stamp_for(
                        {
                            "sleeve_id": str(sleeve.sleeve_id),
                            "target_capital": str(target),
                            "total_capital": str(self.total_capital),
                            "currency": self.currency,
                        },
                        object_id=(f"allocation-decision:{sleeve.sleeve_id}:{now.isoformat()}"),
                        object_kind="allocation_decision",
                        schema_version="allocation-v1",
                        produced_at=now,
                        producer="equal_weight_allocator",
                        parent_ids=(str(sleeve.sleeve_id),),
                    ),
                )
            )
        return tuple(decisions)

    def apply(self, decisions: Sequence[AllocationDecision]) -> None:
        next_allocations = dict(self.allocations)
        for decision in decisions:
            next_allocations[decision.sleeve_id] = decision.target_capital
        object.__setattr__(self, "allocations", freeze_mapping(next_allocations))

    def _now(self) -> datetime:
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("allocator clock must return timezone-aware datetimes")
        return now
