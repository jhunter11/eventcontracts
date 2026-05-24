"""`eventcontracts train` — fit a model from captured data and a labeler.

Reads a single TOML training config, walks a normalized Parquet
partition through the configured feature builder + labeler, fits the
requested model on the resulting examples, writes the artifact as JSON,
and prints the resulting metrics and artifact path.

The config keeps everything declarative so a researcher can version it
alongside their strategy:

```toml
model_name = "obi_classifier"
model_version = "v1"
kind = "logistic_regression"           # or "linear_regression"
output = "models/obi_classifier_v1.json"

[data]
root = "data"                           # ParquetEventStore root
start = "2026-05-01T00:00:00Z"          # optional
end   = "2026-05-15T00:00:00Z"          # optional

[feature_builder]
kind = "rolling_mid_vwap_imbalance"
schema_id = "obi_features"
window_seconds = 30
ewma_half_life_seconds = 5

[labeler]
kind = "next_mid_change_bps"            # next_mid_change_bps |
                                        # binary_profitable_after_fees |
                                        # settlement_probability
horizon_seconds = 30
# Binary labeler only:
# fee_model = "kalshi"  # or "polymarket"

[training]
learning_rate = 0.1
iterations = 500
l2 = 0.001
seed = 0
validate_fraction = 0.2
```
"""

from __future__ import annotations

import argparse
import json
import tomllib
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from eventcontracts.adapters.venues.kalshi import KalshiFeeModel
from eventcontracts.adapters.venues.polymarket import PolymarketFeeModel
from eventcontracts.domain.fees import FeeModel
from eventcontracts.domain.ids import FeatureSchemaId, ModelName
from eventcontracts.domain.models import OutcomeSide
from eventcontracts.features.builders import (
    DeterministicFeatureBuilder,
    RollingMidVwapImbalanceBuilder,
)
from eventcontracts.models import (
    BinaryProfitableAfterFeesLabeler,
    Labeler,
    ModelKind,
    ModelTrainer,
    NextMidChangeBpsLabeler,
    SettlementProbabilityLabeler,
    TrainerConfig,
    TrainingExampleBuilder,
    write_artifact,
)
from eventcontracts.replay import NormalizedReplaySource
from eventcontracts.storage import ParquetEventStore


def register(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "train",
        help="Train a model from a captured Parquet partition.",
    )
    parser.add_argument("--config", type=Path, required=True, help="training config TOML")
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Override the config's output path.",
    )
    parser.set_defaults(handler=_handle)


def _parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _feature_builder_from(config: Mapping[str, Any]) -> DeterministicFeatureBuilder:
    kind = str(config["kind"])
    if kind == "rolling_mid_vwap_imbalance":
        return RollingMidVwapImbalanceBuilder(
            schema_id=FeatureSchemaId(str(config["schema_id"])),
            schema_version=str(config.get("schema_version", "v1")),
            window_seconds=int(config.get("window_seconds", 30)),
            ewma_half_life_seconds=int(config.get("ewma_half_life_seconds", 5)),
        )
    raise ValueError(f"unknown feature_builder.kind: {kind}")


def _fee_model_from(name: str) -> FeeModel:
    if name == "kalshi":
        return KalshiFeeModel()
    if name == "polymarket":
        return PolymarketFeeModel()
    raise ValueError(f"unknown fee_model: {name}")


def _labeler_from(config: Mapping[str, Any]) -> Labeler:
    kind = str(config["kind"])
    horizon = int(config["horizon_seconds"])
    if kind == "next_mid_change_bps":
        return NextMidChangeBpsLabeler(horizon_seconds_value=horizon)
    if kind == "settlement_probability":
        return SettlementProbabilityLabeler(horizon_seconds_value=horizon)
    if kind == "binary_profitable_after_fees":
        fee = _fee_model_from(str(config.get("fee_model", "kalshi")))
        side = OutcomeSide(str(config.get("side", "yes")))
        return BinaryProfitableAfterFeesLabeler(
            horizon_seconds_value=horizon,
            fee_model=fee,
            side=side,
        )
    raise ValueError(f"unknown labeler.kind: {kind}")


def _handle(args: argparse.Namespace) -> int:
    with args.config.open("rb") as file:
        config = tomllib.load(file)

    output_path = args.out or Path(str(config["output"]))
    data_config = config.get("data") or {}
    feature_config = config["feature_builder"]
    labeler_config = config["labeler"]
    training_config = config.get("training") or {}

    data_root = Path(str(data_config["root"]))
    start = _parse_iso(str(data_config["start"])) if "start" in data_config else None
    end = _parse_iso(str(data_config["end"])) if "end" in data_config else None

    feature_builder = _feature_builder_from(feature_config)
    labeler = _labeler_from(labeler_config)

    store = ParquetEventStore(data_root)
    source = NormalizedReplaySource(store)

    def _iter_events() -> Any:
        for event in source.stream():
            ts = _event_ts(event)
            if ts is None:
                yield event
                continue
            if start is not None and ts < start:
                continue
            if end is not None and ts >= end:
                continue
            yield event

    example_builder = TrainingExampleBuilder(feature_builder, labeler)
    examples = example_builder.build(_iter_events())
    if not examples:
        print(
            "train: no labeled examples produced. Check data root, time window, "
            "and labeler horizon."
        )
        return 2

    schema = feature_builder.schema()
    trainer_config = TrainerConfig(
        model_name=ModelName(str(config["model_name"])),
        model_version=str(config["model_version"]),
        kind=ModelKind(str(config["kind"])),
        feature_schema_id=schema.schema_id,
        feature_schema_version=schema.schema_version,
        horizon_seconds=int(labeler_config["horizon_seconds"]),
        learning_rate=float(training_config.get("learning_rate", 0.1)),
        iterations=int(training_config.get("iterations", 500)),
        l2=float(training_config.get("l2", 0.001)),
        seed=int(training_config.get("seed", 0)),
        validate_fraction=float(training_config.get("validate_fraction", 0.2)),
    )
    trainer = ModelTrainer(trainer_config)
    now = datetime.now(UTC)
    result = trainer.train(examples, now=now, producer="eventcontracts.cli.train")

    artifact = write_artifact(
        result.artifact_payload,
        path=output_path,
        producer="eventcontracts.cli.train",
    )

    print(
        json.dumps(
            {
                "model_name": str(artifact.name),
                "model_version": str(artifact.version),
                "uri": artifact.uri,
                "sha256": artifact.sha256,
                "examples": len(examples),
                "metrics": result.metrics.to_dict(),
            },
            indent=2,
            default=str,
        )
    )
    return 0


def _event_ts(event: Any) -> datetime | None:
    """Reuse the wide accessor from the backtest CLI."""

    from eventcontracts.cli.backtest import _event_time

    return _event_time(event)
