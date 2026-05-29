"""``eventcontracts model-train`` — generic tabular → ONNX training.

The model-family-agnostic counterpart to ``tennis-xgboost-train``. It takes a
declarative TOML config pointing at a labeled feature table (Parquet/CSV) plus
a feature schema, trains the requested model family, exports a portable
``model.onnx``, verifies export parity (native estimator vs onnxruntime),
evaluates on a held-out temporal test split, and writes a reference report.
When a strategy spec is supplied it also assembles a promotable artifact
bundle.

```toml
model_name = "macro_cpi_predictor"
model_version = "v1"
model_family = "xgboost"            # sklearn | xgboost | lightgbm
task = "binary_classification"      # | multiclass_classification | regression
out_root = "artifacts/macro_cpi"

[data]
path = "data/macro/cpi_features.parquet"
label_column = "label"
date_column = "as_of_date"          # optional; enables whole-date temporal split

[feature_schema]
path = "contracts/examples/macro_cpi/feature_schema.json"   # source of truth for order

[split]
train_fraction = 0.70
validation_fraction = 0.15

[model]
estimator = "logistic_regression"   # sklearn family only
num_boost_round = 400               # xgboost / lightgbm only
early_stopping_rounds = 40
[model.params]
max_depth = 4
eta = 0.05

[bundle]                            # optional — assemble a promotable bundle
strategy_spec = "configs/strategies/macro-cpi-predictor.toml"
sleeve_spec = "configs/sleeves/macro-cpi-kalshi-paper-a.toml"
```
"""

from __future__ import annotations

import argparse
import json
import tomllib
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from eventcontracts.artifacts import ArtifactBundleValidator, ArtifactBundleWriter
from eventcontracts.artifacts.bundle import load_feature_schema
from eventcontracts.models.evaluation import evaluate_classification, evaluate_regression
from eventcontracts.models.onnx_export import (
    ModelFamily,
    ModelTask,
    export_model_onnx,
    predict_onnx,
    verify_export_parity,
)
from eventcontracts.models.parity import write_parity_cases

_SKLEARN_ESTIMATORS: dict[str, tuple[str, str, dict[str, Any]]] = {
    "logistic_regression": ("sklearn.linear_model", "LogisticRegression", {"max_iter": 1000}),
    "random_forest_classifier": (
        "sklearn.ensemble",
        "RandomForestClassifier",
        {"n_estimators": 200, "random_state": 42},
    ),
    "gradient_boosting_classifier": ("sklearn.ensemble", "GradientBoostingClassifier", {"random_state": 42}),
    "hist_gradient_boosting_classifier": ("sklearn.ensemble", "HistGradientBoostingClassifier", {"random_state": 42}),
    "linear_regression": ("sklearn.linear_model", "LinearRegression", {}),
    "ridge": ("sklearn.linear_model", "Ridge", {"random_state": 42}),
    "random_forest_regressor": ("sklearn.ensemble", "RandomForestRegressor", {"n_estimators": 200, "random_state": 42}),
    "gradient_boosting_regressor": ("sklearn.ensemble", "GradientBoostingRegressor", {"random_state": 42}),
}


def register(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "model-train",
        help="Train any tabular model family, export ONNX, verify parity, and report metrics.",
    )
    parser.add_argument("--config", type=Path, required=True, help="training config TOML")
    parser.add_argument("--out-root", type=Path, default=None, help="override config out_root")
    parser.add_argument("--bundle-id", default=None, help="override the generated bundle id")
    parser.add_argument("--parity-tolerance", type=float, default=1e-5)
    parser.add_argument("--parity-rows", type=int, default=100)
    parser.set_defaults(handler=_handle)


