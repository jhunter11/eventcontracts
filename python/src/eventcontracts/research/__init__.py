"""Researcher-facing helpers for notebooks and ad-hoc experiments.

Small, opinionated functions that make the "load Parquet → run strategy →
look at a report → compare runs" loop one line each. Imports stay lazy
where possible so the module loads cleanly in environments without
matplotlib (the plotting helpers fail gracefully with a clear message).

This is the only place researcher code should import; everything below
this layer is framework internals.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from eventcontracts.cli.backtest import run_backtest
from eventcontracts.config import load_sleeve_spec, load_strategy_spec
from eventcontracts.domain.spec import SleeveSpec, StrategySpec
from eventcontracts.execution import BacktestReport
from eventcontracts.research.tennis_xgboost import (
    FEATURE_SCHEMA_ID as TENNIS_XGBOOST_FEATURE_SCHEMA_ID,
)
from eventcontracts.research.tennis_xgboost import (
    FEATURE_SCHEMA_VERSION as TENNIS_XGBOOST_FEATURE_SCHEMA_VERSION,
)
from eventcontracts.research.tennis_xgboost import (
    TENNIS_XGBOOST_FEATURE_NAMES,
    TENNIS_XGBOOST_FEATURES,
    TennisEvaluation,
    TennisFeatureSpec,
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
    snapshots_to_frame,
    temporal_train_validation_test_split,
    train_xgboost_binary,
    write_feature_schema,
    write_parity_cases,
)
from eventcontracts.runner.base import RunSummary

__all__ = [
    "RunResult",
    "TENNIS_XGBOOST_FEATURE_NAMES",
    "TENNIS_XGBOOST_FEATURE_SCHEMA_ID",
    "TENNIS_XGBOOST_FEATURE_SCHEMA_VERSION",
    "TENNIS_XGBOOST_FEATURES",
    "TennisEvaluation",
    "TennisFeatureSpec",
    "TennisMatchSnapshot",
    "backtest_one",
    "build_sackmann_training_frame",
    "compare_runs",
    "evaluate_probabilities",
    "export_xgboost_onnx",
    "feature_row",
    "feature_schema_document",
    "feature_vector",
    "load_partition_summary",
    "load_sweep_results",
    "onnx_deployment_metadata",
    "predict_onnx_probabilities",
    "predict_xgboost_probabilities",
    "snapshot_from_mapping",
    "plot_equity",
    "summarize_report",
    "snapshots_to_frame",
    "temporal_train_validation_test_split",
    "train_xgboost_binary",
    "write_feature_schema",
    "write_parity_cases",
]


@dataclass(frozen=True)
class RunResult:
    """The (report, summary) pair returned by :func:`backtest_one`."""

    report: BacktestReport
    summary: RunSummary

    def as_dict(self) -> dict[str, Any]:
        return self.report.to_dict()


def backtest_one(
    strategy_path: str | Path,
    sleeve_path: str | Path,
    data_root: str | Path,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    latency_ms: float = 50.0,
    queue_fraction: str | Decimal = "1.0",
    starting_equity: str | Decimal = "0",
    parameter_overrides: Mapping[str, str | int | float | bool] | None = None,
) -> RunResult:
    """One-call backtest from researcher TOML configs.

    ``parameter_overrides`` replaces the matching keys in the loaded
    strategy spec's ``parameters`` mapping without mutating disk state —
    handy for "what if I change buy_below to 0.42" inside a notebook.
    """

    spec = load_strategy_spec(Path(strategy_path))
    sleeve = load_sleeve_spec(Path(sleeve_path))
    if parameter_overrides:
        spec = _replace_parameters(spec, parameter_overrides)
    report, summary = run_backtest(
        spec,
        sleeve,
        Path(data_root),
        start=start,
        end=end,
        latency_ms=latency_ms,
        queue_fraction=queue_fraction,
        starting_equity=starting_equity,
    )
    return RunResult(report=report, summary=summary)


def summarize_report(report: BacktestReport) -> str:
    """Compact one-screen text summary for notebook cells."""

    lines = [
        f"strategy_id        {report.strategy_id}",
        f"sleeve_id          {report.sleeve_id}",
        f"events_processed   {report.events_processed}",
        f"intents_dispatched {report.intents_dispatched}",
        f"intents_rejected   {report.intents_rejected}",
        f"fills              {report.fills}",
        f"fill_rate          {report.fill_rate:.3f}",
        f"realized_pnl       {report.realized_pnl}",
        f"unrealized_pnl     {report.unrealized_pnl}",
        f"total_pnl          {report.total_pnl}",
        f"total_fees_paid    {report.total_fees_paid}",
        f"peak_equity        {report.peak_equity}",
        f"trough_equity      {report.trough_equity}",
        f"max_drawdown       {report.max_drawdown}",
        f"open_positions     {report.open_positions}",
        f"duration_seconds   {report.duration_seconds:.2f}",
    ]
    if report.rejection_reasons:
        lines.append("rejection_reasons:")
        for reason, count in sorted(report.rejection_reasons.items()):
            lines.append(f"  {reason:<32} {count}")
    return "\n".join(lines)


def compare_runs(
    runs: Mapping[str, BacktestReport],
    *,
    columns: Iterable[str] = (
        "fills",
        "fill_rate",
        "realized_pnl",
        "total_pnl",
        "max_drawdown",
        "open_positions",
    ),
) -> str:
    """Side-by-side text comparison of multiple named reports."""

    column_list = list(columns)
    header = ["run"] + column_list
    rows: list[list[str]] = []
    for name, report in runs.items():
        row = [name]
        payload = report.to_dict()
        for col in column_list:
            row.append(str(payload.get(col, "")))
        rows.append(row)
    widths = [max(len(header[i]), *(len(r[i]) for r in rows)) for i in range(len(header))]
    out = ["  ".join(c.ljust(widths[i]) for i, c in enumerate(header))]
    out.append("  ".join("-" * w for w in widths))
    for r in rows:
        out.append("  ".join(c.ljust(widths[i]) for i, c in enumerate(r)))
    return "\n".join(out)


def load_sweep_results(path: str | Path) -> list[dict[str, Any]]:
    """Load a `sweep` results.parquet as a list of plain dicts.

    Returned rows are JSON-friendly — decimals stay as strings, parameter
    sets are re-parsed from ``params_json`` for convenience.
    """

    table = pq.read_table(Path(path))
    rows: list[dict[str, Any]] = list(table.to_pylist())
    for row in rows:
        params = row.get("params_json")
        if isinstance(params, str):
            try:
                row["params"] = json.loads(params)
            except (TypeError, ValueError):
                row["params"] = {}
    return rows


def load_partition_summary(data_root: str | Path) -> dict[str, Any]:
    """Lightweight counts of a ParquetEventStore root.

    Wraps the same logic the `inspect-data` CLI uses so notebooks don't
    have to shell out.
    """

    from eventcontracts.cli import inspect_data as _inspect

    root = Path(data_root)
    summary: dict[str, Any] = {"root": str(root)}
    counts_fn = getattr(_inspect, "_counts", None)
    if callable(counts_fn):
        summary.update(counts_fn(root))
        return summary
    # Fallback: simple file walk grouped by partition kind.
    raw = list((root / "raw").rglob("*.parquet")) if (root / "raw").exists() else []
    norm = list((root / "normalized").rglob("*.parquet")) if (root / "normalized").exists() else []
    summary["raw_files"] = len(raw)
    summary["normalized_files"] = len(norm)
    return summary


def plot_equity(report: BacktestReport) -> Any:  # noqa: ANN401 — returns a matplotlib axes
    """Plot peak/trough/total PnL as a tiny bar chart.

    Returns the matplotlib `Axes`. Raises a helpful error if matplotlib
    isn't installed — keep matplotlib out of the framework's required
    deps; install it in your research environment only.
    """

    try:
        import matplotlib.pyplot as plt
    except ImportError as exc:  # pragma: no cover - matplotlib is optional
        raise RuntimeError(
            "matplotlib is required for plot_equity; install with `pip install matplotlib` in your research env."
        ) from exc

    fig, ax = plt.subplots(figsize=(6, 3.5))
    labels = ["peak", "trough", "realized", "unrealized", "total", "max_drawdown"]
    values = [
        float(report.peak_equity),
        float(report.trough_equity),
        float(report.realized_pnl),
        float(report.unrealized_pnl),
        float(report.total_pnl),
        float(report.max_drawdown),
    ]
    ax.bar(labels, values)
    ax.set_title(f"{report.strategy_id} / {report.sleeve_id}")
    ax.set_ylabel(report.realized_pnl.__class__.__name__.lower())
    ax.tick_params(axis="x", rotation=20)
    fig.tight_layout()
    return ax


def _replace_parameters(
    spec: StrategySpec,
    overrides: Mapping[str, str | int | float | Decimal | bool],
) -> StrategySpec:
    import dataclasses

    merged: dict[str, str | int | Decimal | bool] = dict(spec.parameters)
    for key, value in overrides.items():
        merged[key] = Decimal(str(value)) if isinstance(value, float) else value
    return dataclasses.replace(spec, parameters=merged)


# Imported for type re-export so `eventcontracts.research.SleeveSpec` works.
SleeveSpec = SleeveSpec
StrategySpec = StrategySpec
