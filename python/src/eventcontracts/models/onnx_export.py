"""Generic model → ONNX export, parity verification, and inference.

This is the model-family-agnostic export path that the tennis XGBoost
pipeline (and any future strategy) sits on top of. It converts a trained
estimator into a portable ``model.onnx`` whose contract the Rust runtime can
load without knowing the model family:

* Input tensor ``features`` of shape ``[N, len(feature_names)]``, ``float32``.
* A single, named output tensor that the Rust loader reads via an
  ``output_select`` rule (``"scalar:<idx>"`` for one column, ``"all"`` for the
  whole row).
* A small block of ONNX metadata (``eventcontracts.*`` keys) that pins the
  feature schema, feature order, model family, task, and the input/output
  tensor names. The promoted ``feature_schema.json`` remains the source of
  truth for feature *order*; the embedded copy lets the runtime fail fast on a
  mismatch before it ever scores.

Heavy converters (``skl2onnx``, ``onnxmltools``, ``onnxruntime``) are imported
lazily so the base framework still loads in environments that only build
features. Install them with ``requirements-dev.txt`` (research extras).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from importlib import import_module
from pathlib import Path
from typing import Any

import numpy as np

METADATA_PREFIX = "eventcontracts."
DEFAULT_INPUT_NAME = "features"
DEFAULT_TARGET_OPSET = 15


class ModelTask(str, Enum):
    """What the model predicts — drives the default ONNX output selection."""

    BINARY_CLASSIFICATION = "binary_classification"
    MULTICLASS_CLASSIFICATION = "multiclass_classification"
    REGRESSION = "regression"


class ModelFamily(str, Enum):
    """Trained-estimator family — drives which converter is used."""

    SKLEARN = "sklearn"
    XGBOOST = "xgboost"
    LIGHTGBM = "lightgbm"
    HUGGINGFACE = "huggingface"


@dataclass(frozen=True)
class OnnxExport:
    """The promotion-relevant facts about an exported ONNX model.

    ``output_select`` is a tiny string DSL shared with the Rust loader:

    * ``"scalar:<idx>"`` — return column ``idx`` of the output (e.g. the
      probability of the positive class for a binary classifier).
    * ``"all"`` — return the whole flattened output row (regression or
      multi-output models).
    """

    path: Path
    input_name: str
    output_name: str
    output_select: str
    feature_names: tuple[str, ...]
    model_family: str
    task: str
    feature_schema_id: str
    feature_schema_version: str
    target_opset: int

    @property
    def input_width(self) -> int:
        return len(self.feature_names)

    def metadata(self) -> dict[str, str]:
        """The ``eventcontracts.*`` block embedded in the ONNX graph."""

        return {
            f"{METADATA_PREFIX}feature_schema_id": self.feature_schema_id,
            f"{METADATA_PREFIX}feature_schema_version": self.feature_schema_version,
            f"{METADATA_PREFIX}feature_names_json": json.dumps(list(self.feature_names)),
            f"{METADATA_PREFIX}model_family": self.model_family,
            f"{METADATA_PREFIX}task": self.task,
            f"{METADATA_PREFIX}input_name": self.input_name,
            f"{METADATA_PREFIX}output_name": self.output_name,
            f"{METADATA_PREFIX}output_select": self.output_select,
            f"{METADATA_PREFIX}input_width": str(self.input_width),
        }


def export_model_onnx(
    model: Any,
    feature_names: Sequence[str],
    path: str | Path,
    *,
    model_family: ModelFamily | str,
    task: ModelTask | str,
    feature_schema_id: str,
    feature_schema_version: str,
    input_name: str = DEFAULT_INPUT_NAME,
    target_opset: int = DEFAULT_TARGET_OPSET,
    positive_class_index: int = 1,
) -> OnnxExport:
    """Convert ``model`` to ONNX and write it to ``path``.

    Dispatches on ``model_family`` to the right converter. The resulting graph
    has a single float32 input named ``input_name`` of width
    ``len(feature_names)`` and carries the ``eventcontracts.*`` metadata block.
    """

    family = ModelFamily(model_family)
    model_task = ModelTask(task)
    names = tuple(str(name) for name in feature_names)
    if not names:
        raise ValueError("feature_names must be non-empty")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    onnx_model = _convert(
        model, names, family=family, task=model_task, input_name=input_name, target_opset=target_opset
    )
    output_name = _select_output_name(onnx_model, task=model_task)
    output_select = _output_select(model_task, positive_class_index)
    export = OnnxExport(
        path=target,
        input_name=input_name,
        output_name=output_name,
        output_select=output_select,
        feature_names=names,
        model_family=family.value,
        task=model_task.value,
        feature_schema_id=feature_schema_id,
        feature_schema_version=feature_schema_version,
        target_opset=target_opset,
    )
    embed_metadata(onnx_model, export.metadata())
    _save_onnx(onnx_model, target)
    return export


def predict_onnx(
    model_path: str | Path,
    features: np.ndarray | Sequence[Sequence[float]],
    *,
    output_name: str | None = None,
    output_select: str = "scalar:1",
    input_name: str = DEFAULT_INPUT_NAME,
) -> np.ndarray:
    """Run an exported ONNX model through onnxruntime and apply ``output_select``.

    Returns a 1-D array (one value per row) for ``scalar:<idx>`` and a 2-D
    array for ``all``. Used for export-parity checks and the deterministic
    Python scoring side of strategy decisions.
    """

    ort = _import("onnxruntime", "onnxruntime")
    matrix = np.ascontiguousarray(np.asarray(features, dtype=np.float32))
    if matrix.ndim != 2:
        raise ValueError(f"features must be 2-D [N, width], got shape {matrix.shape}")
    session = ort.InferenceSession(str(model_path), providers=["CPUExecutionProvider"])
    resolved_output = output_name or _runtime_output_name(session, output_select)
    raw = session.run([resolved_output], {input_name: matrix})[0]
    return _apply_output_select(raw, output_select)


def verify_export_parity(
    native: Sequence[float] | np.ndarray,
    exported: Sequence[float] | np.ndarray,
    *,
    tolerance: float = 1e-6,
) -> float:
    """Return the max abs delta between native and ONNX outputs; raise if over.

    ``native`` is what the in-memory estimator predicts; ``exported`` is what
    onnxruntime returns from the saved graph. A small delta is expected from
    float32 truncation — anything over ``tolerance`` means the export is not a
    faithful copy and the artifact must not be promoted.
    """

    left = np.asarray(native, dtype=np.float64).ravel()
    right = np.asarray(exported, dtype=np.float64).ravel()
    if left.shape != right.shape:
        raise ValueError(f"parity arrays differ in shape: {left.shape} != {right.shape}")
    if left.size == 0:
        raise ValueError("cannot verify parity on zero rows")
    max_delta = float(np.max(np.abs(left - right)))
    if max_delta > tolerance:
        raise OnnxExportParityError(max_delta=max_delta, tolerance=tolerance)
    return max_delta


class OnnxExportParityError(RuntimeError):
    """Raised when the ONNX export diverges from the source estimator."""

    def __init__(self, *, max_delta: float, tolerance: float) -> None:
        super().__init__(f"ONNX export parity exceeded tolerance: {max_delta} > {tolerance}")
        self.max_delta = max_delta
        self.tolerance = tolerance


def embed_metadata(onnx_model: Any, values: Mapping[str, str]) -> None:
    """Replace the model's metadata_props with ``values`` (sorted, stable)."""

    del onnx_model.metadata_props[:]
    for key in sorted(values):
        prop = onnx_model.metadata_props.add()
        prop.key = key
        prop.value = str(values[key])


