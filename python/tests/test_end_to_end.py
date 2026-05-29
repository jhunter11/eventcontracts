"""Phase 1-3 vertical slice: Parquet capture → replay → strategy → paper PnL.

This is the deepest integration test in the suite. It exercises every
new module added in the comprehensive implementation:

* ParquetEventStore captures raw envelopes and normalized events
* DuckDbEventStore reads back partition counts
* NormalizedReplaySource streams events to the runner
* example_threshold strategy emits PlaceOrder when prices dip
* SleeveRiskGate evaluates each intent
* MarketPaperSimulator executes against a synthesized book
* KalshiFeeModel attaches taker fees
* PnLTracker accumulates realized PnL and feeds the daily loss ledger

If this test passes, a researcher can iterate on a real strategy
against Parquet-backed data with no other glue.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("duckdb")

from eventcontracts.adapters.venues.kalshi import KalshiFeeModel
from eventcontracts.domain import (
    CashBalance,
    EventSubscription,
    RiskProfile,
    SleeveId,
    SleeveSpec,
    StrategyId,
    StrategySpec,
)
from eventcontracts.domain.decisions import IntentEnvelope, PlaceOrder
from eventcontracts.domain.events import (
    EventProvenance,
    OrderBookEvent,
    QuoteEvent,
    TradeEvent,
)
from eventcontracts.domain.ids import EventId
from eventcontracts.domain.models import (
    InstrumentId,
    OrderBook,
    OrderBookLevel,
    OutcomeSide,
    Quote,
    Trade,
    Venue,
)
from eventcontracts.execution import (
    ConstantLatency,
    FractionalQueueEstimator,
    MarketPaperSimulator,
    PnLTracker,
)
from eventcontracts.plugins.strategies import example_threshold  # noqa: F401 - registers
from eventcontracts.replay import NormalizedReplaySource
from eventcontracts.risk import DailyLossLedger, SleeveRiskGate
from eventcontracts.runner import StrategyRunner
from eventcontracts.storage import DuckDbEventStore, ParquetEventStore
from eventcontracts.strategy import create
from eventcontracts.testing import InMemoryClock, InMemoryContext, StaticContextProvider

NOW = datetime(2026, 1, 15, 10, 0, tzinfo=UTC)
INSTR = InstrumentId(venue=Venue.KALSHI, market_id="WX-NYC-TEMP", outcome_id=None)


def _strategy_spec() -> StrategySpec:
    return StrategySpec(
        strategy_id=StrategyId("example-threshold-v1"),
        name="example_threshold",
        version="0.1.0",
        description="vertical slice",
        subscription=EventSubscription(
            venues=(Venue.KALSHI,),
            instrument_patterns=("*",),
            event_kinds=("quote", "trade"),
        ),
        parameters={"buy_below": "0.55", "size": "10"},
    )


def _sleeve() -> SleeveSpec:
    return SleeveSpec(
        sleeve_id=SleeveId("paper-a"),
        strategy_id=StrategyId("example-threshold-v1"),
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
            max_market_data_age_ms=20_000,
        ),
    )


def _seed_data(store: ParquetEventStore) -> None:
    """Replay one book snapshot with quote/trade ticks from a falling market."""

    event_start = NOW - timedelta(seconds=10)
    book = OrderBookEvent(
        event_id=EventId("book-1"),
        book=OrderBook(
            instrument_id=INSTR,
            yes_bids=(
                OrderBookLevel(price=Decimal("0.43"), quantity=Decimal("50")),
                OrderBookLevel(price=Decimal("0.42"), quantity=Decimal("75")),
            ),
            yes_asks=(
                OrderBookLevel(price=Decimal("0.46"), quantity=Decimal("50")),
                OrderBookLevel(price=Decimal("0.47"), quantity=Decimal("80")),
            ),
            no_bids=(),
            no_asks=(),
            exchange_ts=event_start,
            received_at=event_start,
        ),
        provenance=EventProvenance(source="fixture", channel="book", venue=Venue.KALSHI),
    )
    store.append_normalized(book)

    # Trades stepping down through 0.46, 0.44, 0.42 — first two are above
    # threshold (0.45), the third triggers a buy.
    for i, (bid, ask, price) in enumerate(
        [("0.43", "0.46", "0.46"), ("0.43", "0.44", "0.44"), ("0.41", "0.42", "0.42")]
    ):
        trade_at = event_start + timedelta(seconds=i + 1)
        store.append_normalized(
            QuoteEvent(
                event_id=EventId(f"q-{i:03d}"),
                quote=Quote(
                    instrument_id=INSTR,
                    side=OutcomeSide.YES,
                    bid=OrderBookLevel(price=Decimal(bid), quantity=Decimal("50")),
                    ask=OrderBookLevel(price=Decimal(ask), quantity=Decimal("50")),
                    exchange_ts=trade_at,
                    received_at=trade_at,
                ),
                provenance=EventProvenance(
                    source="fixture",
                    channel="ticker",
                    venue=Venue.KALSHI,
                    source_sequence=f"q-{i}",
                ),
            )
        )
        store.append_normalized(
            TradeEvent(
                event_id=EventId(f"t-{i:03d}"),
                trade=Trade(
                    instrument_id=INSTR,
                    side=OutcomeSide.YES,
                    price=Decimal(price),
                    quantity=Decimal("20"),
                    trade_id=f"tv-{i}",
                    exchange_ts=trade_at,
                    received_at=trade_at,
                    aggressor_side=OutcomeSide.NO,  # seller hits bid
                ),
                provenance=EventProvenance(source="fixture", channel="trade", venue=Venue.KALSHI),
            )
        )
    store.flush()


def test_phase1_phase2_phase3_vertical_slice(tmp_path: Path) -> None:
    store = ParquetEventStore(tmp_path)
    _seed_data(store)

    # Phase 1 evidence: DuckDB sees the partitions.
    with DuckDbEventStore(tmp_path) as duck:
        assert duck.normalized_count() == 7
        assert set(duck.kinds_present()) == {"book", "quote", "trade"}

    spec = _strategy_spec()
    sleeve = _sleeve()
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

    # The IntentSink ties the runner output into the simulator.
    class _Sink:
        def __init__(self) -> None:
            self.fills_observed: list[str] = []

        def emit(self, envelope: IntentEnvelope) -> None:
            if not isinstance(envelope.decision, PlaceOrder):
                return
            for fill in simulator.submit_envelope(envelope):
                self.fills_observed.append(str(fill.fill_id))

    sink = _Sink()

    from collections.abc import Iterator

    from eventcontracts.domain.events import NormalizedEvent

    # Tee normalized events into the simulator so it has book/lifecycle state.
    base_source = NormalizedReplaySource(store)

    class _TeeSource:
        def stream(self) -> Iterator[NormalizedEvent]:
            for event in base_source.stream():
                simulator.on_event(event)
                pnl.on_event(event)
                yield event

    runner = StrategyRunner(
        spec=spec,
        sleeve=sleeve,
        strategy=create(spec.name, spec),
        events=_TeeSource(),
        sink=sink,
        risk=SleeveRiskGate(sleeve=sleeve, daily_loss=daily_loss),
        clock=InMemoryClock(current=NOW),
        context_provider=StaticContextProvider(
            ctx=InMemoryContext(
                strategy_id_value=spec.strategy_id,
                sleeve_id_value=sleeve.sleeve_id,
                clock_now=NOW,
                cash_by_ccy={
                    "USD": CashBalance(
                        currency="USD",
                        total=Decimal("1000"),
                        available=Decimal("1000"),
                        held_for_orders=Decimal("0"),
                        settling=Decimal("0"),
                        updated_at=NOW,
                    )
                },
            )
        ),
    )

    summary = runner.run()

    # Strategy saw the full external-style event stream and emitted an explicit
    # decision for every event.
    assert summary.events_processed == 7
    assert summary.decisions_emitted == 7
    assert summary.intents_rejected == 0

    # With threshold 0.55 and trades at 0.46/0.44/0.42, all three trades
    # trigger BUYs. The 0.46 BUY crosses the 0.46 ask and fills 10 contracts
    # immediately. The 0.44 and 0.42 BUYs rest on the book.
    assert len(sink.fills_observed) == 1

    position = pnl.position(INSTR, OutcomeSide.YES, now=NOW)
    assert position is not None
    assert position.quantity == Decimal("10")
    assert position.average_price == Decimal("0.46")

    # Kalshi taker fee on 10 @ 0.46: ceil(0.07 * 0.46 * 0.54 * 10) = ceil(0.17388) = 0.18
    assert pnl.total_fees_paid == Decimal("0.18")

    # Two resting orders sit in the simulator at 0.44 and 0.42.
    resting = [
        p for p in simulator.pending.values() if p.status.value in ("open", "partially_filled")
    ]
    assert len(resting) == 2
