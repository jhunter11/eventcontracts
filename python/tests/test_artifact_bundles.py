"""Artifact bundle writer/loader/validator tests."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from eventcontracts.artifacts import (
    ArtifactBundleLoader,
    ArtifactBundleValidator,
    ArtifactBundleWriter,
    PromotionRegistry,
)
from tests.conftest import REPO_ROOT


def test_local_artifact_bundle_roundtrips_validates_and_promotes(tmp_path: Path) -> None:
    feature_schema_path = tmp_path / "weather_features.json"
    feature_schema_path.write_text(
        json.dumps(
            {
                "schema_id": "weather_temperature_arbitrage_features",
                "schema_version": "1",
                "features": [
                    {"name": "forecast_prob", "dtype": "float64", "nullable": False},
                    {"name": "market_mid", "dtype": "float64", "nullable": False},
                ],
            }
        ),
        encoding="utf-8",
    )
    writer = ArtifactBundleWriter(tmp_path / "bundles")

    bundle = writer.write_from_files(
        strategy_spec_path=REPO_ROOT / "configs/strategies/weather-temperature-arbitrage.toml",
        sleeve_spec_path=REPO_ROOT / "configs/sleeves/weather-kalshi-paper-a.toml",
        feature_schema_path=feature_schema_path,
        bundle_id="weather_temperature_arbitrage/test-v1",
        created_by="pytest",
    )

    loaded = ArtifactBundleLoader().load(bundle.root_path)
    ArtifactBundleValidator().validate(loaded)
    assert loaded.strategy.name == "weather_temperature_arbitrage"
    assert loaded.sleeve is not None
    assert loaded.feature_schema.schema_id == "weather_temperature_arbitrage_features"

    registry = PromotionRegistry(tmp_path / "promotions")
    registry.promote(loaded, "paper")
    current = registry.current("weather_temperature_arbitrage", "paper")
    assert current is not None
    assert current.bundle_id == "weather_temperature_arbitrage/test-v1"


def test_artifact_bundle_validator_rejects_tampered_file(tmp_path: Path) -> None:
    feature_schema_path = tmp_path / "weather_features.json"
    feature_schema_path.write_text(
        json.dumps(
            {
                "schema_id": "weather_temperature_arbitrage_features",
                "schema_version": "1",
                "features": [{"name": "forecast_prob", "dtype": "float64"}],
            }
        ),
        encoding="utf-8",
    )
    bundle = ArtifactBundleWriter(tmp_path / "bundles").write_from_files(
        strategy_spec_path=REPO_ROOT / "configs/strategies/weather-temperature-arbitrage.toml",
        feature_schema_path=feature_schema_path,
        bundle_id="weather_temperature_arbitrage/tamper-test",
    )
    loaded = ArtifactBundleLoader().load(bundle.root_path)
    (Path(loaded.root_path) / "feature_schema.json").write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="checksum mismatch"):
        ArtifactBundleValidator().validate_checksums(loaded)


def test_live_bundle_requires_nonempty_parity(tmp_path: Path) -> None:
    feature_schema_path = tmp_path / "weather_features.json"
    feature_schema_path.write_text(
        json.dumps(
            {
                "schema_id": "weather_temperature_arbitrage_features",
                "schema_version": "1",
                "features": [{"name": "forecast_prob", "dtype": "float64"}],
            }
        ),
        encoding="utf-8",
    )
    bundle = ArtifactBundleWriter(tmp_path / "bundles").write_from_files(
        strategy_spec_path=REPO_ROOT / "configs/strategies/weather-temperature-arbitrage.toml",
        sleeve_spec_path=REPO_ROOT / "configs/sleeves/weather-kalshi-paper-a.toml",
        feature_schema_path=feature_schema_path,
        bundle_id="weather_temperature_arbitrage/live-gate",
    )
    loaded = ArtifactBundleLoader().load(bundle.root_path)
    assert loaded.sleeve is not None
    live_bundle = replace(
        loaded,
        sleeve=replace(loaded.sleeve, tags={"mode": "live"}),
        parity=None,
    )

    with pytest.raises(ValueError, match="requires non-empty parity"):
        ArtifactBundleValidator().validate_live_sleeve_has_parity(live_bundle)


def test_artifact_bundle_loader_rejects_unpromoted_bundle(tmp_path: Path) -> None:
    feature_schema_path = tmp_path / "weather_features.json"
    feature_schema_path.write_text(
        json.dumps(
            {
                "schema_id": "weather_temperature_arbitrage_features",
                "schema_version": "1",
                "features": [{"name": "forecast_prob", "dtype": "float64"}],
            }
        ),
        encoding="utf-8",
    )
    bundle = ArtifactBundleWriter(tmp_path / "bundles").write_from_files(
        strategy_spec_path=REPO_ROOT / "configs/strategies/weather-temperature-arbitrage.toml",
        feature_schema_path=feature_schema_path,
        bundle_id="weather_temperature_arbitrage/unpromoted",
        promoted=False,
    )

    with pytest.raises(ValueError, match="not promoted"):
        ArtifactBundleLoader().load(bundle.root_path)


def test_artifact_bundle_loader_rejects_model_checksum_drift(tmp_path: Path) -> None:
    feature_schema_path = tmp_path / "weather_features.json"
    feature_schema_path.write_text(
        json.dumps(
            {
                "schema_id": "weather_temperature_arbitrage_features",
                "schema_version": "1",
                "features": [{"name": "forecast_prob", "dtype": "float64"}],
            }
        ),
        encoding="utf-8",
    )
    model_path = tmp_path / "model.onnx"
    model_path.write_bytes(b"original model bytes")
    bundle = ArtifactBundleWriter(tmp_path / "bundles").write_from_files(
        strategy_spec_path=REPO_ROOT / "configs/strategies/weather-temperature-arbitrage.toml",
        feature_schema_path=feature_schema_path,
        model_path=model_path,
        bundle_id="weather_temperature_arbitrage/model-drift",
    )

    (Path(bundle.root_path) / "model" / "model.onnx").write_bytes(b"drifted model bytes")

    with pytest.raises(ValueError, match="model checksum mismatch"):
        ArtifactBundleLoader().load(bundle.root_path)


def test_bundle_records_parity_case_row_count(tmp_path: Path) -> None:
    feature_schema_path = tmp_path / "features.json"
    feature_schema_path.write_text(
        json.dumps(
            {
                "schema_id": "weather_temperature_arbitrage_features",
                "schema_version": "1",
                "features": [{"name": "forecast_prob", "dtype": "float64"}],
            }
        ),
        encoding="utf-8",
    )
    parity_path = tmp_path / "parity.jsonl"
    parity_path.write_text('{"case_id":"a"}\n\n{"case_id":"b"}\n', encoding="utf-8")
    bundle = ArtifactBundleWriter(tmp_path / "bundles").write_from_files(
        strategy_spec_path=REPO_ROOT / "configs/strategies/weather-temperature-arbitrage.toml",
        feature_schema_path=feature_schema_path,
        parity_cases_path=parity_path,
        bundle_id="weather_temperature_arbitrage/parity-count",
    )

    assert bundle.parity is not None
    assert bundle.parity.expected_rows == 2
