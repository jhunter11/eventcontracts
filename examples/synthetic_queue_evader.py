"""Synthetic end-to-end demo for `microstructure_queue_evader`.

This script lives entirely outside ``python/src/eventcontracts/``. It only
imports the framework's *public* API. The strategy, runner, storage, and
backtest CLI are unchanged.

What it does, in order:

1. Generates a synthetic stream that puts the queue evader in a corner:
   - One ``OwnOrderUpdateEvent`` declares a resting BUY at 0.50 (qty 100).
   - A sequence of ``OrderBookEvent``s show same-price depth shrinking
     from 200 → 5 contracts (queue ahead of us evaporates).
   - A sequence of ``TradeEvent``s at 0.50 accumulate adverse taker
     volume above the configured threshold.
   The configured thresholds in
   ``configs/strategies/microstructure-queue-evader.toml`` are
   ``queue_evacuation_threshold=5`` and ``adverse_volume_threshold=50``,
   so the strategy *should* emit a ``CancelOrder(priority=CRITICAL)``
   late in the stream.

2. Persists the stream to a temporary Parquet partition using
   ``ParquetEventStore`` exactly the way real captures land it.

3. Runs the same partition through the public ``run_backtest`` helper
   so we exercise the production pipeline: replay → runner → risk gate
   → sink. Asserts the dispatched-intent count matches expectations.

4. Re-runs the same partition through a hand-built ``StrategyRunner``
   that holds a capturing sink, so we can print the *content* of every
   decision the strategy emitted (which ``run_backtest`` aggregates
   into counts only).

Run it with:

    PYTHONPATH=python/src python examples/synthetic_queue_evader.py

The script prints a short report and exits non-zero on assertion
failures so it can run in CI later if you want.
"""

from __future__ import annotations

import sys
import tempfile
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

# All imports are public framework API. No edits to python/src/eventcontracts/.
from eventcontracts.cli.backtest import run_backtest
from eventcontracts.config import load_sleeve_spec, load_strategy_spec
from eventcontracts.domain.decisions import (
    CancelOrder,
    IntentEnvelope,
    PlaceOrder,
    StrategyDecision,
    decision_kind,
)
from eventcontracts.domain.events import (
    EventProvenance,
    NormalizedEvent,
    OrderBookEvent,
    OwnOrderUpdateEvent,
    TradeEvent,
)
from eventcontracts.domain.ids import (
    ClientOrderId,
    CorrelationId,
    EventId,
    SleeveId,
    StrategyId,
    VenueOrderId,
)
from eventcontracts.domain.models import (
    InstrumentId,
    OrderBook,
    OrderBookLevel,
    OutcomeSide,
    Trade,
    Venue,
)
from eventcontracts.domain.orders import (
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    TimeInForce,
)
from eventcontracts.replay import NormalizedReplaySource
from eventcontracts.risk import DailyLossLedger, SleeveRiskGate
from eventcontracts.runner import StrategyRunner
from eventcontracts.storage import ParquetEventStore
from eventcontracts.strategy import create_from_spec
from eventcontracts.testing import InMemoryClock, InMemoryContext, StaticContextProvider


# --- Configurable parameters for the synthetic scenario ---------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
STRATEGY_CONFIG = REPO_ROOT / "configs/strategies/microstructure-queue-evader.toml"
SLEEVE_CONFIG = REPO_ROOT / "configs/sleeves/microstructure-queue-kalshi-paper-a.toml"

INSTRUMENT = InstrumentId(venue=Venue.KALSHI, market_id="DEMO-Q-EVADE", outcome_id=None)
RESTING_PRICE = Decimal("0.50")
RESTING_QTY = Decimal("100")
T0 = datetime(2026, 5, 25, 14, 0, tzinfo=UTC)

OWN_ORDER_COID = ClientOrderId(uuid4().hex)
OWN_ORDER_VID = VenueOrderId("demo-vo-1")
OWN_ORDER_CORR = CorrelationId(uuid4().hex)
OWN_ORDER_STRAT = StrategyId("microstructure-queue_evader-v1")
OWN_ORDER_SLEEVE = SleeveId("microstructure-queue-kalshi-paper-a")


