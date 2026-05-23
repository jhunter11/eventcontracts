"""Pre-trade policy gate."""

from __future__ import annotations

from dataclasses import dataclass

from eventcontracts.execution.simulator import OrderIntent


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reasons: tuple[str, ...]


class PreTradePolicyService:
    """Evaluates whether an order is allowed before an order ticket is constructed."""

    def evaluate(self, order: OrderIntent) -> PolicyDecision:
        raise NotImplementedError
