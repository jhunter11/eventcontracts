"""End-to-end tests for the generic ``model-train`` CLI."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("polars")
pytest.importorskip("onnxruntime")


def _write_dataset(path: Path, *, seed: int = 7, n: int = 400) -> None:
    import polars as pl

    rng = np.random.default_rng(seed)
    x = rng.standard_normal((n, 4))
    y = (x[:, 0] - x[:, 1] + 0.5 * x[:, 2] > 0).astype(int)
    pl.DataFrame(
        {
            "as_of_date": [20260101 + i for i in range(n)],
            "f0": x[:, 0],
            "f1": x[:, 1],
            "f2": x[:, 2],
            "f3": x[:, 3],
            "label": y,
        }
    ).write_parquet(path)


def _run(config_path: Path) -> int:
    from eventcontracts.cli.main import main

    return main(["model-train", "--config", str(config_path)])


def _config(*, family: str, data: Path, out: Path, model_block: str) -> str:
    return f"""
model_name = "demo_{family}"
model_version = "v1"
model_family = "{family}"
task = "binary_classification"
out_root = "{out.as_posix()}"

[data]
path = "{data.as_posix()}"
label_column = "label"
date_column = "as_of_date"

[split]
train_fraction = 0.7
validation_fraction = 0.15

[model]
{model_block}
"""


def test_model_train_sklearn_end_to_end(tmp_path: Path) -> None:
    pytest.importorskip("skl2onnx")
    data = tmp_path / "data.parquet"
    _write_dataset(data)
    out = tmp_path / "out_sklearn"
    config = tmp_path / "sklearn.toml"
    config.write_text(_config(family="sklearn", data=data, out=out, model_block='estimator = "logistic_regression"'))

    assert _run(config) == 0
    reports = list((out / "reports").glob("*.json"))
    assert len(reports) == 1
    report = json.loads(reports[0].read_text())
    assert report["onnx"]["max_abs_export_delta"] < 1e-4
    assert report["metrics"]["accuracy"] >= 0.8
    assert report["rows"]["test"] > 0
    assert Path(report["onnx"]["model_path"]).exists()


def test_model_train_xgboost_end_to_end(tmp_path: Path) -> None:
    pytest.importorskip("xgboost")
    pytest.importorskip("onnxmltools")
    data = tmp_path / "data.parquet"
    _write_dataset(data, seed=11)
    out = tmp_path / "out_xgb"
    config = tmp_path / "xgb.toml"
    config.write_text(
        _config(
            family="xgboost",
            data=data,
            out=out,
            model_block="num_boost_round = 30\nearly_stopping_rounds = 5",
        )
    )

    assert _run(config) == 0
    report = json.loads(next((out / "reports").glob("*.json")).read_text())
    assert report["onnx"]["output_select"] == "scalar:1"
    assert report["onnx"]["max_abs_export_delta"] < 1e-5
    assert report["metrics"]["roc_auc"] >= 0.8
