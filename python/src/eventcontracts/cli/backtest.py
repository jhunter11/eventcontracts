"""`eventcontracts backtest` — replay normalized events through a strategy.

Wires together: NormalizedEventStore → NormalizedReplaySource →
StrategyRunner with a MarketPaperSimulator sink → PnLTracker. Prints a
summary of events processed, decisions emitted, fills produced, and
realized PnL at the end. Use this from the command line to validate a
strategy spec against a known partition of data without writing any
glue code.
"""

from __future__ import annotations

import argparse
import json
from decimal import Decimal
from pathlib import Path

from eventcontracts.adapters.venues.kalshi import KalshiFeeModel
from eventcontracts.adapters.venues.polymarket import PolymarketFeeModel
from eventcontracts.config import load_sleeve_spec, load_strategy_spec
from eventcontracts.domain.fees import FeeModel
from eventcontracts.domain.models import Venue
from eventcontracts.execution import (
    ConstantLatency,
    FractionalQueueEstimator,
    MarketPaperSimulator,
    PnLTracker,
)
from eventcontracts.replay import NormalizedReplaySource
from eventcontracts.risk import DailyLossLedger, SleeveRiskGate
from eventcontracts.runner import StrategyRunner
from eventcontracts.storage import ParquetEventStore
from eventcontracts.strategy import create, load_entry_points, registry
from eventcontracts.testing import InMemoryClock, InMemoryContext, StaticContextProvider


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "backtest",
        help="Replay normalized events through a strategy with a paper executor.",
    )
    parser.add_argument("--strategy", type=Path, required=True, help="strategy_spec.toml path")
    parser.add_argument("--sleeve", type=Path, required=True, help="sleeve_spec.toml path")
    parser.add_argument("--data", type=Path, required=True, help="ParquetEventStore root")
    parser.add_argument(
        "--latency-ms",
        type=float,
        default=50.0,
        help="Constant submit latency in milliseconds.",
    )
    parser.set_defaults(handler=_handle)


def _fee_model_for(venue: Venue) -> FeeModel:
    if venue is Venue.KALSHI:
        return KalshiFeeModel()
    return PolymarketFeeModel()


def _handle(args: argparse.Namespace) -> int:
    spec = load_strategy_spec(args.strategy)
    sleeve = load_sleeve_spec(args.sleeve)

    # Ensure plugin strategies are discoverable.
    if spec.name not in registry.known():
        load_entry_points()

    strategy = create(spec.name, spec)

    store = ParquetEventStore(args.data)
    events = NormalizedReplaySource(store)

    daily_loss = DailyLossLedger()
    pnl = PnLTracker(currency=sleeve.currency, daily_loss_ledger=daily_loss)
    fee_model = _fee_model_for(sleeve.venue)
    simulator = MarketPaperSimulator(
        fee_model=fee_model,
        latency=ConstantLatency(submit_ms=args.latency_ms),
        queue_estimator=FractionalQueueEstimator(),
        strategy_id=spec.strategy_id,
        sleeve_id=sleeve.sleeve_id,
        fill_sink=pnl,
    )

    # Build an IntentSink that routes envelopes into the simulator.
    from eventcontracts.domain.decisions import IntentEnvelope, PlaceOrder
    from eventcontracts.execution import intent_to_order

    fills_collected: list[tuple[str, str]] = []

    class _Sink:
        def emit(self, envelope: IntentEnvelope) -> None:
            if not isinstance(envelope.decision, PlaceOrder):
                return
            intent = intent_to_order(envelope)
            if intent is None:
                return
            sim_fills = simulator.submit(intent, envelope.emitted_at)
            for fill in sim_fills:
                fills_collected.append((str(fill.fill_id), str(fill.price)))

    # Also forward events into the simulator for book/lifecycle state.
    class _TeeSource:
        def stream(self):
            for event in events.stream():
                simulator.on_event(event)
                pnl.on_event(event)
                yield event

    clock = InMemoryClock()
    ctx = InMemoryContext(
        strategy_id_value=spec.strategy_id,
        sleeve_id_value=sleeve.sleeve_id,
        clock_now=clock.current,
    )

    runner = StrategyRunner(
        spec=spec,
        sleeve=sleeve,
        strategy=strategy,
        events=_TeeSource(),  # type: ignore[arg-type]
        sink=_Sink(),  # type: ignore[arg-type]
        risk=SleeveRiskGate(sleeve=sleeve, daily_loss=daily_loss),
        clock=clock,
        context_provider=StaticContextProvider(ctx),
    )

    summary = runner.run()
    report = {
        "strategy_id": summary.strategy_id,
        "sleeve_id": summary.sleeve_id,
        "events_processed": summary.events_processed,
        "decisions_emitted": summary.decisions_emitted,
        "intents_dispatched": summary.intents_dispatched,
        "intents_rejected": summary.intents_rejected,
        "fills": len(fills_collected),
        "total_fees": str(pnl.total_fees_paid),
        "realized_pnl": str(pnl.cumulative_realized),
        "rejection_reasons": summary.rejection_reasons,
    }
    print(json.dumps(report, indent=2, default=str))
    return 0
