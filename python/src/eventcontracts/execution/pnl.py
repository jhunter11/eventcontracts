"""Position keeper and PnL accountant for paper / replay sleeves.

Subscribes to fills and quote events. Maintains per-(instrument, side)
positions with weighted-average cost basis, accumulates realized PnL
on closing fills, and marks open positions to the latest quote mid for
unrealized PnL.

This is the in-process accountant used by backtests. A live sleeve
would replace this with a service backed by a double-entry ledger, but
the dataflow (fills in, positions/PnL out) is the same.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from eventcontracts.domain.events import NormalizedEvent, QuoteEvent
from eventcontracts.domain.fills import Fill
from eventcontracts.domain.models import InstrumentId, OutcomeSide
from eventcontracts.domain.orders import OrderSide
from eventcontracts.domain.positions import Position
from eventcontracts.execution.market_simulator import FillSink
from eventcontracts.risk.state import DailyLossLedger


@dataclass
class PositionRecord:
    """Mutable position state owned by the PnL tracker."""

    instrument_id: InstrumentId
    outcome_side: OutcomeSide
    quantity: Decimal = Decimal("0")
    average_price: Decimal = Decimal("0")
    realized_pnl: Decimal = Decimal("0")
    mark_price: Decimal | None = None
    updated_at: datetime | None = None

    def to_position(self, now: datetime) -> Position:
        """Snapshot as an immutable domain :class:`Position`."""

        mark = self.mark_price if self.mark_price is not None else self.average_price
        unrealized = (
            (mark - self.average_price) * self.quantity if self.quantity > 0 else Decimal("0")
        )
        return Position(
            instrument_id=self.instrument_id,
            outcome_side=self.outcome_side,
            quantity=self.quantity,
            average_price=self.average_price,
            realized_pnl=self.realized_pnl,
            unrealized_pnl=unrealized,
            updated_at=self.updated_at or now,
        )


@dataclass
class PnLTracker:
    """Stateful PnL accountant. Implements :class:`FillSink`."""

    currency: str = "USD"
    daily_loss_ledger: DailyLossLedger | None = None
    records: dict[tuple[InstrumentId, OutcomeSide], PositionRecord] = field(default_factory=dict)
    total_fees_paid: Decimal = Decimal("0")
    cumulative_realized: Decimal = Decimal("0")

    def _record(self, instrument_id: InstrumentId, side: OutcomeSide) -> PositionRecord:
        key = (instrument_id, side)
        rec = self.records.get(key)
        if rec is None:
            rec = PositionRecord(instrument_id=instrument_id, outcome_side=side)
            self.records[key] = rec
        return rec

    def on_fill(self, fill: Fill) -> None:
        """Update position and realized PnL from a fill."""

        rec = self._record(fill.instrument_id, fill.outcome_side)
        self.total_fees_paid += fill.fee_amount

        if fill.order_side is OrderSide.BUY:
            # Increase position; recompute weighted average price.
            new_qty = rec.quantity + fill.quantity
            if new_qty > 0:
                rec.average_price = (
                    (rec.quantity * rec.average_price) + (fill.quantity * fill.price)
                ) / new_qty
            rec.quantity = new_qty
        else:
            # Sell: close at fill price. Realized PnL captured for the
            # quantity that closes existing inventory; anything beyond
            # would open a short, which prediction venues do not allow,
            # so clamp at zero.
            close_qty = min(rec.quantity, fill.quantity)
            realized = (fill.price - rec.average_price) * close_qty
            rec.realized_pnl += realized - fill.fee_amount
            self.cumulative_realized += realized - fill.fee_amount
            if self.daily_loss_ledger is not None:
                self.daily_loss_ledger.record_realized_pnl(realized - fill.fee_amount, fill.filled_at)
            rec.quantity -= close_qty

        # Buys also incur fees, deducted from realized for accounting.
        if fill.order_side is OrderSide.BUY:
            rec.realized_pnl -= fill.fee_amount
            self.cumulative_realized -= fill.fee_amount

        rec.updated_at = fill.filled_at

    def on_event(self, event: NormalizedEvent) -> None:
        """Update mark prices from quote events."""

        if isinstance(event, QuoteEvent):
            quote = event.quote
            rec = self._record(quote.instrument_id, quote.side)
            if quote.bid is not None and quote.ask is not None:
                rec.mark_price = (quote.bid.price + quote.ask.price) / Decimal("2")
            elif quote.bid is not None:
                rec.mark_price = quote.bid.price
            elif quote.ask is not None:
                rec.mark_price = quote.ask.price
            rec.updated_at = quote.received_at

    # ---------- inspection ----------

    def position(
        self, instrument_id: InstrumentId, side: OutcomeSide, *, now: datetime
    ) -> Position | None:
        rec = self.records.get((instrument_id, side))
        if rec is None or rec.quantity <= 0:
            return None
        return rec.to_position(now)

    def positions(self, *, now: datetime) -> Iterable[Position]:
        return tuple(
            rec.to_position(now)
            for rec in self.records.values()
            if rec.quantity > 0
        )

    def total_pnl(self, *, now: datetime) -> Decimal:
        unrealized = sum(
            (
                rec.to_position(now).unrealized_pnl
                for rec in self.records.values()
                if rec.quantity > 0
            ),
            Decimal("0"),
        )
        return self.cumulative_realized + unrealized
