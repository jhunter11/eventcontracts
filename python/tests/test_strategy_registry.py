"""Coverage for the strategy registry and entry-point discovery."""

from __future__ import annotations

import sys

import pytest

from eventcontracts.domain import EventSubscription, StrategyId, StrategySpec, Venue
from eventcontracts.strategy import (
    ENTRY_POINT_GROUP,
    StrategyRegistry,
    create_from_spec,
    ensure_registered,
    load_entry_points,
    load_package_strategies,
    registry,
)


@pytest.fixture
def isolated_registry() -> object:
    saved = dict(registry._factories)
    saved_modules = {
        name: sys.modules[name]
        for name in list(sys.modules)
        if name.startswith("eventcontracts.plugins.strategies.")
    }
    for name in saved_modules:
        del sys.modules[name]
    registry.clear()
    yield registry
    registry.clear()
    registry._factories.update(saved)
    sys.modules.update(saved_modules)


def test_load_entry_points_finds_example_threshold(isolated_registry: object) -> None:
    loaded = load_entry_points()

    assert "example_threshold" in loaded
    assert "example_threshold" in registry.known()


def test_package_strategy_discovery_imports_repo_strategies(isolated_registry: object) -> None:
    loaded = load_package_strategies()

    assert "example_threshold" in loaded
    assert "example_threshold" in registry.known()


def test_create_from_spec_loads_plugins_before_instantiating(
    isolated_registry: object,
) -> None:
    spec = StrategySpec(
        strategy_id=StrategyId("example-threshold-v1"),
        name="example_threshold",
        version="0.1.0",
        description="registry plug-and-play test",
        subscription=EventSubscription(
            venues=(Venue.KALSHI,),
            instrument_patterns=("*",),
            event_kinds=("trade",),
        ),
    )

    strategy = create_from_spec(spec)

    assert strategy.spec == spec


def test_ensure_registered_explains_missing_strategy(isolated_registry: object) -> None:
    with pytest.raises(KeyError, match="eventcontracts.plugins.strategies"):
        ensure_registered("missing_strategy")


def test_entry_point_group_is_stable() -> None:
    assert ENTRY_POINT_GROUP == "eventcontracts.strategies"


def test_registry_rejects_duplicate_names() -> None:
    local = StrategyRegistry()
    local.register("x", lambda spec: object())  # type: ignore[arg-type, return-value]

    with pytest.raises(ValueError, match="already registered"):
        local.register("x", lambda spec: object())  # type: ignore[arg-type, return-value]
