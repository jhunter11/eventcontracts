"""Polymarket fee model.

Polymarket runs an order book on Polygon. Maker fills are free on most
markets; taker fills pay a category-dependent fee in the quote token
(USDC). The default rate of 2% reflects the historical taker fee on
binary contracts. Pass ``taker_rate`` to override per market.

References:

- https://docs.polymarket.com (CLOB section)
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from eventcontracts.domain.fees import FeeEstimate, FeeModel, FillContext


@dataclass(frozen=True)
class PolymarketFeeModel(FeeModel):
    maker_rate: Decimal = Decimal("0.0")
    taker_rate: Decimal = Decimal("0.02")
    currency: str = "USD"
    name: str = "polymarket-category-fill-level"

    def estimate(self, fill: FillContext) -> FeeEstimate:
        rate = self.maker_rate if fill.liquidity == "maker" else self.taker_rate
        notional = fill.price * fill.quantity
        amount = (rate * notional).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
        return FeeEstimate(
            amount=amount,
            currency=self.currency,
            model_name=self.name,
        )