def _handle(args: argparse.Namespace) -> int:
    with args.config.open("rb") as file:
        config = tomllib.load(file)

    family = ModelFamily(str(config["model_family"]))
    task = ModelTask(str(config["task"]))
    model_name = str(config["model_name"])
    model_version = str(config["model_version"])
    out_root = Path(args.out_root or config["out_root"])

    pl = _polars()
    data_config = config["data"]
    frame = _load_frame(pl, Path(data_config["path"]))
    label_column = str(data_config["label_column"])
    date_column = data_config.get("date_column")

    feature_names = _feature_names(config, frame.columns, label_column, date_column)
    missing = [name for name in feature_names if name not in frame.columns]
    if missing:
        print(f"model-train: feature columns missing from data: {missing}")
        return 2

    split_config = config.get("split") or {}
    train, validation, test = _temporal_split(
        pl,
        frame,
        date_column=date_column,
        train_fraction=float(split_config.get("train_fraction", 0.70)),
        validation_fraction=float(split_config.get("validation_fraction", 0.15)),
    )
    if not train.height or not test.height:
        print("model-train: split produced an empty train or test partition")
        return 2

    x_train = _matrix(train, feature_names)
    y_train = np.asarray(train[label_column].to_numpy())
    x_test = _matrix(test, feature_names)
    y_test = np.asarray(test[label_column].to_numpy())
    x_val = _matrix(validation, feature_names) if validation.height else None
    y_val = np.asarray(validation[label_column].to_numpy()) if validation.height else None

    model, native_test = _train(
        family,
        task,
        config.get("model") or {},
        feature_names=feature_names,
        x_train=x_train,
        y_train=y_train,
        x_val=x_val,
        y_val=y_val,
        x_test=x_test,
    )

    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    bundle_id = args.bundle_id or f"{model_name}/paper-candidate-{timestamp}"
    staging = out_root / "staging" / _safe(bundle_id)
    staging.mkdir(parents=True, exist_ok=True)

    feature_schema_id, feature_schema_version = _schema_identity(config, model_name)
    schema_path = _write_feature_schema(
        staging / "feature_schema.json",
        feature_names=feature_names,
        schema_id=feature_schema_id,
        schema_version=feature_schema_version,
        task=task,
    )

    export = export_model_onnx(
        model,
        feature_names,
        staging / "model.onnx",
        model_family=family,
        task=task,
        feature_schema_id=feature_schema_id,
        feature_schema_version=feature_schema_version,
    )
    onnx_test = predict_onnx(export.path, x_test, output_name=export.output_name, output_select=export.output_select)
    max_delta = verify_export_parity(native_test, onnx_test, tolerance=args.parity_tolerance)

    metrics = _evaluate(task, y_test, onnx_test)
    parity_path = write_parity_cases(
        staging / "parity_cases.jsonl",
        feature_names=feature_names,
        rows=[list(row) for row in x_test],
        expected=[float(value) for value in np.asarray(onnx_test).ravel()],
        schema_id=feature_schema_id,
        schema_version=feature_schema_version,
        labels=[int(value) for value in y_test] if task is not ModelTask.REGRESSION else None,
        scalar_field="expected_output",
        max_rows=args.parity_rows,
    )

    report: dict[str, Any] = {
        "model_name": model_name,
        "model_version": model_version,
        "model_family": family.value,
        "task": task.value,
        "bundle_id": bundle_id,
        "feature_count": len(feature_names),
        "rows": {"train": train.height, "validation": validation.height, "test": test.height},
        "onnx": {
            "model_path": str(export.path),
            "output_name": export.output_name,
            "output_select": export.output_select,
            "max_abs_export_delta": max_delta,
        },
        "metrics": metrics,
    }

    bundle_config = config.get("bundle")
    if isinstance(bundle_config, Mapping) and bundle_config.get("strategy_spec"):
        bundle = ArtifactBundleWriter(out_root / "bundles").write_from_files(
            strategy_spec_path=Path(str(bundle_config["strategy_spec"])),
            sleeve_spec_path=Path(str(bundle_config["sleeve_spec"])) if bundle_config.get("sleeve_spec") else None,
            feature_schema_path=schema_path,
            model_path=export.path,
            parity_cases_path=parity_path,
            bundle_id=bundle_id,
            created_by="eventcontracts.cli.model_train",
        )
        ArtifactBundleValidator().validate(bundle)
        report["bundle_root"] = bundle.root_path
        report["manifest"] = bundle.manifest_path

    report_path = out_root / "reports" / f"{_safe(bundle_id)}.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    report["report"] = str(report_path)
    print(json.dumps(report, indent=2, default=str))
    return 0


