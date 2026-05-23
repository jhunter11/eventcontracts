"""Fill records."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from eventcontracts.domain.ids import (
    ClientOrderId,
    CorrelationId,
    FillId,
    SleeveId,
    StrategyId,
    VenueOrderId,
)
from eventcontracts.domain.metadata import FrozenMap, freeze_mapping
from eventcontracts.domain.models import InstrumentId, OutcomeSide
from eventcontracts.domain.orders import Liquidity, OrderSide
from eventcontracts.domain.validation import (
    require_aware_datetime,
    require_currency,
    require_non_empty,
    require_non_negative_decimal,
    require_optional_aware_datetime,
    require_positive_decimal,
    require_probability_decimal,
)


@dataclass(frozen=True)
class Fill:
    fill_id: FillId
    venue_order_id: VenueOrderId
    client_order_id: ClientOrderId
    instrument_id: InstrumentId
    outcome_side: OutcomeSide
    order_side: OrderSide
    price: Decimal
    quantity: Decimal
    liquidity: Liquidity
    fee_amount: Decimal
    fee_currency: str
    filled_at: datetime
    exchange_ts: datetime | None
    correlation_id: CorrelationId
    strategy_id: StrategyId
    sleeve_id: SleeveId
    metadata: Mapping[str, Any] = field(default_factory=FrozenMap)

    def __post_init__(self) -> None:
        require_non_empty(str(self.fill_id), "fill_id")
        require_non_empty(str(self.venue_order_id), "venue_order_id")
        require_non_empty(str(self.client_order_id), "client_order_id")
        require_probability_decimal(self.price, "price")
        require_positive_decimal(self.quantity, "quantity")
        require_non_negative_decimal(self.fee_amount, "fee_amount")
        require_currency(self.fee_currency, "fee_currency")
        require_aware_datetime(self.filled_at, "filled_at")
        require_optional_aware_datetime(self.exchange_ts, "exchange_ts")
        require_non_empty(str(self.correlation_id), "correlation_id")
        require_non_empty(str(self.strategy_id), "strategy_id")
        require_non_empty(str(self.sleeve_id), "sleeve_id")
        object.__setattr__(self, "metadata", freeze_mapping(self.metadata))
