"""Stateful risk objects: daily loss ledger and kill switch.

These objects are shared between the strategy runner (which feeds them
PnL updates) and the risk gate (which queries them). They live in their
own module so the limit-checking functions in
:mod:`eventcontracts.risk.limits` can remain pure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal


@dataclass
class DailyLossLedger:
    """Accumulates realized loss by UTC date.

    The PnL accountant calls :meth:`record_realized_pnl` each time a
    position closes (or partially closes). Loss is tracked as a
    positive number so it can be compared directly to
    ``RiskProfile.max_daily_loss``.
    """

    losses_by_day: dict[date, Decimal] = field(default_factory=dict)

    def record_realized_pnl(self, pnl: Decimal, at: datetime) -> None:
        if pnl >= 0:
            return
        day = at.date()
        self.losses_by_day[day] = self.losses_by_day.get(day, Decimal("0")) + (-pnl)

    def loss_for(self, at: datetime) -> Decimal:
        return self.losses_by_day.get(at.date(), Decimal("0"))

    def reset(self) -> None:
        self.losses_by_day.clear()


@dataclass
class KillSwitch:
    """Trip-flag that, when set, causes the risk gate to reject everything.

    Tripping is one-way for the lifetime of the process; an operator must
    explicitly :meth:`reset` it once the underlying incident is handled.
    """

    tripped: bool = False
    reason: str | None = None
    tripped_at: datetime | None = None

    def trip(self, reason: str, at: datetime) -> None:
        if self.tripped:
            return
        self.tripped = True
        self.reason = reason
        self.tripped_at = at

    def reset(self) -> None:
        self.tripped = False
        self.reason = None
        self.tripped_at = None