def _train(
    family: ModelFamily,
    task: ModelTask,
    model_config: Mapping[str, Any],
    *,
    feature_names: Sequence[str],
    x_train: np.ndarray,
    y_train: np.ndarray,
    x_val: np.ndarray | None,
    y_val: np.ndarray | None,
    x_test: np.ndarray,
) -> tuple[Any, np.ndarray]:
    """Fit the model and return ``(model, native_test_predictions)``."""

    params = dict(model_config.get("params") or {})
    if family is ModelFamily.SKLEARN:
        estimator_key = str(model_config.get("estimator", "logistic_regression"))
        if estimator_key not in _SKLEARN_ESTIMATORS:
            raise ValueError(f"unknown sklearn estimator {estimator_key!r}; known: {sorted(_SKLEARN_ESTIMATORS)}")
        module_name, class_name, defaults = _SKLEARN_ESTIMATORS[estimator_key]
        from importlib import import_module

        cls = getattr(import_module(module_name), class_name)
        model = cls(**{**defaults, **params})
        model.fit(x_train, y_train)
        native = (
            model.predict_proba(x_test)[:, 1]
            if task is ModelTask.BINARY_CLASSIFICATION
            else model.predict(x_test)
        )
        return model, np.asarray(native)
    if family is ModelFamily.XGBOOST:
        return _train_xgboost(task, model_config, params, x_train, y_train, x_val, y_val, x_test)
    if family is ModelFamily.LIGHTGBM:
        return _train_lightgbm(task, model_config, params, x_train, y_train, x_val, y_val, x_test)
    raise ValueError(f"unsupported model family for model-train: {family}")


def _train_xgboost(
    task: ModelTask,
    model_config: Mapping[str, Any],
    params: dict[str, Any],
    x_train: np.ndarray,
    y_train: np.ndarray,
    x_val: np.ndarray | None,
    y_val: np.ndarray | None,
    x_test: np.ndarray,
) -> tuple[Any, np.ndarray]:
    from importlib import import_module

    xgb = import_module("xgboost")
    objective = params.pop("objective", None) or (
        "reg:squarederror" if task is ModelTask.REGRESSION else "binary:logistic"
    )
    model_params = {"objective": objective, "seed": 42, **params}
    dtrain = xgb.DMatrix(x_train, label=y_train)
    evals = [(dtrain, "train")]
    if x_val is not None and y_val is not None and len(y_val):
        evals.append((xgb.DMatrix(x_val, label=y_val), "validation"))
    booster = xgb.train(
        model_params,
        dtrain,
        num_boost_round=int(model_config.get("num_boost_round", 400)),
        evals=evals,
        early_stopping_rounds=int(model_config.get("early_stopping_rounds", 40)) if len(evals) > 1 else None,
        verbose_eval=False,
    )
    native = booster.predict(xgb.DMatrix(x_test))
    return booster, np.asarray(native)


def _train_lightgbm(
    task: ModelTask,
    model_config: Mapping[str, Any],
    params: dict[str, Any],
    x_train: np.ndarray,
    y_train: np.ndarray,
    x_val: np.ndarray | None,
    y_val: np.ndarray | None,
    x_test: np.ndarray,
) -> tuple[Any, np.ndarray]:
    from importlib import import_module

    try:
        lgb = import_module("lightgbm")
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise RuntimeError("lightgbm is required for the lightgbm family; install it in the research env.") from exc
    objective = params.pop("objective", None) or ("regression" if task is ModelTask.REGRESSION else "binary")
    model_params = {"objective": objective, "seed": 42, "verbosity": -1, **params}
    dtrain = lgb.Dataset(x_train, label=y_train)
    valid_sets = [dtrain]
    if x_val is not None and y_val is not None and len(y_val):
        valid_sets.append(lgb.Dataset(x_val, label=y_val, reference=dtrain))
    booster = lgb.train(
        model_params,
        dtrain,
        num_boost_round=int(model_config.get("num_boost_round", 400)),
        valid_sets=valid_sets,
    )
    native = booster.predict(x_test)
    return booster, np.asarray(native)


def _evaluate(task: ModelTask, y_true: np.ndarray, predictions: np.ndarray) -> dict[str, float | int]:
    if task is ModelTask.REGRESSION:
        return evaluate_regression(y_true, predictions).to_dict()
    return evaluate_classification(y_true.astype(int), predictions).to_dict()


