"""End-to-end smoke test for the strategy/runner plug pattern."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

from eventcontracts.domain import (
    EventId,
    EventSubscription,
    ExecutionPriority,
    InstrumentId,
    LatencyTier,
    OutcomeSide,
    PlaceOrder,
    RiskProfile,
    SleeveId,
    SleeveSpec,
    StrategyId,
    StrategySpec,
    Trade,
    TradeEvent,
    Venue,
)
from eventcontracts.runner import (
    AllowAllRiskGate,
    InMemoryClock,
    InMemoryContext,
    InMemoryEventSource,
    InMemoryIntentSink,
    StaticContextProvider,
    StrategyRunner,
)
from eventcontracts.strategies import example_threshold  # noqa: F401 — registers
from eventcontracts.strategy import create, known
from eventcontracts.strategy.lifecycle import StrategyState


def _spec() -> StrategySpec:
    return StrategySpec(
        strategy_id=StrategyId("example-threshold-v1"),
        name="example_threshold",
        version="0.1.0",
        description="reference strategy used by the smoke test",
        subscription=EventSubscription(
            venues=(Venue.KALSHI,),
            instrument_patterns=("*",),
            event_kinds=("trade",),
        ),
        parameters={"buy_below": "0.50", "size": "5"},
    )


def _sleeve() -> SleeveSpec:
    return SleeveSpec(
        sleeve_id=SleeveId("sleeve-a"),
        strategy_id=StrategyId("example-threshold-v1"),
        strategy_version="0.1.0",
        venue=Venue.KALSHI,
        capital_allocation=Decimal("1000"),
        currency="USD",
        risk=RiskProfile(
            max_order_notional=Decimal("100"),
            max_position_notional=Decimal("500"),
            max_daily_loss=Decimal("50"),
            max_open_orders=5,
            max_gross_exposure=Decimal("500"),
            currency="USD",
        ),
    )


def _trade(price: str, instrument: InstrumentId) -> TradeEvent:
    return TradeEvent(
        event_id=EventId(f"e-{price}"),
        trade=Trade(
            instrument_id=instrument,
            side=None,
            price=Decimal(price),
            quantity=Decimal("1"),
            trade_id=None,
            exchange_ts=None,
            received_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        ),
    )


def test_strategy_registers_by_name() -> None:
    assert "example_threshold" in known()


def test_create_resolves_through_registry() -> None:
    strategy = create("example_threshold", _spec())
    assert strategy.spec.name == "example_threshold"


def test_runner_emits_place_order_below_threshold() -> None:
    spec = _spec()
    sleeve = _sleeve()
    strategy = create("example_threshold", spec)
    instrument = InstrumentId(venue=Venue.KALSHI, market_id="DEMO-1")
    events = (
        _trade("0.60", instrument),
        _trade("0.45", instrument),
        _trade("0.30", instrument),
    )
    sink = InMemoryIntentSink()
    clock = InMemoryClock()
    ctx = InMemoryContext(
        strategy_id_value=spec.strategy_id,
        sleeve_id_value=sleeve.sleeve_id,
        clock_now=clock.now(),
    )
    runner = StrategyRunner(
        spec=spec,
        sleeve=sleeve,
        strategy=strategy,
        events=InMemoryEventSource(events=events),
        sink=sink,
        risk=AllowAllRiskGate(),
        clock=clock,
        context_provider=StaticContextProvider(ctx=ctx),
    )

    summary = runner.run()

    assert summary.events_processed == 3
    assert summary.decisions_emitted == 3
    assert summary.intents_dispatched == 3
    assert summary.intents_rejected == 0
    assert summary.end_state is StrategyState.DISPOSED

    place_orders = [e for e in sink.emitted if isinstance(e.decision, PlaceOrder)]
    assert len(place_orders) == 2
    assert all(
        isinstance(po.decision, PlaceOrder) and po.decision.outcome_side is OutcomeSide.YES
        for po in place_orders
    )


def test_runner_records_rejection_reasons() -> None:
    class RejectAll:
        def evaluate(self, envelope, ctx):  # type: ignore[no-untyped-def]
            from eventcontracts.runner.ports import RiskDecision

            return RiskDecision(allowed=False, reasons=("test-reject",))

    spec = _spec()
    sleeve = _sleeve()
    strategy = create("example_threshold", spec)
    instrument = InstrumentId(venue=Venue.KALSHI, market_id="DEMO-1")
    sink = InMemoryIntentSink()
    clock = InMemoryClock()
    ctx = InMemoryContext(
        strategy_id_value=spec.strategy_id,
        sleeve_id_value=sleeve.sleeve_id,
        clock_now=clock.now(),
    )
    runner = StrategyRunner(
        spec=spec,
        sleeve=sleeve,
        strategy=strategy,
        events=InMemoryEventSource(events=(_trade("0.10", instrument),)),
        sink=sink,
        risk=RejectAll(),
        clock=clock,
        context_provider=StaticContextProvider(ctx=ctx),
    )

    summary = runner.run()

    assert summary.intents_dispatched == 0
    assert summary.intents_rejected == 1
    assert summary.rejection_reasons == {"test-reject": 1}
    assert sink.emitted == []


def test_runner_applies_strategy_default_execution_priority() -> None:
    priority = ExecutionPriority(
        tier=LatencyTier.FAST,
        max_delay_ms=100,
        expires_after_ms=250,
        allow_rate_limit_borrow=True,
        reason="latency-sensitive crypto edge",
    )
    spec = replace(_spec(), default_execution_priority=priority)
    sleeve = _sleeve()
    strategy = create("example_threshold", spec)
    instrument = InstrumentId(venue=Venue.KALSHI, market_id="DEMO-1")
    sink = InMemoryIntentSink()
    clock = InMemoryClock()
    ctx = InMemoryContext(
        strategy_id_value=spec.strategy_id,
        sleeve_id_value=sleeve.sleeve_id,
        clock_now=clock.now(),
    )
    runner = StrategyRunner(
        spec=spec,
        sleeve=sleeve,
        strategy=strategy,
        events=InMemoryEventSource(events=(_trade("0.10", instrument),)),
        sink=sink,
        risk=AllowAllRiskGate(),
        clock=clock,
        context_provider=StaticContextProvider(ctx=ctx),
    )

    summary = runner.run()

    assert summary.intents_dispatched == 1
    assert sink.emitted[0].priority == priority
