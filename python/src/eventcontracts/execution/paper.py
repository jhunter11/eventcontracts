"""Paper broker boundary."""

from __future__ import annotations

from dataclasses import dataclass, field

from eventcontracts.domain.decisions import IntentEnvelope
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
    fills: list[SimulatedFill] = field(default_factory=list)

    def submit(self, order: OrderIntent) -> list[SimulatedFill]:
        self.submitted.append(order)
        fills = self.simulator.submit(order)
        self.fills.extend(fills)
        return fills


@dataclass
class PaperIntentSink:
    """Runner ``IntentSink`` that routes order envelopes into paper execution.

    Handoff:
    ``StrategyRunner`` emits ``IntentEnvelope`` after risk approval.
    ``PaperIntentSink`` records every envelope, converts supported order
    decisions to ``OrderIntent``, and passes them to ``PaperBroker``.
    """

    broker: PaperBroker
    envelopes: list[IntentEnvelope] = field(default_factory=list)
    ignored: list[IntentEnvelope] = field(default_factory=list)

    def emit(self, envelope: IntentEnvelope) -> None:
        self.envelopes.append(envelope)
        order = intent_to_order(envelope)
        if order is None:
            self.ignored.append(envelope)
            return
        self.broker.submit(order)
