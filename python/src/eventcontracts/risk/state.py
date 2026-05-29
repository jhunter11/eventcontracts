"""Stateful risk objects: daily loss ledger and kill switch.

These objects are shared between the strategy runner (which feeds them
PnL updates) and the risk gate (which queries them). They live in their
own module so the limit-checking functions in
:mod:`eventcontracts.risk.limits` can remain pure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal

from eventcontracts.domain.validation import require_aware_datetime


@dataclass
class DailyLossLedger:
    """Accumulates net realized PnL by UTC date.

    The PnL accountant calls :meth:`record_realized_pnl` each time a
    position closes (or partially closes). ``loss_for`` returns the positive
    loss implied by net realized PnL for the day, so wins offset losses.
    """

    net_pnl_by_day: dict[date, Decimal] = field(default_factory=dict)
    unrealized_pnl_by_day: dict[date, Decimal] = field(default_factory=dict)

    def record_realized_pnl(self, pnl: Decimal, at: datetime) -> None:
        day = _utc_day(at)
        self.net_pnl_by_day[day] = self.net_pnl_by_day.get(day, Decimal("0")) + pnl

    def net_pnl_for(self, at: datetime) -> Decimal:
        return self.net_pnl_by_day.get(_utc_day(at), Decimal("0"))

    def record_unrealized_pnl(self, pnl: Decimal, at: datetime) -> None:
        """Store the latest liquidation-mark unrealized PnL for the UTC day."""

        self.unrealized_pnl_by_day[_utc_day(at)] = pnl

    def unrealized_pnl_for(self, at: datetime) -> Decimal:
        return self.unrealized_pnl_by_day.get(_utc_day(at), Decimal("0"))

    def unrealized_drawdown_for(self, at: datetime) -> Decimal:
        unrealized = self.unrealized_pnl_for(at)
        return -unrealized if unrealized < 0 else Decimal("0")

    def loss_for(self, at: datetime) -> Decimal:
        net = self.net_pnl_for(at)
        return -net if net < 0 else Decimal("0")

    def total_loss_for(self, at: datetime) -> Decimal:
        return self.loss_for(at) + self.unrealized_drawdown_for(at)

    def reset(self) -> None:
        self.net_pnl_by_day.clear()
        self.unrealized_pnl_by_day.clear()


def _utc_day(at: datetime) -> date:
    require_aware_datetime(at, "at")
    return at.astimezone(UTC).date()


@dataclass
class DrawdownHalt:
    """Auto-clearing halt for unrealized (mark-to-market) drawdown.

    Unlike the one-way :class:`KillSwitch` — which guards *realized* loss, a
    persistent fact that should require operator review — unrealized
    liquidation-mark drawdown is *recoverable*: a momentary spread-widening on a
    wide event-contract book can dip the mark below the cap and then fully
    recover within a tick. Latching the kill switch on that would permanently
    halt a sleeve over a blip.

    This halt engages when the (realized + unrealized) drawdown reaches the cap
    and clears once it recedes below ``clear_fraction`` of the cap. The
    hysteresis band prevents flapping, and the halt never permanently kills a
    sleeve. While engaged it blocks new risk-*increasing* orders but leaves
    risk-*reducing* exits free (the gate enforces the side check).
    """

    engaged: bool = False
    clear_fraction: Decimal = Decimal("0.8")

    def update(self, total_drawdown: Decimal, limit: Decimal) -> bool:
        if limit <= 0:
            self.engaged = False
        elif total_drawdown >= limit:
            self.engaged = True
        elif total_drawdown < limit * self.clear_fraction:
            self.engaged = False
        return self.engaged


@dataclass
class KillSwitch:
    """Trip-flag that, when set, causes the risk gate to reject everything.

    Tripping is one-way for the lifetime of the process; an operator must
    explicitly :meth:`reset` it once the underlying incident is handled.

    Only *realized* daily loss latches this switch. Unrealized mark-to-market
    drawdown is routed through the auto-clearing :class:`DrawdownHalt` instead,
    so a recoverable mark dip can never permanently kill a sleeve.
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
