"""Pre-trade policy gate.

The gate is the runner's last word before an intent leaves for execution.
It is intentionally stateless except for an injected :class:`DailyLossLedger`
and :class:`KillSwitch`. All other state is read from the
:class:`StrategyContext` so the gate stays composable with replay.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from eventcontracts.domain.decisions import (
    CancelOrder,
    IntentEnvelope,
    PlaceOrder,
    ReplaceOrder,
)
from eventcontracts.domain.spec import SleeveSpec
from eventcontracts.execution.simulator import OrderIntent
from eventcontracts.risk.limits import (
    check_available_cash,
    check_daily_loss,
    check_gross_exposure,
    check_open_orders,
    check_order_notional,
    check_position_notional,
)
from eventcontracts.risk.state import DailyLossLedger, KillSwitch
from eventcontracts.runner.ports import RiskDecision
from eventcontracts.strategy.context import StrategyContext


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reasons: tuple[str, ...]


class PreTradePolicyService:
    """Evaluates a fully-formed :class:`OrderIntent` against venue policy.

    The runner uses :class:`SleeveRiskGate` for strategy-level gating; this
    service exists for the (future) gateway, which receives intents from
    multiple sleeves and applies cross-sleeve policy.
    """

    def __init__(self, sleeve: SleeveSpec) -> None:
        self.sleeve = sleeve

    def evaluate(self, order: OrderIntent) -> PolicyDecision:
        # An OrderIntent has already cleared strategy-level gating; this
        # service applies sleeve policy a second time at the gateway boundary
        # so a misbehaving sleeve cannot send an oversized order downstream.
        notional = order.price * order.quantity
        reasons: list[str] = []
        if notional > self.sleeve.risk.max_order_notional:
            reasons.append("max_order_notional")
        return PolicyDecision(allowed=not reasons, reasons=tuple(reasons))


@dataclass
class SleeveRiskGate:
    """Runner risk gate backed by :class:`SleeveSpec.risk`.

    Checks executed for every :class:`PlaceOrder`:

    * per-order notional (``max_order_notional``)
    * projected position notional (``max_position_notional``)
    * open-order count (``max_open_orders``)
    * gross sleeve exposure (``max_gross_exposure``)
    * day-of-trade realized loss (``max_daily_loss``)
    * kill-switch state

    Cancels and replaces never increase risk, so they pass once the
    kill switch is checked.
    """

    sleeve: SleeveSpec
    daily_loss: DailyLossLedger = field(default_factory=DailyLossLedger)
    kill_switch: KillSwitch = field(default_factory=KillSwitch)

    def evaluate(self, envelope: IntentEnvelope, ctx: StrategyContext) -> RiskDecision:
        if self.kill_switch.tripped:
            return RiskDecision(
                allowed=False,
                reasons=("kill_switch",),
            )

        decision = envelope.decision

        if isinstance(decision, CancelOrder | ReplaceOrder):
            return RiskDecision(allowed=True)

        if not isinstance(decision, PlaceOrder):
            return RiskDecision(allowed=True)

        reasons: list[str] = []
        reasons.extend(check_order_notional(decision, self.sleeve.risk))
        reasons.extend(
            check_position_notional(decision, self.sleeve.risk, ctx.positions())
        )
        reasons.extend(check_open_orders(ctx.open_orders(), self.sleeve.risk))

        exposure_value = ctx.exposure()
        reasons.extend(check_gross_exposure(decision, self.sleeve.risk, exposure_value))
        reasons.extend(check_available_cash(decision, ctx.cash(self.sleeve.currency)))

        when: datetime = envelope.emitted_at
        reasons.extend(
            check_daily_loss(self.sleeve.risk, self.daily_loss.loss_for(when))
        )

        return RiskDecision(allowed=not reasons, reasons=tuple(reasons))
