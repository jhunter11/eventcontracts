"""Tennis XGBoost research feature contract tests."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from eventcontracts.contracts import load_json_contract, validate_contract, validate_json_contract_file
from eventcontracts.research import (
    TENNIS_XGBOOST_FEATURE_NAMES,
    TennisMatchSnapshot,
    build_sackmann_training_frame,
    evaluate_probabilities,
    export_xgboost_onnx,
    feature_row,
    feature_schema_document,
    feature_vector,
    onnx_deployment_metadata,
    predict_onnx_probabilities,
    predict_xgboost_probabilities,
    snapshot_from_mapping,
    temporal_train_validation_test_split,
    train_xgboost_binary,
    write_parity_cases,
)
from tests.conftest import REPO_ROOT


def test_tennis_feature_schema_matches_contract_and_example_file() -> None:
    schema = feature_schema_document()
    contract = load_json_contract(REPO_ROOT / "contracts/schemas/feature_schema.schema.json")

    validate_contract(schema, contract)
    validate_json_contract_file(
        REPO_ROOT / "contracts/examples/tennis_xgboost/feature_schema.json",
        "feature_schema.schema.json",
    )

    example = json.loads((REPO_ROOT / "contracts/examples/tennis_xgboost/feature_schema.json").read_text())
    assert [item["name"] for item in schema["features"]] == list(TENNIS_XGBOOST_FEATURE_NAMES)
    assert [item["name"] for item in example["features"]] == list(TENNIS_XGBOOST_FEATURE_NAMES)


def test_tennis_feature_vector_has_stable_order_and_odds_normalization() -> None:
    snapshot = TennisMatchSnapshot(
        match_id="m-1",
        match_date=date(2026, 1, 1),
        p1_id="p1",
        p2_id="p2",
        surface="Grass",
        tourney_level="G",
        best_of=5,
        p1_elo=1600,
        p2_elo=1500,
        p1_surface_elo=1580,
        p2_surface_elo=1510,
        p1_rank=2,
        p2_rank=20,
        p1_seed=1,
        p2_seed=8,
        p1_age=26.5,
        p2_age=30.0,
        p1_height_cm=188,
        p2_height_cm=180,
        p1_h2h_wins=3,
        p2_h2h_wins=1,
        p1_recent_wins=8,
        p2_recent_wins=5,
        p1_recent_matches=10,
        p2_recent_matches=10,
        p1_days_since_match=3,
        p2_days_since_match=5,
        p1_decimal_odds=1.50,
        p2_decimal_odds=2.75,
        label=1,
    )

    row = feature_row(snapshot)
    vector = feature_vector(snapshot)

    assert tuple(row) == TENNIS_XGBOOST_FEATURE_NAMES
    assert len(vector) == len(TENNIS_XGBOOST_FEATURE_NAMES)
    assert row["elo_diff"] == 100.0
    assert row["surface_grass"] == 1.0
    assert row["best_of_5"] == 1.0
    assert row["p1_implied_prob"] > 0.60
    assert row["implied_prob_diff"] > 0


def test_snapshot_from_mapping_builds_live_snapshot_defaults() -> None:
    snapshot = snapshot_from_mapping(
        {
            "market_id": "KX-ATP-DEMO",
            "match_date": "2026-06-01",
            "p1_id": "player-a",
            "p2_id": "player-b",
            "surface": "Hard",
            "p1_rank": "5",
            "p2_rank": "12",
        }
    )

    assert snapshot.match_id == "KX-ATP-DEMO"
    assert snapshot.match_date == date(2026, 6, 1)
    assert snapshot.p1_elo == 1500.0
    assert snapshot.p2_elo == 1500.0
    assert snapshot.p1_rank == 5


def test_sackmann_training_frame_uses_only_prior_match_state() -> None:
    pl = pytest.importorskip("polars")
    matches = pl.DataFrame(
        {
            "tourney_id": ["2026-A", "2026-B"],
            "tourney_date": [20260101, 20260110],
            "match_num": [1, 1],
            "surface": ["Hard", "Hard"],
            "tourney_level": ["A", "A"],
            "best_of": [3, 3],
            "winner_id": [1, 2],
            "loser_id": [2, 1],
            "winner_rank": [10, 35],
            "loser_rank": [35, 10],
            "winner_age": [25.0, 24.0],
            "loser_age": [24.0, 25.0],
            "winner_ht": [185, 190],
            "loser_ht": [190, 185],
            "AvgW": [1.45, 2.20],
            "AvgL": [2.90, 1.70],
        }
    )

    frame = build_sackmann_training_frame(matches, include_mirrored=False)

    assert frame.height == 2
    assert frame["label"].to_list() == [1, 1]
    assert frame["elo_diff"].to_list()[0] == 0.0
    assert frame["h2h_win_pct_diff"].to_list()[0] == 0.0
    assert frame["elo_diff"].to_list()[1] < 0.0
    assert frame["h2h_win_pct_diff"].to_list()[1] < 0.0


def test_sackmann_training_frame_mirrors_rows_for_binary_training() -> None:
    pl = pytest.importorskip("polars")
    matches = pl.DataFrame(
        {
            "tourney_date": [20260101],
            "surface": ["Clay"],
            "winner_id": [10],
            "loser_id": [20],
            "winner_rank": [5],
            "loser_rank": [50],
        }
    )

    frame = build_sackmann_training_frame(matches, include_mirrored=True)

    assert frame.height == 2
    assert frame["label"].to_list() == [1, 0]
    assert frame["elo_diff"].to_list() == [0.0, 0.0]
    assert frame["rank_log_advantage"].to_list()[0] == -frame["rank_log_advantage"].to_list()[1]


def test_temporal_split_and_probability_metrics() -> None:
    pl = pytest.importorskip("polars")
    frame = pl.DataFrame({"x": list(range(10)), "label": [0, 1] * 5})

    train, validation, test = temporal_train_validation_test_split(
        frame,
        train_fraction=0.6,
        validation_fraction=0.2,
    )
    metrics = evaluate_probabilities([0, 1, 1, 0], [0.1, 0.8, 0.6, 0.4])

    assert train.height == 6
    assert validation.height == 2
    assert test.height == 2
    assert metrics.accuracy == 1.0
    assert metrics.roc_auc == 1.0
    assert metrics.log_loss < 0.4
    assert metrics.brier_score < 0.1


def test_temporal_split_keeps_mirrored_match_dates_together() -> None:
    pl = pytest.importorskip("polars")
    matches = pl.DataFrame(
        {
            "tourney_date": [20260101, 20260102, 20260103, 20260104, 20260105],
            "surface": ["Hard"] * 5,
            "winner_id": [1, 1, 2, 1, 2],
            "loser_id": [2, 2, 1, 2, 1],
        }
    )
    frame = build_sackmann_training_frame(matches, include_mirrored=True)

    train, validation, test = temporal_train_validation_test_split(
        frame,
        train_fraction=0.6,
        validation_fraction=0.2,
    )

    assert set(train["match_date"].to_list()).isdisjoint(validation["match_date"].to_list())
    assert set(validation["match_date"].to_list()).isdisjoint(test["match_date"].to_list())
    assert all(partition.height % 2 == 0 for partition in (train, validation, test))


def test_onnx_deployment_metadata_pins_rust_runner_contract() -> None:
    metadata = onnx_deployment_metadata(model_path="model.onnx")

    assert metadata["model"]["format"] == "onnx"
    assert metadata["model"]["input_name"] == "features"
    assert metadata["model"]["feature_names"] == list(TENNIS_XGBOOST_FEATURE_NAMES)
    assert metadata["rust_runner_contract"]["input_tensor"] == [
        None,
        len(TENNIS_XGBOOST_FEATURE_NAMES),
    ]


def test_xgboost_onnx_export_matches_booster_and_writes_parity_rows(tmp_path: Path) -> None:
    pl = pytest.importorskip("polars")
    pytest.importorskip("xgboost")
    pytest.importorskip("onnxmltools")
    pytest.importorskip("onnxruntime")
    matches = pl.DataFrame(
        {
            "tourney_date": [20260101 + index for index in range(12)],
            "surface": ["Hard", "Clay", "Grass"] * 4,
            "winner_id": [1, 2, 1, 3, 2, 3, 1, 2, 3, 1, 2, 3],
            "loser_id": [2, 1, 3, 2, 3, 1, 2, 3, 1, 3, 1, 2],
            "winner_rank": [5, 20, 5, 30, 20, 30, 5, 20, 30, 5, 20, 30],
            "loser_rank": [20, 5, 30, 20, 30, 5, 20, 30, 5, 30, 5, 20],
        }
    )
    frame = build_sackmann_training_frame(matches)
    train, validation, test = temporal_train_validation_test_split(frame)
    model = train_xgboost_binary(train, validation, num_boost_round=8, early_stopping_rounds=2)
    model_path = export_xgboost_onnx(model, tmp_path / "model.onnx")

    booster_probs = predict_xgboost_probabilities(model, test)
    onnx_probs = predict_onnx_probabilities(model_path, test)
    parity_path = write_parity_cases(test, onnx_probs, tmp_path / "parity.jsonl", max_rows=3)

    assert len(booster_probs) == len(onnx_probs) == test.height
    assert max(abs(left - right) for left, right in zip(booster_probs, onnx_probs, strict=True)) < 1e-6
    assert len(parity_path.read_text(encoding="utf-8").splitlines()) == 3
