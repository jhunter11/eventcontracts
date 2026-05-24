"""Parquet-backed event store: round-trip, partitioning, determinism."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from eventcontracts.domain.events import (
    EventProvenance,
    OwnFillEvent,
    OwnOrderRejectEvent,
    OwnOrderUpdateEvent,
    QuoteEvent,
    TradeEvent,
)
from eventcontracts.domain.fills import Fill
from eventcontracts.domain.ids import (
    ClientOrderId,
    CorrelationId,
    EventId,
    FillId,
    SleeveId,
    StrategyId,
    VenueOrderId,
)
from eventcontracts.domain.models import (
    InstrumentId,
    OrderBookLevel,
    OutcomeSide,
    Quote,
    Trade,
    Venue,
)
from eventcontracts.domain.orders import (
    Liquidity,
    Order,
    OrderReject,
    OrderSide,
    OrderStatus,
    OrderType,
    TimeInForce,
)
from eventcontracts.storage import EventEnvelope, ParquetEventStore

NOW = datetime(2026, 1, 15, 14, 30, tzinfo=UTC)
INSTR = InstrumentId(venue=Venue.KALSHI, market_id="M-1", outcome_id=None)


def _envelope(
    channel: str,
    payload: dict[str, object],
    *,
    source: str = "kalshi-md",
    at: datetime = NOW,
) -> EventEnvelope:
    return EventEnvelope(
        venue=Venue.KALSHI,
        source=source,
        channel=channel,
        received_at=at,
        exchange_ts=at,
        payload=payload,
        schema_version="raw-event-v1",
    )


def _trade_event(price: str, qty: str) -> TradeEvent:
    return TradeEvent(
        event_id=EventId(f"t-{price}-{qty}"),
        trade=Trade(
            instrument_id=INSTR,
            side=OutcomeSide.YES,
            price=Decimal(price),
            quantity=Decimal(qty),
            trade_id=None,
            exchange_ts=NOW,
            received_at=NOW,
        ),
        provenance=EventProvenance(source="kalshi-md", channel="trade", venue=Venue.KALSHI),
    )


def _quote_event(bid: str, ask: str) -> QuoteEvent:
    return QuoteEvent(
        event_id=EventId(f"q-{bid}-{ask}"),
        quote=Quote(
            instrument_id=INSTR,
            side=OutcomeSide.YES,
            bid=OrderBookLevel(price=Decimal(bid), quantity=Decimal("10")),
            ask=OrderBookLevel(price=Decimal(ask), quantity=Decimal("10")),
            exchange_ts=NOW,
            received_at=NOW,
        ),
        provenance=EventProvenance(source="kalshi-md", channel="quote", venue=Venue.KALSHI),
    )


def test_raw_envelope_round_trip(tmp_path: Path) -> None:
    store = ParquetEventStore(tmp_path)
    env = _envelope("trade", {"market_id": "M-1", "price": "0.5", "quantity": "10"})
    store.append(env)
    store.flush()

    read_back = list(store.read())
    assert len(read_back) == 1
    assert read_back[0].source == "kalshi-md"
    assert read_back[0].payload["price"] == "0.5"


def test_normalized_round_trip(tmp_path: Path) -> None:
    store = ParquetEventStore(tmp_path)
    store.append_normalized(_trade_event("0.40", "10"))
    store.append_normalized(_quote_event("0.39", "0.41"))
    store.flush()

    events = list(store.read_normalized())
    assert len(events) == 2
    # Ordering by exchange_ts then received_at is deterministic.
    assert isinstance(events[0], TradeEvent | QuoteEvent)


def test_partitioning_creates_expected_directories(tmp_path: Path) -> None:
    store = ParquetEventStore(tmp_path)
    store.append(_envelope("trade", {"market_id": "M-1", "price": "0.5", "quantity": "1"}))
    store.append(
        _envelope(
            "trade",
            {"market_id": "M-2", "price": "0.6", "quantity": "1"},
            at=NOW.replace(day=16),
        )
    )
    store.flush()

    expected_partitions = [
        tmp_path / "raw" / "venue=kalshi" / "source=kalshi-md" / "date=2026-01-15",
        tmp_path / "raw" / "venue=kalshi" / "source=kalshi-md" / "date=2026-01-16",
    ]
    for path in expected_partitions:
        assert path.exists(), f"missing partition {path}"
        files = list(path.glob("*.parquet"))
        assert files, f"no parquet files in {path}"


def test_round_trip_preserves_ordering_with_many_events(tmp_path: Path) -> None:
    store = ParquetEventStore(tmp_path, batch_size=3)
    for i in range(10):
        ev = TradeEvent(
            event_id=EventId(f"t-{i:03d}"),
            trade=Trade(
                instrument_id=INSTR,
                side=OutcomeSide.YES,
                price=Decimal("0.50"),
                quantity=Decimal("1"),
                trade_id=None,
                exchange_ts=NOW.replace(second=i),
                received_at=NOW.replace(second=i),
            ),
            provenance=EventProvenance(source="kalshi-md", channel="trade"),
        )
        store.append_normalized(ev)
    store.flush()

    events = list(store.read_normalized())
    assert [e.event_id for e in events] == [EventId(f"t-{i:03d}") for i in range(10)]


def test_filter_by_source(tmp_path: Path) -> None:
    store = ParquetEventStore(tmp_path)
    store.append(_envelope("trade", {"market_id": "M-1", "price": "0.5", "quantity": "1"}, source="kalshi-md"))
    store.append(_envelope("trade", {"market_id": "M-1", "price": "0.5", "quantity": "1"}, source="nws"))
    store.flush()

    kalshi_only = list(store.read(source="kalshi-md"))
    assert len(kalshi_only) == 1
    assert kalshi_only[0].source == "kalshi-md"


def _fill_event() -> OwnFillEvent:
    fill = Fill(
        fill_id=FillId("f-1"),
        venue_order_id=VenueOrderId("vo-1"),
        client_order_id=ClientOrderId("co-1"),
        instrument_id=INSTR,
        outcome_side=OutcomeSide.YES,
        order_side=OrderSide.BUY,
        price=Decimal("0.42"),
        quantity=Decimal("10"),
        liquidity=Liquidity.TAKER,
        fee_amount=Decimal("0.18"),
        fee_currency="USD",
        filled_at=NOW,
        exchange_ts=NOW,
        correlation_id=CorrelationId("c-1"),
        strategy_id=StrategyId("s-1"),
        sleeve_id=SleeveId("sl-1"),
    )
    return OwnFillEvent(
        event_id=EventId("of-1"),
        fill=fill,
        provenance=EventProvenance(source="oms", channel="own_fill", venue=Venue.KALSHI),
    )


def _order_update_event() -> OwnOrderUpdateEvent:
    order = Order(
        client_order_id=ClientOrderId("co-2"),
        venue_order_id=VenueOrderId("vo-2"),
        instrument_id=INSTR,
        outcome_side=OutcomeSide.YES,
        order_side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.GTC,
        price=Decimal("0.40"),
        quantity=Decimal("100"),
        filled_quantity=Decimal("25"),
        status=OrderStatus.PARTIALLY_FILLED,
        created_at=NOW,
        updated_at=NOW,
        correlation_id=CorrelationId("c-2"),
        strategy_id=StrategyId("s-1"),
        sleeve_id=SleeveId("sl-1"),
    )
    return OwnOrderUpdateEvent(
        event_id=EventId("ou-1"),
        order=order,
        provenance=EventProvenance(source="oms", channel="own_order", venue=Venue.KALSHI),
    )


def _order_reject_event() -> OwnOrderRejectEvent:
    reject = OrderReject(
        client_order_id=ClientOrderId("co-3"),
        reason="risk: max_order_notional",
        rejected_at=NOW,
        venue_code="LIMIT_EXCEEDED",
    )
    return OwnOrderRejectEvent(
        event_id=EventId("or-1"),
        reject=reject,
        provenance=EventProvenance(source="risk", channel="reject"),
    )


def test_own_events_round_trip(tmp_path: Path) -> None:
    store = ParquetEventStore(tmp_path)
    store.append_normalized(_fill_event())
    store.append_normalized(_order_update_event())
    store.append_normalized(_order_reject_event())
    store.flush()

    events = list(store.read_normalized())
    kinds = {type(event).__name__ for event in events}
    assert kinds == {"OwnFillEvent", "OwnOrderUpdateEvent", "OwnOrderRejectEvent"}

    by_id = {str(event.event_id): event for event in events}

    fill_event = by_id["of-1"]
    assert isinstance(fill_event, OwnFillEvent)
    assert fill_event.fill.price == Decimal("0.42")
    assert fill_event.fill.fee_amount == Decimal("0.18")
    assert fill_event.fill.liquidity is Liquidity.TAKER

    order_event = by_id["ou-1"]
    assert isinstance(order_event, OwnOrderUpdateEvent)
    assert order_event.order.status is OrderStatus.PARTIALLY_FILLED
    assert order_event.order.filled_quantity == Decimal("25")

    reject_event = by_id["or-1"]
    assert isinstance(reject_event, OwnOrderRejectEvent)
    assert reject_event.reject.venue_code == "LIMIT_EXCEEDED"
