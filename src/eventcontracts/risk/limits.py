"""Risk limit primitives."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class RiskLimits:
    max_order_notional: Decimal
    max_position_notional: Decimal
    max_daily_loss: Decimal
    currency: str


class RiskLimitService:
    """Evaluates proposed orders against configured limits."""

    def check(self) -> bool:
        raise NotImplementedError
