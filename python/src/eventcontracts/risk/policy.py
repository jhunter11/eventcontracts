"""Pre-trade policy gate."""

from __future__ import annotations

from dataclasses import dataclass

from eventcontracts.domain.decisions import IntentEnvelope, PlaceOrder
from eventcontracts.domain.spec import SleeveSpec
from eventcontracts.execution.simulator import OrderIntent
from eventcontracts.risk.limits import check_order_notional
from eventcontracts.runner.ports import RiskDecision
from eventcontracts.strategy.context import StrategyContext


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reasons: tuple[str, ...]


class PreTradePolicyService:
    """Evaluates whether an order is allowed before an order ticket is constructed."""

    def evaluate(self, order: OrderIntent) -> PolicyDecision:
        raise NotImplementedError


@dataclass(frozen=True)
class SleeveRiskGate:
    """Minimal runner risk gate backed by ``SleeveSpec.risk``.

    Handoff:
    ``StrategyRunner`` passes an ``IntentEnvelope`` and read-only context.
    The gate inspects order-affecting decisions and returns ``RiskDecision``.
    Later implementations should add position, cash, market-state, eligibility,
    and kill-switch checks here before any live gateway sees an order.
    """

    sleeve: SleeveSpec

    def evaluate(self, envelope: IntentEnvelope, ctx: StrategyContext) -> RiskDecision:
        decision = envelope.decision
        if not isinstance(decision, PlaceOrder):
            return RiskDecision(allowed=True)

        reasons = list(check_order_notional(decision, self.sleeve.risk))
        if len(ctx.open_orders()) >= self.sleeve.risk.max_open_orders:
            reasons.append("max_open_orders")
        return RiskDecision(allowed=not reasons, reasons=tuple(reasons))
