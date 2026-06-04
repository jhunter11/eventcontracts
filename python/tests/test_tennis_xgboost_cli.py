"""Artifact-bundle coverage for the tennis XGBoost ONNX training command."""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from eventcontracts.cli import main


def test_tennis_train_cli_writes_valid_onnx_bundle(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    pl = pytest.importorskip("polars")
    pytest.importorskip("xgboost")
    pytest.importorskip("onnxmltools")
    pytest.importorskip("onnxruntime")
    data_dir = tmp_path / "atp"
    data_dir.mkdir()
    count = 30
    pl.DataFrame(
        {
            "tourney_id": [f"2026-{index:03d}" for index in range(count)],
            "tourney_date": [20260101 + index for index in range(count)],
            "match_num": [1] * count,
            "surface": ["Hard", "Clay", "Grass"] * 10,
            "tourney_level": ["A"] * count,
            "best_of": [3] * count,
            "winner_id": [1, 2, 3] * 10,
            "loser_id": [2, 3, 1] * 10,
            "winner_rank": [4, 15, 30] * 10,
            "loser_rank": [15, 30, 4] * 10,
        }
    ).write_csv(data_dir / "atp_matches_2026.csv")
    out_root = tmp_path / "artifacts"

    exit_code = main(
        [
            "tennis-xgboost-train",
            "--data-dir",
            str(data_dir),
            "--out-root",
            str(out_root),
            "--since-year",
            "2026",
            "--bundle-id",
            "sports_tennis_xgboost/test",
            "--num-boost-round",
            "8",
            "--early-stopping-rounds",
            "2",
            "--parity-rows",
            "3",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    bundle_root = Path(payload["bundle_root"])
    assert exit_code == 0
    assert (bundle_root / "manifest.toml").exists()
    assert (bundle_root / "model" / "model.onnx").exists()
    assert (bundle_root / "feature_schema.json").exists()
    assert payload["parity_rows"] == 3
    assert payload["onnx_max_abs_probability_delta"] < 1e-6


def test_tennis_score_cli_writes_external_signal_jsonl(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    pl = pytest.importorskip("polars")
    pytest.importorskip("xgboost")
    pytest.importorskip("onnxmltools")
    pytest.importorskip("onnxruntime")
    data_dir = tmp_path / "atp"
    data_dir.mkdir()
    pl.DataFrame(
        {
            "tourney_date": [20260101 + index for index in range(24)],
            "surface": ["Hard", "Clay", "Grass"] * 8,
            "winner_id": [1, 2, 3] * 8,
            "loser_id": [2, 3, 1] * 8,
        }
    ).write_csv(data_dir / "atp_matches_2026.csv")
    out_root = tmp_path / "artifacts"
    train_code = main(
        [
            "tennis-xgboost-train",
            "--data-dir",
            str(data_dir),
            "--out-root",
            str(out_root),
            "--since-year",
            "2026",
            "--bundle-id",
            "sports_tennis_xgboost/score-test",
            "--num-boost-round",
            "5",
            "--early-stopping-rounds",
            "2",
            "--confidence-cutoffs",
            "0.5",
            "--parity-rows",
            "2",
        ]
    )
    train_payload = json.loads(capsys.readouterr().out)
    upcoming = tmp_path / "upcoming.csv"
    pl.DataFrame(
        {
            "market_id": ["KX-ATP-DEMO"],
            "match_id": ["demo-match"],
            "match_date": ["2026-06-01"],
            "p1_id": ["1"],
            "p2_id": ["2"],
            "surface": ["Hard"],
            "p1_elo": [1510.0],
            "p2_elo": [1490.0],
            "p1_decimal_odds": [1.75],
            "p2_decimal_odds": [2.10],
        }
    ).write_csv(upcoming)
    signals = tmp_path / "signals.jsonl"

    score_code = main(
        [
            "tennis-xgboost-score",
            "--model",
            train_payload["model"],
            "--input",
            str(upcoming),
            "--out",
            str(signals),
        ]
    )
    score_payload = json.loads(capsys.readouterr().out)
    line = json.loads(signals.read_text(encoding="utf-8").strip())

    assert train_code == 0
    assert score_code == 0
    assert score_payload["rows"] == 1
    assert line["source"] == "tennis_xgboost_onnx"
    assert line["payload"]["market_id"] == "KX-ATP-DEMO"
    assert 0.0 <= line["payload"]["player_1_win_probability"] <= 1.0
    assert 0.5 <= line["payload"]["model_confidence"] <= 1.0
    assert line["payload"]["odds_present"] is True


def test_tennis_train_cli_can_write_v2_bundle(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    pl = pytest.importorskip("polars")
    pytest.importorskip("xgboost")
    pytest.importorskip("onnxmltools")
    pytest.importorskip("onnxruntime")
    data_dir = tmp_path / "atp"
    data_dir.mkdir()
    count = 36
    pl.DataFrame(
        {
            "tourney_id": [f"2026-V2-{index:03d}" for index in range(count)],
            "tourney_date": [
                int((date(2026, 1, 1) + timedelta(days=index)).strftime("%Y%m%d")) for index in range(count)
            ],
            "match_num": [1] * count,
            "surface": ["Hard", "Clay", "Grass"] * 12,
            "tourney_level": ["A"] * count,
            "best_of": [3] * count,
            "round": ["R32", "R16", "QF"] * 12,
            "winner_id": [1, 2, 3] * 12,
            "loser_id": [2, 3, 1] * 12,
            "winner_rank": [4, 15, 30] * 12,
            "loser_rank": [15, 30, 4] * 12,
            "winner_rank_points": [5000, 2200, 900] * 12,
            "loser_rank_points": [2200, 900, 5000] * 12,
            "winner_hand": ["R", "L", "R"] * 12,
            "loser_hand": ["L", "R", "R"] * 12,
            "score": ["6-4 6-4", "7-6 6-7 6-3", "6-3 6-2"] * 12,
            "w_ace": [5, 8, 4] * 12,
            "w_df": [2, 1, 3] * 12,
            "w_svpt": [70, 88, 64] * 12,
            "w_1stWon": [40, 55, 36] * 12,
            "w_2ndWon": [15, 18, 12] * 12,
            "w_bpSaved": [3, 5, 2] * 12,
            "w_bpFaced": [4, 6, 3] * 12,
            "l_ace": [4, 3, 2] * 12,
            "l_df": [3, 5, 4] * 12,
            "l_svpt": [68, 85, 58] * 12,
            "l_1stWon": [38, 48, 30] * 12,
            "l_2ndWon": [12, 14, 10] * 12,
            "l_bpSaved": [2, 3, 1] * 12,
            "l_bpFaced": [5, 7, 5] * 12,
        }
    ).write_csv(data_dir / "atp_matches_2026.csv")
    out_root = tmp_path / "artifacts"

    exit_code = main(
        [
            "tennis-xgboost-train",
            "--data-dir",
            str(data_dir),
            "--out-root",
            str(out_root),
            "--since-year",
            "2026",
            "--bundle-id",
            "sports_tennis_xgboost/v2-test",
            "--model-version",
            "v2",
            "--disable-monotone",
            "--recency-half-life-years",
            "0",
            "--num-boost-round",
            "6",
            "--early-stopping-rounds",
            "2",
            "--confidence-cutoffs",
            "0.5",
            "--parity-rows",
            "2",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    schema = json.loads((Path(payload["bundle_root"]) / "feature_schema.json").read_text(encoding="utf-8"))
    assert exit_code == 0
    assert payload["model_version"] == "v2"
    assert payload["parity_rows"] == 2
    assert payload["confidence_gates"]
    assert schema["schema_version"] == "2"
