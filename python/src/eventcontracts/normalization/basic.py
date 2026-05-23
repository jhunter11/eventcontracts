"""Basic raw-event normalizers for the first local vertical slice.

These functions intentionally accept a small explicit payload shape. Real venue
adapters should translate native API payloads into these raw envelope fields or
register venue-specific normalizers beside them.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from eventcontracts.domain.events import (
    EventProvenance,
    OrderBookEvent,
    QuoteEvent,
    TradeEvent,
)
from eventcontracts.domain.ids import EventId
from eventcontracts.domain.models import (
    InstrumentId,
    OrderBook,
    OrderBookLevel,
    OutcomeSide,
    Quote,
    Trade,
    Venue,
)
from eventcontracts.storage.interfaces import EventEnvelope


def _require_str(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"payload.{key} must be a non-empty string")
    return value


def _optional_side(value: Any) -> OutcomeSide | None:
    if value is None:
        return None
    return OutcomeSide(str(value))


def _decimal(payload: dict[str, Any], key: str) -> Decimal:
    return Decimal(str(payload[key]))


def _instrument(raw: EventEnvelope) -> InstrumentId:
    venue = raw.venue or Venue(str(raw.payload["venue"]))
    return InstrumentId(
        venue=venue,
        market_id=_require_str(dict(raw.payload), "market_id"),
        outcome_id=raw.payload.get("outcome_id"),
    )


def _event_id(raw: EventEnvelope) -> EventId:
    payload = dict(raw.payload)
    explicit = payload.get("event_id")
    if isinstance(explicit, str) and explicit:
        return EventId(explicit)
    sequence = raw.metadata.get("source_sequence")
    if sequence is not None:
        return EventId(f"{raw.source}:{raw.channel}:{sequence}")
    event_time = (raw.exchange_ts or raw.received_at).isoformat()
    return EventId(f"{raw.source}:{raw.channel}:{event_time}")


def _provenance(raw: EventEnvelope) -> EventProvenance:
    return EventProvenance(
        source=raw.source,
        channel=raw.channel,
        schema_version=raw.schema_version,
        venue=raw.venue,
        source_sequence=(
            str(raw.metadata["source_sequence"])
            if "source_sequence" in raw.metadata
            else None
        ),
        normalization_version="basic-v1",
    )


def normalize_trade(raw: EventEnvelope) -> TradeEvent:
    """Convert a raw trade envelope into ``TradeEvent``."""

    payload = dict(raw.payload)
    trade = Trade(
        instrument_id=_instrument(raw),
        side=_optional_side(payload.get("side")),
        price=_decimal(payload, "price"),
        quantity=_decimal(payload, "quantity"),
        trade_id=payload.get("trade_id"),
        exchange_ts=raw.exchange_ts,
        received_at=raw.received_at,
        aggressor_side=_optional_side(payload.get("aggressor_side")),
        metadata={"raw_schema_version": raw.schema_version},
    )
    return TradeEvent(event_id=_event_id(raw), trade=trade, provenance=_provenance(raw))


def normalize_quote(raw: EventEnvelope) -> QuoteEvent:
    """Convert a raw top-of-book envelope into ``QuoteEvent``."""

    payload = dict(raw.payload)
    bid = (
        OrderBookLevel(
            price=_decimal(payload, "bid_price"),
            quantity=_decimal(payload, "bid_quantity"),
        )
        if payload.get("bid_price") is not None
        else None
    )
    ask = (
        OrderBookLevel(
            price=_decimal(payload, "ask_price"),
            quantity=_decimal(payload, "ask_quantity"),
        )
        if payload.get("ask_price") is not None
        else None
    )
    quote = Quote(
        instrument_id=_instrument(raw),
        side=OutcomeSide(str(payload.get("side", OutcomeSide.YES.value))),
        bid=bid,
        ask=ask,
        exchange_ts=raw.exchange_ts,
        received_at=raw.received_at,
    )
    return QuoteEvent(event_id=_event_id(raw), quote=quote, provenance=_provenance(raw))


def _levels(raw_levels: Any) -> tuple[OrderBookLevel, ...]:
    if raw_levels is None:
        return ()
    levels = []
    for level in raw_levels:
        price, quantity = level
        levels.append(
            OrderBookLevel(price=Decimal(str(price)), quantity=Decimal(str(quantity)))
        )
    return tuple(levels)


def normalize_order_book(raw: EventEnvelope) -> OrderBookEvent:
    """Convert a raw full-book envelope into ``OrderBookEvent``."""

    payload = dict(raw.payload)
    book = OrderBook(
        instrument_id=_instrument(raw),
        yes_bids=_levels(payload.get("yes_bids")),
        yes_asks=_levels(payload.get("yes_asks")),
        no_bids=_levels(payload.get("no_bids")),
        no_asks=_levels(payload.get("no_asks")),
        exchange_ts=raw.exchange_ts,
        received_at=raw.received_at,
    )
    return OrderBookEvent(event_id=_event_id(raw), book=book, provenance=_provenance(raw))


BASIC_NORMALIZERS = {
    ("raw-event-v1", "trade"): normalize_trade,
    ("raw-event-v1", "quote"): normalize_quote,
    ("raw-event-v1", "book"): normalize_order_book,
}
