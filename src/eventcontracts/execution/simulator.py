"""Execution simulator boundaries."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from eventcontracts.domain.metadata import FrozenMap, freeze_mapping
from eventcontracts.domain.models import InstrumentId, OutcomeSide
from eventcontracts.domain.validation import (
    require_aware_datetime,
    require_currency,
    require_non_empty,
    require_non_negative_decimal,
    require_positive_decimal,
    require_probability_decimal,
)


@dataclass(frozen=True)
class OrderIntent:
    instrument_id: InstrumentId
    side: OutcomeSide
    price: Decimal
    quantity: Decimal
    order_type: str
    post_only: bool = False
    metadata: Mapping[str, Any] = field(default_factory=FrozenMap)

    def __post_init__(self) -> None:
        require_probability_decimal(self.price, "price")
        require_positive_decimal(self.quantity, "quantity")
        require_non_empty(self.order_type, "order_type")
        object.__setattr__(self, "metadata", freeze_mapping(self.metadata))


@dataclass(frozen=True)
class SimulatedFill:
    order: OrderIntent
    price: Decimal
    quantity: Decimal
    filled_at: datetime
    liquidity: str
    fee_amount: Decimal
    fee_currency: str

    def __post_init__(self) -> None:
        require_probability_decimal(self.price, "price")
        require_positive_decimal(self.quantity, "quantity")
        require_aware_datetime(self.filled_at, "filled_at")
        require_non_empty(self.liquidity, "liquidity")
        require_non_negative_decimal(self.fee_amount, "fee_amount")
        require_currency(self.fee_currency, "fee_currency")


class ExecutionSimulator:
    """Consumes replayed market events and produces simulated fills."""

    def submit(self, order: OrderIntent) -> list[SimulatedFill]:
        raise NotImplementedError
