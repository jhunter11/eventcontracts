"""Paper broker boundary."""

from __future__ import annotations

from dataclasses import dataclass, field

from eventcontracts.domain.decisions import IntentEnvelope
from eventcontracts.domain.fills import Fill
from eventcontracts.execution.simulator import (
    ExecutionSimulator,
    OrderIntent,
    SimulatedFill,
    intent_to_order,
)


@dataclass
class PaperBroker:
    """Coordinates order intents and simulated fills."""

    simulator: ExecutionSimulator
    submitted: list[OrderIntent] = field(default_factory=list)
    fills: list[SimulatedFill | Fill] = field(default_factory=list)

    def submit(self, order: OrderIntent) -> list[SimulatedFill]:
        self.submitted.append(order)
        fills = self.simulator.submit(order)
        self.fills.extend(fills)
        return fills

    def submit_envelope(self, envelope: IntentEnvelope) -> list[SimulatedFill] | list[Fill]:
        if hasattr(self.simulator, "submit_envelope"):
            fills: list[SimulatedFill] | list[Fill] = self.simulator.submit_envelope(envelope)
            self.fills.extend(fills)
            return fills
        order = intent_to_order(envelope)
        if order is None:
            return []
        return self.submit(order)


@dataclass
class PaperIntentSink:
    """Runner ``IntentSink`` that routes order envelopes into paper execution.

    Handoff:
    ``StrategyRunner`` emits ``IntentEnvelope`` after risk approval.
    ``PaperIntentSink`` records every envelope and lets the broker/simulator
    consume the envelope natively when that boundary supports it. The
    ``OrderIntent`` conversion remains only for legacy simulator adapters.
    """

    broker: PaperBroker
    envelopes: list[IntentEnvelope] = field(default_factory=list)
    ignored: list[IntentEnvelope] = field(default_factory=list)

    def emit(self, envelope: IntentEnvelope) -> None:
        self.envelopes.append(envelope)
        fills = self.broker.submit_envelope(envelope)
        if not fills and intent_to_order(envelope) is None:
            self.ignored.append(envelope)
