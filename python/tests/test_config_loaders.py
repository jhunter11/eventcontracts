"""Typed config loader coverage."""

from __future__ import annotations

from decimal import Decimal

from eventcontracts.config import (
    load_sleeve_spec,
    load_storage_config,
    load_strategy_spec,
    load_venue_config,
)
from eventcontracts.domain import LatencyTier, Venue
from tests.conftest import REPO_ROOT

CONFIGS = REPO_ROOT / "configs"


def test_load_strategy_spec_from_toml() -> None:
    spec = load_strategy_spec(CONFIGS / "strategies/example-threshold.toml")

    assert spec.name == "example_threshold"
    assert spec.subscription.venues == (Venue.KALSHI,)
    assert spec.subscription.event_kinds == ("trade",)
    assert spec.default_execution_priority.tier is LatencyTier.STANDARD
    assert spec.parameters["buy_below"] == "0.50"


def test_load_sleeve_spec_from_toml() -> None:
    sleeve = load_sleeve_spec(CONFIGS / "sleeves/example-kalshi-paper.toml")

    assert sleeve.venue is Venue.KALSHI
    assert sleeve.capital_allocation == Decimal("1000")
    assert sleeve.risk.max_open_orders == 5


def test_load_storage_and_venue_configs() -> None:
    storage = load_storage_config(CONFIGS / "storage/lake.toml")
    venue = load_venue_config(CONFIGS / "venues/kalshi.toml")

    assert storage.raw.schema_version == "raw-event-v1"
    assert venue.venue["name"] == "kalshi"
