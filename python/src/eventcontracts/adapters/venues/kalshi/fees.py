"""Kalshi fee model.

Kalshi charges per-fill trading fees on expected earnings. The standard taker
curve is 7% of ``price * (1 - price) * contracts`` rounded up to the nearest
cent. Some series also charge maker fees; the default maker curve here is the
published 1.75% formula so passive strategy tests do not assume free fills.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal

from eventcontracts.domain.fees import FeeEstimate, FeeModel, FillContext


@dataclass(frozen=True)
class KalshiFeeModel(FeeModel):
    rate: Decimal = Decimal("0.07")
    maker_rate: Decimal = Decimal("0.0175")
    currency: str = "USD"
    name: str = "kalshi-fill-level"

    def estimate(self, fill: FillContext) -> FeeEstimate:
        rate = self.maker_rate if fill.liquidity == "maker" else self.rate
        notional_per_contract = fill.price * (Decimal("1") - fill.price)
        raw = rate * notional_per_contract * fill.quantity
        amount = raw.quantize(Decimal("0.01"), rounding=ROUND_CEILING)
        return FeeEstimate(
            amount=amount,
            currency=self.currency,
            model_name=self.name,
            notes=(("maker-fee",) if fill.liquidity == "maker" else ()),
        )
