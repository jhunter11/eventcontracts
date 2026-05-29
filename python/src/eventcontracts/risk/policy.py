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
from eventcontracts.domain.orders import OrderSide
from eventcontracts.domain.spec import SleeveSpec
from eventcontracts.execution.simulator import OrderIntent
from eventcontracts.risk.limits import (
    check_available_cash,
    check_daily_loss,
    check_execution_bounds,
    check_fee_adjusted_edge,
    check_gross_exposure,
    check_market_snapshot,
    check_open_orders,
    check_order_notional,
    check_position_notional,
)
from eventcontracts.risk.state import DailyLossLedger, DrawdownHalt, KillSwitch
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

    Cancels never increase risk, so they pass once the kill switch is checked.
    Replaces are evaluated as cancel-new using the referenced open order plus
    the replacement price/quantity.
    """

    sleeve: SleeveSpec
    daily_loss: DailyLossLedger = field(default_factory=DailyLossLedger)
    kill_switch: KillSwitch = field(default_factory=KillSwitch)
    drawdown_halt: DrawdownHalt = field(default_factory=DrawdownHalt)

    def evaluate(self, envelope: IntentEnvelope, ctx: StrategyContext) -> RiskDecision:
        if self.kill_switch.tripped:
            return RiskDecision(
                allowed=False,
                reasons=("kill_switch",),
            )

        decision = envelope.decision

        if isinstance(decision, CancelOrder):
            return RiskDecision(allowed=True)

        if isinstance(decision, ReplaceOrder):
            replacement = _replacement_as_place_order(decision, ctx)
            if replacement is None:
                return RiskDecision(
                    allowed=False,
                    reasons=("replace_order_not_found",),
                )
            return self._evaluate_place_order(
                replacement,
                ctx,
                when=envelope.emitted_at,
                check_open_order_count=False,
            )

        if not isinstance(decision, PlaceOrder):
            return RiskDecision(allowed=True)

        return self._evaluate_place_order(
            decision,
            ctx,
            when=envelope.emitted_at,
            check_open_order_count=True,
        )

    def _evaluate_place_order(
        self,
        decision: PlaceOrder,
        ctx: StrategyContext,
        *,
        when: datetime,
        check_open_order_count: bool,
    ) -> RiskDecision:
        reasons: list[str] = []
        reasons.extend(check_execution_bounds(decision, self.sleeve.risk, when))
        reasons.extend(check_market_snapshot(decision, self.sleeve.risk, when))
        reasons.extend(check_order_notional(decision, self.sleeve.risk))
        reasons.extend(check_fee_adjusted_edge(decision))
        reasons.extend(check_position_notional(decision, self.sleeve.risk, ctx.positions()))
        if check_open_order_count:
            reasons.extend(check_open_orders(ctx.open_orders(), self.sleeve.risk))

        exposure_value = ctx.exposure()
        reasons.extend(check_gross_exposure(decision, self.sleeve.risk, exposure_value))
        reasons.extend(check_available_cash(decision, ctx.cash(self.sleeve.currency)))

        # Realized daily loss is a persistent fact: latch the one-way kill
        # switch. Unrealized liquidation drawdown is recoverable, so it must NOT
        # latch the kill switch (a transient mark dip would permanently kill the
        # sleeve). It is routed through the auto-clearing drawdown halt below.
        realized_loss = self.daily_loss.loss_for(when)
        if check_daily_loss(self.sleeve.risk, realized_loss):
            reasons.append("max_daily_loss")
            self.kill_switch.trip("max_daily_loss", when)

        # Unrealized drawdown soft-halt: block new risk-INCREASING orders while
        # underwater, but never block a risk-reducing exit (a SELL closes
        # inventory). Auto-clears with hysteresis when the mark recovers.
        total_drawdown = self.daily_loss.total_loss_for(when)
        self.drawdown_halt.update(total_drawdown, self.sleeve.risk.max_daily_loss)
        if self.drawdown_halt.engaged and decision.order_side is OrderSide.BUY:
            reasons.append("unrealized_drawdown_halt")

        return RiskDecision(allowed=not reasons, reasons=tuple(reasons))


def _replacement_as_place_order(
    decision: ReplaceOrder,
    ctx: StrategyContext,
) -> PlaceOrder | None:
    for order in ctx.open_orders():
        if order.client_order_id != decision.client_order_id:
            continue
        price = decision.new_price if decision.new_price is not None else order.price
        if price is None:
            return None
        return PlaceOrder(
            client_order_id=decision.client_order_id,
            instrument_id=order.instrument_id,
            outcome_side=order.outcome_side,
            order_side=order.order_side,
            order_type=order.order_type,
            time_in_force=order.time_in_force,
            quantity=decision.new_quantity if decision.new_quantity is not None else order.quantity,
            price=price,
            market_snapshot=decision.market_snapshot,
            expires_at=order.expires_at,
            reason=decision.reason,
            priority=decision.priority,
            metadata=order.metadata,
        )
    return None
