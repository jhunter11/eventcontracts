"""Replay determinism: running the same data twice must produce identical output.

The determinism contract is the bedrock of every parity test and CI
gate that follows. If two replays of the same Parquet partition yield
different fills, the framework cannot claim Python/Rust parity in
:mod:`docs.artifact-contract`.

This test seeds a small partition, runs the full pipeline twice, and
asserts the report serializations are identical.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from eventcontracts.adapters.venues.kalshi import KalshiFeeModel
from eventcontracts.domain.decisions import IntentEnvelope, PlaceOrder
from eventcontracts.domain.events import EventProvenance, OrderBookEvent, TradeEvent
from eventcontracts.domain.ids import EventId
from eventcontracts.domain.models import (
    InstrumentId,
    OrderBook,
    OrderBookLevel,
    OutcomeSide,
    Trade,
    Venue,
)
from eventcontracts.execution import (
    BacktestReport,
    ConstantLatency,
    FractionalQueueEstimator,
    MarketPaperSimulator,
    PnLTracker,
    intent_to_order,
)
from eventcontracts.plugins.strategies import example_threshold  # noqa: F401
from eventcontracts.replay import NormalizedReplaySource
from eventcontracts.risk import DailyLossLedger, SleeveRiskGate
from eventcontracts.runner import StrategyRunner
from eventcontracts.storage import ParquetEventStore
from eventcontracts.strategy import create
from eventcontracts.testing import InMemoryClock, InMemoryContext, StaticContextProvider

from eventcontracts.domain import (
    EventSubscription,
    RiskProfile,
    SleeveId,
    SleeveSpec,
    StrategyId,
    StrategySpec,
)


NOW = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)
INSTR = InstrumentId(venue=Venue.KALSHI, market_id="WX-NYC-TEMP", outcome_id=None)


def _seed(root: Path) -> None:
    store = ParquetEventStore(root)
    store.append_normalized(
        OrderBookEvent(
            event_id=EventId("book-1"),
            book=OrderBook(
                instrument_id=INSTR,
                yes_bids=(OrderBookLevel(price=Decimal("0.43"), quantity=Decimal("50")),),
                yes_asks=(OrderBookLevel(price=Decimal("0.46"), quantity=Decimal("50")),),
                no_bids=(),
                no_asks=(),
                exchange_ts=NOW,
                received_at=NOW,
            ),
            provenance=EventProvenance(source="fixture", channel="book", venue=Venue.KALSHI),
        )
    )
    for i, price in enumerate(["0.46", "0.44", "0.42"]):
        store.append_normalized(
            TradeEvent(
                event_id=EventId(f"t-{i:03d}"),
                trade=Trade(
                    instrument_id=INSTR,
                    side=OutcomeSide.YES,
                    price=Decimal(price),
                    quantity=Decimal("20"),
                    trade_id=f"tv-{i}",
                    exchange_ts=NOW.replace(second=i + 1),
                    received_at=NOW.replace(second=i + 1),
                    aggressor_side=OutcomeSide.NO,
                ),
                provenance=EventProvenance(source="fixture", channel="trade", venue=Venue.KALSHI),
            )
        )
    store.flush()


def _run_one_pass(root: Path) -> BacktestReport:
    spec = StrategySpec(
        strategy_id=StrategyId("example-threshold-v1"),
        name="example_threshold",
        version="0.1.0",
        description="determinism harness",
        subscription=EventSubscription(
            venues=(Venue.KALSHI,),
            instrument_patterns=("*",),
            event_kinds=("trade",),
        ),
        parameters={"buy_below": "0.55", "size": "10"},
    )
    sleeve = SleeveSpec(
        sleeve_id=SleeveId("paper-a"),
        strategy_id=spec.strategy_id,
        strategy_version="0.1.0",
        venue=Venue.KALSHI,
        capital_allocation=Decimal("10000"),
        currency="USD",
        risk=RiskProfile(
            max_order_notional=Decimal("100"),
            max_position_notional=Decimal("500"),
            max_daily_loss=Decimal("50"),
            max_open_orders=10,
            max_gross_exposure=Decimal("1000"),
            currency="USD",
        ),
    )
    daily_loss = DailyLossLedger()
    pnl = PnLTracker(currency=sleeve.currency, daily_loss_ledger=daily_loss)
    simulator = MarketPaperSimulator(
        fee_model=KalshiFeeModel(),
        latency=ConstantLatency(submit_ms=0),
        queue_estimator=FractionalQueueEstimator(),
        strategy_id=spec.strategy_id,
        sleeve_id=sleeve.sleeve_id,
        fill_sink=pnl,
    )

    fills_recorded: list = []

    class _Sink:
        def emit(self, envelope: IntentEnvelope) -> None:
            if not isinstance(envelope.decision, PlaceOrder):
                return
            intent = intent_to_order(envelope)
            if intent is None:
                return
            fills_recorded.extend(simulator.submit(intent, envelope.emitted_at))

    source = NormalizedReplaySource(ParquetEventStore(root))

    class _TeeSource:
        def stream(self):
            for event in source.stream():
                simulator.on_event(event)
                pnl.on_event(event)
                yield event

    runner = StrategyRunner(
        spec=spec,
        sleeve=sleeve,
        strategy=create(spec.name, spec),
        events=_TeeSource(),  # type: ignore[arg-type]
        sink=_Sink(),  # type: ignore[arg-type]
        risk=SleeveRiskGate(sleeve=sleeve, daily_loss=daily_loss),
        clock=InMemoryClock(current=NOW),
        context_provider=StaticContextProvider(
            ctx=InMemoryContext(
                strategy_id_value=spec.strategy_id,
                sleeve_id_value=sleeve.sleeve_id,
                clock_now=NOW,
            )
        ),
    )

    summary = runner.run()
    return BacktestReport.from_run(summary, pnl, fills=fills_recorded, now=NOW)


def test_two_replays_produce_identical_reports(tmp_path: Path) -> None:
    _seed(tmp_path)

    report_a = _run_one_pass(tmp_path)
    report_b = _run_one_pass(tmp_path)

    # Compare everything except wall-clock timestamps that depend on InMemoryClock.
    a = report_a.to_dict()
    b = report_b.to_dict()
    for key in ("started_at", "ended_at", "duration_seconds"):
        a.pop(key)
        b.pop(key)
    assert a == b


def test_backtest_report_serializes_with_drawdown(tmp_path: Path) -> None:
    _seed(tmp_path)
    report = _run_one_pass(tmp_path)
    data = report.to_dict()
    # Spot-check the keys we promise.
    assert "max_drawdown" in data
    assert "fill_rate" in data
    assert "total_pnl" in data
    assert "rejection_reasons" in data
