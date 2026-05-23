"""End-to-end type handoff across the partial implementation layers."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from eventcontracts.domain import (
    EventSubscription,
    RiskProfile,
    SleeveId,
    SleeveSpec,
    StrategyId,
    StrategySpec,
    Venue,
)
from eventcontracts.execution import ImmediateFillSimulator, PaperBroker, PaperIntentSink
from eventcontracts.ingestion import IngestionJob, IngestionPipeline, IterableCaptureSource
from eventcontracts.normalization import BASIC_NORMALIZERS, EventNormalizer, NormalizationPipeline
from eventcontracts.replay import NormalizedReplaySource, RawReplayEngine
from eventcontracts.risk import SleeveRiskGate
from eventcontracts.runner import InMemoryClock, InMemoryContext, StaticContextProvider, StrategyRunner
from eventcontracts.storage import EventEnvelope, InMemoryEventStore
from eventcontracts.strategies import example_threshold  # noqa: F401 - registers strategy
from eventcontracts.strategy import create


def _strategy_spec() -> StrategySpec:
    return StrategySpec(
        strategy_id=StrategyId("example-threshold-v1"),
        name="example_threshold",
        version="0.1.0",
        description="vertical-slice strategy",
        subscription=EventSubscription(
            venues=(Venue.KALSHI,),
            instrument_patterns=("*",),
            event_kinds=("trade",),
        ),
        parameters={"buy_below": "0.50", "size": "5"},
    )


def _sleeve() -> SleeveSpec:
    return SleeveSpec(
        sleeve_id=SleeveId("paper-a"),
        strategy_id=StrategyId("example-threshold-v1"),
        strategy_version="0.1.0",
        venue=Venue.KALSHI,
        capital_allocation=Decimal("1000"),
        currency="USD",
        risk=RiskProfile(
            max_order_notional=Decimal("100"),
            max_position_notional=Decimal("500"),
            max_daily_loss=Decimal("50"),
            max_open_orders=10,
            max_gross_exposure=Decimal("500"),
            currency="USD",
        ),
    )


def test_raw_capture_to_paper_fill_vertical_slice() -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    raw_trade = EventEnvelope(
        venue=Venue.KALSHI,
        source="fixture",
        channel="trade",
        received_at=now,
        exchange_ts=now,
        payload={
            "event_id": "raw-trade-1",
            "market_id": "KXDEMO",
            "side": "yes",
            "price": "0.40",
            "quantity": "2",
            "trade_id": "venue-trade-1",
        },
        schema_version="raw-event-v1",
        metadata={"source_sequence": "1"},
    )

    store = InMemoryEventStore()
    ingestion = IngestionPipeline(
        store=store,
        sources={"fixture": IterableCaptureSource(events=(raw_trade,))},
    )
    job = IngestionJob(name="fixture-trades", venue=Venue.KALSHI, source="fixture")
    assert ingestion.run(job) == 1

    raw_replay = RawReplayEngine(store, source="fixture")
    assert tuple(raw_replay.replay()) == (raw_trade,)

    normalization = NormalizationPipeline(
        raw_store=store,
        normalized_store=store,
        normalizer=EventNormalizer(BASIC_NORMALIZERS),
    )
    results = normalization.run(source="fixture")
    assert len(results) == 1
    assert results[0].accepted is True
    assert results[0].normalized is not None

    spec = _strategy_spec()
    sleeve = _sleeve()
    clock = InMemoryClock(current=now)
    ctx = InMemoryContext(
        strategy_id_value=spec.strategy_id,
        sleeve_id_value=sleeve.sleeve_id,
        clock_now=now,
    )
    broker = PaperBroker(simulator=ImmediateFillSimulator(filled_at=now))
    sink = PaperIntentSink(broker=broker)
    runner = StrategyRunner(
        spec=spec,
        sleeve=sleeve,
        strategy=create(spec.name, spec),
        events=NormalizedReplaySource(store),
        sink=sink,
        risk=SleeveRiskGate(sleeve),
        clock=clock,
        context_provider=StaticContextProvider(ctx=ctx),
    )

    summary = runner.run()

    assert summary.events_processed == 1
    assert summary.intents_dispatched == 1
    assert summary.intents_rejected == 0
    assert len(sink.envelopes) == 1
    assert len(broker.submitted) == 1
    assert len(broker.fills) == 1
    assert broker.submitted[0].metadata["client_order_id"]
    assert broker.fills[0].price == Decimal("0.40")
