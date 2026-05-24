"""Capital allocation scaffolds."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import cast

from eventcontracts.audit import AuditStamp, audit_stamp_for
from eventcontracts.domain.ids import SleeveId
from eventcontracts.domain.metadata import FrozenMap, freeze_mapping
from eventcontracts.domain.spec import SleeveSpec
from eventcontracts.domain.validation import (
    require_aware_datetime,
    require_currency,
    require_non_empty,
    require_non_negative_decimal,
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
                        object_id=(
                            f"allocation-decision:{sleeve.sleeve_id}:"
                            f"{now.isoformat()}"
                        ),
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