def _feature_names(
    config: Mapping[str, Any],
    columns: Sequence[str],
    label_column: str,
    date_column: str | None,
) -> list[str]:
    schema_config = config.get("feature_schema") or {}
    if schema_config.get("path"):
        schema = load_feature_schema(Path(str(schema_config["path"])))
        return [feature.name for feature in schema.features]
    explicit = config.get("data", {}).get("feature_columns")
    if explicit:
        return [str(name) for name in explicit]
    reserved = {label_column} | ({date_column} if date_column else set())
    return [name for name in columns if name not in reserved]


def _schema_identity(config: Mapping[str, Any], model_name: str) -> tuple[str, str]:
    schema_config = config.get("feature_schema") or {}
    if schema_config.get("path"):
        schema = load_feature_schema(Path(str(schema_config["path"])))
        return str(schema.schema_id), str(schema.schema_version)
    return str(schema_config.get("schema_id", f"{model_name}_features")), str(schema_config.get("schema_version", "1"))


def _write_feature_schema(
    path: Path,
    *,
    feature_names: Sequence[str],
    schema_id: str,
    schema_version: str,
    task: ModelTask,
) -> Path:
    document = {
        "schema_id": schema_id,
        "schema_version": schema_version,
        "description": f"Auto-generated feature schema for {schema_id} ({task.value}).",
        "features": [
            {"name": name, "dtype": "float32", "description": "", "nullable": False, "default": 0.0}
            for name in feature_names
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    return path


def _matrix(frame: Any, feature_names: Sequence[str]) -> np.ndarray:
    return np.ascontiguousarray(frame.select(list(feature_names)).to_numpy().astype(np.float32))


def _temporal_split(
    pl: Any,
    frame: Any,
    *,
    date_column: str | None,
    train_fraction: float,
    validation_fraction: float,
) -> tuple[Any, Any, Any]:
    """Chronological split. Whole dates stay together when ``date_column`` is set."""

    if not 0 < train_fraction < 1:
        raise ValueError("train_fraction must be in (0, 1)")
    if not 0 <= validation_fraction < 1 or train_fraction + validation_fraction >= 1:
        raise ValueError("train + validation fractions must leave test rows")
    if date_column and date_column in frame.columns:
        ordered = frame.sort(date_column)
        date_groups = ordered.group_by(date_column).len().sort(date_column).rows()
        if len(date_groups) >= 3:
            cumulative: list[int] = []
            running = 0
            for _value, count in date_groups:
                running += int(count)
                cumulative.append(running)
            total = ordered.height
            train_groups = min(
                range(1, len(date_groups) - 1),
                key=lambda end: abs(cumulative[end - 1] - total * train_fraction),
            )
            validation_target = total * (train_fraction + validation_fraction)
            validation_groups = min(
                range(train_groups + 1, len(date_groups)),
                key=lambda end: abs(cumulative[end - 1] - validation_target),
            )
            train_end = date_groups[train_groups - 1][0]
            validation_end = date_groups[validation_groups - 1][0]
            return (
                ordered.filter(pl.col(date_column) <= train_end),
                ordered.filter((pl.col(date_column) > train_end) & (pl.col(date_column) <= validation_end)),
                ordered.filter(pl.col(date_column) > validation_end),
            )
        frame = ordered
    rows = frame.height
    train_end = int(rows * train_fraction)
    validation_end = train_end + int(rows * validation_fraction)
    return frame.slice(0, train_end), frame.slice(train_end, validation_end - train_end), frame.slice(validation_end)


def _load_frame(pl: Any, path: Path) -> Any:
    if path.suffix.lower() == ".parquet":
        return pl.read_parquet(path)
    if path.suffix.lower() in (".csv", ".tsv"):
        return pl.read_csv(path, infer_schema_length=10000)
    raise ValueError(f"unsupported data file type: {path.suffix}")


def _safe(value: str) -> str:
    return value.replace("/", "__").replace("\\", "__").strip(".")


def _polars() -> Any:
    from importlib import import_module

    try:
        return import_module("polars")
    except ImportError as exc:  # pragma: no cover - research dependency
        raise RuntimeError("polars is required for model-train") from exc
