"""`eventcontracts sweep` — parameter grid × time-window backtest matrix.

Runs one backtest per `(parameter combination, window)` pair, optionally in
parallel via `multiprocessing.Pool`, and writes every result as a row to a
Parquet file. Inputs:

* ``--strategy``: a template ``strategy_spec.toml``. The sweep loads it
  per worker, overrides ``parameters`` with the combination, and runs.
* ``--sleeve``: a ``sleeve_spec.toml``.
* ``--params``: TOML with a ``[grid]`` table whose values are lists of
  parameter values. The cartesian product becomes the parameter axis.
* ``--windows``: TOML with ``[[windows]]`` entries, each with ``name``,
  ``start``, ``end``, and optional ``kind`` (``train`` / ``validate`` /
  ``test``).
* ``--data``: a ParquetEventStore root containing normalized events.
* ``--out``: destination Parquet path for the results table.

Workers are forked into separate processes (default = ``min(8, cpu_count)``).
Each worker re-loads the strategy/sleeve TOMLs, applies the parameter
overrides, runs :func:`backtest.run_backtest`, and returns one row.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import itertools
import json
import multiprocessing
import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from eventcontracts.cli.backtest import run_backtest
from eventcontracts.config import load_sleeve_spec, load_strategy_spec
from eventcontracts.domain.spec import StrategySpec

ParamValue = str | int | float | bool


def register(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "sweep",
        help="Run a parameter-grid × time-window backtest matrix.",
    )
    parser.add_argument("--strategy", type=Path, required=True, help="strategy_spec.toml template")
    parser.add_argument("--sleeve", type=Path, required=True, help="sleeve_spec.toml")
    parser.add_argument(
        "--params",
        type=Path,
        required=True,
        help="TOML file with a [grid] table whose values are lists of parameter values.",
    )
    parser.add_argument(
        "--windows",
        type=Path,
        required=True,
        help="TOML file with [[windows]] entries (name, start, end, optional kind).",
    )
    parser.add_argument("--data", type=Path, required=True, help="ParquetEventStore root")
    parser.add_argument("--out", type=Path, required=True, help="Output Parquet path")
    parser.add_argument(
        "--workers",
        type=int,
        default=None,
        help="Number of worker processes (default = min(8, cpu_count)).",
    )
    parser.add_argument("--latency-ms", type=float, default=50.0)
    parser.add_argument("--queue-fraction", type=str, default="1.0")
    parser.add_argument("--starting-equity", type=str, default="0")
    parser.set_defaults(handler=_handle)


@dataclass(frozen=True)
class _Window:
    name: str
    start: datetime
    end: datetime
    kind: str


def _load_param_grid(path: Path) -> dict[str, list[ParamValue]]:
    with path.open("rb") as file:
        doc = tomllib.load(file)
    grid = doc.get("grid")
    if not isinstance(grid, Mapping) or not grid:
        raise ValueError(f"{path}: expected a non-empty [grid] table")
    out: dict[str, list[ParamValue]] = {}
    for key, values in grid.items():
        if not isinstance(values, list) or not values:
            raise ValueError(f"{path}: grid.{key} must be a non-empty list")
        for value in values:
            if not isinstance(value, str | int | float | bool):
                raise ValueError(
                    f"{path}: grid.{key} entries must be str/int/float/bool, got {type(value).__name__}"
                )
        out[str(key)] = list(values)
    return out


def _load_windows(path: Path) -> tuple[_Window, ...]:
    with path.open("rb") as file:
        doc = tomllib.load(file)
    entries = doc.get("windows")
    if not isinstance(entries, list) or not entries:
        raise ValueError(f"{path}: expected at least one [[windows]] entry")
    windows: list[_Window] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise ValueError(f"{path}: each [[windows]] entry must be a table")
        try:
            name = str(entry["name"])
            start_raw = str(entry["start"])
            end_raw = str(entry["end"])
        except KeyError as exc:
            raise ValueError(f"{path}: window entry missing required key {exc}") from exc
        start = _parse_iso(start_raw)
        end = _parse_iso(end_raw)
        if end <= start:
            raise ValueError(f"{path}: window {name!r} has end <= start")
        kind = str(entry.get("kind", "train"))
        windows.append(_Window(name=name, start=start, end=end, kind=kind))
    return tuple(windows)


def _parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _cartesian(grid: Mapping[str, list[ParamValue]]) -> list[dict[str, ParamValue]]:
    keys = sorted(grid)
    combinations = itertools.product(*(grid[key] for key in keys))
    return [dict(zip(keys, combo, strict=True)) for combo in combinations]


def _params_hash(params: Mapping[str, ParamValue]) -> str:
    canonical = json.dumps(params, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _override_parameters(
    spec: StrategySpec, overrides: Mapping[str, ParamValue]
) -> StrategySpec:
    merged: dict[str, ParamValue] = dict(spec.parameters)
    merged.update(overrides)
    return dataclasses.replace(spec, parameters=merged)


RESULTS_SCHEMA = pa.schema(
    [
        ("run_id", pa.string()),
        ("strategy_id", pa.string()),
        ("sleeve_id", pa.string()),
        ("window_name", pa.string()),
        ("window_kind", pa.string()),
        ("window_start", pa.timestamp("us", tz="UTC")),
        ("window_end", pa.timestamp("us", tz="UTC")),
        ("params_hash", pa.string()),
        ("params_json", pa.string()),
        ("events_processed", pa.int64()),
        ("decisions_emitted", pa.int64()),
        ("intents_dispatched", pa.int64()),
        ("intents_rejected", pa.int64()),
        ("fills", pa.int64()),
        ("fill_rate", pa.float64()),
        ("realized_pnl", pa.string()),
        ("unrealized_pnl", pa.string()),
        ("total_pnl", pa.string()),
        ("total_fees_paid", pa.string()),
        ("peak_equity", pa.string()),
        ("trough_equity", pa.string()),
        ("max_drawdown", pa.string()),
        ("open_positions", pa.int64()),
        ("duration_seconds", pa.float64()),
        ("rejection_reasons_json", pa.string()),
        ("error", pa.string()),
    ]
)


@dataclass(frozen=True)
class _WorkerArgs:
    """Plain primitives passed to worker subprocesses (must be picklable)."""

    strategy_toml: str
    sleeve_toml: str
    data_root: str
    params_overrides: dict[str, ParamValue]
    params_hash: str
    window_name: str
    window_kind: str
    window_start_iso: str
    window_end_iso: str
    latency_ms: float
    queue_fraction: str
    starting_equity: str


def _sweep_worker(args: _WorkerArgs) -> dict[str, Any]:
    """Subprocess entrypoint. Loads specs, runs one backtest, returns one row."""

    spec = _override_parameters(
        load_strategy_spec(Path(args.strategy_toml)), args.params_overrides
    )
    sleeve = load_sleeve_spec(Path(args.sleeve_toml))
    start = _parse_iso(args.window_start_iso)
    end = _parse_iso(args.window_end_iso)
    run_id = (
        f"{spec.strategy_id}__{args.window_name}__{args.params_hash}_"
        f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%f')}"
    )
    base_row: dict[str, Any] = {
        "run_id": run_id,
        "strategy_id": str(spec.strategy_id),
        "sleeve_id": str(sleeve.sleeve_id),
        "window_name": args.window_name,
        "window_kind": args.window_kind,
        "window_start": start,
        "window_end": end,
        "params_hash": args.params_hash,
        "params_json": json.dumps(args.params_overrides, sort_keys=True, default=str),
    }
    try:
        report, _summary = run_backtest(
            spec,
            sleeve,
            args.data_root,
            latency_ms=args.latency_ms,
            queue_fraction=args.queue_fraction,
            starting_equity=args.starting_equity,
            start=start,
            end=end,
        )
    except Exception as exc:  # noqa: BLE001 — sweep workers must not crash the pool
        base_row.update(
            {
                "events_processed": 0,
                "decisions_emitted": 0,
                "intents_dispatched": 0,
                "intents_rejected": 0,
                "fills": 0,
                "fill_rate": 0.0,
                "realized_pnl": "0",
                "unrealized_pnl": "0",
                "total_pnl": "0",
                "total_fees_paid": "0",
                "peak_equity": "0",
                "trough_equity": "0",
                "max_drawdown": "0",
                "open_positions": 0,
                "duration_seconds": 0.0,
                "rejection_reasons_json": "{}",
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        return base_row

    base_row.update(
        {
            "events_processed": int(report.events_processed),
            "decisions_emitted": int(report.decisions_emitted),
            "intents_dispatched": int(report.intents_dispatched),
            "intents_rejected": int(report.intents_rejected),
            "fills": int(report.fills),
            "fill_rate": float(report.fill_rate),
            "realized_pnl": str(report.realized_pnl),
            "unrealized_pnl": str(report.unrealized_pnl),
            "total_pnl": str(report.total_pnl),
            "total_fees_paid": str(report.total_fees_paid),
            "peak_equity": str(report.peak_equity),
            "trough_equity": str(report.trough_equity),
            "max_drawdown": str(report.max_drawdown),
            "open_positions": int(report.open_positions),
            "duration_seconds": float(report.duration_seconds),
            "rejection_reasons_json": json.dumps(report.rejection_reasons, sort_keys=True),
            "error": "",
        }
    )
    return base_row


def _handle(args: argparse.Namespace) -> int:
    grid = _load_param_grid(args.params)
    windows = _load_windows(args.windows)
    combinations = _cartesian(grid)

    worker_args: list[_WorkerArgs] = []
    for combo in combinations:
        params_hash = _params_hash(combo)
        for window in windows:
            worker_args.append(
                _WorkerArgs(
                    strategy_toml=str(args.strategy.resolve()),
                    sleeve_toml=str(args.sleeve.resolve()),
                    data_root=str(args.data.resolve()),
                    params_overrides=combo,
                    params_hash=params_hash,
                    window_name=window.name,
                    window_kind=window.kind,
                    window_start_iso=window.start.isoformat(),
                    window_end_iso=window.end.isoformat(),
                    latency_ms=args.latency_ms,
                    queue_fraction=args.queue_fraction,
                    starting_equity=args.starting_equity,
                )
            )

    workers = args.workers or min(8, os.cpu_count() or 1)
    print(
        f"sweep: {len(combinations)} param combos × {len(windows)} windows = "
        f"{len(worker_args)} runs across {workers} worker(s)",
        flush=True,
    )

    rows: list[dict[str, Any]] = []
    if workers <= 1 or len(worker_args) <= 1:
        for one in worker_args:
            rows.append(_sweep_worker(one))
    else:
        ctx = multiprocessing.get_context("spawn")
        with ctx.Pool(processes=workers) as pool:
            for row in pool.imap_unordered(_sweep_worker, worker_args):
                rows.append(row)

    # Stable ordering for reproducibility.
    rows.sort(key=lambda r: (r["window_name"], r["params_hash"]))

    args.out.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist(rows, schema=RESULTS_SCHEMA)
    pq.write_table(table, args.out, compression="snappy")  # type: ignore[no-untyped-call]
    error_count = sum(1 for r in rows if r["error"])
    print(
        f"sweep: wrote {len(rows)} rows to {args.out}"
        + (f" ({error_count} runs errored)" if error_count else ""),
        flush=True,
    )
    return 0
