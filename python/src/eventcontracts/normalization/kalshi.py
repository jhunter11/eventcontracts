"""Normalizers for native Kalshi REST and WebSocket payloads."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from eventcontracts.domain.events import (
    EventProvenance,
    LifecycleEvent,
    NormalizedEvent,
    OrderBookEvent,
    QuoteEvent,
    TradeEvent,
)
from eventcontracts.domain.ids import EventId
from eventcontracts.domain.lifecycle import MarketLifecycleEvent, MarketLifecycleKind
from eventcontracts.domain.models import (
    InstrumentId,
    OrderBook,
    OrderBookLevel,
    OutcomeSide,
    Quote,
    Trade,
    Venue,
)
from eventcontracts.normalization.pipeline import NormalizeFn, NormalizerKey
from eventcontracts.storage.interfaces import EventEnvelope


@dataclass
class _KalshiBookState:
    yes_bids: dict[Decimal, Decimal] = field(default_factory=dict)
    no_bids: dict[Decimal, Decimal] = field(default_factory=dict)


class KalshiNormalizer:
    """Stateful native-payload normalizer.

    Kalshi orderbook deltas are incremental, so this normalizer maintains the
    latest bid ladders by market ticker and emits a full `OrderBookEvent` after
    each snapshot or delta.
    """

    def __init__(self) -> None:
        self._books: dict[str, _KalshiBookState] = {}

    def handlers(self) -> dict[NormalizerKey, NormalizeFn]:
        return {
            ("kalshi-ws-v1", "ticker"): self.normalize_ticker,
            ("kalshi-ws-v1", "trade"): self.normalize_trade,
            ("kalshi-ws-v1", "orderbook_snapshot"): self.normalize_orderbook_snapshot,
            ("kalshi-ws-v1", "orderbook_delta"): self.normalize_orderbook_delta,
            ("kalshi-ws-v1", "market_lifecycle_v2"): self.normalize_lifecycle,
            ("kalshi-rest-v1", "book"): self.normalize_rest_orderbook,
            ("kalshi-rest-v1", "trade"): self.normalize_trade,
        }

    def normalize_ticker(self, raw: EventEnvelope) -> tuple[QuoteEvent, ...]:
        msg = _message(raw)
        instrument = _instrument(msg)
        exchange_ts = _message_time(msg) or raw.exchange_ts
        yes_bid = _optional_decimal(msg.get("yes_bid_dollars"))
        yes_ask = _optional_decimal(msg.get("yes_ask_dollars"))
        yes_bid_size_raw = msg.get("yes_bid_size_fp")
        yes_ask_size_raw = msg.get("yes_ask_size_fp")
        yes_bid_size = _optional_decimal(yes_bid_size_raw or "1")
        yes_ask_size = _optional_decimal(yes_ask_size_raw or "1")

        explicit_no_bid = _optional_decimal(msg.get("no_bid_dollars"))
        explicit_no_ask = _optional_decimal(msg.get("no_ask_dollars"))
        no_bid = explicit_no_bid if explicit_no_bid is not None else _invert_probability(yes_ask)
        no_ask = explicit_no_ask if explicit_no_ask is not None else _invert_probability(yes_bid)
        no_bid_size_raw = msg.get("no_bid_size_fp")
        no_ask_size_raw = msg.get("no_ask_size_fp")
        no_bid_size = _optional_decimal(
            no_bid_size_raw or (yes_ask_size_raw if explicit_no_bid is None else None) or "1"
        )
        no_ask_size = _optional_decimal(
            no_ask_size_raw or (yes_bid_size_raw if explicit_no_ask is None else None) or "1"
        )
        synthetic_liquidity = (
            (yes_bid is not None and yes_bid_size_raw is None)
            or (yes_ask is not None and yes_ask_size_raw is None)
            or (explicit_no_bid is not None and no_bid_size_raw is None)
            or (explicit_no_ask is not None and no_ask_size_raw is None)
            or (no_bid is not None and explicit_no_bid is None and yes_ask_size_raw is None)
            or (no_ask is not None and explicit_no_ask is None and yes_bid_size_raw is None)
        )
        provenance = _provenance(
            raw,
            extra_metadata=(
                {
                    "synthetic_liquidity": True,
                    "liquidity_source": "ticker_missing_size",
                }
                if synthetic_liquidity
                else None
            ),
        )

        events: list[QuoteEvent] = []
        if yes_bid is not None or yes_ask is not None:
            events.append(
                QuoteEvent(
                    event_id=_event_id_with_suffix(raw, msg, "yes"),
                    quote=Quote(
                        instrument_id=instrument,
                        side=OutcomeSide.YES,
                        bid=_level(yes_bid, yes_bid_size),
                        ask=_level(yes_ask, yes_ask_size),
                        exchange_ts=exchange_ts,
                        received_at=raw.received_at,
                    ),
                    provenance=provenance,
                )
            )
        if no_bid is not None or no_ask is not None:
            events.append(
                QuoteEvent(
                    event_id=_event_id_with_suffix(raw, msg, "no"),
                    quote=Quote(
                        instrument_id=instrument,
                        side=OutcomeSide.NO,
                        bid=_level(no_bid, no_bid_size),
                        ask=_level(no_ask, no_ask_size),
                        exchange_ts=exchange_ts,
                        received_at=raw.received_at,
                    ),
                    provenance=provenance,
                )
            )
        return tuple(events)

    def normalize_trade(self, raw: EventEnvelope) -> TradeEvent:
        msg = _message(raw)
        side_raw = msg.get("taker_outcome_side") or msg.get("taker_side") or msg.get("side")
        side = OutcomeSide(str(side_raw)) if side_raw else OutcomeSide.YES
        yes_price = msg.get("yes_price_dollars") or msg.get("yes_price")
        no_price = msg.get("no_price_dollars") or msg.get("no_price")
        quantity_raw = msg.get("count_fp") or msg.get("quantity") or msg.get("count")
        trade = Trade(
            instrument_id=_instrument(msg),
            side=side,
            price=_trade_price(side=side, yes_price=yes_price, no_price=no_price, fallback=msg.get("price")),
            quantity=Decimal(str(quantity_raw or "1")),
            trade_id=str(msg["trade_id"]) if msg.get("trade_id") is not None else None,
            exchange_ts=_message_time(msg) or raw.exchange_ts,
            received_at=raw.received_at,
            aggressor_side=side,
            metadata={
                "raw_schema_version": raw.schema_version,
                **({"synthetic_quantity": True} if quantity_raw is None else {}),
            },
        )
        return TradeEvent(
            event_id=_event_id(raw, msg),
            trade=trade,
            provenance=_provenance(
                raw,
                extra_metadata=(
                    {"synthetic_quantity": True, "liquidity_source": "trade_missing_quantity"}
                    if quantity_raw is None
                    else None
                ),
            ),
        )

    def normalize_rest_orderbook(self, raw: EventEnvelope) -> OrderBookEvent:
        payload = dict(raw.payload)
        book_payload = _as_mapping(payload.get("orderbook_fp") or payload.get("orderbook") or payload)
        market_ticker = str(payload.get("market_ticker") or payload.get("ticker") or payload.get("market_id"))
        msg = {"market_ticker": market_ticker}
        book = _book_from_ladders(
            instrument_id=_instrument(msg),
            yes_bids=_levels(book_payload.get("yes_dollars") or book_payload.get("yes")),
            no_bids=_levels(book_payload.get("no_dollars") or book_payload.get("no")),
            exchange_ts=raw.exchange_ts,
            received_at=raw.received_at,
        )
        return OrderBookEvent(event_id=_event_id(raw, msg), book=book, provenance=_provenance(raw))

    def normalize_orderbook_snapshot(self, raw: EventEnvelope) -> OrderBookEvent:
        msg = _message(raw)
        market_ticker = _market_ticker(msg)
        state = self._books.setdefault(market_ticker, _KalshiBookState())
        state.yes_bids = _ladder_dict(_levels(msg.get("yes_dollars_fp") or msg.get("yes_dollars") or msg.get("yes")))
        state.no_bids = _ladder_dict(_levels(msg.get("no_dollars_fp") or msg.get("no_dollars") or msg.get("no")))
        return OrderBookEvent(
            event_id=_event_id(raw, msg),
            book=_book_from_ladders(
                instrument_id=_instrument(msg),
                yes_bids=_dict_levels(state.yes_bids, descending=True),
                no_bids=_dict_levels(state.no_bids, descending=True),
                exchange_ts=_message_time(msg) or raw.exchange_ts,
                received_at=raw.received_at,
            ),
            provenance=_provenance(raw),
        )

    def normalize_orderbook_delta(self, raw: EventEnvelope) -> OrderBookEvent:
        msg = _message(raw)
        market_ticker = _market_ticker(msg)
        state = self._books.setdefault(market_ticker, _KalshiBookState())
        side = OutcomeSide(str(msg["side"]))
        price = Decimal(str(msg["price_dollars"]))
        delta = Decimal(str(msg.get("delta_fp") or msg.get("delta") or "0"))
        ladder = state.yes_bids if side is OutcomeSide.YES else state.no_bids
        new_quantity = ladder.get(price, Decimal("0")) + delta
        if new_quantity <= 0:
            ladder.pop(price, None)
        else:
            ladder[price] = new_quantity
        return OrderBookEvent(
            event_id=_event_id(raw, msg),
            book=_book_from_ladders(
                instrument_id=_instrument(msg),
                yes_bids=_dict_levels(state.yes_bids, descending=True),
                no_bids=_dict_levels(state.no_bids, descending=True),
                exchange_ts=_message_time(msg) or raw.exchange_ts,
                received_at=raw.received_at,
            ),
            provenance=_provenance(raw),
        )

    def normalize_lifecycle(self, raw: EventEnvelope) -> NormalizedEvent:
        msg = _message(raw)
        event_type = str(msg.get("event_type", "metadata_updated"))
        lifecycle = MarketLifecycleEvent(
            instrument_id=_instrument(msg),
            kind=_lifecycle_kind(event_type),
            exchange_ts=_message_time(msg) or raw.exchange_ts,
            received_at=raw.received_at,
            reason=event_type,
            metadata={"kalshi_event_type": event_type},
        )
        return LifecycleEvent(event_id=_event_id(raw, msg), lifecycle=lifecycle, provenance=_provenance(raw))


def kalshi_normalizers() -> dict[NormalizerKey, NormalizeFn]:
    """Return a fresh handler set with isolated orderbook-delta state."""

    return KalshiNormalizer().handlers()


def _message(raw: EventEnvelope) -> dict[str, Any]:
    payload = dict(raw.payload)
    return _as_mapping(payload.get("msg", payload))


def _as_mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"expected object payload, got {type(value).__name__}")
    return dict(value)


def _market_ticker(msg: Mapping[str, Any]) -> str:
    ticker = msg.get("market_ticker") or msg.get("ticker") or msg.get("market_id")
    if not isinstance(ticker, str) or not ticker:
        raise ValueError("Kalshi payload missing market_ticker")
    return ticker


def _instrument(msg: Mapping[str, Any]) -> InstrumentId:
    return InstrumentId(venue=Venue.KALSHI, market_id=_market_ticker(msg))


def _event_id(raw: EventEnvelope, msg: Mapping[str, Any]) -> EventId:
    explicit = msg.get("event_id") or msg.get("trade_id")
    if isinstance(explicit, str) and explicit:
        return EventId(f"{raw.source}:{raw.channel}:{explicit}")
    sequence = raw.metadata.get("source_sequence")
    if sequence is not None:
        return EventId(f"{raw.source}:{raw.channel}:{_market_ticker(msg)}:{sequence}")
    return EventId(f"{raw.source}:{raw.channel}:{_market_ticker(msg)}:{raw.received_at.isoformat()}")


def _event_id_with_suffix(raw: EventEnvelope, msg: Mapping[str, Any], suffix: str) -> EventId:
    return EventId(f"{_event_id(raw, msg)}:{suffix}")


def _provenance(
    raw: EventEnvelope,
    *,
    extra_metadata: Mapping[str, Any] | None = None,
) -> EventProvenance:
    metadata: dict[str, Any] = {"ws_type": str(raw.metadata.get("ws_type", ""))} if "ws_type" in raw.metadata else {}
    if extra_metadata:
        metadata.update(extra_metadata)
    return EventProvenance(
        source=raw.source,
        channel=raw.channel,
        schema_version=raw.schema_version,
        venue=raw.venue,
        source_sequence=str(raw.metadata["source_sequence"]) if "source_sequence" in raw.metadata else None,
        normalization_version="kalshi-v1",
        metadata=metadata,
    )


def _levels(raw_levels: Any) -> tuple[OrderBookLevel, ...]:
    if raw_levels is None:
        return ()
    levels: list[OrderBookLevel] = []
    for raw_level in raw_levels:
        if not isinstance(raw_level, Sequence) or len(raw_level) < 2:
            continue
        levels.append(OrderBookLevel(price=Decimal(str(raw_level[0])), quantity=Decimal(str(raw_level[1]))))
    return tuple(levels)


def _ladder_dict(levels: Sequence[OrderBookLevel]) -> dict[Decimal, Decimal]:
    return {level.price: level.quantity for level in levels}


def _dict_levels(levels: Mapping[Decimal, Decimal], *, descending: bool) -> tuple[OrderBookLevel, ...]:
    return tuple(
        OrderBookLevel(price=price, quantity=levels[price])
        for price in sorted((price for price, qty in levels.items() if qty > 0), reverse=descending)
    )


def _book_from_ladders(
    *,
    instrument_id: InstrumentId,
    yes_bids: tuple[OrderBookLevel, ...],
    no_bids: tuple[OrderBookLevel, ...],
    exchange_ts: datetime | None,
    received_at: datetime,
) -> OrderBook:
    sorted_yes_bids = _sort_levels(yes_bids, descending=True)
    sorted_no_bids = _sort_levels(no_bids, descending=True)
    return OrderBook(
        instrument_id=instrument_id,
        yes_bids=sorted_yes_bids,
        yes_asks=_inverted_ask_levels(sorted_no_bids),
        no_bids=sorted_no_bids,
        no_asks=_inverted_ask_levels(sorted_yes_bids),
        exchange_ts=exchange_ts,
        received_at=received_at,
    )


def _optional_decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


def _invert_probability(value: Decimal | None) -> Decimal | None:
    if value is None:
        return None
    return Decimal("1") - value


def _level(price: Decimal | None, quantity: Decimal | None) -> OrderBookLevel | None:
    if price is None:
        return None
    return OrderBookLevel(price=price, quantity=quantity or Decimal("1"))


def _sort_levels(levels: Sequence[OrderBookLevel], *, descending: bool) -> tuple[OrderBookLevel, ...]:
    return tuple(sorted(levels, key=lambda level: level.price, reverse=descending))


def _inverted_ask_levels(bids: Sequence[OrderBookLevel]) -> tuple[OrderBookLevel, ...]:
    levels = tuple(
        OrderBookLevel(price=Decimal("1") - level.price, quantity=level.quantity)
        for level in bids
        if Decimal("0") <= Decimal("1") - level.price <= Decimal("1")
    )
    return _sort_levels(levels, descending=False)


def _trade_price(
    *,
    side: OutcomeSide,
    yes_price: object,
    no_price: object,
    fallback: object,
) -> Decimal:
    if side is OutcomeSide.NO:
        if no_price is not None:
            return Decimal(str(no_price))
        if yes_price is not None:
            return Decimal("1") - Decimal(str(yes_price))
    if yes_price is not None:
        return Decimal(str(yes_price))
    if no_price is not None:
        return Decimal("1") - Decimal(str(no_price))
    return Decimal(str(fallback))


def _message_time(message: Mapping[str, Any]) -> datetime | None:
    ts_ms = message.get("ts_ms")
    if isinstance(ts_ms, int | float):
        return datetime.fromtimestamp(float(ts_ms) / 1000, tz=UTC)
    ts = message.get("ts") or message.get("open_ts")
    if isinstance(ts, int | float):
        return datetime.fromtimestamp(float(ts), tz=UTC)
    raw_time = message.get("time")
    if isinstance(raw_time, str) and raw_time:
        normalized = f"{raw_time[:-1]}+00:00" if raw_time.endswith("Z") else raw_time
        return datetime.fromisoformat(normalized)
    return None


def _lifecycle_kind(event_type: str) -> MarketLifecycleKind:
    mapping = {
        "created": MarketLifecycleKind.LISTED,
        "activated": MarketLifecycleKind.OPENED,
        "deactivated": MarketLifecycleKind.PAUSED,
        "close_date_updated": MarketLifecycleKind.METADATA_UPDATED,
        "determined": MarketLifecycleKind.DETERMINED,
        "settled": MarketLifecycleKind.FINALIZED,
        "metadata_updated": MarketLifecycleKind.METADATA_UPDATED,
    }
    return mapping.get(event_type, MarketLifecycleKind.LISTED)
