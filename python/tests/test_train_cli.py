"""End-to-end tests for `eventcontracts train`.

Builds a small synthetic Kalshi-shaped normalized partition, writes a
TOML training config that targets it, runs `train`, and verifies the
resulting artifact loads cleanly through the runner.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from eventcontracts.cli import main as _main_fn
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
from eventcontracts.models import InProcessModelRunner, load_artifact
from eventcontracts.storage import ParquetEventStore


def cli(argv: list[str]) -> int:
    return _main_fn(argv)


INSTRUMENT = InstrumentId(venue=Venue.KALSHI, market_id="M-1")
T0 = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)


def _seed_partition(root: Path) -> None:
    store = ParquetEventStore(root)
    for i in range(80):
        ts = T0 + timedelta(seconds=i)
        bid = Decimal("0.45") + Decimal("0.001") * i
        ask = bid + Decimal("0.01")
        store.append_normalized(
            QuoteEvent(
                event_id=EventId(f"q-{i:03d}"),
                quote=Quote(
                    instrument_id=INSTRUMENT,
                    side=OutcomeSide.YES,
                    bid=OrderBookLevel(price=bid, quantity=Decimal("80")),
                    ask=OrderBookLevel(price=ask, quantity=Decimal("60")),
                    exchange_ts=ts,
                    received_at=ts,
                ),
                provenance=EventProvenance(source="fixture", channel="quote"),
            )
        )
        store.append_normalized(
            OrderBookEvent(
                event_id=EventId(f"b-{i:03d}"),
                book=OrderBook(
                    instrument_id=INSTRUMENT,
                    yes_bids=(OrderBookLevel(price=bid, quantity=Decimal("80")),),
                    yes_asks=(OrderBookLevel(price=ask, quantity=Decimal("60")),),
                    no_bids=(),
                    no_asks=(),
                    exchange_ts=ts,
                    received_at=ts,
                ),
                provenance=EventProvenance(source="fixture", channel="book"),
            )
        )
        store.append_normalized(
            TradeEvent(
                event_id=EventId(f"t-{i:03d}"),
                trade=Trade(
                    instrument_id=INSTRUMENT,
                    side=OutcomeSide.YES,
                    price=(bid + ask) / Decimal("2"),
                    quantity=Decimal("1"),
                    trade_id=f"tv-{i}",
                    exchange_ts=ts,
                    received_at=ts,
                ),
                provenance=EventProvenance(source="fixture", channel="trade"),
            )
        )
    store.flush()


def test_train_cli_produces_loadable_artifact(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    _seed_partition(data_root)
    model_path = tmp_path / "models" / "obi_v1.json"
    config_path = tmp_path / "train.toml"
    config_path.write_text(
        f"""
model_name = "obi_v1"
model_version = "v1"
kind = "linear_regression"
output = "{model_path.as_posix()}"

[data]
root = "{data_root.as_posix()}"

[feature_builder]
kind = "rolling_mid_vwap_imbalance"
schema_id = "obi_features"
window_seconds = 5
ewma_half_life_seconds = 2

[labeler]
kind = "next_mid_change_bps"
horizon_seconds = 5

[training]
learning_rate = 0.1
iterations = 200
l2 = 0.001
seed = 0
validate_fraction = 0.2
""".strip()
    )

    rc = cli(["train", "--config", str(config_path)])
    assert rc == 0
    assert model_path.exists()
    payload = json.loads(model_path.read_text())
    assert payload["model_name"] == "obi_v1"
    assert payload["kind"] == "linear_regression"
    assert payload["feature_names"], "trained model should record its feature names"
    assert payload["train_samples"] > 0


def test_train_cli_binary_classifier_round_trip(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    _seed_partition(data_root)
    model_path = tmp_path / "models" / "obi_clf.json"
    config_path = tmp_path / "train.toml"
    config_path.write_text(
        f"""
model_name = "obi_clf"
model_version = "v1"
kind = "logistic_regression"
output = "{model_path.as_posix()}"

[data]
root = "{data_root.as_posix()}"

[feature_builder]
kind = "rolling_mid_vwap_imbalance"
schema_id = "obi_features"
window_seconds = 5
ewma_half_life_seconds = 2

[labeler]
kind = "binary_profitable_after_fees"
horizon_seconds = 5
fee_model = "kalshi"

[training]
learning_rate = 0.5
iterations = 200
seed = 0
""".strip()
    )

    rc = cli(["train", "--config", str(config_path)])
    assert rc == 0
    assert model_path.exists()

    # Reload artifact and verify the runner returns probabilities in [0,1].
    raw = model_path.read_text(encoding="utf-8").strip()
    from hashlib import sha256

    digest = sha256(raw.encode("utf-8")).hexdigest()
    payload = json.loads(raw)
    created_at = datetime.fromisoformat(str(payload["created_at"]).replace("Z", "+00:00"))
    from eventcontracts.audit import audit_stamp_for
    from eventcontracts.domain.ids import ModelName, ModelVersion
    from eventcontracts.models import ModelArtifact

    artifact = ModelArtifact(
        name=ModelName("obi_clf"),
        version=ModelVersion("v1"),
        uri=str(model_path.resolve()),
        sha256=digest,
        format=payload["kind"],
        created_at=created_at,
        audit=audit_stamp_for(
            payload,
            object_id="model-artifact:obi_clf:v1",
            object_kind="model_artifact",
            schema_version="model-artifact-v1",
            produced_at=created_at,
            producer="test",
        ),
    )
    runner = InProcessModelRunner()
    runner.load(artifact)
    from eventcontracts.domain.features import FeatureVector
    from eventcontracts.domain.ids import FeatureSchemaId

    vector = FeatureVector(
        schema_id=FeatureSchemaId("obi_features"),
        schema_version="v1",
        instrument_id=INSTRUMENT,
        timestamp=T0,
        values=tuple((name, 0.5) for name in payload["feature_names"]),
    )
    prediction = runner.predict(ModelName("obi_clf"), vector)
    assert 0.0 <= prediction.value <= 1.0
    # Also confirm load_artifact round-trips identically.
    model, _payload = load_artifact(artifact)
    assert model.predict({"mid_ewma": 0.5}) is not None
