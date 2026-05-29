"""Generic model export-parity case writer.

Export-parity cases pin what a promoted model *should* output for a fixed set
of feature rows. They are the reference fixture recorded in an artifact bundle
so a reviewer (or a later Rust load) can confirm the saved ONNX graph still
produces the same numbers the researcher saw. This is distinct from the
cross-language *decision* parity harness (``rust/crates/parity``), which
replays whole events through both strategy gates.

The writer is model-agnostic: pass the ordered feature names, the feature
rows, and the expected outputs. Domain pipelines (e.g. tennis) wrap it with
their own field names.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


def write_parity_cases(
    path: str | Path,
    *,
    feature_names: Sequence[str],
    rows: Sequence[Sequence[float] | Mapping[str, float]],
    expected: Sequence[float | Sequence[float]],
    schema_id: str,
    schema_version: str,
    case_ids: Sequence[str] | None = None,
    labels: Sequence[int | None] | None = None,
    scalar_field: str = "expected_output",
    extra: Mapping[str, Sequence[Any]] | None = None,
    max_rows: int = 100,
) -> Path:
    """Write up to ``max_rows`` export-parity rows as JSONL.

    Each line carries the case id, the feature schema id/version, the ordered
    feature vector, and the expected model output under ``scalar_field``.
    """

    if max_rows <= 0:
        raise ValueError("max_rows must be > 0")
    if len(rows) != len(expected):
        raise ValueError(f"rows and expected differ in length: {len(rows)} != {len(expected)}")
    names = tuple(str(name) for name in feature_names)
    extra = extra or {}
    for key, column in extra.items():
        if len(column) != len(rows):
            raise ValueError(f"extra column {key!r} has length {len(column)}, expected {len(rows)}")

    limit = min(max_rows, len(rows))
    documents: list[str] = []
    for index in range(limit):
        row = rows[index]
        features = (
            [float(row[name]) for name in names]
            if isinstance(row, Mapping)
            else [float(value) for value in row]
        )
        if len(features) != len(names):
            raise ValueError(f"row {index} has {len(features)} features, expected {len(names)}")
        case: dict[str, Any] = {
            "case_id": str(case_ids[index]) if case_ids is not None else f"case-{index:08d}",
            "feature_schema_id": schema_id,
            "feature_schema_version": schema_version,
            "features": features,
            scalar_field: _coerce_expected(expected[index]),
        }
        if labels is not None and labels[index] is not None:
            case["label"] = int(labels[index])  # type: ignore[arg-type]
        for key, column in extra.items():
            case[key] = column[index]
        documents.append(json.dumps(case, separators=(",", ":"), default=str))

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(documents) + ("\n" if documents else ""), encoding="utf-8")
    return target


def _coerce_expected(value: float | Sequence[float]) -> float | list[float]:
    if isinstance(value, (int, float)):
        return float(value)
    return [float(item) for item in value]
