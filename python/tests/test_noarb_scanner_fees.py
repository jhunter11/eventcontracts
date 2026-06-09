"""Audit F7 regression tests for the Kalshi no-arb scanner.

Covers the two defects: per-order fee underestimation (no cent-ceil) and trusting
ladder exhaustiveness (no contiguity check).
"""

from __future__ import annotations

from decimal import Decimal

from eventcontracts.plugins.strategies.kalshi_noarb_scanner import (
    _Bracket,
    _exclusive_ladder_gap,
    _fee,
)


def test_fee_is_ceiled_to_the_next_cent() -> None:
    # 0.07 * 0.5 * 0.5 = 0.0175 -> Kalshi charges the next whole cent: 0.02.
    assert _fee(Decimal("0.50")) == Decimal("0.02")


def test_fee_ceils_tiny_nonzero_to_one_cent() -> None:
    # Near-certain leg: 0.07 * 0.99 * 0.01 = 0.000693 -> still a 1c minimum fee.
    assert _fee(Decimal("0.99")) == Decimal("0.01")


def test_fee_zero_at_the_boundaries() -> None:
    assert _fee(Decimal("0")) == Decimal("0")
    assert _fee(Decimal("1")) == Decimal("0")


def test_fee_never_understates_the_raw_curve() -> None:
    for cents in range(1, 100):
        p = Decimal(cents) / Decimal("100")
        raw = Decimal("0.07") * p * (Decimal("1") - p)
        assert _fee(p) >= raw  # ceil is always >= the exact fee


def test_ceiled_fee_can_flip_a_marginal_lock_negative() -> None:
    # Eight identical 0.50 legs. Raw fee 8*0.0175 = 0.14; ceiled 8*0.02 = 0.16.
    # A sum_ask of 0.85 looks like +0.01 edge raw but is -0.01 once fees are real.
    legs = [Decimal("0.50")] * 8
    raw_total = sum(Decimal("0.07") * p * (Decimal("1") - p) for p in legs)
    ceil_total = sum(_fee(p) for p in legs)
    sum_ask = Decimal("0.85")
    assert Decimal("1") - sum_ask - raw_total > 0  # phantom edge under the old fee
    assert Decimal("1") - sum_ask - ceil_total < 0  # correctly net-negative now


def test_contiguous_exclusive_ladder_has_no_gap() -> None:
    brackets = [
        _Bracket("B71", Decimal("71"), Decimal("73")),
        _Bracket("B73", Decimal("73"), Decimal("75")),
        _Bracket("B75", Decimal("75"), Decimal("77")),
    ]
    assert _exclusive_ladder_gap(brackets) is None


def test_gappy_exclusive_ladder_is_refused() -> None:
    # 73..75 missing -> "exactly one resolves YES" is false; must be flagged.
    brackets = [
        _Bracket("B71", Decimal("71"), Decimal("73")),
        _Bracket("B75", Decimal("75"), Decimal("77")),
    ]
    gap = _exclusive_ladder_gap(brackets)
    assert gap is not None and "non_contiguous_ladder" in gap


def test_overlapping_exclusive_ladder_is_refused() -> None:
    brackets = [
        _Bracket("B71", Decimal("71"), Decimal("74")),
        _Bracket("B73", Decimal("73"), Decimal("75")),
    ]
    assert _exclusive_ladder_gap(brackets) is not None
