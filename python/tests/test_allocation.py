"""Dynamic sleeve allocation tests."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from eventcontracts.allocation import EqualWeightAllocator, InMemorySleeveRegistry
from eventcontracts.domain import RiskProfile, SleeveId, SleeveSpec, StrategyId, Venue

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