def read_metadata(model_path: str | Path) -> dict[str, str]:
    """Read back the ``eventcontracts.*`` metadata from a saved ONNX file."""

    onnx = _import("onnx", "onnx")
    model = onnx.load(str(model_path))
    out: dict[str, str] = {}
    for prop in model.metadata_props:
        if prop.key.startswith(METADATA_PREFIX):
            out[prop.key[len(METADATA_PREFIX) :]] = prop.value
    return out


# ---------------------------------------------------------------------------
# Converters (lazy imports keep skl2onnx / onnxmltools out of the base deps).
# ---------------------------------------------------------------------------


def _convert(
    model: Any,
    feature_names: Sequence[str],
    *,
    family: ModelFamily,
    task: ModelTask,
    input_name: str,
    target_opset: int,
) -> Any:
    width = len(feature_names)
    if family is ModelFamily.SKLEARN:
        skl2onnx = _import("skl2onnx", "skl2onnx")
        initial_types = [(input_name, _float_tensor_type(width, "skl2onnx.common.data_types"))]
        # zipmap=False makes the probability output a plain float tensor instead
        # of a sequence-of-maps, which is what the Rust OnnxScorer expects.
        options = {id(model): {"zipmap": False}} if task is not ModelTask.REGRESSION else None
        return skl2onnx.convert_sklearn(
            model,
            initial_types=initial_types,
            target_opset=target_opset,
            options=options,
        )
    if family in (ModelFamily.XGBOOST, ModelFamily.LIGHTGBM):
        # onnxmltools converters require their *own* FloatTensorType, not
        # skl2onnx's — they are not interchangeable at the shape calculator.
        initial_types = [(input_name, _float_tensor_type(width, "onnxmltools.convert.common.data_types"))]
        onnxmltools = _import("onnxmltools", "onnxmltools")
        convert = onnxmltools.convert_xgboost if family is ModelFamily.XGBOOST else onnxmltools.convert_lightgbm
        return convert(model, initial_types=initial_types, target_opset=target_opset)
    if family is ModelFamily.HUGGINGFACE:
        raise ValueError(
            "HuggingFace models export through eventcontracts.models.hf, not export_model_onnx"
        )
    raise ValueError(f"unsupported model family: {family}")


