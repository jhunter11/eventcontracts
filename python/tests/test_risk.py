"""Risk gate, limits, and stateful risk objects."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from eventcontracts.domain.decisions import (
    CancelOrder,
    IntentEnvelope,
    PlaceOrder,
)
from eventcontracts.domain.ids import (
    ClientOrderId,
    CorrelationId,
    EventId,
    SleeveId,
    StrategyId,
)
from eventcontracts.domain.models import InstrumentId, OutcomeSide, Venue
from eventcontracts.domain.orders import OrderSide, OrderType, TimeInForce
from eventcontracts.domain.positions import CashBalance, Exposure, Position
from eventcontracts.domain.spec import RiskProfile, SleeveSpec
from eventcontracts.risk import (
    DailyLossLedger,
    KillSwitch,
    SleeveRiskGate,
    check_daily_loss,
    check_gross_exposure,
    check_position_notional,
)
from eventcontracts.testing import InMemoryContext

NOW = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)


def _profile(**overrides: object) -> RiskProfile:
    defaults = dict(
        max_order_notional=Decimal("100"),
        max_position_notional=Decimal("500"),
        max_daily_loss=Decimal("50"),
        max_open_orders=5,
        max_gross_exposure=Decimal("1000"),
        currency="USD",
    )
    defaults.update(overrides)
    return RiskProfile(**defaults)  # type: ignore[arg-type]


def _sleeve(profile: RiskProfile | None = None) -> SleeveSpec:
    return SleeveSpec(
        sleeve_id=SleeveId("sleeve-1"),
        strategy_id=StrategyId("strat-1"),
        strategy_version="0.1.0",
        venue=Venue.KALSHI,
        capital_allocation=Decimal("10000"),
        currency="USD",
        risk=profile or _profile(),
    )


def _place_order(quantity: str = "10", price: str = "0.5") -> PlaceOrder:
    return PlaceOrder(
        instrument_id=InstrumentId(venue=Venue.KALSHI, market_id="MKT-1", outcome_id=None),
        outcome_side=OutcomeSide.YES,
        order_side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.GTC,
        quantity=Decimal(quantity),
        price=Decimal(price),
        client_order_id=ClientOrderId("co-1"),
    )


def _envelope(decision: PlaceOrder | CancelOrder) -> IntentEnvelope:
    return IntentEnvelope(
        decision=decision,
        strategy_id=StrategyId("strat-1"),
        sleeve_id=SleeveId("sleeve-1"),
        correlation_id=CorrelationId("corr-1"),
        emitted_at=NOW,
        triggered_by_event_id=EventId("ev-1"),
    )


def _ctx(**kwargs: object) -> InMemoryContext:
    kwargs.setdefault(
        "cash_by_ccy",
        {
            "USD": CashBalance(
                currency="USD",
                total=Decimal("10000"),
                available=Decimal("10000"),
                held_for_orders=Decimal("0"),
                settling=Decimal("0"),
                updated_at=NOW,
            )
        },
    )
    return InMemoryContext(
        strategy_id_value=StrategyId("strat-1"),
        sleeve_id_value=SleeveId("sleeve-1"),
        clock_now=NOW,
        **kwargs,  # type: ignore[arg-type]
    )


def test_order_notional_limit_rejects() -> None:
    gate = SleeveRiskGate(sleeve=_sleeve())
    # qty=10 * price=0.5 = 5 notional, well within limit
    assert gate.evaluate(_envelope(_place_order()), _ctx()).allowed
    # qty=300 * price=0.5 = 150 > max 100
    big = _place_order(quantity="300", price="0.5")
    verdict = gate.evaluate(_envelope(big), _ctx())
    assert not verdict.allowed
    assert "max_order_notional" in verdict.reasons


def test_available_cash_rejects_overspend() -> None:
    gate = SleeveRiskGate(sleeve=_sleeve(_profile(max_order_notional=Decimal("1000"))))
    ctx = _ctx(
        cash_by_ccy={
            "USD": CashBalance(
                currency="USD",
                total=Decimal("10"),
                available=Decimal("10"),
                held_for_orders=Decimal("0"),
                settling=Decimal("0"),
                updated_at=NOW,
            )
        }
    )
    verdict = gate.evaluate(_envelope(_place_order(quantity="50", price="0.5")), ctx)
    assert not verdict.allowed
    assert "available_cash" in verdict.reasons


def test_max_open_orders_blocks_new_orders() -> None:
    from eventcontracts.domain.orders import Order, OrderStatus, TimeInForce

    gate = SleeveRiskGate(sleeve=_sleeve(_profile(max_open_orders=2)))

    def _open_order(coid: str) -> Order:
        return Order(
            client_order_id=ClientOrderId(coid),
            venue_order_id=None,
            instrument_id=InstrumentId(venue=Venue.KALSHI, market_id="MKT-1", outcome_id=None),
            outcome_side=OutcomeSide.YES,
            order_side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            time_in_force=TimeInForce.GTC,
            price=Decimal("0.5"),
            quantity=Decimal("1"),
            filled_quantity=Decimal("0"),
            status=OrderStatus.OPEN,
            created_at=NOW,
            updated_at=NOW,
            correlation_id=CorrelationId("c"),
            strategy_id=StrategyId("strat-1"),
            sleeve_id=SleeveId("sleeve-1"),
        )

    ctx = _ctx(open_order_list=[_open_order("a"), _open_order("b")])
    verdict = gate.evaluate(_envelope(_place_order()), ctx)
    assert not verdict.allowed
    assert "max_open_orders" in verdict.reasons


def test_position_notional_projects_new_holding() -> None:
    profile = _profile(max_position_notional=Decimal("10"))
    instrument = InstrumentId(venue=Venue.KALSHI, market_id="MKT-1", outcome_id=None)
    existing = Position(
        instrument_id=instrument,
        outcome_side=OutcomeSide.YES,
        quantity=Decimal("15"),
        average_price=Decimal("0.5"),
        realized_pnl=Decimal("0"),
        unrealized_pnl=Decimal("0"),
        updated_at=NOW,
    )
    order = _place_order(quantity="10", price="0.5")
    reasons = check_position_notional(order, profile, [existing])
    assert "max_position_notional" in reasons


def test_gross_exposure_blocks_when_projected_exceeds() -> None:
    profile = _profile(max_gross_exposure=Decimal("100"))
    exposure = Exposure(
        sleeve_id=SleeveId("sleeve-1"),
        currency="USD",
        gross_notional=Decimal("90"),
        net_notional=Decimal("90"),
        long_notional=Decimal("90"),
        short_notional=Decimal("0"),
        updated_at=NOW,
    )
    order = _place_order(quantity="50", price="0.5")  # 25 notional → 90 + 25 = 115
    reasons = check_gross_exposure(order, profile, exposure)
    assert "max_gross_exposure" in reasons


def test_daily_loss_blocks_after_threshold() -> None:
    profile = _profile(max_daily_loss=Decimal("50"))
    ledger = DailyLossLedger()
    ledger.record_realized_pnl(Decimal("-60"), NOW)
    assert "max_daily_loss" in check_daily_loss(profile, ledger.loss_for(NOW))


def test_daily_loss_zero_disables_check() -> None:
    profile = _profile(max_daily_loss=Decimal("0"))
    ledger = DailyLossLedger()
    ledger.record_realized_pnl(Decimal("-1000000"), NOW)
    assert check_daily_loss(profile, ledger.loss_for(NOW)) == ()


def test_kill_switch_rejects_everything() -> None:
    gate = SleeveRiskGate(sleeve=_sleeve())
    gate.kill_switch.trip("manual halt", NOW)
    verdict = gate.evaluate(_envelope(_place_order()), _ctx())
    assert not verdict.allowed
    assert "kill_switch" in verdict.reasons


def test_cancel_orders_skip_position_checks() -> None:
    gate = SleeveRiskGate(sleeve=_sleeve())
    cancel = CancelOrder(client_order_id=ClientOrderId("co-1"), reason="test")
    assert gate.evaluate(_envelope(cancel), _ctx()).allowed


def test_kill_switch_is_one_way_until_reset() -> None:
    switch = KillSwitch()
    switch.trip("incident", NOW)
    switch.trip("other reason", NOW)  # second trip is a no-op
    assert switch.reason == "incident"
    switch.reset()
    assert not switch.tripped
    assert switch.reason is None


def test_daily_loss_only_records_negative_pnl() -> None:
    ledger = DailyLossLedger()
    ledger.record_realized_pnl(Decimal("50"), NOW)  # win, ignored
    ledger.record_realized_pnl(Decimal("-30"), NOW)
    ledger.record_realized_pnl(Decimal("-20"), NOW)
    assert ledger.loss_for(NOW) == Decimal("50")


def test_pretrade_policy_service_rejects_oversize_intent() -> None:
    from eventcontracts.execution.simulator import OrderIntent
    from eventcontracts.risk.policy import PreTradePolicyService

    sleeve = _sleeve(_profile(max_order_notional=Decimal("10")))
    service = PreTradePolicyService(sleeve)
    intent = OrderIntent(
        instrument_id=InstrumentId(venue=Venue.KALSHI, market_id="M-1"),
        side=OutcomeSide.YES,
        order_side=OrderSide.BUY,
        price=Decimal("0.50"),
        quantity=Decimal("100"),
        order_type="limit",
    )
    decision = service.evaluate(intent)
    assert not decision.allowed
    assert decision.reasons == ("max_order_notional",)


def test_pretrade_policy_service_accepts_within_limit() -> None:
    from eventcontracts.execution.simulator import OrderIntent
    from eventcontracts.risk.policy import PreTradePolicyService

    sleeve = _sleeve()
    service = PreTradePolicyService(sleeve)
    intent = OrderIntent(
        instrument_id=InstrumentId(venue=Venue.KALSHI, market_id="M-1"),
        side=OutcomeSide.YES,
        order_side=OrderSide.BUY,
        price=Decimal("0.50"),
        quantity=Decimal("10"),
        order_type="limit",
    )
    decision = service.evaluate(intent)
    assert decision.allowed
    assert decision.reasons == ()
