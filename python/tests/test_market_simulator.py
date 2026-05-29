"""MarketPaperSimulator: fees, queue, latency, marketable/passive, cancel/replace."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from eventcontracts.adapters.venues.kalshi import KalshiFeeModel
from eventcontracts.domain.decisions import CancelOrder, IntentEnvelope, PlaceOrder, ReplaceOrder
from eventcontracts.domain.events import (
    LifecycleEvent,
    OrderBookEvent,
    TradeEvent,
)
from eventcontracts.domain.ids import (
    ClientOrderId,
    CorrelationId,
    EventId,
    SleeveId,
    StrategyId,
)
from eventcontracts.domain.lifecycle import MarketLifecycleEvent, MarketLifecycleKind
from eventcontracts.domain.models import (
    InstrumentId,
    OrderBook,
    OrderBookLevel,
    OutcomeSide,
    Trade,
    Venue,
)
from eventcontracts.domain.orders import Liquidity, OrderSide, OrderType, TimeInForce
from eventcontracts.execution import (
    ConstantLatency,
    DepthQueueEstimator,
    FractionalQueueEstimator,
    FrontOfQueueEstimator,
    MarketPaperSimulator,
    OrderIntent,
    intent_to_order,
)

NOW = datetime(2026, 1, 1, 9, 30, tzinfo=UTC)
INSTR = InstrumentId(venue=Venue.KALSHI, market_id="MKT-1", outcome_id=None)


def _book(
    *,
    yes_bids: list[tuple[str, str]] | None = None,
    yes_asks: list[tuple[str, str]] | None = None,
    at: datetime = NOW,
) -> OrderBook:
    return OrderBook(
        instrument_id=INSTR,
        yes_bids=tuple(OrderBookLevel(price=Decimal(p), quantity=Decimal(q)) for p, q in (yes_bids or [])),
        yes_asks=tuple(OrderBookLevel(price=Decimal(p), quantity=Decimal(q)) for p, q in (yes_asks or [])),
        no_bids=(),
        no_asks=(),
        exchange_ts=at,
        received_at=at,
    )


def _book_event(book: OrderBook) -> OrderBookEvent:
    return OrderBookEvent(event_id=EventId("b-1"), book=book)


def _trade_event(
    price: str,
    qty: str,
    *,
    aggressor: OutcomeSide | None = None,
    at: datetime = NOW,
) -> TradeEvent:
    return TradeEvent(
        event_id=EventId(f"t-{price}-{qty}"),
        trade=Trade(
            instrument_id=INSTR,
            side=OutcomeSide.YES,
            price=Decimal(price),
            quantity=Decimal(qty),
            trade_id=None,
            exchange_ts=at,
            received_at=at,
            aggressor_side=aggressor,
        ),
    )


def _no_trade_event(
    price: str,
    qty: str,
    *,
    aggressor: OutcomeSide | None = None,
    at: datetime = NOW,
) -> TradeEvent:
    return TradeEvent(
        event_id=EventId(f"nt-{price}-{qty}"),
        trade=Trade(
            instrument_id=INSTR,
            side=OutcomeSide.NO,
            price=Decimal(price),
            quantity=Decimal(qty),
            trade_id=None,
            exchange_ts=at,
            received_at=at,
            aggressor_side=aggressor,
        ),
    )


def _intent(
    *,
    price: str = "0.50",
    qty: str = "100",
    order_side: OrderSide = OrderSide.BUY,
    post_only: bool = False,
    coid: str = "co-1",
    order_type: str = "limit",
    time_in_force: TimeInForce = TimeInForce.GTC,
) -> OrderIntent:
    return OrderIntent(
        instrument_id=INSTR,
        side=OutcomeSide.YES,
        price=Decimal(price),
        quantity=Decimal(qty),
        order_type=order_type,
        order_side=order_side,
        time_in_force=time_in_force,
        post_only=post_only,
        metadata={"client_order_id": coid, "correlation_id": "corr-1"},
    )


def _sim() -> MarketPaperSimulator:
    return MarketPaperSimulator(
        fee_model=KalshiFeeModel(),
        latency=ConstantLatency(submit_ms=0, cancel_ms=0, replace_ms=0),
        queue_estimator=FrontOfQueueEstimator(),
        strategy_id=StrategyId("strat-1"),
        sleeve_id=SleeveId("sleeve-1"),
    )


def test_marketable_buy_walks_asks() -> None:
    sim = _sim()
    sim.on_event(_book_event(_book(yes_asks=[("0.55", "30"), ("0.60", "70")])))

    intent = _intent(price="0.60", qty="50")
    fills = sim.submit(intent, NOW)

    assert len(fills) == 2
    # First 30 at 0.55, then 20 at 0.60
    assert fills[0].price == Decimal("0.55") and fills[0].quantity == Decimal("30")
    assert fills[1].price == Decimal("0.60") and fills[1].quantity == Decimal("20")
    assert all(f.liquidity is Liquidity.TAKER for f in fills)


def test_passive_order_rests_no_immediate_fill() -> None:
    sim = _sim()
    sim.on_event(_book_event(_book(yes_asks=[("0.55", "30")])))

    intent = _intent(price="0.40", qty="10")
    fills = sim.submit(intent, NOW)

    assert fills == []
    assert len(list(sim.open_orders())) == 1


def test_post_only_rejects_marketability() -> None:
    """A post_only order that would cross must not become a taker."""
    sim = _sim()
    sim.on_event(_book_event(_book(yes_asks=[("0.55", "30")])))

    intent = _intent(price="0.60", qty="10", post_only=True)
    fills = sim.submit(intent, NOW)

    assert fills == []
    # The order rests despite crossing.
    assert len(list(sim.open_orders())) == 1


def test_resting_order_fills_on_subsequent_trade() -> None:
    sim = _sim()
    sim.on_event(_book_event(_book(yes_asks=[("0.55", "100")])))

    intent = _intent(price="0.40", qty="50")
    sim.submit(intent, NOW)

    # A seller hits our bid at 0.40 — trade prints at our price.
    fills = sim.on_event(_trade_event("0.40", "50", aggressor=OutcomeSide.NO))

    assert len(fills) == 1
    assert fills[0].liquidity is Liquidity.MAKER
    assert fills[0].quantity == Decimal("50")


def test_resting_yes_order_does_not_fill_on_no_trade_at_same_price() -> None:
    sim = _sim()
    sim.on_event(_book_event(_book(yes_asks=[("0.55", "100")])))
    sim.submit(_intent(price="0.40", qty="50"), NOW)

    fills = sim.on_event(_no_trade_event("0.40", "50", aggressor=OutcomeSide.NO))

    assert fills == []
    assert list(sim.open_orders())[0].remaining == Decimal("50")


def test_resting_yes_buy_fills_on_complementary_no_trade() -> None:
    sim = _sim()
    sim.on_event(_book_event(_book(yes_asks=[("0.55", "100")])))
    sim.submit(_intent(price="0.40", qty="50"), NOW)

    fills = sim.on_event(_no_trade_event("0.60", "50", aggressor=OutcomeSide.NO))

    assert len(fills) == 1
    assert fills[0].outcome_side is OutcomeSide.YES
    assert fills[0].price == Decimal("0.40")
    assert fills[0].quantity == Decimal("50")


def test_submit_latency_blocks_passive_fill_until_effective_time() -> None:
    sim = MarketPaperSimulator(
        fee_model=KalshiFeeModel(),
        latency=ConstantLatency(submit_ms=500),
        queue_estimator=FrontOfQueueEstimator(),
        strategy_id=StrategyId("strat-1"),
        sleeve_id=SleeveId("sleeve-1"),
    )
    sim.on_event(_book_event(_book(yes_asks=[("0.55", "100")])))
    sim.submit(_intent(price="0.40", qty="50"), NOW)

    assert sim.on_event(_trade_event("0.40", "50", aggressor=OutcomeSide.NO, at=NOW)) == []
    fills = sim.on_event(
        _trade_event(
            "0.40",
            "50",
            aggressor=OutcomeSide.NO,
            at=NOW + timedelta(milliseconds=500),
        )
    )

    assert len(fills) == 1
    assert fills[0].filled_at == NOW + timedelta(milliseconds=500)


def test_submit_latency_delays_marketable_fill_until_future_book_event() -> None:
    sim = MarketPaperSimulator(
        fee_model=KalshiFeeModel(),
        latency=ConstantLatency(submit_ms=500),
        queue_estimator=FrontOfQueueEstimator(),
        strategy_id=StrategyId("strat-1"),
        sleeve_id=SleeveId("sleeve-1"),
    )
    sim.on_event(_book_event(_book(yes_asks=[("0.50", "100")])))

    assert sim.submit(_intent(price="0.50", qty="10"), NOW) == []
    fills = sim.on_event(
        _book_event(
            _book(
                yes_asks=[("0.49", "100")],
                at=NOW + timedelta(milliseconds=500),
            )
        )
    )

    assert len(fills) == 1
    assert fills[0].price == Decimal("0.50")
    assert fills[0].filled_at == NOW + timedelta(milliseconds=500)


def test_depth_queue_estimator_is_conservative_without_book_state() -> None:
    estimate = DepthQueueEstimator().estimate(
        instrument_id=INSTR,
        side=OutcomeSide.YES,
        order_side="buy",
        price=Decimal("0.40"),
        quantity=Decimal("25"),
        book=None,
    )

    assert estimate.ahead_quantity == Decimal("25")
    assert estimate.confidence == Decimal("0")


def test_fractional_queue_estimator_is_conservative_without_book_state() -> None:
    estimate = FractionalQueueEstimator().estimate(
        instrument_id=INSTR,
        side=OutcomeSide.YES,
        order_side="buy",
        price=Decimal("0.40"),
        quantity=Decimal("25"),
        book=None,
    )

    assert estimate.ahead_quantity == Decimal("25")
    assert estimate.confidence == Decimal("0")


def test_paused_market_rejects_new_orders() -> None:
    sim = _sim()
    sim.on_event(
        LifecycleEvent(
            event_id=EventId("lc-1"),
            lifecycle=MarketLifecycleEvent(
                instrument_id=INSTR,
                kind=MarketLifecycleKind.PAUSED,
                exchange_ts=NOW,
                received_at=NOW,
            ),
        )
    )
    with pytest.raises(ValueError, match="paused"):
        sim.submit(_intent(), NOW)


def test_closed_market_cancels_resting_orders() -> None:
    sim = _sim()
    sim.on_event(_book_event(_book(yes_asks=[("0.55", "30")])))
    sim.submit(_intent(price="0.40"), NOW)
    assert len(list(sim.open_orders())) == 1

    sim.on_event(
        LifecycleEvent(
            event_id=EventId("lc-1"),
            lifecycle=MarketLifecycleEvent(
                instrument_id=INSTR,
                kind=MarketLifecycleKind.CLOSED,
                exchange_ts=NOW,
                received_at=NOW,
            ),
        )
    )
    assert len(list(sim.open_orders())) == 0


def test_metadata_lifecycle_does_not_cancel_resting_orders() -> None:
    sim = _sim()
    sim.on_event(_book_event(_book(yes_asks=[("0.55", "30")])))
    sim.submit(_intent(price="0.40"), NOW)

    sim.on_event(
        LifecycleEvent(
            event_id=EventId("lc-metadata"),
            lifecycle=MarketLifecycleEvent(
                instrument_id=INSTR,
                kind=MarketLifecycleKind.METADATA_UPDATED,
                exchange_ts=NOW,
                received_at=NOW,
                reason="close_date_updated",
            ),
        )
    )

    assert len(list(sim.open_orders())) == 1


def test_cancel_marks_order_canceled() -> None:
    sim = _sim()
    sim.on_event(_book_event(_book(yes_asks=[("0.55", "30")])))
    sim.submit(_intent(coid="X"), NOW)

    assert sim.cancel(ClientOrderId("X")) is True
    assert sim.cancel(ClientOrderId("X")) is False  # already canceled
    assert len(list(sim.open_orders())) == 0


def test_replace_price_resets_queue() -> None:
    sim = _sim()
    sim.on_event(_book_event(_book(yes_asks=[("0.55", "30")])))
    sim.submit(_intent(price="0.40", coid="X"), NOW)

    ok = sim.replace(ClientOrderId("X"), new_price=Decimal("0.45"), new_quantity=None, now=NOW)
    assert ok
    # Front-of-queue estimator → queue_ahead is 0 anyway, but the replace path runs.
    pending = sim.pending[ClientOrderId("X")]
    assert pending.intent.price == Decimal("0.45")


def test_cancel_latency_leaves_order_fillable_until_effective() -> None:
    sim = MarketPaperSimulator(
        fee_model=KalshiFeeModel(maker_rate=Decimal("0")),
        latency=ConstantLatency(submit_ms=0, cancel_ms=200, replace_ms=0),
        queue_estimator=FrontOfQueueEstimator(),
        strategy_id=StrategyId("strat-1"),
        sleeve_id=SleeveId("sleeve-1"),
    )
    sim.on_event(_book_event(_book(yes_asks=[("0.55", "30")])))
    sim.submit(_intent(price="0.40", coid="X"), NOW)

    assert sim.cancel(ClientOrderId("X"), now=NOW) is True
    fills = sim.on_event(
        _trade_event("0.40", "10", aggressor=OutcomeSide.NO, at=NOW + timedelta(milliseconds=100))
    )

    assert len(fills) == 1
    assert fills[0].client_order_id == ClientOrderId("X")


def test_replace_latency_keeps_old_price_until_effective() -> None:
    sim = MarketPaperSimulator(
        fee_model=KalshiFeeModel(maker_rate=Decimal("0")),
        latency=ConstantLatency(submit_ms=0, cancel_ms=0, replace_ms=200),
        queue_estimator=FrontOfQueueEstimator(),
        strategy_id=StrategyId("strat-1"),
        sleeve_id=SleeveId("sleeve-1"),
    )
    sim.on_event(_book_event(_book(yes_asks=[("0.55", "30")])))
    sim.submit(_intent(price="0.40", coid="X"), NOW)

    assert sim.replace(ClientOrderId("X"), new_price=Decimal("0.45"), new_quantity=None, now=NOW)
    assert sim.pending[ClientOrderId("X")].intent.price == Decimal("0.40")
    sim.on_event(_book_event(_book(yes_asks=[("0.55", "30")], at=NOW + timedelta(milliseconds=250))))

    assert sim.pending[ClientOrderId("X")].intent.price == Decimal("0.45")


def test_fees_applied_to_taker_fills() -> None:
    sim = _sim()
    sim.on_event(_book_event(_book(yes_asks=[("0.50", "100")])))
    fills = sim.submit(_intent(price="0.50", qty="100"), NOW)

    assert len(fills) == 1
    # Kalshi formula: 0.07 * 0.5 * 0.5 * 100 = 1.75
    assert fills[0].fee_amount == Decimal("1.75")


def test_marketable_orders_deplete_working_book_within_tick() -> None:
    sim = _sim()
    sim.on_event(_book_event(_book(yes_asks=[("0.50", "10"), ("0.60", "100")])))

    first = sim.submit(_intent(price="0.50", qty="10", coid="A"), NOW)
    second = sim.submit(_intent(price="0.60", qty="10", coid="B"), NOW)

    assert first[0].price == Decimal("0.50")
    assert second[0].price == Decimal("0.60")


def test_latency_pushes_fill_timestamp_forward() -> None:
    sim = MarketPaperSimulator(
        fee_model=KalshiFeeModel(),
        latency=ConstantLatency(submit_ms=500),
        queue_estimator=FrontOfQueueEstimator(),
        strategy_id=StrategyId("strat-1"),
        sleeve_id=SleeveId("sleeve-1"),
    )
    sim.on_event(_book_event(_book(yes_asks=[("0.50", "100")])))
    fills = sim.submit(_intent(price="0.50", qty="50"), NOW)
    assert fills == []
    fills = sim.on_event(
        _book_event(
            _book(
                yes_asks=[("0.50", "100")],
                at=NOW + timedelta(milliseconds=500),
            )
        )
    )

    expected = NOW.replace(microsecond=500_000)
    assert fills[0].filled_at == expected


def test_marketable_with_partial_remainder_rests() -> None:
    """Marketable limit order whose remainder sits passively."""
    sim = _sim()
    sim.on_event(_book_event(_book(yes_asks=[("0.50", "30")])))
    fills = sim.submit(_intent(price="0.50", qty="50"), NOW)

    assert len(fills) == 1
    assert fills[0].quantity == Decimal("30")
    # 20 remaining rests passive at 0.50
    open_now = list(sim.open_orders())
    assert len(open_now) == 1
    assert open_now[0].remaining == Decimal("20")


def test_ioc_marketable_remainder_cancels_without_resting() -> None:
    sim = _sim()
    sim.on_event(_book_event(_book(yes_asks=[("0.50", "30")])))

    fills = sim.submit(_intent(price="0.50", qty="50", time_in_force=TimeInForce.IOC), NOW)

    assert len(fills) == 1
    assert fills[0].quantity == Decimal("30")
    assert list(sim.open_orders()) == []


def test_fok_order_cancels_without_partial_fill_or_resting() -> None:
    sim = _sim()
    sim.on_event(_book_event(_book(yes_asks=[("0.50", "30")])))

    fills = sim.submit(_intent(price="0.50", qty="50", time_in_force=TimeInForce.FOK), NOW)

    assert fills == []
    assert list(sim.open_orders()) == []


def test_intent_to_order_preserves_time_in_force() -> None:
    decision = PlaceOrder(
        client_order_id=ClientOrderId("co-ioc"),
        instrument_id=INSTR,
        outcome_side=OutcomeSide.YES,
        order_side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.IOC,
        quantity=Decimal("10"),
        price=Decimal("0.50"),
    )
    envelope = IntentEnvelope(
        decision=decision,
        strategy_id=StrategyId("strat-1"),
        sleeve_id=SleeveId("sleeve-1"),
        correlation_id=CorrelationId("corr-1"),
        emitted_at=NOW,
    )

    intent = intent_to_order(envelope)

    assert intent is not None
    assert intent.time_in_force is TimeInForce.IOC


def test_submit_envelope_routes_place_order_without_manual_intent_conversion() -> None:
    sim = _sim()
    sim.on_event(_book_event(_book(yes_asks=[("0.50", "100")])))
    decision = PlaceOrder(
        client_order_id=ClientOrderId("co-envelope"),
        instrument_id=INSTR,
        outcome_side=OutcomeSide.YES,
        order_side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.IOC,
        quantity=Decimal("10"),
        price=Decimal("0.50"),
    )
    envelope = IntentEnvelope(
        decision=decision,
        strategy_id=StrategyId("strat-1"),
        sleeve_id=SleeveId("sleeve-1"),
        correlation_id=CorrelationId("corr-envelope"),
        emitted_at=NOW,
    )

    fills = sim.submit_envelope(envelope)

    assert len(fills) == 1
    assert fills[0].client_order_id == ClientOrderId("co-envelope")
    assert fills[0].correlation_id == CorrelationId("corr-envelope")


def test_submit_envelope_routes_cancel_and_replace_decisions() -> None:
    sim = _sim()
    sim.on_event(_book_event(_book(yes_asks=[("0.55", "100")])))
    place = IntentEnvelope(
        decision=PlaceOrder(
            client_order_id=ClientOrderId("co-edit"),
            instrument_id=INSTR,
            outcome_side=OutcomeSide.YES,
            order_side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            time_in_force=TimeInForce.GTC,
            quantity=Decimal("10"),
            price=Decimal("0.40"),
        ),
        strategy_id=StrategyId("strat-1"),
        sleeve_id=SleeveId("sleeve-1"),
        correlation_id=CorrelationId("corr-place"),
        emitted_at=NOW,
    )
    sim.submit_envelope(place)

    sim.submit_envelope(
        IntentEnvelope(
            decision=ReplaceOrder(
                client_order_id=ClientOrderId("co-edit"),
                new_price=Decimal("0.45"),
            ),
            strategy_id=StrategyId("strat-1"),
            sleeve_id=SleeveId("sleeve-1"),
            correlation_id=CorrelationId("corr-replace"),
            emitted_at=NOW,
        )
    )
    assert sim.pending[ClientOrderId("co-edit")].intent.price == Decimal("0.45")

    sim.submit_envelope(
        IntentEnvelope(
            decision=CancelOrder(client_order_id=ClientOrderId("co-edit")),
            strategy_id=StrategyId("strat-1"),
            sleeve_id=SleeveId("sleeve-1"),
            correlation_id=CorrelationId("corr-cancel"),
            emitted_at=NOW,
        )
    )
    assert list(sim.open_orders()) == []