def _float_tensor_type(width: int, module_name: str) -> Any:
    """``FloatTensorType([None, width])`` from the converter-specific module.

    The skl2onnx and onnxmltools ``FloatTensorType`` classes are NOT
    interchangeable — each converter's shape calculator rejects the other's.
    Callers pass the module that matches the converter they will use.
    """

    try:
        module = import_module(module_name)
    except ImportError as exc:  # pragma: no cover - exercised only without extras
        raise RuntimeError(
            f"{module_name} is required for ONNX export; install the research extras (requirements-dev.txt)."
        ) from exc
    return module.FloatTensorType([None, width])


def _select_output_name(onnx_model: Any, *, task: ModelTask) -> str:
    """Pick the meaningful output: probability tensor for classifiers, else the first."""

    outputs = [str(output.name) for output in onnx_model.graph.output]
    if not outputs:
        raise ValueError("converted ONNX model has no graph outputs")
    if task is ModelTask.REGRESSION:
        return outputs[0]
    for name in outputs:
        if "probab" in name.lower():
            return name
    # XGBoost/LightGBM converters emit [label, probabilities]; fall back to the
    # last output, which is the probability tensor for those converters.
    return outputs[-1]


def _output_select(task: ModelTask, positive_class_index: int) -> str:
    if task is ModelTask.BINARY_CLASSIFICATION:
        return f"scalar:{positive_class_index}"
    return "all"


def _runtime_output_name(session: Any, output_select: str) -> str:
    outputs = session.get_outputs()
    if not outputs:
        raise ValueError("ONNX session exposes no outputs")
    if output_select == "all":
        return str(outputs[0].name)
    for output in outputs:
        if "probab" in str(output.name).lower():
            return str(output.name)
    return str(outputs[-1].name)


def _apply_output_select(raw: Any, output_select: str) -> np.ndarray:
    array = np.asarray(raw)
    if output_select == "all":
        return array.astype(np.float64, copy=False)
    if output_select.startswith("scalar:"):
        index = int(output_select.split(":", 1)[1])
        if array.ndim == 2:
            return array[:, index].astype(np.float64, copy=False)
        if array.ndim == 1:
            # Some converters return a 1-D probability for the positive class.
            return array.astype(np.float64, copy=False)
        raise ValueError(f"cannot apply {output_select!r} to output of shape {array.shape}")
    raise ValueError(f"unknown output_select rule: {output_select!r}")


def _save_onnx(onnx_model: Any, path: Path) -> None:
    onnx = _import("onnx", "onnx")
    onnx.save_model(onnx_model, str(path))


def _import(module_name: str, package_label: str) -> Any:
    try:
        return import_module(module_name)
    except ImportError as exc:  # pragma: no cover - exercised only without extras
        raise RuntimeError(
            f"{package_label} is required for ONNX export/inference; "
            "install the research extras in requirements-dev.txt."
        ) from exc
