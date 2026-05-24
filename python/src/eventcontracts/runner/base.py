"""Reference strategy runner.

This is the integration point that wires a ``Strategy`` to its environment.
Sleeves in production are instances of this class with different specs,
event sources, sinks, and risk gates. Backtests use the same class with an
in-memory event source and a paper sink.

The runner is intentionally synchronous and single-threaded. Concurrency
belongs to the layers around it: the bus delivers events one at a time, the
gateway batches intents, the allocator runs in its own process.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

from eventcontracts.domain.decisions import (
    Alert,
    AlertSeverity,
    IntentEnvelope,
    StrategyDecision,
    decision_kind,
    decision_priority,
)
from eventcontracts.domain.events import NormalizedEvent
from eventcontracts.domain.ids import CorrelationId
from eventcontracts.domain.spec import SleeveSpec, StrategySpec
from eventcontracts.runner.ports import (
    Clock,
    ContextProvider,
    EventSource,
    IntentSink,
    RiskGate,
    StateStore,
)
from eventcontracts.strategy.base import Strategy
from eventcontracts.strategy.lifecycle import StrategyState, can_transition


@dataclass
class RunSummary:
    sleeve_id: str
    strategy_id: str
    events_processed: int
    decisions_emitted: int
    intents_dispatched: int
    intents_rejected: int
    started_at: datetime
    ended_at: datetime
    end_state: StrategyState
    rejection_reasons: dict[str, int] = field(default_factory=dict)


@dataclass
class StrategyRunner:
    spec: StrategySpec
    sleeve: SleeveSpec
    strategy: Strategy
    events: EventSource
    sink: IntentSink
    risk: RiskGate
    clock: Clock
    context_provider: ContextProvider
    state_store: StateStore | None = None
    state: StrategyState = StrategyState.CREATED

    def transition(self, target: StrategyState) -> None:
        if not can_transition(self.state, target):
            raise RuntimeError(
                f"invalid strategy transition: {self.state.value} -> {target.value}"
            )
        self.state = target

    def run(self) -> RunSummary:
        started_at = self.clock.now()
        events_processed = 0
        decisions_emitted = 0
        intents_dispatched = 0
        intents_rejected = 0
        rejection_reasons: dict[str, int] = {}

        self.transition(StrategyState.INITIALIZING)
        ctx = self.context_provider.context()
        if self.state_store is not None:
            saved = self.state_store.load(self.spec.strategy_id)
            if saved is not None:
                self.strategy.restore(saved)
        self.strategy.on_init(ctx)
        self.transition(StrategyState.READY)
        self.transition(StrategyState.RUNNING)

        try:
            for event in self.events.stream():
                if self.state is not StrategyState.RUNNING:
                    break
                events_processed += 1
                ctx = self.context_provider.context()
                decisions = self.strategy.on_event(event, ctx)
                for decision in decisions:
                    decisions_emitted += 1
                    envelope = self._wrap(decision, event)
                    verdict = self.risk.evaluate(envelope, ctx)
                    if verdict.allowed:
                        self.sink.emit(envelope)
                        intents_dispatched += 1
                    else:
                        intents_rejected += 1
                        for reason in verdict.reasons or ("unspecified",):
                            rejection_reasons[reason] = (
                                rejection_reasons.get(reason, 0) + 1
                            )
        finally:
            if self.state is StrategyState.RUNNING:
                self.transition(StrategyState.DRAINING)
            ctx = self.context_provider.context()
            self.strategy.on_shutdown(ctx)
            if self.state_store is not None:
                self.state_store.save(self.spec.strategy_id, self.strategy.snapshot())
            if self.state is StrategyState.DRAINING:
                self.transition(StrategyState.DISPOSED)

        ended_at = self.clock.now()
        return RunSummary(
            sleeve_id=str(self.sleeve.sleeve_id),
            strategy_id=str(self.spec.strategy_id),
            events_processed=events_processed,
            decisions_emitted=decisions_emitted,
            intents_dispatched=intents_dispatched,
            intents_rejected=intents_rejected,
            started_at=started_at,
            ended_at=ended_at,
            end_state=self.state,
            rejection_reasons=rejection_reasons,
        )

    def halt(self, reason: str) -> None:
        if can_transition(self.state, StrategyState.HALTED):
            self.transition(StrategyState.HALTED)
        # If we are already past RUNNING we cannot halt cleanly; the caller
        # should rely on the run() finally-block to dispose.

    def _wrap(
        self, decision: StrategyDecision, triggering: NormalizedEvent
    ) -> IntentEnvelope:
        return IntentEnvelope(
            decision=decision,
            strategy_id=self.spec.strategy_id,
            sleeve_id=self.sleeve.sleeve_id,
            correlation_id=CorrelationId(uuid4().hex),
            emitted_at=self.clock.now(),
            priority=decision_priority(decision, self.spec.default_execution_priority),
            triggered_by_event_id=getattr(triggering, "event_id", None),
            metadata={"decision_kind": decision_kind(decision)},
        )


def utcnow() -> datetime:
    return datetime.now(tz=UTC)


def severity_is_fatal(alert: Alert) -> bool:
    return alert.severity is AlertSeverity.ERROR
