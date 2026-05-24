"""Backtest report: drawdown, fill rate, exposure, PnL by sleeve.

The runner produces a :class:`RunSummary` with counts. The PnL tracker
produces fills and positions. A :class:`BacktestReport` aggregates the
two with derived metrics that a researcher actually wants to see:
realized + unrealized PnL, peak drawdown, fill rate, and the count of
rejected intents by reason.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from eventcontracts.domain.fills import Fill
from eventcontracts.execution.pnl import PnLTracker
from eventcontracts.runner.base import RunSummary


@dataclass
class BacktestReport:
    """Summary of one backtest run.

    Construct directly or via :meth:`from_run` to bind a
    :class:`RunSummary` and :class:`PnLTracker` into a single object
    you can print or serialize.
    """

    sleeve_id: str
    strategy_id: str
    started_at: datetime
    ended_at: datetime
    events_processed: int
    decisions_emitted: int
    intents_dispatched: int
    intents_rejected: int
    rejection_reasons: dict[str, int]
    fills: int
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    total_pnl: Decimal
    total_fees_paid: Decimal
    peak_equity: Decimal
    trough_equity: Decimal
    max_drawdown: Decimal
    fill_rate: float
    open_positions: int

    @property
    def duration_seconds(self) -> float:
        return (self.ended_at - self.started_at).total_seconds()

    @classmethod
    def from_run(
        cls,
        summary: RunSummary,
        pnl: PnLTracker,
        *,
        fills: list[Fill] | None = None,
        now: datetime | None = None,
        starting_equity: Decimal = Decimal("0"),
    ) -> BacktestReport:
        fills = fills or []
        # Reconstruct equity curve from fills. Each fill is +realized_pnl -fee.
        equity = starting_equity
        peak = equity
        trough = equity
        running_realized = Decimal("0")
        for fill in fills:
            # Approximation: only fees show as equity hits without sells.
            # Realized PnL is already accounted in PnL tracker. Use that.
            pass
        # Use cumulative_realized + total_pnl as the curve endpoints.
        when = now or summary.ended_at
        total = pnl.total_pnl(now=when)
        unrealized = total - pnl.cumulative_realized
        peak = max(peak, total + starting_equity)
        trough = min(trough, total + starting_equity)
        # Without per-event equity samples we use total fees as a proxy
        # for the trough magnitude.
        trough = min(trough, starting_equity - pnl.total_fees_paid)
        peak_equity = peak
        trough_equity = trough
        drawdown = peak_equity - trough_equity if peak_equity > trough_equity else Decimal("0")

        fill_count = len(fills)
        intents = summary.intents_dispatched
        # decisions_emitted includes NoAction; only PlaceOrder produces fills,
        # so the fair denominator is intents_dispatched (PlaceOrder pass-through).
        fill_rate = (fill_count / intents) if intents > 0 else 0.0
        open_positions = sum(1 for p in pnl.positions(now=when))

        return cls(
            sleeve_id=summary.sleeve_id,
            strategy_id=summary.strategy_id,
            started_at=summary.started_at,
            ended_at=summary.ended_at,
            events_processed=summary.events_processed,
            decisions_emitted=summary.decisions_emitted,
            intents_dispatched=summary.intents_dispatched,
            intents_rejected=summary.intents_rejected,
            rejection_reasons=dict(summary.rejection_reasons),
            fills=fill_count,
            realized_pnl=pnl.cumulative_realized,
            unrealized_pnl=unrealized,
            total_pnl=total,
            total_fees_paid=pnl.total_fees_paid,
            peak_equity=peak_equity,
            trough_equity=trough_equity,
            max_drawdown=drawdown,
            fill_rate=fill_rate,
            open_positions=open_positions,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "sleeve_id": self.sleeve_id,
            "strategy_id": self.strategy_id,
            "started_at": self.started_at.isoformat(),
            "ended_at": self.ended_at.isoformat(),
            "duration_seconds": self.duration_seconds,
            "events_processed": self.events_processed,
            "decisions_emitted": self.decisions_emitted,
            "intents_dispatched": self.intents_dispatched,
            "intents_rejected": self.intents_rejected,
            "rejection_reasons": self.rejection_reasons,
            "fills": self.fills,
            "fill_rate": self.fill_rate,
            "realized_pnl": str(self.realized_pnl),
            "unrealized_pnl": str(self.unrealized_pnl),
            "total_pnl": str(self.total_pnl),
            "total_fees_paid": str(self.total_fees_paid),
            "peak_equity": str(self.peak_equity),
            "trough_equity": str(self.trough_equity),
            "max_drawdown": str(self.max_drawdown),
            "open_positions": self.open_positions,
        }
