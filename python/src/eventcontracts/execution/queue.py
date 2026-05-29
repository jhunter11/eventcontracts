"""Queue-position modeling.

When a passive (post-only / limit-maker) order rests on the book, its
fill probability depends on how much volume sits ahead of it at the same
price level. Queue position is invisible until the order is at the
front, so the simulator estimates it from the visible depth at submit
time.

Estimators here are pure functions of the order intent plus a current
:class:`OrderBook` snapshot. The full paper simulator threads the
result through fill-probability calculations as time passes.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from eventcontracts.domain.models import InstrumentId, OrderBook, OrderBookLevel, OutcomeSide
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


class QueuePositionEstimator(Protocol):
    """Estimate passive-order queue position at submit time."""

    def estimate(
        self,
        *,
        instrument_id: InstrumentId,
        side: OutcomeSide,
        order_side: str,
        price: Decimal,
        quantity: Decimal,
        book: OrderBook | None,
    ) -> QueueEstimate: ...


def _select_levels(
    book: OrderBook,
    side: OutcomeSide,
    order_side: str,
) -> tuple[OrderBookLevel, ...]:
    """Pick the book side a passive order rests on.

    A BUY on YES rests on yes_bids; a SELL on YES rests on yes_asks. NO
    contracts are handled symmetrically.
    """

    if side is OutcomeSide.YES:
        return book.yes_bids if order_side == "buy" else book.yes_asks
    return book.no_bids if order_side == "buy" else book.no_asks


def _ahead_at_price(levels: tuple[OrderBookLevel, ...], price: Decimal) -> Decimal:
    """Sum visible quantity at the exact resting price."""

    return sum((lvl.quantity for lvl in levels if lvl.price == price), Decimal("0"))


@dataclass(frozen=True)
class DepthQueueEstimator:
    """Use total visible depth at the resting price as the ahead estimate.

    This is conservative: it assumes the submitter joins the back of
    the queue, which is the worst case for passive fill probability.
    """

    name: str = "depth-at-level"

    def estimate(
        self,
        *,
        instrument_id: InstrumentId,
        side: OutcomeSide,
        order_side: str,
        price: Decimal,
        quantity: Decimal,
        book: OrderBook | None,
    ) -> QueueEstimate:
        if book is None:
            return QueueEstimate(
                ahead_quantity=quantity,
                confidence=Decimal("0"),
                source=self.name,
            )
        levels = _select_levels(book, side, order_side)
        ahead = _ahead_at_price(levels, price)
        return QueueEstimate(
            ahead_quantity=ahead,
            confidence=Decimal("0.6"),
            source=self.name,
        )


@dataclass(frozen=True)
class FractionalQueueEstimator:
    """Use a configurable fraction of visible depth as the ahead estimate.

    Real venues such as Kalshi expose per-order queue position; until
    that wiring lands, this is a useful tunable approximation. Pass a
    fraction in (0, 1] to model the assumption that the submitter is on
    average at the back of some queue depth.
    """

    fraction: Decimal = Decimal("1.0")
    confidence: Decimal = Decimal("0.4")
    name: str = "fractional-depth"

    def __post_init__(self) -> None:
        if not (Decimal("0") < self.fraction <= Decimal("1")):
            raise ValueError("fraction must be in (0, 1]")
        if not (Decimal("0") <= self.confidence <= Decimal("1")):
            raise ValueError("confidence must be in [0, 1]")

    def estimate(
        self,
        *,
        instrument_id: InstrumentId,
        side: OutcomeSide,
        order_side: str,
        price: Decimal,
        quantity: Decimal,
        book: OrderBook | None,
    ) -> QueueEstimate:
        if book is None:
            return QueueEstimate(
                ahead_quantity=quantity,
                confidence=Decimal("0"),
                source=self.name,
            )
        levels = _select_levels(book, side, order_side)
        total = _ahead_at_price(levels, price)
        return QueueEstimate(
            ahead_quantity=total * self.fraction,
            confidence=self.confidence,
            source=self.name,
        )


@dataclass(frozen=True)
class FrontOfQueueEstimator:
    """For tests: claim the submitter is at the front (ahead == 0)."""

    name: str = "front-of-queue"

    def estimate(
        self,
        *,
        instrument_id: InstrumentId,
        side: OutcomeSide,
        order_side: str,
        price: Decimal,
        quantity: Decimal,
        book: OrderBook | None,
    ) -> QueueEstimate:
        return QueueEstimate(
            ahead_quantity=Decimal("0"),
            confidence=Decimal("1.0"),
            source=self.name,
        )