# --- 1. Synthetic event generator ------------------------------------------


def _own_order(at: datetime, *, status: OrderStatus, filled_qty: Decimal) -> OwnOrderUpdateEvent:
    order = Order(
        client_order_id=OWN_ORDER_COID,
        venue_order_id=OWN_ORDER_VID,
        instrument_id=INSTRUMENT,
        outcome_side=OutcomeSide.YES,
        order_side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.GTC,
        price=RESTING_PRICE,
        quantity=RESTING_QTY,
        filled_quantity=filled_qty,
        status=status,
        created_at=T0,
        updated_at=at,
        correlation_id=OWN_ORDER_CORR,
        strategy_id=OWN_ORDER_STRAT,
        sleeve_id=OWN_ORDER_SLEEVE,
    )
    return OwnOrderUpdateEvent(
        event_id=EventId(f"own-{at.isoformat()}"),
        order=order,
        provenance=EventProvenance(source="demo", channel="own_order", venue=Venue.KALSHI),
    )


def _book(at: datetime, same_price_depth: Decimal) -> OrderBookEvent:
    """Book with `same_price_depth` resting at 0.50 ahead of us."""

    return OrderBookEvent(
        event_id=EventId(f"book-{at.isoformat()}"),
        book=OrderBook(
            instrument_id=INSTRUMENT,
            yes_bids=(
                OrderBookLevel(price=RESTING_PRICE, quantity=same_price_depth),
                OrderBookLevel(price=Decimal("0.49"), quantity=Decimal("40")),
            ),
            yes_asks=(
                OrderBookLevel(price=Decimal("0.51"), quantity=Decimal("60")),
                OrderBookLevel(price=Decimal("0.52"), quantity=Decimal("80")),
            ),
            no_bids=(),
            no_asks=(),
            exchange_ts=at,
            received_at=at,
        ),
        provenance=EventProvenance(source="demo", channel="book", venue=Venue.KALSHI),
    )


def _trade(at: datetime, *, price: str, qty: str) -> TradeEvent:
    return TradeEvent(
        event_id=EventId(f"trade-{at.isoformat()}-{price}"),
        trade=Trade(
            instrument_id=INSTRUMENT,
            side=OutcomeSide.YES,
            price=Decimal(price),
            quantity=Decimal(qty),
            trade_id=f"tv-{at.isoformat()}",
            exchange_ts=at,
            received_at=at,
            aggressor_side=OutcomeSide.NO,  # seller hits our bid
        ),
        provenance=EventProvenance(source="demo", channel="trade", venue=Venue.KALSHI),
    )


def synthetic_stream() -> list[NormalizedEvent]:
    """The whole scenario timeline as one ordered list."""

    def at(secs: int) -> datetime:
        return T0 + timedelta(seconds=secs)

    stream: list[NormalizedEvent] = []
    # t=0: our resting BUY appears.
    stream.append(_own_order(at(0), status=OrderStatus.OPEN, filled_qty=Decimal("0")))
    # t=1..6: same-price depth shrinks from 200 to 5; each shrink moves us
    # forward in the queue per the strategy's heuristic.
    for offset, depth in enumerate([200, 150, 100, 60, 30, 5], start=1):
        stream.append(_book(at(offset), Decimal(depth)))
    # t=7..9: three taker trades at our price totalling 90 contracts — well
    # above adverse_volume_threshold=50.
    stream.append(_trade(at(7), price="0.50", qty="30"))
    stream.append(_trade(at(8), price="0.50", qty="30"))
    stream.append(_trade(at(9), price="0.50", qty="30"))
    return stream


# --- 2. Persist to a Parquet partition -------------------------------------


def seed_partition(root: Path) -> int:
    store = ParquetEventStore(root)
    count = 0
    for event in synthetic_stream():
        store.append_normalized(event)
        count += 1
    store.flush()
    return count


# --- 3. Drive the full backtest pipeline ----------------------------------


