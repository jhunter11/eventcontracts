"""`eventcontracts backtest` — replay normalized events through a strategy.

Wires together: NormalizedEventStore → NormalizedReplaySource →
StrategyRunner with a MarketPaperSimulator sink → PnLTracker → BacktestReport.

Outputs the full :class:`BacktestReport` (drawdown, fill rate, peak/trough
equity, realized + unrealized PnL, rejection breakdown) as JSON. Pass
``--out`` to also persist the report to disk.

The :func:`run_backtest` helper exposes the same loop without argparse so
the sweep harness (and other in-process callers) can reuse it.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterable, Iterator
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from eventcontracts.adapters.venues.kalshi import KalshiFeeModel
from eventcontracts.adapters.venues.polymarket import PolymarketFeeModel
from eventcontracts.audit import audit_stamp_for
from eventcontracts.config import load_sleeve_spec, load_strategy_spec
from eventcontracts.domain.decisions import IntentEnvelope, PlaceOrder
from eventcontracts.domain.events import NormalizedEvent
from eventcontracts.domain.fees import FeeModel
from eventcontracts.domain.fills import Fill
from eventcontracts.domain.models import Venue
from eventcontracts.domain.spec import SleeveSpec, StrategySpec
from eventcontracts.execution import (
    BacktestReport,
    ConstantLatency,
    FractionalQueueEstimator,
    MarketPaperSimulator,
    PnLTracker,
    intent_to_order,
)
from eventcontracts.features import (
    DeterministicFeatureBuilder,
    InMemoryFeatureStore,
    OnlineFeatureState,
)
from eventcontracts.models import (
    InProcessModelRunner,
    ModelArtifact,
)
from eventcontracts.replay import NormalizedReplaySource
from eventcontracts.risk import DailyLossLedger, SleeveRiskGate
from eventcontracts.runner import StrategyRunner
from eventcontracts.runner.base import RunSummary
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
        "--start",
        type=str,
        default=None,
        help="Optional ISO-8601 UTC lower bound on event exchange_ts/received_at.",
    )
    parser.add_argument(
        "--end",
        type=str,
        default=None,
        help="Optional ISO-8601 UTC upper bound on event exchange_ts/received_at.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Optional path to write the full BacktestReport as JSON.",
    )
    parser.add_argument(
        "--model",
        type=Path,
        default=None,
        action="append",
        help=(
            "Path to a model artifact JSON (as produced by `eventcontracts "
            "train`). Pass multiple times to load several models; strategies "
            "select by name via ctx.predict(model_name, ...)."
        ),
    )
    parser.set_defaults(handler=_handle)


def _fee_model_for(venue: Venue) -> FeeModel:
    if venue is Venue.KALSHI:
        return KalshiFeeModel()
    return PolymarketFeeModel()


def _event_time(event: NormalizedEvent) -> datetime | None:
    """Best-effort event-time for windowing.

    Falls back across the variant-specific fields so windowing works for any
    normalized variant without each one needing a dedicated accessor.
    """

    for path in (
        ("trade", "exchange_ts"),
        ("trade", "received_at"),
        ("quote", "exchange_ts"),
        ("quote", "received_at"),
        ("book", "exchange_ts"),
        ("book", "received_at"),
        ("lifecycle", "exchange_ts"),
        ("lifecycle", "received_at"),
        ("settlement", "settled_at"),
        ("fill", "exchange_ts"),
        ("fill", "filled_at"),
        ("order", "updated_at"),
        ("reject", "rejected_at"),
    ):
        cursor: Any = event
        for attr in path:
            cursor = getattr(cursor, attr, None)
            if cursor is None:
                break
        if isinstance(cursor, datetime):
            return cursor
    for attr in ("exchange_ts", "received_at", "timestamp"):
        cursor = getattr(event, attr, None)
        if isinstance(cursor, datetime):
            return cursor
    return None


def run_backtest(
    strategy_spec: StrategySpec,
    sleeve_spec: SleeveSpec,
    data_root: Path | str,
    *,
    latency_ms: float = 50.0,
    queue_fraction: str | Decimal = "1.0",
    starting_equity: str | Decimal = "0",
    start: datetime | None = None,
    end: datetime | None = None,
    feature_builder: DeterministicFeatureBuilder | None = None,
    feature_store: InMemoryFeatureStore | None = None,
    model_runner: InProcessModelRunner | None = None,
) -> tuple[BacktestReport, RunSummary]:
    """Run one backtest in-process. Returns the report and run summary.

    When a ``feature_builder`` is provided, each event is fed through the
    builder; every emitted vector is written to ``feature_store`` (defaults
    to a fresh ``InMemoryFeatureStore``) and exposed to the strategy via
    ``StrategyContext.feature()`` / ``feature_vector()``.
    """

    strategy = create_from_spec(strategy_spec)
    store = ParquetEventStore(Path(data_root))
    base_source = NormalizedReplaySource(store)

    daily_loss = DailyLossLedger()
    pnl = PnLTracker(currency=sleeve_spec.currency, daily_loss_ledger=daily_loss)
    fee_model = _fee_model_for(sleeve_spec.venue)
    simulator = MarketPaperSimulator(
        fee_model=fee_model,
        latency=ConstantLatency(submit_ms=latency_ms),
        queue_estimator=FractionalQueueEstimator(fraction=Decimal(str(queue_fraction))),
        strategy_id=strategy_spec.strategy_id,
        sleeve_id=sleeve_spec.sleeve_id,
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

    def _filtered(events: Iterable[NormalizedEvent]) -> Iterator[NormalizedEvent]:
        for event in events:
            ts = _event_time(event)
            if start is not None and ts is not None and ts < start:
                continue
            if end is not None and ts is not None and ts >= end:
                continue
            yield event

    clock = InMemoryClock()
    ctx = InMemoryContext(
        strategy_id_value=strategy_spec.strategy_id,
        sleeve_id_value=sleeve_spec.sleeve_id,
        clock_now=clock.current,
        model_runner=model_runner,
    )

    # Feature wiring: each emitted vector is persisted to the store and
    # surfaced through ctx.features so the strategy sees real values.
    active_store = feature_store if feature_store is not None else InMemoryFeatureStore()
    feature_state: OnlineFeatureState | None = (
        feature_builder.warmup(()) if feature_builder is not None else None
    )

    class _TeeSource:
        def stream(self) -> Iterator[NormalizedEvent]:
            nonlocal feature_state
            for event in _filtered(base_source.stream()):
                simulator.on_event(event)
                pnl.on_event(event)
                if feature_builder is not None and feature_state is not None:
                    feature_state = feature_builder.update(feature_state, event)
                    vector = feature_state.vector
                    if vector is not None:
                        active_store.write_vector(
                            vector,
                            audit=audit_stamp_for(
                                vector,
                                object_id=(
                                    f"feature:{vector.schema_id}:{vector.timestamp.isoformat()}"
                                ),
                                object_kind="feature_vector",
                                schema_version=vector.schema_version,
                                produced_at=vector.timestamp,
                                producer=f"backtest:{type(feature_builder).__name__}",
                            ),
                        )
                        ctx.features = vector
                yield event

    runner = StrategyRunner(
        spec=strategy_spec,
        sleeve=sleeve_spec,
        strategy=strategy,
        events=_TeeSource(),
        sink=_Sink(),
        risk=SleeveRiskGate(sleeve=sleeve_spec, daily_loss=daily_loss),
        clock=clock,
        context_provider=StaticContextProvider(ctx),
    )

    summary = runner.run()
    report = BacktestReport.from_run(
        summary,
        pnl,
        fills=fills_collected,
        starting_equity=Decimal(str(starting_equity)),
    )
    return report, summary


def _parse_optional_datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _load_model_runner(model_paths: list[Path] | None) -> InProcessModelRunner | None:
    """Build a runner from `--model` artifact paths.

    Each artifact is verified against its on-disk sha256 before being
    registered. Returns `None` when no model paths were passed.
    """

    if not model_paths:
        return None
    import json

    runner = InProcessModelRunner()
    for path in model_paths:
        raw = path.read_text(encoding="utf-8").strip()
        digest_bytes = raw.encode("utf-8")
        from hashlib import sha256

        digest = sha256(digest_bytes).hexdigest()
        payload = json.loads(raw)
        from eventcontracts.audit import audit_stamp_for
        from eventcontracts.domain.ids import ModelName as _ModelName
        from eventcontracts.domain.ids import ModelVersion as _ModelVersion

        created_at = datetime.fromisoformat(
            str(payload["created_at"]).replace("Z", "+00:00")
        )
        artifact = ModelArtifact(
            name=_ModelName(str(payload["model_name"])),
            version=_ModelVersion(str(payload["model_version"])),
            uri=str(path.resolve()),
            sha256=digest,
            format=str(payload["kind"]),
            created_at=created_at,
            audit=audit_stamp_for(
                payload,
                object_id=f"model-artifact:{payload['model_name']}:{payload['model_version']}",
                object_kind="model_artifact",
                schema_version="model-artifact-v1",
                produced_at=created_at,
                producer="backtest_cli",
            ),
        )
        runner.load(artifact)
    return runner


def _handle(args: argparse.Namespace) -> int:
    spec = load_strategy_spec(args.strategy)
    sleeve = load_sleeve_spec(args.sleeve)
    model_runner = _load_model_runner(args.model)
    report, _summary = run_backtest(
        spec,
        sleeve,
        args.data,
        latency_ms=args.latency_ms,
        queue_fraction=args.queue_fraction,
        starting_equity=args.starting_equity,
        start=_parse_optional_datetime(args.start),
        end=_parse_optional_datetime(args.end),
        model_runner=model_runner,
    )
    payload = report.to_dict()
    rendered = json.dumps(payload, indent=2, default=str)
    print(rendered)
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rendered + "\n", encoding="utf-8")
    return 0
