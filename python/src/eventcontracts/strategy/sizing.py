"""Edge-proportional position sizing for binary event contracts.

Fixed per-strategy clip sizes over-bet thin edges and under-bet fat ones. This
helper sizes an *entry* (a BUY that opens/adds to a position) proportional to the
model edge via a fractional-Kelly rule, then hard-caps the stake by the sleeve's
risk limits and available cash. It is pure and deterministic so it can be
parity-tested and reused by every archetype.

Kelly for a binary contract that costs ``p`` and pays 1 on win with win
probability ``q`` (the model fair value): the cost-fraction of bankroll at full
Kelly is ``(q - p) / (1 - p)``. We apply a ``fraction`` multiplier (quarter- or
half-Kelly) for drawdown control, convert the dollar stake to whole contracts at
the entry price, and clamp by every supplied cap. Exits (risk-reducing sells)
are a separate concern and are not sized here.
"""

from __future__ import annotations

from decimal import ROUND_FLOOR, Decimal


def fractional_kelly_contracts(
    *,
    fair: Decimal,
    price: Decimal,
    bankroll: Decimal,
    fraction: Decimal = Decimal("0.25"),
    max_order_notional: Decimal | None = None,
    max_position_notional: Decimal | None = None,
    available_cash: Decimal | None = None,
    current_position_notional: Decimal = Decimal("0"),
) -> int:
    """Whole-contract entry size from edge, capped by risk limits and cash.

    Returns 0 when there is no positive edge, the price is outside ``(0, 1)``,
    the bankroll is non-positive, or every cap leaves no room. The result is
    always a non-negative integer (event contracts are integer-quantity).
    """

    edge = fair - price
    if edge <= 0 or price <= 0 or price >= 1 or bankroll <= 0 or fraction <= 0:
        return 0

    kelly_cost_fraction = edge / (Decimal("1") - price)
    stake_dollars = fraction * kelly_cost_fraction * bankroll

    if available_cash is not None:
        stake_dollars = min(stake_dollars, available_cash)
    if max_order_notional is not None:
        stake_dollars = min(stake_dollars, max_order_notional)
    if max_position_notional is not None:
        headroom = max_position_notional - current_position_notional
        stake_dollars = min(stake_dollars, headroom if headroom > 0 else Decimal("0"))

    if stake_dollars <= 0:
        return 0
    contracts = (stake_dollars / price).to_integral_value(rounding=ROUND_FLOOR)
    return max(int(contracts), 0)
