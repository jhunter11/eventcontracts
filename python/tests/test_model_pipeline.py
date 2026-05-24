"""End-to-end model training and inference tests.

Covers labelers, dataset assembly, model fitting (linear + logistic),
artifact write/load (with sha256 verification), in-memory + file-backed
registries, the in-process runner, and the StrategyContext.predict()
path through the InMemoryContext.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import numpy as np
import pytest

from eventcontracts.adapters.venues.kalshi import KalshiFeeModel
from eventcontracts.domain.events import (
    EventProvenance,
    OrderBookEvent,
    QuoteEvent,
    SettlementResolvedEvent,
    TradeEvent,
)
from eventcontracts.domain.features import FeatureVector
from eventcontracts.domain.ids import (
    EventId,
    FeatureSchemaId,
    ModelName,
    ModelVersion,
    SleeveId,
    StrategyId,
)
from eventcontracts.domain.lifecycle import SettlementEvent
from eventcontracts.domain.models import (
    InstrumentId,
    OrderBook,
    OrderBookLevel,
    OutcomeSide,
    Quote,
    Trade,
    Venue,
)
from eventcontracts.features.builders import RollingMidVwapImbalanceBuilder
from eventcontracts.models import (
    BinaryProfitableAfterFeesLabeler,
    InMemoryModelRegistry,
    InProcessModelRunner,
    LinearRegressionModel,
    LocalFileModelRegistry,
    LogisticRegressionModel,
    ModelKind,
    ModelTrainer,
    NextMidChangeBpsLabeler,
    SettlementProbabilityLabeler,
    TrainerConfig,
    TrainingExampleBuilder,
    load_artifact,
    write_artifact,
)
from eventcontracts.testing import InMemoryContext

INSTRUMENT = InstrumentId(venue=Venue.KALSHI, market_id="M-1")
SCHEMA = FeatureSchemaId("test_features")
T0 = datetime(2026, 5, 25, 14, 0, tzinfo=UTC)


def _quote(seconds: int, bid: str, ask: str) -> QuoteEvent:
    ts = T0 + timedelta(seconds=seconds)
    return QuoteEvent(
        event_id=EventId(f"q-{seconds:03d}"),
        quote=Quote(
            instrument_id=INSTRUMENT,
            side=OutcomeSide.YES,
            bid=OrderBookLevel(price=Decimal(bid), quantity=Decimal("100")),
            ask=OrderBookLevel(price=Decimal(ask), quantity=Decimal("100")),
            exchange_ts=ts,
            received_at=ts,
        ),
        provenance=EventProvenance(source="fixture", channel="quote"),
    )


def _trade(seconds: int, price: str, qty: str) -> TradeEvent:
    ts = T0 + timedelta(seconds=seconds)
    return TradeEvent(
        event_id=EventId(f"t-{seconds:03d}"),
        trade=Trade(
            instrument_id=INSTRUMENT,
            side=OutcomeSide.YES,
            price=Decimal(price),
            quantity=Decimal(qty),
            trade_id=f"tv-{seconds}",
            exchange_ts=ts,
            received_at=ts,
        ),
        provenance=EventProvenance(source="fixture", channel="trade"),
    )


def _book(seconds: int, bid_qty: str, ask_qty: str, bid: str = "0.50", ask: str = "0.51") -> OrderBookEvent:
    ts = T0 + timedelta(seconds=seconds)
    return OrderBookEvent(
        event_id=EventId(f"b-{seconds:03d}"),
        book=OrderBook(
            instrument_id=INSTRUMENT,
            yes_bids=(OrderBookLevel(price=Decimal(bid), quantity=Decimal(bid_qty)),),
            yes_asks=(OrderBookLevel(price=Decimal(ask), quantity=Decimal(ask_qty)),),
            no_bids=(),
            no_asks=(),
            exchange_ts=ts,
            received_at=ts,
        ),
        provenance=EventProvenance(source="fixture", channel="book"),
    )


def test_next_mid_change_labeler_returns_bps_diff() -> None:
    labeler = NextMidChangeBpsLabeler(horizon_seconds_value=10)
    future = [_quote(15, "0.60", "0.62")]
    label = labeler.label(
        instrument_id=INSTRUMENT,
        as_of=T0,
        as_of_mid=Decimal("0.50"),
        future_events=future,
    )
    # mid=0.50 -> mid=0.61 = +2200 bps
    assert label == pytest.approx(2200.0, abs=1.0)


def test_settlement_labeler_returns_binary_outcome() -> None:
    labeler = SettlementProbabilityLabeler(horizon_seconds_value=60)
    settlement_event = SettlementResolvedEvent(
        event_id=EventId("s-1"),
        settlement=SettlementEvent(
            instrument_id=INSTRUMENT,
            resolved_side=OutcomeSide.YES,
            payout_per_contract=Decimal("1.00"),
            currency="USD",
            settled_at=T0 + timedelta(seconds=30),
            source="venue",
        ),
        provenance=EventProvenance(source="fixture", channel="settlement"),
    )
    label = labeler.label(
        instrument_id=INSTRUMENT,
        as_of=T0,
        as_of_mid=Decimal("0.50"),
        future_events=[settlement_event],
    )
    assert label == 1.0


def test_binary_profitable_labeler_uses_fee_model() -> None:
    labeler = BinaryProfitableAfterFeesLabeler(
        horizon_seconds_value=10,
        fee_model=KalshiFeeModel(),
    )
    # Big mid jump easily covers Kalshi taker fee.
    future = [_quote(15, "0.70", "0.72")]
    profitable = labeler.label(
        instrument_id=INSTRUMENT,
        as_of=T0,
        as_of_mid=Decimal("0.50"),
        future_events=future,
    )
    assert profitable == 1.0
    # Tiny move smaller than fees -> 0.
    flat_future = [_quote(15, "0.501", "0.502")]
    flat = labeler.label(
        instrument_id=INSTRUMENT,
        as_of=T0,
        as_of_mid=Decimal("0.50"),
        future_events=flat_future,
    )
    assert flat == 0.0


def test_dataset_builder_emits_chronological_examples() -> None:
    builder = TrainingExampleBuilder(
        feature_builder=RollingMidVwapImbalanceBuilder(
            schema_id=SCHEMA,
            window_seconds=5,
            ewma_half_life_seconds=2,
        ),
        labeler=NextMidChangeBpsLabeler(horizon_seconds_value=5),
    )
    stream: list[QuoteEvent | TradeEvent] = []
    # Mid drifts from 0.50 → 0.60 over 30 seconds; labeler should produce
    # positive bps targets for early windows.
    for i in range(30):
        bid = Decimal("0.50") + Decimal("0.005") * i
        ask = bid + Decimal("0.01")
        stream.append(_quote(i, str(bid), str(ask)))
        if i % 3 == 0:
            stream.append(_trade(i, str(bid + Decimal("0.005")), "10"))
    examples = builder.build(stream)
    assert examples, "expected at least one labeled example"
    assert all(ex.features.timestamp <= examples[-1].features.timestamp for ex in examples)
    # First half should have positive labels because mid keeps rising.
    early = examples[: len(examples) // 2]
    assert sum(1 for ex in early if ex.label > 0) > len(early) // 2


def test_linear_regression_recovers_coefficients() -> None:
    feature_names = ("x1", "x2")
    rng = np.random.default_rng(0)
    x = rng.standard_normal((400, 2))
    y = 2.0 + 3.0 * x[:, 0] - 1.5 * x[:, 1] + rng.standard_normal(400) * 0.05
    model = LinearRegressionModel.fit(feature_names, x, y)
    assert model.intercept == pytest.approx(2.0, abs=0.1)
    assert model.coefficients[0] == pytest.approx(3.0, abs=0.1)
    assert model.coefficients[1] == pytest.approx(-1.5, abs=0.1)
    pred = model.predict({"x1": 1.0, "x2": -1.0})
    assert pred == pytest.approx(2.0 + 3.0 + 1.5, abs=0.2)


def test_logistic_regression_separates_two_clusters() -> None:
    feature_names = ("x1", "x2")
    rng = np.random.default_rng(0)
    pos = rng.normal(loc=2.0, scale=0.5, size=(200, 2))
    neg = rng.normal(loc=-2.0, scale=0.5, size=(200, 2))
    x = np.vstack([pos, neg])
    y = np.concatenate([np.ones(200), np.zeros(200)])
    model = LogisticRegressionModel.fit(
        feature_names, x, y, learning_rate=0.5, iterations=300, seed=0
    )
    # Well-separated clusters should produce sharp predictions.
    assert model.predict({"x1": 2.0, "x2": 2.0}) > 0.9
    assert model.predict({"x1": -2.0, "x2": -2.0}) < 0.1


def test_trainer_emits_metrics_and_payload() -> None:
    from eventcontracts.models.dataset import TrainingExample

    feature_names = ("x1", "x2")
    rng = np.random.default_rng(1)
    examples = []
    for i in range(150):
        ts = T0 + timedelta(seconds=i)
        x = rng.standard_normal(2)
        label = float(x[0] > x[1])
        vector = FeatureVector(
            schema_id=SCHEMA,
            schema_version="v1",
            instrument_id=INSTRUMENT,
            timestamp=ts,
            values=(("x1", float(x[0])), ("x2", float(x[1]))),
        )
        examples.append(TrainingExample(features=vector, label=label))
    trainer = ModelTrainer(
        TrainerConfig(
            model_name=ModelName("test_logistic"),
            model_version="v1",
            kind=ModelKind.LOGISTIC_REGRESSION,
            feature_schema_id=SCHEMA,
            feature_schema_version="v1",
            horizon_seconds=10,
            iterations=200,
            learning_rate=0.3,
            seed=0,
        )
    )
    result = trainer.train(examples, now=T0)
    assert result.metrics.feature_count == 2
    assert result.metrics.train_samples + result.metrics.validate_samples == len(examples)
    assert result.metrics.accuracy is not None
    assert result.artifact_payload["kind"] == ModelKind.LOGISTIC_REGRESSION.value
    assert result.artifact_payload["feature_names"] == list(feature_names)


def test_artifact_roundtrip_with_sha256_verification(tmp_path: Path) -> None:
    payload = {
        "model_name": "round_trip",
        "model_version": "v1",
        "kind": ModelKind.LINEAR_REGRESSION.value,
        "feature_schema_id": str(SCHEMA),
        "feature_schema_version": "v1",
        "horizon_seconds": 5,
        "created_at": T0.isoformat(),
        "feature_names": ["x1", "x2"],
        "coefficients": [1.5, -0.5],
        "intercept": 0.25,
    }
    artifact = write_artifact(payload, path=tmp_path / "m.json")
    model, loaded = load_artifact(artifact)
    assert isinstance(model, LinearRegressionModel)
    assert loaded["feature_names"] == ["x1", "x2"]
    assert model.predict({"x1": 2.0, "x2": 1.0}) == pytest.approx(0.25 + 3.0 - 0.5)
    # Tamper with the file and ensure the sha256 verifier fails.
    (tmp_path / "m.json").write_text(
        json.dumps({**payload, "intercept": 99.0}, sort_keys=True, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        load_artifact(artifact)


def test_in_memory_registry_register_and_get(tmp_path: Path) -> None:
    payload = {
        "model_name": "reg_model",
        "model_version": "v1",
        "kind": ModelKind.LINEAR_REGRESSION.value,
        "feature_schema_id": str(SCHEMA),
        "feature_schema_version": "v1",
        "horizon_seconds": 5,
        "created_at": T0.isoformat(),
        "feature_names": ["x1"],
        "coefficients": [1.0],
        "intercept": 0.0,
    }
    artifact = write_artifact(payload, path=tmp_path / "reg.json")
    registry = InMemoryModelRegistry()
    registry.register(artifact)
    fetched = registry.get(artifact.name, artifact.version)
    assert fetched.sha256 == artifact.sha256
    registry.promote(artifact, "production")
    assert registry.current(artifact.name, "production") == artifact
    assert artifact.version in registry.list_versions(artifact.name)


def test_local_file_registry_round_trip(tmp_path: Path) -> None:
    payload = {
        "model_name": "file_reg",
        "model_version": "v1",
        "kind": ModelKind.LINEAR_REGRESSION.value,
        "feature_schema_id": str(SCHEMA),
        "feature_schema_version": "v1",
        "horizon_seconds": 5,
        "created_at": T0.isoformat(),
        "feature_names": ["x1"],
        "coefficients": [2.0],
        "intercept": 0.5,
    }
    registry = LocalFileModelRegistry(tmp_path / "registry")
    artifact = registry.write_from_payload(payload)
    fetched = registry.get(artifact.name, artifact.version)
    assert fetched.uri == artifact.uri
    registry.promote(artifact, "shadow")
    assert registry.current(artifact.name, "shadow") == fetched
    assert ModelVersion("v1") in registry.list_versions(artifact.name)


def test_runner_predicts_through_ctx_predict(tmp_path: Path) -> None:
    payload = {
        "model_name": "ctx_demo",
        "model_version": "v1",
        "kind": ModelKind.LINEAR_REGRESSION.value,
        "feature_schema_id": str(SCHEMA),
        "feature_schema_version": "v1",
        "horizon_seconds": 5,
        "created_at": T0.isoformat(),
        "feature_names": ["x1", "x2"],
        "coefficients": [1.0, 2.0],
        "intercept": 0.5,
    }
    artifact = write_artifact(payload, path=tmp_path / "demo.json")
    runner = InProcessModelRunner()
    runner.load(artifact)

    ctx = InMemoryContext(
        strategy_id_value=StrategyId("s-1"),
        sleeve_id_value=SleeveId("sl-1"),
        clock_now=T0,
        model_runner=runner,
    )
    features = FeatureVector(
        schema_id=SCHEMA,
        schema_version="v1",
        instrument_id=INSTRUMENT,
        timestamp=T0,
        values=(("x1", 1.0), ("x2", -1.0)),
    )
    prediction = ctx.predict("ctx_demo", features)
    assert prediction.value == pytest.approx(0.5 + 1.0 - 2.0)
    assert str(prediction.model_name) == "ctx_demo"


def test_runner_missing_model_raises() -> None:
    runner = InProcessModelRunner()
    features = FeatureVector(
        schema_id=SCHEMA,
        schema_version="v1",
        instrument_id=INSTRUMENT,
        timestamp=T0,
        values=(("x1", 1.0),),
    )
    with pytest.raises(KeyError):
        runner.predict(ModelName("missing"), features)
