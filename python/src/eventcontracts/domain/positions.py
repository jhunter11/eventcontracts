"""Position, cash, exposure, and ledger primitives.

These are the state objects a strategy reads through ``StrategyContext`` and
that the runner reconciles after fills and settlements.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from eventcontracts.domain.ids import CorrelationId, SleeveId
from eventcontracts.domain.models import InstrumentId, OutcomeSide
from eventcontracts.domain.validation import (
    require_aware_datetime,
    require_currency,
    require_non_empty,
    require_non_negative_decimal,
)


@dataclass(frozen=True)
class Position:
    instrument_id: InstrumentId
    outcome_side: OutcomeSide
    quantity: Decimal
    average_price: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    updated_at: datetime

    def __post_init__(self) -> None:
        require_non_negative_decimal(self.quantity, "quantity")
        require_non_negative_decimal(self.average_price, "average_price")
        require_aware_datetime(self.updated_at, "updated_at")
        if self.quantity == Decimal("0"):
            object.__setattr__(self, "average_price", Decimal("0"))
            object.__setattr__(self, "unrealized_pnl", Decimal("0"))


@dataclass(frozen=True)
class CashBalance:
    currency: str
    total: Decimal
    available: Decimal
    held_for_orders: Decimal
    settling: Decimal
    updated_at: datetime

    def __post_init__(self) -> None:
        require_currency(self.currency, "currency")
        require_non_negative_decimal(self.total, "total")
        require_non_negative_decimal(self.available, "available")
        require_non_negative_decimal(self.held_for_orders, "held_for_orders")
        require_non_negative_decimal(self.settling, "settling")
        require_aware_datetime(self.updated_at, "updated_at")


@dataclass(frozen=True)
class Exposure:
    sleeve_id: SleeveId
    currency: str
    gross_notional: Decimal
    net_notional: Decimal
    long_notional: Decimal
    short_notional: Decimal
    updated_at: datetime

    def __post_init__(self) -> None:
        require_non_empty(str(self.sleeve_id), "sleeve_id")
        require_currency(self.currency, "currency")
        require_non_negative_decimal(self.gross_notional, "gross_notional")
        require_non_negative_decimal(self.long_notional, "long_notional")
        require_non_negative_decimal(self.short_notional, "short_notional")
        require_aware_datetime(self.updated_at, "updated_at")


@dataclass(frozen=True)
class LedgerEntry:
    entry_id: str
    sleeve_id: SleeveId
    timestamp: datetime
    kind: str
    instrument_id: InstrumentId | None
    amount: Decimal
    currency: str
    correlation_id: CorrelationId | None
    notes: str = ""

    def __post_init__(self) -> None:
        require_non_empty(self.entry_id, "entry_id")
        require_non_empty(str(self.sleeve_id), "sleeve_id")
        require_aware_datetime(self.timestamp, "timestamp")
        require_non_empty(self.kind, "kind")
        require_currency(self.currency, "currency")
        if self.correlation_id is not None:
            require_non_empty(str(self.correlation_id), "correlation_id")
