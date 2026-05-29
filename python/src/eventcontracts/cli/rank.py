"""`eventcontracts rank` — sort and display sweep results.

Loads the Parquet file produced by `eventcontracts sweep`, optionally
filters by window kind (`train`/`validate`/`test`) or aggregates across
windows of the same kind, sorts by a chosen metric, and prints the top-N
rows in a compact table.

Numeric columns persisted as strings (`realized_pnl`, `total_pnl`,
`max_drawdown`, etc.) are parsed to floats for ranking.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

NUMERIC_STRING_COLUMNS = {
    "realized_pnl",
    "unrealized_pnl",
    "total_pnl",
    "total_fees_paid",
    "peak_equity",
    "trough_equity",
    "max_drawdown",
}

DEFAULT_METRICS = (
    "total_pnl",
    "realized_pnl",
    "max_drawdown",
    "fill_rate",
    "fills",
    "events_processed",
)


def register(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "rank",
        help="Rank sweep results by a metric and print the top-N.",
    )
    parser.add_argument("--results", type=Path, required=True, help="sweep results.parquet")
    parser.add_argument(
        "--sort-by",
        type=str,
        default="total_pnl",
        help=f"Column to sort by (default: total_pnl). Common: {', '.join(DEFAULT_METRICS)}",
    )
    parser.add_argument(
        "--ascending",
        action="store_true",
        help="Sort ascending instead of descending (useful for drawdown / fees).",
    )
    parser.add_argument("--top", type=int, default=20)
    parser.add_argument(
        "--kind",
        type=str,
        default=None,
        choices=("train", "validate", "test"),
        help="Optional filter by window kind.",
    )
    parser.add_argument(
        "--aggregate",
        action="store_true",
        help=(
            "Aggregate across windows by params_hash, averaging the sort metric "
            "and summing fills/events. Useful for walk-forward stability."
        ),
    )
    parser.add_argument(
        "--show-error",
        action="store_true",
        help="Include rows that errored out (default: filtered out).",
    )
    parser.set_defaults(handler=_handle)


def _coerce_number(row: Mapping[str, Any], column: str) -> float:
    value = row.get(column)
    if value is None:
        return 0.0
    if column in NUMERIC_STRING_COLUMNS:
        try:
            return float(str(value))
        except ValueError:
            return 0.0
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, int | float):
        return float(value)
    try:
        return float(str(value))
    except ValueError:
        return 0.0


def _aggregate(rows: list[dict[str, Any]], sort_by: str) -> list[dict[str, Any]]:
    by_hash: dict[str, dict[str, Any]] = {}
    counts: dict[str, int] = {}
    for row in rows:
        ph = str(row["params_hash"])
        existing = by_hash.get(ph)
        counts[ph] = counts.get(ph, 0) + 1
        if existing is None:
            by_hash[ph] = dict(row)
            by_hash[ph]["windows"] = 1
            by_hash[ph][f"avg_{sort_by}"] = _coerce_number(row, sort_by)
            continue
        existing["windows"] += 1
        existing[f"avg_{sort_by}"] = (
            existing[f"avg_{sort_by}"] + _coerce_number(row, sort_by)
        )
        for column in ("events_processed", "fills", "intents_dispatched", "intents_rejected"):
            existing[column] = int(existing.get(column, 0)) + int(row.get(column, 0))
    aggregated: list[dict[str, Any]] = []
    for ph, row in by_hash.items():
        row[f"avg_{sort_by}"] = row[f"avg_{sort_by}"] / counts[ph]
        aggregated.append(row)
    return aggregated


def _handle(args: argparse.Namespace) -> int:
    if not args.results.exists():
        print(f"error: results file not found: {args.results}")
        return 2
    table = pq.read_table(args.results)
    rows: list[dict[str, Any]] = list(table.to_pylist())
    if not rows:
        print(f"rank: {args.results} contains 0 rows")
        return 0

    if not args.show_error:
        rows = [r for r in rows if not r.get("error")]

    if args.kind is not None:
        rows = [r for r in rows if r.get("window_kind") == args.kind]

    if not rows:
        print("rank: no rows after filters")
        return 0

    if args.aggregate:
        rows = _aggregate(rows, args.sort_by)
        sort_key = f"avg_{args.sort_by}"
    else:
        sort_key = args.sort_by

    rows.sort(
        key=lambda r: _coerce_number(r, sort_key),
        reverse=not args.ascending,
    )
    top = rows[: args.top]

    header_cols = (
        ["params_hash", "strategy_id", "window_name", "windows", sort_key]
        if args.aggregate
        else [
            "window_name",
            "window_kind",
            "params_hash",
            "total_pnl",
            "max_drawdown",
            "fill_rate",
            "fills",
            "events_processed",
        ]
    )
    if args.sort_by not in header_cols and not args.aggregate:
        header_cols.insert(2, args.sort_by)

    widths = {col: max(len(col), 6) for col in header_cols}
    for row in top:
        for col in header_cols:
            widths[col] = max(widths[col], len(str(row.get(col, ""))))

    line = "  ".join(col.ljust(widths[col]) for col in header_cols)
    print(line)
    print("  ".join("-" * widths[col] for col in header_cols))
    for row in top:
        print("  ".join(str(row.get(col, "")).ljust(widths[col]) for col in header_cols))

    if not args.aggregate:
        print()
        print("params:")
        for row in top[: min(5, len(top))]:
            params = row.get("params_json", "{}")
            try:
                pretty = json.dumps(json.loads(params), sort_keys=True)
            except (TypeError, ValueError):
                pretty = str(params)
            print(f"  {row.get('params_hash')}  {pretty}")
    return 0
