"""Queue-position modeling interfaces."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from eventcontracts.domain.models import InstrumentId, OutcomeSide
from eventcontracts.domain.validation import require_non_empty, require_non_negative_decimal


@dataclass(frozen=True)
class QueueEstimate:
    ahead_quantity: Decimal
    confidence: Decimal
    source: str

    def __post_init__(self) -> None:
        require_non_negative_decimal(self.ahead_quantity, "ahead_quantity")
        if self.confidence < Decimal("0") or self.confidence > Decimal("1"):
            raise ValueError(f"confidence must be between 0 and 1: {self.confidence}")
        require_non_empty(self.source, "source")


class QueuePositionEstimator:
    """Estimate passive-order queue position from native APIs or reconstructed books."""

    def estimate(
        self,
        instrument_id: InstrumentId,
        side: OutcomeSide,
        price: Decimal,
        quantity: Decimal,
    ) -> QueueEstimate:
        raise NotImplementedError