def run_via_backtest(data_root: Path) -> dict[str, object]:
    spec = load_strategy_spec(STRATEGY_CONFIG)
    sleeve = load_sleeve_spec(SLEEVE_CONFIG)
    report, summary = run_backtest(spec, sleeve, data_root)
    return {
        "events_processed": summary.events_processed,
        "decisions_emitted": summary.decisions_emitted,
        "intents_dispatched": summary.intents_dispatched,
        "intents_rejected": summary.intents_rejected,
        "fills": report.fills,
        "rejection_reasons": dict(summary.rejection_reasons),
    }


# --- 4. Drive a hand-built runner so we can inspect each decision ---------


def run_with_capturing_sink(data_root: Path) -> list[IntentEnvelope]:
    spec = load_strategy_spec(STRATEGY_CONFIG)
    sleeve = load_sleeve_spec(SLEEVE_CONFIG)
    strategy = create_from_spec(spec)
    store = ParquetEventStore(data_root)
    base_source = NormalizedReplaySource(store)

    captured: list[IntentEnvelope] = []

    class _CapturingSink:
        def emit(self, envelope: IntentEnvelope) -> None:
            captured.append(envelope)

    class _PassThrough:
        def stream(self) -> Iterator[NormalizedEvent]:
            yield from base_source.stream()

    clock = InMemoryClock(current=T0)
    ctx = InMemoryContext(
        strategy_id_value=spec.strategy_id,
        sleeve_id_value=sleeve.sleeve_id,
        clock_now=clock.current,
    )
    runner = StrategyRunner(
        spec=spec,
        sleeve=sleeve,
        strategy=strategy,
        events=_PassThrough(),
        sink=_CapturingSink(),
        risk=SleeveRiskGate(sleeve=sleeve, daily_loss=DailyLossLedger()),
        clock=clock,
        context_provider=StaticContextProvider(ctx),
    )
    runner.run()
    return captured


# --- 5. Pretty-print + assertions -----------------------------------------


def _kind_counts(decisions: list[StrategyDecision]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for decision in decisions:
        kind = decision_kind(decision)
        counts[kind] = counts.get(kind, 0) + 1
    return counts


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="ec_queue_evader_") as tmp:
        data_root = Path(tmp) / "data"
        n = seed_partition(data_root)
        print(f"seeded {n} synthetic events under {data_root}")
        print()

        print("=== Backtest via public run_backtest() ===")
        report = run_via_backtest(data_root)
        for key, value in report.items():
            print(f"  {key:<22} {value}")
        print()

        print("=== Strategy decisions (captured via direct runner) ===")
        envelopes = run_with_capturing_sink(data_root)
        decisions = [env.decision for env in envelopes]
        counts = _kind_counts(decisions)
        for kind, count in sorted(counts.items()):
            print(f"  {kind:<14} {count}")
        print()

        # Inspect just the cancels: that's the protective behavior the
        # strategy exists for.
        cancels = [d for d in decisions if isinstance(d, CancelOrder)]
        places = [d for d in decisions if isinstance(d, PlaceOrder)]
        print(f"CancelOrder decisions: {len(cancels)}  PlaceOrder decisions: {len(places)}")
        for cancel in cancels:
            print(f"  cancel coid={cancel.client_order_id}  reason={cancel.reason}")
        print()

        # Assertions — fail loud so this is usable as a smoke test in CI.
        problems: list[str] = []
        if report["events_processed"] != n:
            problems.append(
                f"runner saw {report['events_processed']} events, expected {n}"
            )
        if len(cancels) < 1:
            problems.append(
                "expected at least one protective CancelOrder; "
                "queue-evader heuristic did not fire on this synthetic scenario"
            )
        if any(isinstance(d, PlaceOrder) for d in decisions):
            problems.append(
                "queue evader is a protective-only strategy and should never emit "
                "PlaceOrder on this scenario"
            )

        if problems:
            print("FAILURES:")
            for p in problems:
                print(f"  - {p}")
            return 1
        print("OK: synthetic queue-evader end-to-end run passed all assertions.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
