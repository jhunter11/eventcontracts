"""Generic model → ONNX export, parity, and metadata tests."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from eventcontracts.models.onnx_export import (
    ModelFamily,
    ModelTask,
    OnnxExportParityError,
    export_model_onnx,
    predict_onnx,
    read_metadata,
    verify_export_parity,
)

pytest.importorskip("onnxruntime")


def _xor_ish_dataset(seed: int = 0) -> tuple[np.ndarray, np.ndarray, list[str]]:
    rng = np.random.default_rng(seed)
    x = rng.standard_normal((300, 5)).astype(np.float32)
    y = (x[:, 0] + 0.5 * x[:, 1] - x[:, 2] > 0).astype(int)
    return x, y, [f"f{i}" for i in range(5)]


def test_sklearn_binary_classifier_exports_with_parity_and_metadata(tmp_path: Path) -> None:
    pytest.importorskip("skl2onnx")
    from sklearn.linear_model import LogisticRegression

    x, y, names = _xor_ish_dataset()
    model = LogisticRegression(max_iter=500).fit(x, y)
    export = export_model_onnx(
        model,
        names,
        tmp_path / "model.onnx",
        model_family=ModelFamily.SKLEARN,
        task=ModelTask.BINARY_CLASSIFICATION,
        feature_schema_id="demo_features",
        feature_schema_version="1",
    )

    assert export.output_select == "scalar:1"
    assert export.input_width == 5
    native = model.predict_proba(x)[:, 1]
    exported = predict_onnx(export.path, x, output_name=export.output_name, output_select=export.output_select)
    assert verify_export_parity(native, exported, tolerance=1e-4) < 1e-4

    meta = read_metadata(export.path)
    assert meta["feature_schema_id"] == "demo_features"
    assert meta["model_family"] == "sklearn"
    assert meta["input_width"] == "5"
    assert meta["output_select"] == "scalar:1"


def test_xgboost_booster_exports_with_parity(tmp_path: Path) -> None:
    xgb = pytest.importorskip("xgboost")
    pytest.importorskip("onnxmltools")

    x, y, names = _xor_ish_dataset(1)
    booster = xgb.train(
        {"objective": "binary:logistic", "max_depth": 3, "eta": 0.3, "seed": 42},
        xgb.DMatrix(x, label=y),
        num_boost_round=20,
    )
    export = export_model_onnx(
        booster,
        names,
        tmp_path / "model.onnx",
        model_family=ModelFamily.XGBOOST,
        task=ModelTask.BINARY_CLASSIFICATION,
        feature_schema_id="demo_features",
        feature_schema_version="1",
    )
    native = booster.predict(xgb.DMatrix(x))
    exported = predict_onnx(export.path, x, output_name=export.output_name, output_select=export.output_select)
    assert verify_export_parity(native, exported, tolerance=1e-5) < 1e-5


def test_sklearn_regressor_exports_all_outputs(tmp_path: Path) -> None:
    pytest.importorskip("skl2onnx")
    from sklearn.linear_model import LinearRegression

    x, _y, names = _xor_ish_dataset(2)
    target = x @ np.array([1.0, -2.0, 0.5, 0.0, 3.0])
    model = LinearRegression().fit(x, target)
    export = export_model_onnx(
        model,
        names,
        tmp_path / "model.onnx",
        model_family=ModelFamily.SKLEARN,
        task=ModelTask.REGRESSION,
        feature_schema_id="demo_features",
        feature_schema_version="1",
    )
    assert export.output_select == "all"
    exported = predict_onnx(export.path, x, output_name=export.output_name, output_select=export.output_select).ravel()
    assert verify_export_parity(model.predict(x), exported, tolerance=1e-3) < 1e-3


def test_verify_export_parity_raises_over_tolerance() -> None:
    with pytest.raises(OnnxExportParityError):
        verify_export_parity([0.1, 0.2, 0.3], [0.1, 0.9, 0.3], tolerance=1e-6)


def test_export_rejects_empty_feature_names(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        export_model_onnx(
            object(),
            [],
            tmp_path / "model.onnx",
            model_family=ModelFamily.SKLEARN,
            task=ModelTask.BINARY_CLASSIFICATION,
            feature_schema_id="x",
            feature_schema_version="1",
        )
