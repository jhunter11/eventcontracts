"""Ports the runner depends on.

The runner is integration-only: it owns the loop, lifecycle, and decision
flow, but never talks to a venue, the bus, storage, or a model directly. It
asks for those capabilities through these Protocols. Concrete adapters (NATS,
S3, Kalshi, ONNX-via-ort, etc.) implement them later and are wired in at
startup.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from eventcontracts.domain.decisions import IntentEnvelope
from eventcontracts.domain.events import NormalizedEvent
from eventcontracts.domain.ids import StrategyId
from eventcontracts.strategy.context import StrategyContext


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reasons: tuple[str, ...] = ()


class EventSource(Protocol):
    def stream(self) -> Iterator[NormalizedEvent]: ...


class IntentSink(Protocol):
    def emit(self, envelope: IntentEnvelope) -> None: ...


class StateStore(Protocol):
    def save(self, strategy_id: StrategyId, state: bytes) -> None: ...

    def load(self, strategy_id: StrategyId) -> bytes | None: ...


class Clock(Protocol):
    def now(self) -> datetime: ...


class RiskGate(Protocol):
    def evaluate(
        self, envelope: IntentEnvelope, ctx: StrategyContext
    ) -> RiskDecision: ...


class ContextProvider(Protocol):
    """Builds a fresh ``StrategyContext`` for each event.

    Implementations roll up positions/cash/features from the latest known
    state. The runner does not maintain context itself — that's a different
    component's job (position keeper, feature store).
    """

    def context(self) -> StrategyContext: ...
