"""PnL tracker: position keeping, realized/unrealized PnL, mark-to-market."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from eventcontracts.domain.events import QuoteEvent
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
    Venue,
)
from eventcontracts.domain.orders import Liquidity, OrderSide
from eventcontracts.execution import PnLTracker
from eventcontracts.risk import DailyLossLedger


NOW = datetime(2026, 1, 1, 9, 30, tzinfo=timezone.utc)
INSTR = InstrumentId(venue=Venue.KALSHI, market_id="M-1", outcome_id=None)


def _fill(
    *,
    price: str,
    qty: str,
    side: OrderSide,
    fee: str = "0.00",
    when: datetime = NOW,
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
        filled_at=when,
        exchange_ts=when,
        correlation_id=CorrelationId("c-1"),
        strategy_id=StrategyId("strat-1"),
        sleeve_id=SleeveId("sleeve-1"),
    )


def _quote_event(bid: str | None, ask: str | None) -> QuoteEvent:
    return QuoteEvent(
        event_id=EventId("q-1"),
        quote=Quote(
            instrument_id=INSTR,
            side=OutcomeSide.YES,
            bid=OrderBookLevel(price=Decimal(bid), quantity=Decimal("100")) if bid else None,
            ask=OrderBookLevel(price=Decimal(ask), quantity=Decimal("100")) if ask else None,
            exchange_ts=NOW,
            received_at=NOW,
        ),
    )


def test_buy_then_sell_realizes_pnl() -> None:
    pnl = PnLTracker()
    pnl.on_fill(_fill(price="0.40", qty="100", side=OrderSide.BUY))
    pnl.on_fill(_fill(price="0.55", qty="100", side=OrderSide.SELL, fill_id="f-2"))
    # Realized: (0.55 - 0.40) * 100 = 15.00
    assert pnl.cumulative_realized == Decimal("15.00")
    # Position closed
    assert pnl.position(INSTR, OutcomeSide.YES, now=NOW) is None


def test_weighted_average_price_on_partial_buys() -> None:
    pnl = PnLTracker()
    pnl.on_fill(_fill(price="0.40", qty="100", side=OrderSide.BUY))
    pnl.on_fill(_fill(price="0.50", qty="100", side=OrderSide.BUY, fill_id="f-2"))
    rec = pnl.records[(INSTR, OutcomeSide.YES)]
    assert rec.average_price == Decimal("0.45")
    assert rec.quantity == Decimal("200")


def test_mark_to_market_updates_unrealized() -> None:
    pnl = PnLTracker()
    pnl.on_fill(_fill(price="0.40", qty="100", side=OrderSide.BUY))
    pnl.on_event(_quote_event("0.50", "0.52"))
    pos = pnl.position(INSTR, OutcomeSide.YES, now=NOW)
    assert pos is not None
    # Mark is mid = 0.51, basis 0.40 → unrealized = (0.51 - 0.40) * 100 = 11.00
    assert pos.unrealized_pnl == Decimal("11.00")


def test_fees_reduce_realized() -> None:
    pnl = PnLTracker()
    pnl.on_fill(_fill(price="0.40", qty="100", side=OrderSide.BUY, fee="2.80"))
    pnl.on_fill(_fill(price="0.55", qty="100", side=OrderSide.SELL, fee="1.74", fill_id="f-2"))
    # Realized PnL: 15.00 - 2.80 (buy fee) - 1.74 (sell fee) = 10.46
    assert pnl.cumulative_realized == Decimal("10.46")
    assert pnl.total_fees_paid == Decimal("4.54")


def test_loss_recorded_to_ledger() -> None:
    ledger = DailyLossLedger()
    pnl = PnLTracker(daily_loss_ledger=ledger)
    pnl.on_fill(_fill(price="0.60", qty="100", side=OrderSide.BUY))
    pnl.on_fill(_fill(price="0.50", qty="100", side=OrderSide.SELL, fill_id="f-2"))
    # Realized: (0.50 - 0.60) * 100 = -10. Loss reported as +10.
    assert ledger.loss_for(NOW) == Decimal("10")


def test_partial_close_keeps_position_open() -> None:
    pnl = PnLTracker()
    pnl.on_fill(_fill(price="0.40", qty="100", side=OrderSide.BUY))
    pnl.on_fill(_fill(price="0.55", qty="40", side=OrderSide.SELL, fill_id="f-2"))
    rec = pnl.records[(INSTR, OutcomeSide.YES)]
    assert rec.quantity == Decimal("60")
    # Realized portion: 40 * 0.15 = 6.0
    assert pnl.cumulative_realized == Decimal("6.00")


def test_total_pnl_combines_realized_and_mark() -> None:
    pnl = PnLTracker()
    pnl.on_fill(_fill(price="0.40", qty="100", side=OrderSide.BUY))
    pnl.on_event(_quote_event("0.50", "0.52"))
    # Realized = 0, unrealized = 11.00 → total = 11.00
    assert pnl.total_pnl(now=NOW) == Decimal("11.00")
