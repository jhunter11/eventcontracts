"""Position keeper and PnL accountant for paper / replay sleeves.

Subscribes to fills and quote events. Maintains per-(instrument, side)
positions with weighted-average cost basis, accumulates realized PnL
on closing fills, and marks open positions to the latest quote. Reporting
defaults to mid marks; risk drawdown is fed from liquidation marks.

This is the in-process accountant used by backtests. A live sleeve
would replace this with a service backed by a double-entry ledger, but
the dataflow (fills in, positions/PnL out) is the same.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from eventcontracts.domain.events import NormalizedEvent, QuoteEvent, SettlementResolvedEvent
from eventcontracts.domain.fees import FeeModel, FillContext
from eventcontracts.domain.fills import Fill
from eventcontracts.domain.lifecycle import SettlementEvent
from eventcontracts.domain.models import InstrumentId, OutcomeSide
from eventcontracts.domain.orders import OrderSide
from eventcontracts.domain.positions import Position
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
    liquidation_mark_price: Decimal | None = None
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

    def unrealized_pnl(self, *, mode: str = "mid") -> Decimal:
        if self.quantity <= 0:
            return Decimal("0")
        mark = self.liquidation_mark_price if mode == "liquidation" else self.mark_price
        if mark is None:
            mark = self.average_price
        return (mark - self.average_price) * self.quantity


@dataclass
class PnLTracker:
    """Stateful PnL accountant. Implements :class:`FillSink`."""

    currency: str = "USD"
    daily_loss_ledger: DailyLossLedger | None = None
    settlement_fee_model: FeeModel | None = None
    mark_mode: str = "mid"
    records: dict[tuple[InstrumentId, OutcomeSide], PositionRecord] = field(default_factory=dict)
    total_fees_paid: Decimal = Decimal("0")
    cumulative_realized: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        if self.mark_mode not in {"mid", "liquidation"}:
            raise ValueError("mark_mode must be 'mid' or 'liquidation'")

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
            if self.daily_loss_ledger is not None and fill.fee_amount > 0:
                self.daily_loss_ledger.record_realized_pnl(-fill.fee_amount, fill.filled_at)

        rec.updated_at = fill.filled_at

    def on_event(self, event: NormalizedEvent) -> None:
        """Update mark prices from quotes and realize PnL on settlement."""

        if isinstance(event, QuoteEvent):
            quote = event.quote
            rec = self.records.get((quote.instrument_id, quote.side))
            if rec is None:
                return
            mid_mark: Decimal | None = None
            liquidation_mark: Decimal | None = None
            if quote.bid is not None and quote.ask is not None:
                mid_mark = (quote.bid.price + quote.ask.price) / Decimal("2")
                liquidation_mark = quote.bid.price
            elif quote.bid is not None:
                mid_mark = quote.bid.price
                liquidation_mark = quote.bid.price
            elif quote.ask is not None:
                mid_mark = quote.ask.price
                liquidation_mark = quote.ask.price
            rec.mark_price = liquidation_mark if self.mark_mode == "liquidation" else mid_mark
            rec.liquidation_mark_price = liquidation_mark
            rec.updated_at = quote.received_at
            if self.daily_loss_ledger is not None:
                self.daily_loss_ledger.record_unrealized_pnl(
                    self.unrealized_pnl(now=quote.received_at, mark_mode="liquidation"),
                    quote.received_at,
                )
        elif isinstance(event, SettlementResolvedEvent):
            self.on_settlement(event.settlement)

    def on_settlement(self, settlement: SettlementEvent) -> None:
        """Realize PnL for any open position in a resolved instrument.

        Event-contract settlement is the dominant PnL realization for any
        held-to-expiry strategy. ``resolved_side`` names the winning outcome;
        holders of that outcome receive ``payout_per_contract``, holders of the
        opposite outcome receive zero. ``resolved_side`` of ``None`` (canceled
        / disputed) does not realize — the venue's cancellation flow is
        out-of-scope for this in-process tracker.
        """

        if settlement.resolved_side is None:
            return

        for side in (OutcomeSide.YES, OutcomeSide.NO):
            rec = self.records.get((settlement.instrument_id, side))
            if rec is None or rec.quantity <= 0:
                continue
            payout = (
                settlement.payout_per_contract
                if side is settlement.resolved_side
                else Decimal("0")
            )
            settlement_fee = self._settlement_fee(
                settlement.instrument_id,
                side,
                payout,
                rec.quantity,
            )
            realized = ((payout - rec.average_price) * rec.quantity) - settlement_fee
            rec.realized_pnl += realized
            self.cumulative_realized += realized
            self.total_fees_paid += settlement_fee
            if self.daily_loss_ledger is not None:
                self.daily_loss_ledger.record_realized_pnl(realized, settlement.settled_at)
            rec.quantity = Decimal("0")
            rec.average_price = Decimal("0")
            rec.mark_price = payout
            rec.updated_at = settlement.settled_at

    def _settlement_fee(
        self,
        instrument_id: InstrumentId,
        side: OutcomeSide,
        payout: Decimal,
        quantity: Decimal,
    ) -> Decimal:
        if self.settlement_fee_model is None:
            return Decimal("0")
        estimate = self.settlement_fee_model.estimate(
            FillContext(
                instrument_id=instrument_id,
                side=side,
                price=payout,
                quantity=quantity,
                liquidity="settlement",
            )
        )
        return estimate.amount

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
        return self.cumulative_realized + self.unrealized_pnl(now=now)

    def unrealized_pnl(self, *, now: datetime, mark_mode: str | None = None) -> Decimal:
        del now
        mode = mark_mode or self.mark_mode
        return sum(
            (rec.unrealized_pnl(mode=mode) for rec in self.records.values() if rec.quantity > 0),
            Decimal("0"),
        )
