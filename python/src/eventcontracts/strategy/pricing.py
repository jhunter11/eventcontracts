"""Deterministic price discretisation helpers.

Venue prices live on a tick grid (Kalshi: 1 cent). A model fair value almost
never lands on a tick, so a strategy must round to place an order — and the
*direction* of that rounding decides whether the rounding step itself consumes
edge. The rule these helpers enforce:

* a BUY limit must be **floored** to a tick (never pay above fair), and
* a SELL limit must be **ceiled** to a tick (never sell below fair).

Rounding a 0.556 fair to a 0.56 buy limit would pay 0.004 above fair and can
flip a positive model edge negative; flooring to 0.55 preserves it. Using these
helpers — instead of ad-hoc ``round()``/``_clip`` in each strategy — keeps the
edge-preserving direction consistent and parity-testable against the Rust
``runner::pricing`` equivalents.
"""

from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal

# Kalshi trades on a 1-cent grid. Polymarket is finer; pass an explicit tick.
DEFAULT_TICK = Decimal("0.01")


def _validate_tick(tick: Decimal) -> None:
    if tick <= 0:
        raise ValueError("tick must be positive")


def floor_to_tick(price: Decimal, tick: Decimal = DEFAULT_TICK) -> Decimal:
    """Largest tick-multiple <= ``price``."""

    _validate_tick(tick)
    return (price / tick).to_integral_value(rounding=ROUND_FLOOR) * tick


def ceil_to_tick(price: Decimal, tick: Decimal = DEFAULT_TICK) -> Decimal:
    """Smallest tick-multiple >= ``price``."""

    _validate_tick(tick)
    return (price / tick).to_integral_value(rounding=ROUND_CEILING) * tick


def round_to_tick(price: Decimal, tick: Decimal = DEFAULT_TICK) -> Decimal:
    """Nearest tick-multiple (ties away from zero). For display/marks, not edge."""

    _validate_tick(tick)
    return (price / tick).to_integral_value(rounding=ROUND_HALF_UP) * tick


def clamp_price(price: Decimal, tick: Decimal = DEFAULT_TICK) -> Decimal:
    """Clamp into the tradable band ``[tick, 1 - tick]`` for a binary contract."""

    _validate_tick(tick)
    low = tick
    high = Decimal("1") - tick
    if price < low:
        return low
    if price > high:
        return high
    return price


def buy_limit_from_fair(fair: Decimal, tick: Decimal = DEFAULT_TICK) -> Decimal:
    """Edge-preserving BUY limit: floor the fair value to the tick grid."""

    return clamp_price(floor_to_tick(fair, tick), tick)


def sell_limit_from_fair(fair: Decimal, tick: Decimal = DEFAULT_TICK) -> Decimal:
    """Edge-preserving SELL limit: ceil the fair value to the tick grid."""

    return clamp_price(ceil_to_tick(fair, tick), tick)
