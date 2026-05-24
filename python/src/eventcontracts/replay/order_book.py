"""Order book reconstruction for replay.

When the only book events on disk are quote snapshots and trade prints,
strategies that want a full depth-of-book view need the engine to
reconstruct it. This module maintains a running :class:`OrderBook` per
instrument by applying inbound events in arrival order.

Two consumers benefit:

1. The paper executor, which uses the latest book to walk taker orders
   and to estimate passive queue position. It already accepts
   :class:`OrderBookEvent` directly; the reconstructor lets a venue that
   only ships top-of-book quotes still drive the simulator.
2. Strategies that subscribe to depth even when the venue only publishes
   the top.

Logic is deterministic and side-effect-free aside from the cached state
inside the reconstructor.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from eventcontracts.domain.events import (
    NormalizedEvent,
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
)


@dataclass
class BookState:
    """Mutable bid/ask ladders keyed by side for one instrument."""

    yes_bids: dict[Decimal, Decimal] = field(default_factory=dict)
    yes_asks: dict[Decimal, Decimal] = field(default_factory=dict)
    no_bids: dict[Decimal, Decimal] = field(default_factory=dict)
    no_asks: dict[Decimal, Decimal] = field(default_factory=dict)

    def snapshot(
        self,
        instrument_id: InstrumentId,
        *,
        exchange_ts: datetime | None,
        received_at: datetime,
    ) -> OrderBook:
        def _levels(d: dict[Decimal, Decimal], descending: bool) -> tuple[OrderBookLevel, ...]:
            prices = sorted((p for p, q in d.items() if q > 0), reverse=descending)
            return tuple(OrderBookLevel(price=p, quantity=d[p]) for p in prices)

        return OrderBook(
            instrument_id=instrument_id,
            yes_bids=_levels(self.yes_bids, descending=True),
            yes_asks=_levels(self.yes_asks, descending=False),
            no_bids=_levels(self.no_bids, descending=True),
            no_asks=_levels(self.no_asks, descending=False),
            exchange_ts=exchange_ts,
            received_at=received_at,
        )


class OrderBookReconstructor:
    """Replay over normalized events, emitting an updated book on each tick.

    Usage::

        reconstructor = OrderBookReconstructor()
        for event in source.stream():
            yield event
            book = reconstructor.observe(event)
            if book is not None:
                yield book  # synthetic OrderBookEvent
    """

    def __init__(self) -> None:
        self._states: dict[InstrumentId, BookState] = {}
        self._sequence: int = 0

    def _state(self, instrument_id: InstrumentId) -> BookState:
        st = self._states.get(instrument_id)
        if st is None:
            st = BookState()
            self._states[instrument_id] = st
        return st

    def latest(self, instrument_id: InstrumentId) -> BookState | None:
        return self._states.get(instrument_id)

    def observe(self, event: NormalizedEvent) -> OrderBookEvent | None:
        """Update internal state; return a synthetic book event if the
        change affected the visible book.

        Order-book snapshots overwrite the ladders. Quote events update
        only the top level. Trade events decrement the corresponding
        level by trade size.
        """

        if isinstance(event, OrderBookEvent):
            book = event.book
            state = self._state(book.instrument_id)
            state.yes_bids = {lvl.price: lvl.quantity for lvl in book.yes_bids}
            state.yes_asks = {lvl.price: lvl.quantity for lvl in book.yes_asks}
            state.no_bids = {lvl.price: lvl.quantity for lvl in book.no_bids}
            state.no_asks = {lvl.price: lvl.quantity for lvl in book.no_asks}
            return None  # caller already has the snapshot

        if isinstance(event, QuoteEvent):
            quote = event.quote
            state = self._state(quote.instrument_id)
            ladder_bid = state.yes_bids if quote.side is OutcomeSide.YES else state.no_bids
            ladder_ask = state.yes_asks if quote.side is OutcomeSide.YES else state.no_asks
            if quote.bid is not None:
                ladder_bid[quote.bid.price] = quote.bid.quantity
            if quote.ask is not None:
                ladder_ask[quote.ask.price] = quote.ask.quantity
            return self._emit(quote.instrument_id, quote.exchange_ts, quote.received_at)

        if isinstance(event, TradeEvent):
            trade = event.trade
            state = self._state(trade.instrument_id)
            # A trade consumes liquidity at its price. We do not always
            # know which side was lifted, so debit both sides if present.
            if trade.aggressor_side is OutcomeSide.YES:
                ladder = state.yes_asks
            elif trade.aggressor_side is OutcomeSide.NO:
                ladder = state.yes_bids
            else:
                # Without aggressor info, debit whichever ladder has the price.
                if trade.price in state.yes_bids:
                    ladder = state.yes_bids
                elif trade.price in state.yes_asks:
                    ladder = state.yes_asks
                else:
                    return None
            if trade.price in ladder:
                remaining = ladder[trade.price] - trade.quantity
                if remaining <= 0:
                    del ladder[trade.price]
                else:
                    ladder[trade.price] = remaining
            return self._emit(trade.instrument_id, trade.exchange_ts, trade.received_at)

        return None

    def _emit(
        self,
        instrument_id: InstrumentId,
        exchange_ts: datetime | None,
        received_at: datetime,
    ) -> OrderBookEvent:
        self._sequence += 1
        snapshot = self._state(instrument_id).snapshot(
            instrument_id,
            exchange_ts=exchange_ts,
            received_at=received_at,
        )
        return OrderBookEvent(
            event_id=EventId(f"reconstructed-{self._sequence:09d}"),
            book=snapshot,
        )


def reconstruct_books(
    events: Iterator[NormalizedEvent],
) -> Iterator[NormalizedEvent]:
    """Yield events as-is, but interleave synthetic OrderBookEvents.

    The synthetic events show the book state after each quote/trade so
    downstream consumers (the paper simulator, depth-aware strategies)
    see a complete book at every tick.
    """

    reconstructor = OrderBookReconstructor()
    for event in events:
        yield event
        synthetic = reconstructor.observe(event)
        if synthetic is not None:
            yield synthetic
