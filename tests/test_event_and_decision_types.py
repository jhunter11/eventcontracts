"""Type-level coverage for the event and decision sum types."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from eventcontracts.domain import (
    Alert,
    AlertSeverity,
    CancelOrder,
    ClientOrderId,
    EventId,
    InstrumentId,
    ExecutionPriority,
    LatencyTier,
    Liquidity,
    MarketLifecycleEvent,
    MarketLifecycleKind,
    NoAction,
    OrderBook,
    OrderBookEvent,
    OrderBookLevel,
    OrderSide,
    OrderType,
    OutcomeSide,
    PlaceOrder,
    Quote,
    QuoteEvent,
    SettlementEvent,
    SettlementResolvedEvent,
    TimeInForce,
    TimerEvent,
    Trade,
    TradeEvent,
    Venue,
    decision_kind,
    decision_priority,
    event_kind,
)


def _instrument() -> InstrumentId:
    return InstrumentId(venue=Venue.KALSHI, market_id="DEMO")


def test_event_kind_covers_every_variant() -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    inst = _instrument()
    cases = {
        QuoteEvent(
            event_id=EventId("q"),
            quote=Quote(
                instrument_id=inst,
                side=OutcomeSide.YES,
                bid=None,
                ask=None,
                exchange_ts=None,
                received_at=now,
            ),
        ): "quote",
        TradeEvent(
            event_id=EventId("t"),
            trade=Trade(
                instrument_id=inst,
                side=None,
                price=Decimal("0.5"),
                quantity=Decimal("1"),
                trade_id=None,
                exchange_ts=None,
                received_at=now,
            ),
        ): "trade",
        OrderBookEvent(
            event_id=EventId("b"),
            book=OrderBook(
                instrument_id=inst,
                yes_bids=(OrderBookLevel(price=Decimal("0.4"), quantity=Decimal("10")),),
                yes_asks=(),
                no_bids=(),
                no_asks=(),
                exchange_ts=None,
                received_at=now,
            ),
        ): "book",
        SettlementResolvedEvent(
            event_id=EventId("s"),
            settlement=SettlementEvent(
                instrument_id=inst,
                resolved_side=OutcomeSide.YES,
                payout_per_contract=Decimal("1"),
                currency="USD",
                settled_at=now,
                source="venue",
            ),
        ): "settlement",
        TimerEvent(event_id=EventId("tk"), timestamp=now, label="1s"): "timer",
    }
    for event, expected in cases.items():
        assert event_kind(event) == expected


def test_lifecycle_event_kind() -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    lifecycle = MarketLifecycleEvent(
        instrument_id=_instrument(),
        kind=MarketLifecycleKind.PAUSED,
        exchange_ts=None,
        received_at=now,
    )
    from eventcontracts.domain import LifecycleEvent

    event = LifecycleEvent(event_id=EventId("l"), lifecycle=lifecycle)
    assert event_kind(event) == "lifecycle"


def test_decision_kind_covers_every_variant() -> None:
    priority = ExecutionPriority(
        tier=LatencyTier.FAST,
        max_delay_ms=100,
        expires_after_ms=250,
        allow_rate_limit_borrow=True,
        reason="crypto lead-lag",
    )
    place = PlaceOrder(
        client_order_id=ClientOrderId("c"),
        instrument_id=_instrument(),
        outcome_side=OutcomeSide.YES,
        order_side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.GTC,
        quantity=Decimal("1"),
        price=Decimal("0.5"),
        priority=priority,
    )
    cancel = CancelOrder(client_order_id=ClientOrderId("c"))
    alert = Alert(severity=AlertSeverity.INFO, message="ok")
    no_action = NoAction()

    assert decision_kind(place) == "place_order"
    assert decision_kind(cancel) == "cancel_order"
    assert decision_kind(alert) == "alert"
    assert decision_kind(no_action) == "no_action"
    assert decision_priority(place) == priority
    assert decision_priority(cancel).tier is LatencyTier.STANDARD


def test_liquidity_enum_round_trips() -> None:
    assert Liquidity("maker") is Liquidity.MAKER
    assert Liquidity.TAKER.value == "taker"
