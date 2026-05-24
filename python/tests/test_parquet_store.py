"""Parquet-backed event store: round-trip, partitioning, determinism."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from eventcontracts.domain.events import EventProvenance, QuoteEvent, TradeEvent
from eventcontracts.domain.ids import EventId
from eventcontracts.domain.models import (
    InstrumentId,
    OrderBookLevel,
    OutcomeSide,
    Quote,
    Trade,
    Venue,
)
from eventcontracts.storage import EventEnvelope, ParquetEventStore


NOW = datetime(2026, 1, 15, 14, 30, tzinfo=timezone.utc)
INSTR = InstrumentId(venue=Venue.KALSHI, market_id="M-1", outcome_id=None)


def _envelope(channel: str, payload: dict, *, source: str = "kalshi-md", at: datetime = NOW) -> EventEnvelope:
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
