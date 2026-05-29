"""Deterministic replay sort keys shared by storage backends."""

from __future__ import annotations

from datetime import datetime

from eventcontracts.domain.events import (
    ExternalSignalEvent,
    LifecycleEvent,
    NormalizedEvent,
    OrderBookEvent,
    OwnFillEvent,
    OwnOrderRejectEvent,
    OwnOrderUpdateEvent,
    QuoteEvent,
    SettlementResolvedEvent,
    TimerEvent,
    TradeEvent,
)
from eventcontracts.storage.interfaces import EventEnvelope, NormalizationReject


def envelope_sort_key(event: EventEnvelope) -> tuple[str, str, str, str, str]:
    """Deterministic raw-event ordering for replay."""

    event_time = event.exchange_ts or event.received_at
    sequence = str(event.metadata.get("source_sequence", ""))
    return (
        event_time.isoformat(),
        event.received_at.isoformat(),
        event.source,
        event.channel,
        sequence,
    )


def normalized_event_sort_key(event: NormalizedEvent) -> tuple[str, str, str, str, str, str]:
    """Deterministic normalized-event ordering for replay."""

    event_time, received_at = _normalized_times(event)
    provenance = event.provenance
    sequence = provenance.source_sequence or ""
    return (
        event_time.isoformat() if event_time is not None else "",
        received_at.isoformat() if received_at is not None else "",
        provenance.source,
        provenance.channel,
        sequence,
        str(event.event_id),
    )


def normalization_reject_sort_key(reject: NormalizationReject) -> tuple[str, str, str, str, str, str]:
    return (*envelope_sort_key(reject.raw), reject.raw_sha256)


def _normalized_times(event: NormalizedEvent) -> tuple[datetime | None, datetime | None]:
    if isinstance(event, QuoteEvent):
        return event.quote.exchange_ts or event.quote.received_at, event.quote.received_at
    if isinstance(event, TradeEvent):
        return event.trade.exchange_ts or event.trade.received_at, event.trade.received_at
    if isinstance(event, OrderBookEvent):
        return event.book.exchange_ts or event.book.received_at, event.book.received_at
    if isinstance(event, LifecycleEvent):
        return (
            event.lifecycle.exchange_ts or event.lifecycle.received_at,
            event.lifecycle.received_at,
        )
    if isinstance(event, SettlementResolvedEvent):
        return event.settlement.settled_at, event.settlement.settled_at
    if isinstance(event, ExternalSignalEvent):
        return event.received_at, event.received_at
    if isinstance(event, TimerEvent):
        return event.timestamp, event.timestamp
    if isinstance(event, OwnFillEvent):
        return event.fill.exchange_ts or event.fill.filled_at, event.fill.filled_at
    if isinstance(event, OwnOrderUpdateEvent):
        return event.order.updated_at, event.order.updated_at
    if isinstance(event, OwnOrderRejectEvent):
        return event.reject.rejected_at, event.reject.rejected_at
    return None, None
