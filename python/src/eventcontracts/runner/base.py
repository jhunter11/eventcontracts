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

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from uuid import uuid4

from eventcontracts.domain.decisions import (
    Alert,
    AlertSeverity,
    CancelOrder,
    IntentEnvelope,
    PlaceOrder,
    ReplaceOrder,
    StrategyDecision,
    decision_kind,
    decision_priority,
)
from eventcontracts.domain.events import (
    NormalizedEvent,
    OrderBookEvent,
    QuoteEvent,
    market_snapshot_from_book_event,
    market_snapshot_from_quote_event,
)
from eventcontracts.domain.ids import ClientOrderId, CorrelationId
from eventcontracts.domain.spec import SleeveSpec, StrategySpec
from eventcontracts.runner.ports import (
    Clock,
    ContextProvider,
    EventSource,
    IntentSink,
    RiskDecision,
    RiskGate,
    StateStore,
)
from eventcontracts.strategy.base import Strategy, StrategyFeedback
from eventcontracts.strategy.context import StrategyContext
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


@dataclass(frozen=True)
class StepResult:
    events_processed: int = 0
    decisions_emitted: int = 0
    intents_dispatched: int = 0
    intents_rejected: int = 0
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
    verdict_sink: Callable[[IntentEnvelope, RiskDecision], None] | None = None
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

        self.start()
        try:
            for event in self.events.stream():
                result = self.process_event(event)
                if result.events_processed == 0:
                    break
                events_processed += result.events_processed
                decisions_emitted += result.decisions_emitted
                intents_dispatched += result.intents_dispatched
                intents_rejected += result.intents_rejected
                for reason, count in result.rejection_reasons.items():
                    rejection_reasons[reason] = rejection_reasons.get(reason, 0) + count
        finally:
            self.stop()

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

    def start(self) -> None:
        """Initialize the strategy and enter RUNNING.

        Async adapters can call ``start`` once, then feed live events through
        :meth:`process_event`, and finally call :meth:`stop`.
        """

        if self.state is not StrategyState.CREATED:
            raise RuntimeError(f"cannot start runner from {self.state.value}")
        self.transition(StrategyState.INITIALIZING)
        ctx = self.context_provider.context()
        if self.state_store is not None:
            saved = self.state_store.load(self.spec.strategy_id)
            if saved is not None:
                self.strategy.restore(saved)
        self.strategy.on_init(ctx)
        self.transition(StrategyState.READY)
        self.transition(StrategyState.RUNNING)

    def process_event(self, event: NormalizedEvent) -> StepResult:
        if self.state is not StrategyState.RUNNING:
            return StepResult()

        ctx = self.context_provider.context()
        decisions_emitted = 0
        intents_dispatched = 0
        intents_rejected = 0
        rejection_reasons: dict[str, int] = {}

        decisions = self.strategy.on_event(event, ctx)
        for decision in decisions:
            decisions_emitted += 1
            envelope = self._wrap(decision, event)
            risk_ctx = self.context_provider.context()
            verdict = self.risk.evaluate(envelope, risk_ctx)
            if self.verdict_sink is not None:
                self.verdict_sink(envelope, verdict)
            if verdict.allowed:
                reservation_reasons = self._reserve_if_supported(envelope)
                if reservation_reasons:
                    intents_rejected += 1
                    for reason in reservation_reasons:
                        rejection_reasons[reason] = rejection_reasons.get(reason, 0) + 1
                    self._emit_feedback(
                        "IntentRejected",
                        envelope,
                        risk_ctx,
                        reasons=reservation_reasons,
                    )
                    continue
                self.sink.emit(envelope)
                self._emit_feedback("IntentAccepted", envelope, risk_ctx)
                intents_dispatched += 1
            else:
                intents_rejected += 1
                for reason in verdict.reasons or ("unspecified",):
                    rejection_reasons[reason] = rejection_reasons.get(reason, 0) + 1
                self._emit_feedback(
                    "IntentRejected",
                    envelope,
                    risk_ctx,
                    reasons=verdict.reasons or ("unspecified",),
                )

        return StepResult(
            events_processed=1,
            decisions_emitted=decisions_emitted,
            intents_dispatched=intents_dispatched,
            intents_rejected=intents_rejected,
            rejection_reasons=rejection_reasons,
        )

    def stop(self) -> None:
        if self.state is StrategyState.DISPOSED:
            return
        if self.state is StrategyState.RUNNING:
            self.transition(StrategyState.DRAINING)
        ctx = self.context_provider.context()
        self.strategy.on_shutdown(ctx)
        if self.state_store is not None:
            self.state_store.save(self.spec.strategy_id, self.strategy.snapshot())
        if self.state is StrategyState.DRAINING:
            self.transition(StrategyState.DISPOSED)

    def _wrap(
        self, decision: StrategyDecision, triggering: NormalizedEvent
    ) -> IntentEnvelope:
        decision = self._attach_triggering_snapshot(decision, triggering)
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

    def _attach_triggering_snapshot(
        self,
        decision: StrategyDecision,
        triggering: NormalizedEvent,
    ) -> StrategyDecision:
        if not isinstance(decision, (PlaceOrder, ReplaceOrder)):
            return decision
        if decision.market_snapshot is not None:
            return decision
        side = decision.outcome_side if isinstance(decision, PlaceOrder) else None
        if side is None:
            return decision
        if isinstance(triggering, QuoteEvent):
            return replace(
                decision,
                market_snapshot=market_snapshot_from_quote_event(
                    triggering,
                    side=side,
                ),
            )
        if isinstance(triggering, OrderBookEvent):
            return replace(
                decision,
                market_snapshot=market_snapshot_from_book_event(
                    triggering,
                    side=side,
                ),
            )
        return decision

    def _reserve_if_supported(self, envelope: IntentEnvelope) -> tuple[str, ...]:
        reserve = getattr(self.context_provider, "reserve_intent", None)
        if reserve is None:
            return ()
        result = reserve(envelope)
        if result is None:
            return ()
        return tuple(result)

    def _emit_feedback(
        self,
        kind: str,
        envelope: IntentEnvelope,
        ctx: StrategyContext,
        *,
        reasons: tuple[str, ...] = (),
    ) -> None:
        client_order_id = _client_order_id(envelope.decision)
        self.strategy.on_feedback(
            StrategyFeedback(
                kind=kind,
                envelope=envelope,
                client_order_id=client_order_id,
                reasons=reasons,
            ),
            ctx,
        )


def _client_order_id(decision: StrategyDecision) -> ClientOrderId | None:
    if isinstance(decision, (PlaceOrder, CancelOrder, ReplaceOrder)):
        return decision.client_order_id
    return None


def utcnow() -> datetime:
    return datetime.now(tz=UTC)


def severity_is_fatal(alert: Alert) -> bool:
    return alert.severity is AlertSeverity.ERROR
