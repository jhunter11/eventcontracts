"""Unit tests for eventcontracts.crypto.pricing helpers."""

from __future__ import annotations

from decimal import Decimal

from eventcontracts.crypto import (
    bracket_parity_deviation,
    bs_above_probability,
    monotone_violations,
    realized_volatility,
)


def test_bs_at_the_money_is_near_half() -> None:
    p = bs_above_probability(
        spot=Decimal("100000"),
        strike=Decimal("100000"),
        sigma_annual=Decimal("0.60"),
        tau_seconds=Decimal("900"),
    )
    # ATM with non-zero vol should round to ~0.50; mild floor at 0.4 / 0.6.
    assert Decimal("0.45") < p < Decimal("0.55")


def test_bs_far_otm_clips_at_floor() -> None:
    p = bs_above_probability(
        spot=Decimal("100000"),
        strike=Decimal("200000"),
        sigma_annual=Decimal("0.60"),
        tau_seconds=Decimal("900"),
    )
    assert p == Decimal("0.0001")


def test_bs_far_itm_clips_at_ceiling() -> None:
    p = bs_above_probability(
        spot=Decimal("200000"),
        strike=Decimal("100000"),
        sigma_annual=Decimal("0.60"),
        tau_seconds=Decimal("900"),
    )
    assert p == Decimal("0.9999")


def test_bs_higher_strike_gives_lower_probability() -> None:
    common = dict(
        spot=Decimal("100000"),
        sigma_annual=Decimal("0.60"),
        tau_seconds=Decimal("900"),
    )
    p_low = bs_above_probability(strike=Decimal("99000"), **common)
    p_high = bs_above_probability(strike=Decimal("101000"), **common)
    assert p_low > p_high


def test_bs_twap_shrinks_terminal_variance() -> None:
    """Longer TWAP window reduces terminal variance, so P(above) moves
    toward the deterministic indicator of (spot >= strike). With spot
    just below the strike, the deterministic value is 0, so a longer
    TWAP window pushes P(above) toward zero."""

    common = dict(
        spot=Decimal("99000"),
        strike=Decimal("100000"),
        sigma_annual=Decimal("0.60"),
        tau_seconds=Decimal("900"),
    )
    p_instant = bs_above_probability(twap_window_seconds=Decimal("0"), **common)
    p_twap = bs_above_probability(twap_window_seconds=Decimal("600"), **common)
    assert p_twap < p_instant


def test_bracket_parity_deviation_positive_when_sum_exceeds_one() -> None:
    dev = bracket_parity_deviation(
        {"a": Decimal("0.35"), "b": Decimal("0.35"), "c": Decimal("0.35")}
    )
    assert dev == Decimal("0.05")


def test_bracket_parity_deviation_negative_when_sum_below_one() -> None:
    dev = bracket_parity_deviation(
        {"a": Decimal("0.20"), "b": Decimal("0.30"), "c": Decimal("0.40")}
    )
    assert dev == Decimal("-0.10")


def test_monotone_violations_detects_inversion() -> None:
    violations = monotone_violations(
        (
            (Decimal("100000"), Decimal("0.60")),
            (Decimal("101000"), Decimal("0.65")),  # impossible higher strike
            (Decimal("102000"), Decimal("0.40")),
        )
    )
    assert len(violations) == 1
    assert violations[0] == (
        Decimal("100000"),
        Decimal("0.60"),
        Decimal("101000"),
        Decimal("0.65"),
    )


def test_monotone_violations_empty_when_well_ordered() -> None:
    violations = monotone_violations(
        (
            (Decimal("100000"), Decimal("0.70")),
            (Decimal("101000"), Decimal("0.50")),
            (Decimal("102000"), Decimal("0.30")),
        )
    )
    assert violations == ()


def test_realized_volatility_zero_for_constant_series() -> None:
    rv = realized_volatility([Decimal("100000")] * 60)
    assert rv == Decimal("0")


def test_realized_volatility_grows_with_jumps() -> None:
    quiet = realized_volatility([Decimal("100000") + Decimal(str(i)) for i in range(60)])
    noisy = realized_volatility(
        [Decimal("100000") + Decimal(str(i * 100)) for i in range(60)]
    )
    assert noisy > quiet


def test_realized_volatility_short_series_returns_zero() -> None:
    assert realized_volatility([Decimal("100000")]) == Decimal("0")
    assert realized_volatility([]) == Decimal("0")
