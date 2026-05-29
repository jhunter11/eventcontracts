"""Dynamic sleeve allocation tests."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from eventcontracts.allocation import (
    EqualWeightAllocator,
    InMemorySleeveRegistry,
    IntentOutcome,
    PortfolioRiskAllocator,
)
from eventcontracts.domain import (
    ClientOrderId,
    CorrelationId,
    IntentEnvelope,
    OrderSide,
    OrderType,
    OutcomeSide,
    PlaceOrder,
    RiskProfile,
    SleeveId,
    SleeveSpec,
    StrategyId,
    TimeInForce,
    Venue,
)
from eventcontracts.domain.models import InstrumentId

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _sleeve(name: str, cap: str = "1000") -> SleeveSpec:
    return SleeveSpec(
        sleeve_id=SleeveId(name),
        strategy_id=StrategyId("example-threshold-v1"),
        strategy_version="0.1.0",
        venue=Venue.KALSHI,
        capital_allocation=Decimal(cap),
        currency="USD",
        risk=RiskProfile(
            max_order_notional=Decimal("100"),
            max_position_notional=Decimal("500"),
            max_daily_loss=Decimal("50"),
            max_open_orders=10,
            max_gross_exposure=Decimal("500"),
            currency="USD",
        ),
    )


def test_in_memory_sleeve_registry_lists_stable_order() -> None:
    registry = InMemorySleeveRegistry([_sleeve("sleeve-b"), _sleeve("sleeve-a")])

    assert tuple(s.sleeve_id for s in registry.list_active()) == (
        SleeveId("sleeve-a"),
        SleeveId("sleeve-b"),
    )


def test_equal_weight_allocator_proposes_audited_capital_targets() -> None:
    sleeves = [_sleeve("sleeve-a"), _sleeve("sleeve-b")]
    allocator = EqualWeightAllocator(
        total_capital=Decimal("1000"),
        currency="USD",
        clock=lambda: NOW,
    )

    decisions = allocator.propose(sleeves)

    assert tuple(d.target_capital for d in decisions) == (
        Decimal("500"),
        Decimal("500"),
    )
    assert all(d.audit.object_kind == "allocation_decision" for d in decisions)


def test_equal_weight_allocator_caps_at_sleeve_allocation() -> None:
    sleeves = [_sleeve("sleeve-a", cap="100"), _sleeve("sleeve-b", cap="1000")]
    allocator = EqualWeightAllocator(
        total_capital=Decimal("1000"),
        currency="USD",
        clock=lambda: NOW,
    )

    decisions = allocator.propose(sleeves)

    assert tuple(d.target_capital for d in decisions) == (
        Decimal("100"),
        Decimal("500"),
    )


def test_equal_weight_allocator_apply_updates_snapshot() -> None:
    sleeves = [_sleeve("sleeve-a"), _sleeve("sleeve-b")]
    allocator = EqualWeightAllocator(
        total_capital=Decimal("1000"),
        currency="USD",
        clock=lambda: NOW,
    )

    allocator.apply(allocator.propose(sleeves))
    snapshot = allocator.snapshot()

    assert snapshot.allocated_capital == Decimal("1000")
    assert snapshot.audit.object_kind == "capital_snapshot"


def test_portfolio_risk_allocator_reserves_by_sleeve_and_group() -> None:
    allocator = PortfolioRiskAllocator(
        total_capital=Decimal("100"),
        currency="USD",
        sleeve_budgets={SleeveId("sleeve-a"): Decimal("50")},
        group_budgets={"macro-cpi": Decimal("25")},
        clock=lambda: NOW,
    )

    reservation, reasons = allocator.reserve(_envelope("co-1"), event_group_id="macro-cpi")

    assert reasons == ()
    assert reservation is not None
    assert reservation.amount == Decimal("20.00")
    assert allocator.reserved_total() == Decimal("20.00")

    second, reasons = allocator.reserve(_envelope("co-2"), event_group_id="macro-cpi")

    assert second is None
    assert reasons == ("event_group_budget",)


def test_portfolio_risk_allocator_releases_reservations() -> None:
    allocator = PortfolioRiskAllocator(
        total_capital=Decimal("100"),
        currency="USD",
        sleeve_budgets={SleeveId("sleeve-a"): Decimal("50")},
        clock=lambda: NOW,
    )
    reservation, reasons = allocator.reserve(_envelope("co-1"))

    assert reasons == ()
    assert reservation is not None
    assert allocator.release(ClientOrderId("co-1")) == reservation
    assert allocator.reserved_total() == Decimal("0")


def test_portfolio_risk_allocator_releases_on_rejected_outcome() -> None:
    allocator = PortfolioRiskAllocator(
        total_capital=Decimal("100"),
        currency="USD",
        sleeve_budgets={SleeveId("sleeve-a"): Decimal("50")},
        clock=lambda: NOW,
    )
    reservation, reasons = allocator.reserve(_envelope("co-1"))

    assert reasons == ()
    assert reservation is not None
    assert allocator.on_intent_outcome(ClientOrderId("co-1"), IntentOutcome.REJECTED) == reservation
    assert allocator.reserved_total() == Decimal("0")


def _envelope(client_order_id: str) -> IntentEnvelope:
    decision = PlaceOrder(
        client_order_id=ClientOrderId(client_order_id),
        instrument_id=InstrumentId(venue=Venue.KALSHI, market_id="KXCPI-HIGH"),
        outcome_side=OutcomeSide.YES,
        order_side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.IOC,
        quantity=Decimal("40"),
        price=Decimal("0.50"),
    )
    return IntentEnvelope(
        decision=decision,
        strategy_id=StrategyId("strategy-a"),
        sleeve_id=SleeveId("sleeve-a"),
        correlation_id=CorrelationId(f"corr-{client_order_id}"),
        emitted_at=NOW,
    )
