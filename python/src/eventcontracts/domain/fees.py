"""Fee model interfaces."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal

from eventcontracts.domain.models import InstrumentId, OutcomeSide
from eventcontracts.domain.validation import (
    require_currency,
    require_non_empty,
    require_non_negative_decimal,
    require_positive_decimal,
    require_probability_decimal,
)


@dataclass(frozen=True)
class FeeEstimate:
    amount: Decimal
    currency: str
    model_name: str
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        require_non_negative_decimal(self.amount, "amount")
        require_currency(self.currency, "currency")
        require_non_empty(self.model_name, "model_name")
        object.__setattr__(self, "notes", tuple(self.notes))


@dataclass(frozen=True)
class FillContext:
    instrument_id: InstrumentId
    side: OutcomeSide
    price: Decimal
    quantity: Decimal
    liquidity: str
    market_category: str | None = None

    def __post_init__(self) -> None:
        require_probability_decimal(self.price, "price")
        require_positive_decimal(self.quantity, "quantity")
        require_non_empty(self.liquidity, "liquidity")
        if self.market_category is not None:
            require_non_empty(self.market_category, "market_category")


class FeeModel(ABC):
    """Estimate venue fees at fill granularity."""

    name: str

    @abstractmethod
    def estimate(self, fill: FillContext) -> FeeEstimate:
        """Return the expected fee for a hypothetical fill."""
