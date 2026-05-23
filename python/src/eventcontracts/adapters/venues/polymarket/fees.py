"""Polymarket fee model placeholder."""

from __future__ import annotations

from eventcontracts.domain.fees import FeeEstimate, FeeModel, FillContext


class PolymarketFeeModel(FeeModel):
    name = "polymarket-category-fill-level"

    def estimate(self, fill: FillContext) -> FeeEstimate:
        raise NotImplementedError
