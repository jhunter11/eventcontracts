"""Risk gate, limits, and stateful risk objects."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

from eventcontracts.domain.decisions import (
    CancelOrder,
    IntentEnvelope,
    PlaceOrder,
    ReplaceOrder,
)
from eventcontracts.domain.ids import (
    ClientOrderId,
    CorrelationId,
    EventId,
    SleeveId,
    StrategyId,
)
from eventcontracts.domain.models import InstrumentId, MarketSnapshot, OrderBookLevel, OutcomeSide, Venue
from eventcontracts.domain.orders import Order, OrderSide, OrderStatus, OrderType, TimeInForce
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


def _snapshot(*, received_at: datetime = NOW, sequence_gap: bool = False) -> MarketSnapshot:
    return MarketSnapshot(
        instrument_id=InstrumentId(venue=Venue.KALSHI, market_id="MKT-1", outcome_id=None),
        side=OutcomeSide.YES,
        bid=OrderBookLevel(price=Decimal("0.49"), quantity=Decimal("100")),
        ask=OrderBookLevel(price=Decimal("0.51"), quantity=Decimal("100")),
        exchange_ts=received_at,
        received_at=received_at,
        source="fixture",
        source_sequence="42",
        sequence_gap=sequence_gap,
    )


def _place_order(quantity: str = "10", price: str = "0.5") -> PlaceOrder:
    return PlaceOrder(
        instrument_id=InstrumentId(venue=Venue.KALSHI, market_id="MKT-1", outcome_id=None),
        outcome_side=OutcomeSide.YES,
        order_side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.GTD,
        quantity=Decimal(quantity),
        price=Decimal(price),
        client_order_id=ClientOrderId("co-1"),
        expires_at=NOW + timedelta(seconds=1),
        market_snapshot=_snapshot(),
    )


def _envelope(decision: PlaceOrder | CancelOrder | ReplaceOrder) -> IntentEnvelope:
    return IntentEnvelope(
        decision=decision,
        strategy_id=StrategyId("strat-1"),
        sleeve_id=SleeveId("sleeve-1"),
        correlation_id=CorrelationId("corr-1"),
        emitted_at=NOW,
        triggered_by_event_id=EventId("ev-1"),
    )


def _open_order(coid: str = "co-1", *, quantity: str = "1", price: str = "0.01") -> Order:
    return Order(
        client_order_id=ClientOrderId(coid),
        venue_order_id=None,
        instrument_id=InstrumentId(venue=Venue.KALSHI, market_id="MKT-1", outcome_id=None),
        outcome_side=OutcomeSide.YES,
        order_side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.GTD,
        price=Decimal(price),
        quantity=Decimal(quantity),
        filled_quantity=Decimal("0"),
        status=OrderStatus.OPEN,
        created_at=NOW,
        updated_at=NOW,
        correlation_id=CorrelationId("c"),
        strategy_id=StrategyId("strat-1"),
        sleeve_id=SleeveId("sleeve-1"),
        expires_at=NOW + timedelta(seconds=1),
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


def test_missing_market_snapshot_rejects_place_order() -> None:
    gate = SleeveRiskGate(sleeve=_sleeve())
    order = _place_order()
    order = PlaceOrder(
        client_order_id=order.client_order_id,
        instrument_id=order.instrument_id,
        outcome_side=order.outcome_side,
        order_side=order.order_side,
        order_type=order.order_type,
        time_in_force=order.time_in_force,
        quantity=order.quantity,
        price=order.price,
    )

    verdict = gate.evaluate(_envelope(order), _ctx())

    assert not verdict.allowed
    assert "missing_market_snapshot" in verdict.reasons


def test_stale_or_gapped_market_snapshot_rejects_place_order() -> None:
    gate = SleeveRiskGate(sleeve=_sleeve(_profile(max_market_data_age_ms=100)))
    stale = _place_order()
    stale = PlaceOrder(
        **{
            **stale.__dict__,
            "market_snapshot": _snapshot(received_at=datetime(2026, 1, 15, 11, 59, tzinfo=UTC)),
        }
    )
    gapped = _place_order()
    gapped = PlaceOrder(
        **{
            **gapped.__dict__,
            "market_snapshot": _snapshot(sequence_gap=True),
        }
    )

    stale_verdict = gate.evaluate(_envelope(stale), _ctx())
    gap_verdict = gate.evaluate(_envelope(gapped), _ctx())

    assert "stale_market_snapshot" in stale_verdict.reasons
    assert "market_snapshot_sequence_gap" in gap_verdict.reasons


def test_executable_limit_larger_than_l1_depth_rejects() -> None:
    gate = SleeveRiskGate(sleeve=_sleeve())
    buy = _place_order(quantity="150", price="0.51")
    sell = PlaceOrder(
        **{
            **_place_order(quantity="150", price="0.49").__dict__,
            "order_side": OrderSide.SELL,
        }
    )

    buy_verdict = gate.evaluate(_envelope(buy), _ctx())
    sell_verdict = gate.evaluate(_envelope(sell), _ctx())

    assert "order_quantity_exceeds_l1_depth" in buy_verdict.reasons
    assert "order_quantity_exceeds_l1_depth" in sell_verdict.reasons


def test_passive_limit_larger_than_l1_depth_is_allowed_by_impact_gate() -> None:
    gate = SleeveRiskGate(sleeve=_sleeve())
    passive_buy = _place_order(quantity="150", price="0.49")

    verdict = gate.evaluate(_envelope(passive_buy), _ctx())

    assert "order_quantity_exceeds_l1_depth" not in verdict.reasons


def test_unbounded_market_and_gtc_orders_reject_by_default() -> None:
    gate = SleeveRiskGate(sleeve=_sleeve())
    market_order = PlaceOrder(
        client_order_id=ClientOrderId("co-market"),
        instrument_id=InstrumentId(venue=Venue.KALSHI, market_id="MKT-1", outcome_id=None),
        outcome_side=OutcomeSide.YES,
        order_side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.IOC,
        quantity=Decimal("1"),
        market_snapshot=_snapshot(),
    )
    gtc_order = PlaceOrder(
        **{
            **_place_order().__dict__,
            "time_in_force": TimeInForce.GTC,
            "expires_at": None,
        }
    )

    market_verdict = gate.evaluate(_envelope(market_order), _ctx())
    gtc_verdict = gate.evaluate(_envelope(gtc_order), _ctx())

    assert "market_orders_disabled" in market_verdict.reasons
    assert "unpriced_market_order" in market_verdict.reasons
    assert "unbounded_gtc_order" in gtc_verdict.reasons


def test_order_ttl_longer_than_profile_rejects() -> None:
    gate = SleeveRiskGate(sleeve=_sleeve(_profile(max_order_lifetime_ms=100)))
    order = PlaceOrder(
        **{
            **_place_order().__dict__,
            "expires_at": NOW + timedelta(seconds=1),
        }
    )

    verdict = gate.evaluate(_envelope(order), _ctx())

    assert "order_ttl_too_long" in verdict.reasons


def test_max_open_orders_blocks_new_orders() -> None:
    gate = SleeveRiskGate(sleeve=_sleeve(_profile(max_open_orders=2)))
    ctx = _ctx(open_order_list=[_open_order("a"), _open_order("b")])
    verdict = gate.evaluate(_envelope(_place_order()), ctx)
    assert not verdict.allowed
    assert "max_open_orders" in verdict.reasons


def test_replace_order_revalidates_new_quantity_against_notional_limit() -> None:
    gate = SleeveRiskGate(sleeve=_sleeve(_profile(max_order_notional=Decimal("100"))))
    replace = ReplaceOrder(
        client_order_id=ClientOrderId("co-1"),
        new_price=Decimal("0.50"),
        new_quantity=Decimal("10000"),
    )

    verdict = gate.evaluate(_envelope(replace), _ctx(open_order_list=[_open_order()]))

    assert not verdict.allowed
    assert "max_order_notional" in verdict.reasons


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


def test_gross_exposure_allows_risk_reducing_sell() -> None:
    # V6-T1: a SELL closes inventory and cannot increase gross exposure, so it is
    # never blocked here even when the sleeve is already at the cap.
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
    sell = PlaceOrder(
        **{**_place_order(quantity="50", price="0.49").__dict__, "order_side": OrderSide.SELL}
    )
    assert check_gross_exposure(sell, profile, exposure) == ()


def test_available_cash_allows_risk_reducing_sell() -> None:
    # V6-T1: a SELL returns cash; low available cash must not block the exit.
    gate = SleeveRiskGate(sleeve=_sleeve(_profile(max_order_notional=Decimal("1000"))))
    ctx = _ctx(
        cash_by_ccy={
            "USD": CashBalance(
                currency="USD",
                total=Decimal("5"),
                available=Decimal("5"),
                held_for_orders=Decimal("0"),
                settling=Decimal("0"),
                updated_at=NOW,
            )
        }
    )
    sell = PlaceOrder(
        **{**_place_order(quantity="50", price="0.49").__dict__, "order_side": OrderSide.SELL}
    )
    verdict = gate.evaluate(_envelope(sell), ctx)
    assert "available_cash" not in verdict.reasons
    assert verdict.allowed


def test_daily_loss_blocks_after_threshold() -> None:
    profile = _profile(max_daily_loss=Decimal("50"))
    ledger = DailyLossLedger()
    ledger.record_realized_pnl(Decimal("-60"), NOW)
    assert "max_daily_loss" in check_daily_loss(profile, ledger.loss_for(NOW))


def test_fee_adjusted_edge_rejects_negative_after_fee() -> None:
    gate = SleeveRiskGate(sleeve=_sleeve())
    order = replace(
        _place_order(quantity="1", price="0.50"),
        metadata={
            "fair_price": "0.51",
            "min_executable_edge_ticks": "0",
            "fee_rate_bps": "700",
        },
    )

    verdict = gate.evaluate(_envelope(order), _ctx())

    assert not verdict.allowed
    assert "negative_edge_after_fees" in verdict.reasons


def test_fee_adjusted_edge_accepts_positive_after_fee() -> None:
    gate = SleeveRiskGate(sleeve=_sleeve())
    order = replace(
        _place_order(quantity="1", price="0.50"),
        metadata={
            "fair_price": "0.53",
            "min_executable_edge_ticks": "0",
            "fee_rate_bps": "700",
        },
    )

    verdict = gate.evaluate(_envelope(order), _ctx())

    assert verdict.allowed


def test_daily_loss_zero_disables_check() -> None:
    profile = _profile(max_daily_loss=Decimal("0"))
    ledger = DailyLossLedger()
    ledger.record_realized_pnl(Decimal("-1000000"), NOW)
    assert check_daily_loss(profile, ledger.loss_for(NOW)) == ()


def test_unrealized_drawdown_soft_halts_buys_without_tripping_kill_switch() -> None:
    # V6-T2: a recoverable unrealized (liquidation-mark) drawdown must NOT latch
    # the one-way kill switch — it engages an auto-clearing soft halt that blocks
    # new risk-increasing BUYs only.
    gate = SleeveRiskGate(sleeve=_sleeve(_profile(max_daily_loss=Decimal("50"))))
    gate.daily_loss.record_unrealized_pnl(Decimal("-55"), NOW)

    verdict = gate.evaluate(_envelope(_place_order()), _ctx())

    assert not verdict.allowed
    assert "unrealized_drawdown_halt" in verdict.reasons
    assert "max_daily_loss" not in verdict.reasons
    assert not gate.kill_switch.tripped


def test_unrealized_drawdown_still_allows_risk_reducing_exit() -> None:
    # V6-T1/T2: a SELL closes inventory; the soft halt never blocks an exit.
    gate = SleeveRiskGate(sleeve=_sleeve(_profile(max_daily_loss=Decimal("50"))))
    gate.daily_loss.record_unrealized_pnl(Decimal("-55"), NOW)

    sell = PlaceOrder(
        **{**_place_order(quantity="10", price="0.49").__dict__, "order_side": OrderSide.SELL}
    )
    verdict = gate.evaluate(_envelope(sell), _ctx())

    assert verdict.allowed


def test_unrealized_drawdown_soft_halt_auto_clears_on_recovery() -> None:
    # V6-T2: a transient mark dip then recovery must leave the sleeve tradable.
    gate = SleeveRiskGate(sleeve=_sleeve(_profile(max_daily_loss=Decimal("50"))))
    gate.daily_loss.record_unrealized_pnl(Decimal("-55"), NOW)
    assert not gate.evaluate(_envelope(_place_order()), _ctx()).allowed
    # Mark recovers below the 0.8*cap hysteresis band -> halt clears.
    gate.daily_loss.record_unrealized_pnl(Decimal("0"), NOW)
    assert gate.evaluate(_envelope(_place_order()), _ctx()).allowed
    assert not gate.kill_switch.tripped


def test_realized_daily_loss_latches_kill_switch() -> None:
    # Realized loss is a persistent fact -> permanent one-way latch (unchanged).
    gate = SleeveRiskGate(sleeve=_sleeve(_profile(max_daily_loss=Decimal("50"))))
    gate.daily_loss.record_realized_pnl(Decimal("-60"), NOW)

    verdict = gate.evaluate(_envelope(_place_order()), _ctx())

    assert not verdict.allowed
    assert "max_daily_loss" in verdict.reasons
    assert gate.kill_switch.tripped


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


def test_daily_loss_uses_net_realized_pnl() -> None:
    ledger = DailyLossLedger()
    ledger.record_realized_pnl(Decimal("50"), NOW)
    ledger.record_realized_pnl(Decimal("-30"), NOW)
    assert ledger.loss_for(NOW) == Decimal("0")
    ledger.record_realized_pnl(Decimal("-40"), NOW)
    assert ledger.loss_for(NOW) == Decimal("20")


def test_daily_loss_groups_by_utc_day() -> None:
    ledger = DailyLossLedger()
    local_late = datetime(2026, 1, 1, 23, 30, tzinfo=timezone(timedelta(hours=-5)))
    ledger.record_realized_pnl(Decimal("-7"), local_late)

    assert ledger.loss_for(datetime(2026, 1, 2, 4, 30, tzinfo=UTC)) == Decimal("7")
    assert ledger.loss_for(datetime(2026, 1, 1, 23, 30, tzinfo=UTC)) == Decimal("0")


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
