"""Contract hardening coverage for domain immutability and validation."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from eventcontracts.domain import (
    EventId,
    EventProvenance,
    InstrumentId,
    OutcomeSide,
    Trade,
    TradeEvent,
    Venue,
    canonical_sha256,
    to_canonical_json,
)
from eventcontracts.domain.decisions import PlaceOrder
from eventcontracts.domain.orders import OrderSide, OrderType, TimeInForce


def _instrument() -> InstrumentId:
    return InstrumentId(venue=Venue.KALSHI, market_id="KXDEMO")


def test_domain_metadata_is_frozen_and_hashable() -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    event = TradeEvent(
        event_id=EventId("event-1"),
        trade=Trade(
            instrument_id=_instrument(),
            side=OutcomeSide.YES,
            price=Decimal("0.42"),
            quantity=Decimal("2"),
            trade_id="trade-1",
            exchange_ts=now,
            received_at=now,
            metadata={"nested": {"venue_sequence": "10"}},
        ),
        provenance=EventProvenance(
            source="kalshi",
            channel="trades",
            venue=Venue.KALSHI,
            source_sequence="10",
        ),
    )

    assert event.trade.metadata["nested"]["venue_sequence"] == "10"
    assert hash(event)
    assert {event: "trade"}[event] == "trade"


def test_trade_rejects_invalid_price_and_naive_time() -> None:
    with pytest.raises(ValueError, match="price"):
        Trade(
            instrument_id=_instrument(),
            side=OutcomeSide.YES,
            price=Decimal("1.01"),
            quantity=Decimal("1"),
            trade_id=None,
            exchange_ts=None,
            received_at=datetime(2026, 1, 1),
        )


def test_order_decision_requires_limit_price() -> None:
    with pytest.raises(ValueError, match="price is required"):
        PlaceOrder(
            client_order_id="client-1",
            instrument_id=_instrument(),
            outcome_side=OutcomeSide.YES,
            order_side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            time_in_force=TimeInForce.GTC,
            quantity=Decimal("1"),
        )


def test_canonical_serialization_is_stable() -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    event = TradeEvent(
        event_id=EventId("event-1"),
        trade=Trade(
            instrument_id=_instrument(),
            side=OutcomeSide.YES,
            price=Decimal("0.42"),
            quantity=Decimal("2"),
            trade_id="trade-1",
            exchange_ts=now,
            received_at=now,
            metadata={"b": 2, "a": 1},
        ),
    )

    assert to_canonical_json(event) == to_canonical_json(event)
    assert len(canonical_sha256(event)) == 64
