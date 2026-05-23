"""Normalized inbound events.

These are the *only* event shapes strategies ever consume. Venue adapters,
external-data clients, and the runner itself all produce ``NormalizedEvent``
values. Strategies match on the variant and react.

The sum type stays small on purpose. Adding a new event variant is a
deliberate cross-cutting change — it touches every strategy that has to
handle it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, TypeAlias

from eventcontracts.domain.fills import Fill
from eventcontracts.domain.ids import EventId
from eventcontracts.domain.lifecycle import MarketLifecycleEvent, SettlementEvent
from eventcontracts.domain.metadata import FrozenMap, freeze_mapping
from eventcontracts.domain.models import OrderBook, Quote, Trade
from eventcontracts.domain.models import Venue
from eventcontracts.domain.orders import Order, OrderReject
from eventcontracts.domain.validation import (
    require_aware_datetime,
    require_non_empty,
    require_optional_aware_datetime,
)


@dataclass(frozen=True)
class EventProvenance:
    source: str = "unknown"
    channel: str = "unknown"
    schema_version: str = "normalized-event-v1"
    venue: Venue | None = None
    source_sequence: str | None = None
    normalization_version: str = "normalizer-v1"
    metadata: Mapping[str, Any] = field(default_factory=FrozenMap)

    def __post_init__(self) -> None:
        require_non_empty(self.source, "source")
        require_non_empty(self.channel, "channel")
        require_non_empty(self.schema_version, "schema_version")
        require_non_empty(self.normalization_version, "normalization_version")
        if self.source_sequence is not None:
            require_non_empty(self.source_sequence, "source_sequence")
        object.__setattr__(self, "metadata", freeze_mapping(self.metadata))


def _validate_event_id(event_id: EventId) -> None:
    require_non_empty(str(event_id), "event_id")


@dataclass(frozen=True)
class QuoteEvent:
    event_id: EventId
    quote: Quote
    provenance: EventProvenance = field(default_factory=EventProvenance)

    def __post_init__(self) -> None:
        _validate_event_id(self.event_id)


@dataclass(frozen=True)
class TradeEvent:
    event_id: EventId
    trade: Trade
    provenance: EventProvenance = field(default_factory=EventProvenance)

    def __post_init__(self) -> None:
        _validate_event_id(self.event_id)


@dataclass(frozen=True)
class OrderBookEvent:
    event_id: EventId
    book: OrderBook
    provenance: EventProvenance = field(default_factory=EventProvenance)

    def __post_init__(self) -> None:
        _validate_event_id(self.event_id)


@dataclass(frozen=True)
class LifecycleEvent:
    event_id: EventId
    lifecycle: MarketLifecycleEvent
    provenance: EventProvenance = field(default_factory=EventProvenance)

    def __post_init__(self) -> None:
        _validate_event_id(self.event_id)


@dataclass(frozen=True)
class SettlementResolvedEvent:
    event_id: EventId
    settlement: SettlementEvent
    provenance: EventProvenance = field(default_factory=EventProvenance)

    def __post_init__(self) -> None:
        _validate_event_id(self.event_id)


@dataclass(frozen=True)
class ExternalSignalEvent:
    event_id: EventId
    source: str
    exchange_ts: datetime | None
    received_at: datetime
    schema_version: str
    payload: Mapping[str, Any] = field(default_factory=FrozenMap)
    provenance: EventProvenance = field(default_factory=EventProvenance)

    def __post_init__(self) -> None:
        _validate_event_id(self.event_id)
        require_non_empty(self.source, "source")
        require_non_empty(self.schema_version, "schema_version")
        require_optional_aware_datetime(self.exchange_ts, "exchange_ts")
        require_aware_datetime(self.received_at, "received_at")
        object.__setattr__(self, "payload", freeze_mapping(self.payload))


@dataclass(frozen=True)
class TimerEvent:
    event_id: EventId
    timestamp: datetime
    label: str
    provenance: EventProvenance = field(default_factory=EventProvenance)

    def __post_init__(self) -> None:
        _validate_event_id(self.event_id)
        require_aware_datetime(self.timestamp, "timestamp")
        require_non_empty(self.label, "label")


@dataclass(frozen=True)
class OwnFillEvent:
    event_id: EventId
    fill: Fill
    provenance: EventProvenance = field(default_factory=EventProvenance)

    def __post_init__(self) -> None:
        _validate_event_id(self.event_id)


@dataclass(frozen=True)
class OwnOrderUpdateEvent:
    event_id: EventId
    order: Order
    provenance: EventProvenance = field(default_factory=EventProvenance)

    def __post_init__(self) -> None:
        _validate_event_id(self.event_id)


@dataclass(frozen=True)
class OwnOrderRejectEvent:
    event_id: EventId
    reject: OrderReject
    provenance: EventProvenance = field(default_factory=EventProvenance)

    def __post_init__(self) -> None:
        _validate_event_id(self.event_id)


NormalizedEvent: TypeAlias = (
    QuoteEvent
    | TradeEvent
    | OrderBookEvent
    | LifecycleEvent
    | SettlementResolvedEvent
    | ExternalSignalEvent
    | TimerEvent
    | OwnFillEvent
    | OwnOrderUpdateEvent
    | OwnOrderRejectEvent
)


EVENT_KINDS: tuple[str, ...] = (
    "quote",
    "trade",
    "book",
    "lifecycle",
    "settlement",
    "external",
    "timer",
    "own_fill",
    "own_order_update",
    "own_order_reject",
)


def event_kind(event: NormalizedEvent) -> str:
    """Stable string tag for an event variant — useful for filters and logs."""
    match event:
        case QuoteEvent():
            return "quote"
        case TradeEvent():
            return "trade"
        case OrderBookEvent():
            return "book"
        case LifecycleEvent():
            return "lifecycle"
        case SettlementResolvedEvent():
            return "settlement"
        case ExternalSignalEvent():
            return "external"
        case TimerEvent():
            return "timer"
        case OwnFillEvent():
            return "own_fill"
        case OwnOrderUpdateEvent():
            return "own_order_update"
        case OwnOrderRejectEvent():
            return "own_order_reject"
