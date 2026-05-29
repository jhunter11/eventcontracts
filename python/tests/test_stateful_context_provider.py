"""Stateful strategy context fed by execution fills."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from eventcontracts.domain.decisions import IntentEnvelope, PlaceOrder
from eventcontracts.domain.events import OwnFillEvent, OwnOrderRejectEvent, OwnOrderUpdateEvent
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
from eventcontracts.domain.models import InstrumentId, OutcomeSide, Venue
from eventcontracts.domain.orders import (
    Liquidity,
    Order,
    OrderReject,
    OrderSide,
    OrderStatus,
    OrderType,
    TimeInForce,
)
from eventcontracts.runner import StatefulContextProvider

NOW = datetime(2026, 1, 1, 9, 30, tzinfo=UTC)
INSTR = InstrumentId(venue=Venue.KALSHI, market_id="M-1")


def _fill(
    *,
    price: str,
    qty: str,
    side: OrderSide,
    fee: str = "0.00",
    fill_id: str = "f-1",
) -> Fill:
    return Fill(
        fill_id=FillId(fill_id),
        venue_order_id=VenueOrderId("vo-1"),
        client_order_id=ClientOrderId("co-1"),
        instrument_id=INSTR,
        outcome_side=OutcomeSide.YES,
        order_side=side,
        price=Decimal(price),
        quantity=Decimal(qty),
        liquidity=Liquidity.TAKER,
        fee_amount=Decimal(fee),
        fee_currency="USD",
        filled_at=NOW,
        exchange_ts=NOW,
        correlation_id=CorrelationId("corr-1"),
        strategy_id=StrategyId("strat-1"),
        sleeve_id=SleeveId("sleeve-1"),
    )


def test_stateful_context_provider_updates_position_cash_and_exposure_from_fills() -> None:
    provider = StatefulContextProvider(
        strategy_id=StrategyId("strat-1"),
        sleeve_id=SleeveId("sleeve-1"),
        currency="USD",
        starting_cash=Decimal("100.00"),
        clock=lambda: NOW,
    )

    provider.on_fill(_fill(price="0.40", qty="10", side=OrderSide.BUY, fee="0.10"))

    ctx = provider.context()
    position = ctx.position(INSTR, OutcomeSide.YES)
    assert position is not None
    assert position.quantity == Decimal("10")
    assert position.average_price == Decimal("0.40")
    assert ctx.cash("USD").available == Decimal("95.90")
    assert ctx.exposure().gross_notional == Decimal("4.00")

    provider.on_fill(_fill(price="0.50", qty="4", side=OrderSide.SELL, fee="0.05", fill_id="f-2"))

    ctx = provider.context()
    position = ctx.position(INSTR, OutcomeSide.YES)
    assert position is not None
    assert position.quantity == Decimal("6")
    assert ctx.cash("USD").available == Decimal("97.85")
    assert ctx.exposure().gross_notional == Decimal("2.40")


def test_stateful_context_provider_reserves_cash_and_consumes_on_fill() -> None:
    provider = StatefulContextProvider(
        strategy_id=StrategyId("strat-1"),
        sleeve_id=SleeveId("sleeve-1"),
        currency="USD",
        starting_cash=Decimal("100.00"),
        clock=lambda: NOW,
    )
    order = PlaceOrder(
        client_order_id=ClientOrderId("co-1"),
        instrument_id=INSTR,
        outcome_side=OutcomeSide.YES,
        order_side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.GTC,
        quantity=Decimal("20"),
        price=Decimal("0.50"),
    )
    envelope = IntentEnvelope(
        decision=order,
        strategy_id=StrategyId("strat-1"),
        sleeve_id=SleeveId("sleeve-1"),
        correlation_id=CorrelationId("corr-1"),
        emitted_at=NOW,
    )

    assert provider.reserve_intent(envelope) == ()
    assert provider.context().cash("USD").available == Decimal("90.00")
    assert provider.context().cash("USD").held_for_orders == Decimal("10.00")

    provider.on_fill(_fill(price="0.50", qty="4", side=OrderSide.BUY, fee="0.05"))

    balance = provider.context().cash("USD")
    assert balance.total == Decimal("97.95")
    assert balance.available == Decimal("89.95")
    assert balance.held_for_orders == Decimal("8.00")


def test_stateful_context_provider_routes_private_events() -> None:
    provider = StatefulContextProvider(
        strategy_id=StrategyId("strat-1"),
        sleeve_id=SleeveId("sleeve-1"),
        currency="USD",
        starting_cash=Decimal("100.00"),
        clock=lambda: NOW,
    )
    order = _order(status=OrderStatus.OPEN, quantity="20", filled="0")

    provider.on_order_update(order)
    assert provider.context().cash("USD").held_for_orders == Decimal("10.00")

    provider.on_event(OwnFillEvent(event_id=EventId("own-fill"), fill=_fill(price="0.50", qty="4", side=OrderSide.BUY)))
    assert provider.context().cash("USD").held_for_orders == Decimal("8.00")

    provider.on_event(
        OwnOrderUpdateEvent(
            event_id=EventId("own-cancel"),
            order=_order(status=OrderStatus.CANCELED, quantity="20", filled="4"),
        )
    )
    balance = provider.context().cash("USD")
    assert balance.held_for_orders == Decimal("0.00")
    assert provider.context().open_orders() == ()


def test_stateful_context_provider_releases_reservation_on_reject_event() -> None:
    provider = StatefulContextProvider(
        strategy_id=StrategyId("strat-1"),
        sleeve_id=SleeveId("sleeve-1"),
        currency="USD",
        starting_cash=Decimal("100.00"),
        clock=lambda: NOW,
    )
    provider.on_order_update(_order(status=OrderStatus.OPEN, quantity="20", filled="0"))

    provider.on_event(
        OwnOrderRejectEvent(
            event_id=EventId("own-reject"),
            reject=OrderReject(
                client_order_id=ClientOrderId("co-1"),
                reason="venue_reject",
                rejected_at=NOW,
            ),
        )
    )

    balance = provider.context().cash("USD")
    assert balance.available == Decimal("100.00")
    assert balance.held_for_orders == Decimal("0.00")


def _order(*, status: OrderStatus, quantity: str, filled: str) -> Order:
    return Order(
        client_order_id=ClientOrderId("co-1"),
        venue_order_id=VenueOrderId("vo-1"),
        instrument_id=INSTR,
        outcome_side=OutcomeSide.YES,
        order_side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.GTC,
        price=Decimal("0.50"),
        quantity=Decimal(quantity),
        filled_quantity=Decimal(filled),
        status=status,
        created_at=NOW,
        updated_at=NOW,
        correlation_id=CorrelationId("corr-1"),
        strategy_id=StrategyId("strat-1"),
        sleeve_id=SleeveId("sleeve-1"),
    )
