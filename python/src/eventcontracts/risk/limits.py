"""Risk limit primitives.

Each ``check_*`` function returns a tuple of rejection reasons (empty tuple
means "no objection"). Composing them gives the full pre-trade gate behavior
used by :class:`eventcontracts.risk.policy.SleeveRiskGate`.

Limits are deterministic and side-effect-free; stateful concerns (daily loss
ledger, kill switches) live in :mod:`eventcontracts.risk.state`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from eventcontracts.domain.decisions import PlaceOrder
from eventcontracts.domain.orders import Order, OrderSide
from eventcontracts.domain.positions import CashBalance, Exposure, Position
from eventcontracts.domain.spec import RiskProfile


@dataclass(frozen=True)
class RiskLimits:
    max_order_notional: Decimal
    max_position_notional: Decimal
    max_daily_loss: Decimal
    currency: str


def order_notional(order: PlaceOrder) -> Decimal:
    """Conservative notional estimate for a strategy order decision."""

    price = order.price if order.price is not None else Decimal("1")
    return price * order.quantity


def check_order_notional(order: PlaceOrder, profile: RiskProfile) -> tuple[str, ...]:
    """Reject if the order notional exceeds the per-order limit."""

    if order_notional(order) > profile.max_order_notional:
        return ("max_order_notional",)
    return ()


def check_position_notional(
    order: PlaceOrder,
    profile: RiskProfile,
    positions: Sequence[Position],
) -> tuple[str, ...]:
    """Reject if the order would push position notional over the limit.

    Notional is computed at the same outcome-side bucket as the order. For
    a binary contract, a BUY on YES adds to the YES position; a SELL on YES
    reduces it (and only triggers the limit if it crosses to net short, which
    binary contracts on Kalshi do not allow, so the SELL path is conservative).
    """

    key = (order.instrument_id, order.outcome_side)
    current_quantity = Decimal("0")
    current_avg = Decimal("0")
    for pos in positions:
        if (pos.instrument_id, pos.outcome_side) == key:
            current_quantity = pos.quantity
            current_avg = pos.average_price

    delta = order.quantity if order.order_side is OrderSide.BUY else -order.quantity
    projected_quantity = current_quantity + delta
    price = order.price if order.price is not None else Decimal("1")
    if projected_quantity < 0:
        # Reducing past zero is treated as a sale of the existing position only;
        # the venue rejects the rest. Cap at zero for notional purposes.
        projected_quantity = Decimal("0")

    avg = current_avg if current_quantity > 0 else price
    projected_notional = projected_quantity * avg
    if projected_notional > profile.max_position_notional:
        return ("max_position_notional",)
    return ()


def check_open_orders(open_orders: Sequence[Order], profile: RiskProfile) -> tuple[str, ...]:
    if len(open_orders) >= profile.max_open_orders:
        return ("max_open_orders",)
    return ()


def check_gross_exposure(
    order: PlaceOrder,
    profile: RiskProfile,
    exposure: Exposure | None,
) -> tuple[str, ...]:
    """Reject if the order would push sleeve gross exposure over the limit."""

    if exposure is None:
        return ()
    projected = exposure.gross_notional + order_notional(order)
    if projected > profile.max_gross_exposure:
        return ("max_gross_exposure",)
    return ()


def check_available_cash(order: PlaceOrder, balance: CashBalance) -> tuple[str, ...]:
    """Reject if the order would spend more cash than is currently available."""

    if order_notional(order) > balance.available:
        return ("available_cash",)
    return ()


def check_daily_loss(
    profile: RiskProfile,
    realized_loss_today: Decimal,
) -> tuple[str, ...]:
    """Reject everything once the sleeve has already lost more than the limit today."""

    if profile.max_daily_loss > 0 and realized_loss_today >= profile.max_daily_loss:
        return ("max_daily_loss",)
    return ()
