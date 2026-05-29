"""Venue-specific fee models."""

from __future__ import annotations

from decimal import Decimal

from eventcontracts.adapters.venues.kalshi import KalshiFeeModel
from eventcontracts.adapters.venues.polymarket import PolymarketFeeModel
from eventcontracts.domain.fees import FillContext
from eventcontracts.domain.models import InstrumentId, OutcomeSide, Venue


def _fill(price: str, qty: str, liquidity: str = "taker", venue: Venue = Venue.KALSHI) -> FillContext:
    return FillContext(
        instrument_id=InstrumentId(venue=venue, market_id="M-1", outcome_id=None),
        side=OutcomeSide.YES,
        price=Decimal(price),
        quantity=Decimal(qty),
        liquidity=liquidity,
    )


def test_kalshi_taker_fee_matches_published_formula() -> None:
    model = KalshiFeeModel()
    # price 0.50, qty 100. 0.07 * 0.5 * 0.5 * 100 = 1.75
    est = model.estimate(_fill("0.50", "100"))
    assert est.amount == Decimal("1.75")
    assert est.currency == "USD"


def test_kalshi_fee_rounds_up_to_cent() -> None:
    model = KalshiFeeModel()
    # price 0.51, qty 1. 0.07 * 0.51 * 0.49 * 1 = 0.0174..., rounds up to 0.02
    est = model.estimate(_fill("0.51", "1"))
    assert est.amount == Decimal("0.02")


def test_kalshi_maker_fee_uses_configured_maker_curve() -> None:
    model = KalshiFeeModel()
    est = model.estimate(_fill("0.50", "100", liquidity="maker"))
    assert est.amount == Decimal("0.44")


def test_kalshi_maker_fee_can_be_disabled_for_no_maker_fee_series() -> None:
    model = KalshiFeeModel(maker_rate=Decimal("0"))
    est = model.estimate(_fill("0.50", "100", liquidity="maker"))
    assert est.amount == Decimal("0.00")


def test_polymarket_taker_default_2_percent() -> None:
    model = PolymarketFeeModel()
    # 0.6 * 100 = 60 notional; 60 * 0.02 = 1.20
    est = model.estimate(_fill("0.60", "100", venue=Venue.POLYMARKET_GLOBAL))
    assert est.amount == Decimal("1.200000")


def test_polymarket_maker_default_zero() -> None:
    model = PolymarketFeeModel()
    est = model.estimate(
        _fill("0.60", "100", liquidity="maker", venue=Venue.POLYMARKET_GLOBAL)
    )
    assert est.amount == Decimal("0.000000")


def test_polymarket_custom_taker_rate() -> None:
    model = PolymarketFeeModel(taker_rate=Decimal("0.005"))
    est = model.estimate(_fill("0.50", "200", venue=Venue.POLYMARKET_GLOBAL))
    assert est.amount == Decimal("0.500000")
