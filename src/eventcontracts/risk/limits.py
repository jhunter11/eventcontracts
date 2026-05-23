"""Risk limit primitives."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from eventcontracts.domain.decisions import PlaceOrder
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
    """Return rejection reasons for simple order-level risk checks."""

    notional = order_notional(order)
    reasons: list[str] = []
    if notional > profile.max_order_notional:
        reasons.append("max_order_notional")
    return tuple(reasons)


class RiskLimitService:
    """Evaluates proposed orders against configured limits."""

    def check(self) -> bool:
        raise NotImplementedError
