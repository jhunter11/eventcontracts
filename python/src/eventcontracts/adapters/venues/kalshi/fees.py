"""Kalshi fee model.

Kalshi charges a per-fill trading fee on most markets. The published
formula is::

    fee_cents = ceil(100 * 0.07 * price * (1 - price) * contracts)

with ``price`` in dollars (i.e. between 0 and 1). The fee is paid in
USD and rounded up to the nearest cent. Some market families
(e.g. some FX or rate markets) use a reduced rate; pass ``rate`` to
override.

References:

- https://kalshi.com/docs/fees
- https://kalshi.com/docs/api (commissions section)
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal

from eventcontracts.domain.fees import FeeEstimate, FeeModel, FillContext


@dataclass(frozen=True)
class KalshiFeeModel(FeeModel):
    rate: Decimal = Decimal("0.07")
    currency: str = "USD"
    name: str = "kalshi-fill-level"

    def estimate(self, fill: FillContext) -> FeeEstimate:
        # Always paid by the taker side of the trade. For maker fills,
        # Kalshi rebates nothing (fee remains zero) but the executor
        # may still call us with liquidity="maker" — return zero.
        if fill.liquidity == "maker":
            return FeeEstimate(
                amount=Decimal("0"),
                currency=self.currency,
                model_name=self.name,
                notes=("maker-rebate-implicit",),
            )

        notional_per_contract = fill.price * (Decimal("1") - fill.price)
        raw = self.rate * notional_per_contract * fill.quantity
        # Round up to the cent.
        amount = raw.quantize(Decimal("0.01"), rounding=ROUND_CEILING)
        return FeeEstimate(
            amount=amount,
            currency=self.currency,
            model_name=self.name,
        )
