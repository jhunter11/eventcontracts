"""Tick-pricing and edge-proportional sizing helpers (V6-C3 / V6-T5)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from eventcontracts.strategy.pricing import (
    buy_limit_from_fair,
    ceil_to_tick,
    clamp_price,
    floor_to_tick,
    sell_limit_from_fair,
)
from eventcontracts.strategy.sizing import fractional_kelly_contracts


def test_floor_and_ceil_to_tick() -> None:
    assert floor_to_tick(Decimal("0.556")) == Decimal("0.55")
    assert ceil_to_tick(Decimal("0.556")) == Decimal("0.56")
    # Exact tick is unchanged in both directions.
    assert floor_to_tick(Decimal("0.55")) == Decimal("0.55")
    assert ceil_to_tick(Decimal("0.55")) == Decimal("0.55")


def test_buy_limit_floors_and_sell_limit_ceils_to_preserve_edge() -> None:
    # A buy must never round above fair (overpaying erodes edge); a sell must
    # never round below fair.
    assert buy_limit_from_fair(Decimal("0.556")) == Decimal("0.55")
    assert sell_limit_from_fair(Decimal("0.556")) == Decimal("0.56")
    assert buy_limit_from_fair(Decimal("0.556")) <= Decimal("0.556")
    assert sell_limit_from_fair(Decimal("0.556")) >= Decimal("0.556")


def test_clamp_keeps_prices_in_tradable_band() -> None:
    assert clamp_price(Decimal("0.004")) == Decimal("0.01")
    assert clamp_price(Decimal("0.999")) == Decimal("0.99")
    assert buy_limit_from_fair(Decimal("0.003")) == Decimal("0.01")
    assert sell_limit_from_fair(Decimal("0.999")) == Decimal("0.99")


def test_tick_must_be_positive() -> None:
    with pytest.raises(ValueError):
        floor_to_tick(Decimal("0.5"), Decimal("0"))


def test_kelly_size_scales_with_edge() -> None:
    small = fractional_kelly_contracts(
        fair=Decimal("0.52"), price=Decimal("0.50"), bankroll=Decimal("1000")
    )
    large = fractional_kelly_contracts(
        fair=Decimal("0.70"), price=Decimal("0.50"), bankroll=Decimal("1000")
    )
    assert 0 < small < large


def test_kelly_size_zero_when_no_edge() -> None:
    assert (
        fractional_kelly_contracts(
            fair=Decimal("0.50"), price=Decimal("0.50"), bankroll=Decimal("1000")
        )
        == 0
    )
    assert (
        fractional_kelly_contracts(
            fair=Decimal("0.40"), price=Decimal("0.50"), bankroll=Decimal("1000")
        )
        == 0
    )


def test_kelly_size_capped_by_limits_and_cash() -> None:
    # Full size would be large; the order-notional cap binds.
    capped = fractional_kelly_contracts(
        fair=Decimal("0.90"),
        price=Decimal("0.50"),
        bankroll=Decimal("100000"),
        fraction=Decimal("1.0"),
        max_order_notional=Decimal("25"),
    )
    assert capped == 50  # $25 / $0.50 = 50 contracts

    cash_capped = fractional_kelly_contracts(
        fair=Decimal("0.90"),
        price=Decimal("0.50"),
        bankroll=Decimal("100000"),
        fraction=Decimal("1.0"),
        available_cash=Decimal("5"),
    )
    assert cash_capped == 10  # $5 / $0.50

    no_headroom = fractional_kelly_contracts(
        fair=Decimal("0.90"),
        price=Decimal("0.50"),
        bankroll=Decimal("100000"),
        fraction=Decimal("1.0"),
        max_position_notional=Decimal("100"),
        current_position_notional=Decimal("100"),
    )
    assert no_headroom == 0
