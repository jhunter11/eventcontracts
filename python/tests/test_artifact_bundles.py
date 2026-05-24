"""Artifact bundle writer/loader/validator tests."""

from __future__ import annotations

import json
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
