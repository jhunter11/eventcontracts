"""Adversarial point-in-time leakage tests for ``DeterministicFeatureBuilder``.

The single property under test: at every vector emitted with timestamp
``as_of = T``, the builder must only have consumed events whose
event-time is ``<= T``. Out-of-order delivery must fail loudly.

We construct a deterministic synthetic stream, run a reference builder on
the sorted stream as the baseline, then permute the order (shuffle and
reverse) and assert the builder rejects future-leaking inputs.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from eventcontracts.domain.events import (
    EventProvenance,
    OrderBookEvent,
    QuoteEvent,
    TradeEvent,
)
from eventcontracts.domain.ids import EventId, FeatureSchemaId
from eventcontracts.domain.models import (
    InstrumentId,
    OrderBook,
    OrderBookLevel,
    OutcomeSide,
    Quote,
    Trade,
    Venue,
)
from eventcontracts.features import (
    FeatureLeakageError,
    RollingMidVwapImbalanceBuilder,
)

INSTRUMENT = InstrumentId(venue=Venue.KALSHI, market_id="M-1", outcome_id=None)
SCHEMA_ID = FeatureSchemaId("ref_rolling_mid_vwap_imbalance")
T0 = datetime(2026, 5, 25, 14, 0, tzinfo=UTC)


def _make_stream() -> list[QuoteEvent | TradeEvent | OrderBookEvent]:
    """A 60-event stream of quotes, books, and trades on one instrument."""

    events: list[QuoteEvent | TradeEvent | OrderBookEvent] = []
    for i in range(60):
        ts = T0 + timedelta(seconds=i)
        bid_price = Decimal("0.50") + Decimal("0.0001") * i
        ask_price = bid_price + Decimal("0.01")
        if i % 5 == 0:
            events.append(
                OrderBookEvent(
                    event_id=EventId(f"book-{i:03d}"),
                    book=OrderBook(
                        instrument_id=INSTRUMENT,
                        yes_bids=(OrderBookLevel(price=bid_price, quantity=Decimal("100")),),
                        yes_asks=(OrderBookLevel(price=ask_price, quantity=Decimal("80")),),
                        no_bids=(),
                        no_asks=(),
                        exchange_ts=ts,
                        received_at=ts,
                    ),
                    provenance=EventProvenance(source="fixture", channel="book"),
                )
            )
        if i % 3 == 0:
            events.append(
                QuoteEvent(
                    event_id=EventId(f"quote-{i:03d}"),
                    quote=Quote(
                        instrument_id=INSTRUMENT,
                        side=OutcomeSide.YES,
                        bid=OrderBookLevel(price=bid_price, quantity=Decimal("90")),
                        ask=OrderBookLevel(price=ask_price, quantity=Decimal("70")),
                        exchange_ts=ts,
                        received_at=ts,
                    ),
                    provenance=EventProvenance(source="fixture", channel="quote"),
                )
            )
        events.append(
            TradeEvent(
                event_id=EventId(f"trade-{i:03d}"),
                trade=Trade(
                    instrument_id=INSTRUMENT,
                    side=OutcomeSide.YES,
                    price=ask_price,
                    quantity=Decimal("1"),
                    trade_id=f"tv-{i}",
                    exchange_ts=ts,
                    received_at=ts,
                ),
                provenance=EventProvenance(source="fixture", channel="trade"),
            )
        )
    return events


def _builder() -> RollingMidVwapImbalanceBuilder:
    return RollingMidVwapImbalanceBuilder(
        schema_id=SCHEMA_ID,
        window_seconds=10,
        ewma_half_life_seconds=3,
        instrument_id=INSTRUMENT,
    )


def test_sorted_stream_emits_in_order() -> None:
    builder = _builder()
    vectors = builder.build_offline(_make_stream())
    timestamps = [v.timestamp for v in vectors]
    assert timestamps == sorted(timestamps)
    # Each emitted vector's as_of must equal an event time, never the future.
    assert all(v.timestamp <= T0 + timedelta(seconds=60) for v in vectors)


def test_shuffled_stream_is_rejected_loudly() -> None:
    stream = _make_stream()
    rng = random.Random(42)
    rng.shuffle(stream)
    # The shuffled stream is almost certainly not monotonic in event-time;
    # the builder must raise the moment it sees a regression.
    builder = _builder()
    with pytest.raises(FeatureLeakageError):
        builder.build_offline(stream)


def test_reversed_stream_is_rejected_loudly() -> None:
    builder = _builder()
    with pytest.raises(FeatureLeakageError):
        builder.build_offline(list(reversed(_make_stream())))


def test_two_runs_on_same_stream_produce_identical_vectors() -> None:
    stream = _make_stream()
    vectors_a = _builder().build_offline(stream)
    vectors_b = _builder().build_offline(stream)
    assert len(vectors_a) == len(vectors_b)
    for a, b in zip(vectors_a, vectors_b, strict=True):
        assert a == b


def test_builder_uses_only_past_events_for_each_vector() -> None:
    """The strongest direct assertion: every event consumed before vector V
    has event-time <= V.timestamp."""

    builder = _builder()
    state = builder.warmup(())
    stream = _make_stream()
    consumed_so_far: list[datetime] = []
    for event in stream:
        state = builder.update(state, event)
        ts = _event_time(event)
        if ts is not None:
            consumed_so_far.append(ts)
        if state.vector is not None:
            assert all(t <= state.vector.timestamp for t in consumed_so_far), (
                f"Vector at {state.vector.timestamp.isoformat()} saw an event "
                f"from {max(consumed_so_far).isoformat()}"
            )


def _event_time(event: QuoteEvent | TradeEvent | OrderBookEvent) -> datetime | None:
    if isinstance(event, QuoteEvent):
        return event.quote.exchange_ts or event.quote.received_at
    if isinstance(event, TradeEvent):
        return event.trade.exchange_ts or event.trade.received_at
    if isinstance(event, OrderBookEvent):
        return event.book.exchange_ts or event.book.received_at
    return None
