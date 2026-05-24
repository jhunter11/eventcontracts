"""`eventcontracts backtest` — replay normalized events through a strategy.

Wires together: NormalizedEventStore → NormalizedReplaySource →
StrategyRunner with a MarketPaperSimulator sink → PnLTracker → BacktestReport.

Outputs the full :class:`BacktestReport` (drawdown, fill rate, peak/trough
equity, realized + unrealized PnL, rejection breakdown) as JSON. Pass
``--out`` to also persist the report to disk.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

from eventcontracts.adapters.venues.kalshi import KalshiFeeModel
from eventcontracts.adapters.venues.polymarket import PolymarketFeeModel
from eventcontracts.config import load_sleeve_spec, load_strategy_spec
from eventcontracts.domain.decisions import IntentEnvelope, PlaceOrder
from eventcontracts.domain.events import NormalizedEvent
from eventcontracts.domain.fees import FeeModel
from eventcontracts.domain.fills import Fill
from eventcontracts.domain.models import Venue
from eventcontracts.execution import (
    BacktestReport,
    ConstantLatency,
    FractionalQueueEstimator,
    MarketPaperSimulator,
    PnLTracker,
    intent_to_order,
)
from eventcontracts.replay import NormalizedReplaySource
from eventcontracts.risk import DailyLossLedger, SleeveRiskGate
from eventcontracts.runner import StrategyRunner
from eventcontracts.storage import ParquetEventStore
from eventcontracts.strategy import create_from_spec
from eventcontracts.testing import InMemoryClock, InMemoryContext, StaticContextProvider


def register(subparsers: Any) -> None:
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
    parser.add_argument(
        "--queue-fraction",
        type=str,
        default="1.0",
        help="Fraction of visible same-price depth assumed ahead of passive orders.",
    )
    parser.add_argument(
        "--starting-equity",
        type=str,
        default="0",
        help="Starting equity (in sleeve currency) for drawdown calculations.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Optional path to write the full BacktestReport as JSON.",
    )
    parser.set_defaults(handler=_handle)


def _fee_model_for(venue: Venue) -> FeeModel:
    if venue is Venue.KALSHI:
        return KalshiFeeModel()
    return PolymarketFeeModel()


def _handle(args: argparse.Namespace) -> int:
    spec = load_strategy_spec(args.strategy)
    sleeve = load_sleeve_spec(args.sleeve)

    strategy = create_from_spec(spec)

    store = ParquetEventStore(args.data)
    events = NormalizedReplaySource(store)

    daily_loss = DailyLossLedger()
    pnl = PnLTracker(currency=sleeve.currency, daily_loss_ledger=daily_loss)
    fee_model = _fee_model_for(sleeve.venue)
    simulator = MarketPaperSimulator(
        fee_model=fee_model,
        latency=ConstantLatency(submit_ms=args.latency_ms),
        queue_estimator=FractionalQueueEstimator(fraction=Decimal(args.queue_fraction)),
        strategy_id=spec.strategy_id,
        sleeve_id=sleeve.sleeve_id,
        fill_sink=pnl,
    )

    fills_collected: list[Fill] = []

    class _Sink:
        def emit(self, envelope: IntentEnvelope) -> None:
            if not isinstance(envelope.decision, PlaceOrder):
                return
            intent = intent_to_order(envelope)
            if intent is None:
                return
            fills_collected.extend(simulator.submit(intent, envelope.emitted_at))

    class _TeeSource:
        def stream(self) -> Iterator[NormalizedEvent]:
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
        events=_TeeSource(),
        sink=_Sink(),
        risk=SleeveRiskGate(sleeve=sleeve, daily_loss=daily_loss),
        clock=clock,
        context_provider=StaticContextProvider(ctx),
    )

    summary = runner.run()
    report = BacktestReport.from_run(
        summary,
        pnl,
        fills=fills_collected,
        starting_equity=Decimal(args.starting_equity),
    )
    payload = report.to_dict()
    rendered = json.dumps(payload, indent=2, default=str)
    print(rendered)

    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rendered + "\n", encoding="utf-8")
    return 0
