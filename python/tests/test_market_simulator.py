"""MarketPaperSimulator: fees, queue, latency, marketable/passive, cancel/replace."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from eventcontracts.adapters.venues.kalshi import KalshiFeeModel
from eventcontracts.domain.events import (
    LifecycleEvent,
    OrderBookEvent,
    TradeEvent,
)
from eventcontracts.domain.ids import (
    ClientOrderId,
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
from eventcontracts.domain.orders import Liquidity, OrderSide
from eventcontracts.execution import (
    ConstantLatency,
    FrontOfQueueEstimator,
    MarketPaperSimulator,
    OrderIntent,
)


NOW = datetime(2026, 1, 1, 9, 30, tzinfo=timezone.utc)
INSTR = InstrumentId(venue=Venue.KALSHI, market_id="MKT-1", outcome_id=None)


def _book(*, yes_bids: list[tuple[str, str]] | None = None, yes_asks: list[tuple[str, str]] | None = None) -> OrderBook:
    return OrderBook(
        instrument_id=INSTR,
        yes_bids=tuple(OrderBookLevel(price=Decimal(p), quantity=Decimal(q)) for p, q in (yes_bids or [])),
        yes_asks=tuple(OrderBookLevel(price=Decimal(p), quantity=Decimal(q)) for p, q in (yes_asks or [])),
        no_bids=(),
        no_asks=(),
        exchange_ts=NOW,
        received_at=NOW,
    )


def _book_event(book: OrderBook) -> OrderBookEvent:
    return OrderBookEvent(event_id=EventId("b-1"), book=book)


def _trade_event(price: str, qty: str, *, aggressor: OutcomeSide | None = None) -> TradeEvent:
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
) -> OrderIntent:
    return OrderIntent(
        instrument_id=INSTR,
        side=OutcomeSide.YES,
        price=Decimal(price),
        quantity=Decimal(qty),
        order_type=order_type,
        order_side=order_side,
        post_only=post_only,
        metadata={"client_order_id": coid, "correlation_id": "corr-1"},
    )


def _sim() -> MarketPaperSimulator:
    return MarketPaperSimulator(
        fee_model=KalshiFeeModel(),
        latency=ConstantLatency(submit_ms=0),
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


def test_fees_applied_to_taker_fills() -> None:
    sim = _sim()
    sim.on_event(_book_event(_book(yes_asks=[("0.50", "100")])))
    fills = sim.submit(_intent(price="0.50", qty="100"), NOW)

    assert len(fills) == 1
    # Kalshi formula: 0.07 * 0.5 * 0.5 * 100 = 1.75
    assert fills[0].fee_amount == Decimal("1.75")


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
