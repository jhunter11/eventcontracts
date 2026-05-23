"""Paper broker boundary."""

from __future__ import annotations

from dataclasses import dataclass

from eventcontracts.execution.simulator import OrderIntent, SimulatedFill


@dataclass
class PaperBroker:
    """Coordinates order intents and simulated fills."""

    def submit(self, order: OrderIntent) -> list[SimulatedFill]:
        raise NotImplementedError
