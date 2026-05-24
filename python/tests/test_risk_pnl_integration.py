"""Risk gate + PnL ledger integration.

Verifies that once the PnL tracker reports a realized loss above the
sleeve's ``max_daily_loss``, the SleeveRiskGate starts rejecting
PlaceOrders for the rest of the day.

This is the live-gate guarantee from the roadmap: a strategy that
loses too much in a single day cannot keep trading.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from eventcontracts.domain.decisions import IntentEnvelope, PlaceOrder
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
from eventcontracts.domain.orders import Liquidity, OrderSide, OrderType, TimeInForce
from eventcontracts.domain.spec import RiskProfile, SleeveSpec
from eventcontracts.execution import PnLTracker
from eventcontracts.risk import DailyLossLedger, SleeveRiskGate
from eventcontracts.testing import InMemoryContext


NOW = datetime(2026, 3, 1, 13, 0, tzinfo=timezone.utc)
INSTR = InstrumentId(venue=Venue.KALSHI, market_id="M-1", outcome_id=None)


def _sleeve(max_daily_loss: str) -> SleeveSpec:
    return SleeveSpec(
        sleeve_id=SleeveId("s-1"),
        strategy_id=StrategyId("strat-1"),
        strategy_version="0.1.0",
        venue=Venue.KALSHI,
        capital_allocation=Decimal("10000"),
        currency="USD",
        risk=RiskProfile(
            max_order_notional=Decimal("1000"),
            max_position_notional=Decimal("5000"),
            max_daily_loss=Decimal(max_daily_loss),
            max_open_orders=10,
            max_gross_exposure=Decimal("10000"),
            currency="USD",
        ),
    )


def _fill(price: str, qty: str, side: OrderSide, fill_id: str = "f-1") -> Fill:
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
        fee_amount=Decimal("0"),
        fee_currency="USD",
        filled_at=NOW,
        exchange_ts=NOW,
        correlation_id=CorrelationId("c-1"),
        strategy_id=StrategyId("strat-1"),
        sleeve_id=SleeveId("s-1"),
    )


def _envelope_buy() -> IntentEnvelope:
    return IntentEnvelope(
        decision=PlaceOrder(
            instrument_id=INSTR,
            outcome_side=OutcomeSide.YES,
            order_side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            time_in_force=TimeInForce.GTC,
            quantity=Decimal("10"),
            price=Decimal("0.50"),
            client_order_id=ClientOrderId("co-new"),
        ),
        strategy_id=StrategyId("strat-1"),
        sleeve_id=SleeveId("s-1"),
        correlation_id=CorrelationId("c-1"),
        emitted_at=NOW,
        triggered_by_event_id=EventId("ev-1"),
    )


def _ctx() -> InMemoryContext:
    return InMemoryContext(
        strategy_id_value=StrategyId("strat-1"),
        sleeve_id_value=SleeveId("s-1"),
        clock_now=NOW,
    )


def test_pnl_loss_blocks_subsequent_buys_via_shared_ledger() -> None:
    """Realized loss recorded by PnLTracker must block the next buy."""

    sleeve = _sleeve("50")
    ledger = DailyLossLedger()
    pnl = PnLTracker(daily_loss_ledger=ledger)
    gate = SleeveRiskGate(sleeve=sleeve, daily_loss=ledger)

    # First trade: buy 100 @ 0.60.
    pnl.on_fill(_fill("0.60", "100", OrderSide.BUY))
    # Close at 0.10: realized = (0.10 - 0.60) * 100 = -50.00 → loss of 50.
    pnl.on_fill(_fill("0.10", "100", OrderSide.SELL, fill_id="f-2"))

    # Ledger now reports 50 loss for today, which equals max_daily_loss.
    assert ledger.loss_for(NOW) == Decimal("50")

    verdict = gate.evaluate(_envelope_buy(), _ctx())
    assert not verdict.allowed
    assert "max_daily_loss" in verdict.reasons


def test_below_threshold_loss_does_not_block() -> None:
    sleeve = _sleeve("100")
    ledger = DailyLossLedger()
    pnl = PnLTracker(daily_loss_ledger=ledger)
    gate = SleeveRiskGate(sleeve=sleeve, daily_loss=ledger)

    pnl.on_fill(_fill("0.60", "100", OrderSide.BUY))
    pnl.on_fill(_fill("0.30", "100", OrderSide.SELL, fill_id="f-2"))  # loss = 30

    verdict = gate.evaluate(_envelope_buy(), _ctx())
    assert verdict.allowed


def test_new_day_resets_loss_check() -> None:
    """A loss recorded yesterday must not block today's trades."""

    from datetime import timedelta

    sleeve = _sleeve("50")
    ledger = DailyLossLedger()
    yesterday = NOW - timedelta(days=1)

    # Yesterday's loss.
    ledger.record_realized_pnl(Decimal("-60"), yesterday)
    # Today's check passes because today's loss is zero.
    assert ledger.loss_for(NOW) == Decimal("0")

    gate = SleeveRiskGate(sleeve=sleeve, daily_loss=ledger)
    verdict = gate.evaluate(_envelope_buy(), _ctx())
    assert verdict.allowed
