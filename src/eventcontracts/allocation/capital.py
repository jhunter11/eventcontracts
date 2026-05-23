"""Capital allocation scaffolds."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from eventcontracts.domain.ids import SleeveId
from eventcontracts.domain.spec import SleeveSpec
from eventcontracts.domain.validation import (
    require_aware_datetime,
    require_non_empty,
    require_non_negative_decimal,
)


@dataclass(frozen=True)
class CapitalSnapshot:
    as_of: datetime
    total_capital: Decimal
    allocated_capital: Decimal
    currency: str

    def __post_init__(self) -> None:
        require_aware_datetime(self.as_of, "as_of")
        require_non_negative_decimal(self.total_capital, "total_capital")
        require_non_negative_decimal(self.allocated_capital, "allocated_capital")
        require_non_empty(self.currency, "currency")


@dataclass(frozen=True)
class AllocationDecision:
    sleeve_id: SleeveId
    target_capital: Decimal
    reason: str

    def __post_init__(self) -> None:
        require_non_empty(str(self.sleeve_id), "sleeve_id")
        require_non_negative_decimal(self.target_capital, "target_capital")


class SleeveRegistry:
    """Registry of deployable sleeves and their immutable specs."""

    def register(self, sleeve: SleeveSpec) -> None:
        raise NotImplementedError

    def get(self, sleeve_id: SleeveId) -> SleeveSpec:
        raise NotImplementedError

    def list_active(self) -> Sequence[SleeveSpec]:
        raise NotImplementedError


class Allocator:
    """Capital allocation policy across sleeves."""

    def snapshot(self) -> CapitalSnapshot:
        raise NotImplementedError

    def propose(self, sleeves: Sequence[SleeveSpec]) -> Sequence[AllocationDecision]:
        raise NotImplementedError

    def apply(self, decisions: Sequence[AllocationDecision]) -> None:
        raise NotImplementedError
