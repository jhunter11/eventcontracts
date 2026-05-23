"""Order primitives.

These describe orders as they exist *after* a strategy decision has been
accepted and turned into a venue-bound ticket. Strategies themselves emit
``PlaceOrder`` decisions (see ``decisions.py``); the runner+gateway translate
those into ``Order`` records.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from eventcontracts.domain.ids import (
    ClientOrderId,
    CorrelationId,
    SleeveId,
    StrategyId,
    VenueOrderId,
)
from eventcontracts.domain.metadata import FrozenMap, freeze_mapping
from eventcontracts.domain.models import InstrumentId, OutcomeSide
from eventcontracts.domain.validation import (
    require_aware_datetime,
    require_non_empty,
    require_non_negative_decimal,
    require_optional_aware_datetime,
    require_positive_decimal,
    require_probability_decimal,
)


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    LIMIT = "limit"
    MARKET = "market"
    POST_ONLY = "post_only"


class TimeInForce(str, Enum):
    GTC = "gtc"
    IOC = "ioc"
    FOK = "fok"
    GTD = "gtd"


class Liquidity(str, Enum):
    MAKER = "maker"
    TAKER = "taker"
    UNKNOWN = "unknown"


class OrderStatus(str, Enum):
    PENDING = "pending"
    OPEN = "open"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"
    EXPIRED = "expired"


@dataclass(frozen=True)
class Order:
    client_order_id: ClientOrderId
    venue_order_id: VenueOrderId | None
    instrument_id: InstrumentId
    outcome_side: OutcomeSide
    order_side: OrderSide
    order_type: OrderType
    time_in_force: TimeInForce
    price: Decimal | None
    quantity: Decimal
    filled_quantity: Decimal
    status: OrderStatus
    created_at: datetime
    updated_at: datetime
    correlation_id: CorrelationId
    strategy_id: StrategyId
    sleeve_id: SleeveId
    expires_at: datetime | None = None
    metadata: Mapping[str, Any] = field(default_factory=FrozenMap)

    def __post_init__(self) -> None:
        require_non_empty(str(self.client_order_id), "client_order_id")
        if self.venue_order_id is not None:
            require_non_empty(str(self.venue_order_id), "venue_order_id")
        if self.price is not None:
            require_probability_decimal(self.price, "price")
        require_positive_decimal(self.quantity, "quantity")
        require_non_negative_decimal(self.filled_quantity, "filled_quantity")
        if self.filled_quantity > self.quantity:
            raise ValueError("filled_quantity cannot exceed quantity")
        require_aware_datetime(self.created_at, "created_at")
        require_aware_datetime(self.updated_at, "updated_at")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot be before created_at")
        require_non_empty(str(self.correlation_id), "correlation_id")
        require_non_empty(str(self.strategy_id), "strategy_id")
        require_non_empty(str(self.sleeve_id), "sleeve_id")
        require_optional_aware_datetime(self.expires_at, "expires_at")
        object.__setattr__(self, "metadata", freeze_mapping(self.metadata))


@dataclass(frozen=True)
class OrderReject:
    client_order_id: ClientOrderId
    reason: str
    rejected_at: datetime
    venue_code: str | None = None

    def __post_init__(self) -> None:
        require_non_empty(str(self.client_order_id), "client_order_id")
        require_non_empty(self.reason, "reason")
        require_aware_datetime(self.rejected_at, "rejected_at")
        if self.venue_code is not None:
            require_non_empty(self.venue_code, "venue_code")
