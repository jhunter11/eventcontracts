"""Core venue-neutral domain models."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from eventcontracts.domain.metadata import FrozenMap, freeze_mapping
from eventcontracts.domain.validation import (
    require_aware_datetime,
    require_non_empty,
    require_optional_aware_datetime,
    require_positive_decimal,
    require_probability_decimal,
)


class Venue(str, Enum):
    KALSHI = "kalshi"
    POLYMARKET_GLOBAL = "polymarket_global"
    POLYMARKET_US = "polymarket_us"


class OutcomeSide(str, Enum):
    YES = "yes"
    NO = "no"


class MarketStatus(str, Enum):
    UNKNOWN = "unknown"
    OPEN = "open"
    PAUSED = "paused"
    CLOSED = "closed"
    DETERMINED = "determined"
    DISPUTED = "disputed"
    FINALIZED = "finalized"


@dataclass(frozen=True)
class Probability:
    value: Decimal

    def __post_init__(self) -> None:
        require_probability_decimal(self.value)


@dataclass(frozen=True)
class InstrumentId:
    venue: Venue
    market_id: str
    outcome_id: str | None = None

    def __post_init__(self) -> None:
        require_non_empty(self.market_id, "market_id")
        if self.outcome_id is not None:
            require_non_empty(self.outcome_id, "outcome_id")


@dataclass(frozen=True)
class Market:
    instrument_id: InstrumentId
    title: str
    category: str | None
    status: MarketStatus
    opens_at: datetime | None = None
    closes_at: datetime | None = None
    settles_at: datetime | None = None
    metadata: Mapping[str, Any] = field(default_factory=FrozenMap)

    def __post_init__(self) -> None:
        require_non_empty(self.title, "title")
        require_optional_aware_datetime(self.opens_at, "opens_at")
        require_optional_aware_datetime(self.closes_at, "closes_at")
        require_optional_aware_datetime(self.settles_at, "settles_at")
        if self.opens_at is not None and self.closes_at is not None and self.closes_at <= self.opens_at:
            raise ValueError("closes_at must be after opens_at")
        object.__setattr__(self, "metadata", freeze_mapping(self.metadata))


@dataclass(frozen=True)
class OrderBookLevel:
    price: Decimal
    quantity: Decimal

    def __post_init__(self) -> None:
        require_probability_decimal(self.price, "price")
        require_positive_decimal(self.quantity, "quantity")


@dataclass(frozen=True)
class Quote:
    instrument_id: InstrumentId
    side: OutcomeSide
    bid: OrderBookLevel | None
    ask: OrderBookLevel | None
    exchange_ts: datetime | None
    received_at: datetime

    def __post_init__(self) -> None:
        require_optional_aware_datetime(self.exchange_ts, "exchange_ts")
        require_aware_datetime(self.received_at, "received_at")


@dataclass(frozen=True)
class OrderBook:
    instrument_id: InstrumentId
    yes_bids: tuple[OrderBookLevel, ...]
    yes_asks: tuple[OrderBookLevel, ...]
    no_bids: tuple[OrderBookLevel, ...]
    no_asks: tuple[OrderBookLevel, ...]
    exchange_ts: datetime | None
    received_at: datetime

    def __post_init__(self) -> None:
        require_optional_aware_datetime(self.exchange_ts, "exchange_ts")
        require_aware_datetime(self.received_at, "received_at")


@dataclass(frozen=True)
class Trade:
    instrument_id: InstrumentId
    side: OutcomeSide | None
    price: Decimal
    quantity: Decimal
    trade_id: str | None
    exchange_ts: datetime | None
    received_at: datetime
    aggressor_side: OutcomeSide | None = None
    metadata: Mapping[str, Any] = field(default_factory=FrozenMap)

    def __post_init__(self) -> None:
        require_probability_decimal(self.price, "price")
        require_positive_decimal(self.quantity, "quantity")
        require_optional_aware_datetime(self.exchange_ts, "exchange_ts")
        require_aware_datetime(self.received_at, "received_at")
        object.__setattr__(self, "metadata", freeze_mapping(self.metadata))
