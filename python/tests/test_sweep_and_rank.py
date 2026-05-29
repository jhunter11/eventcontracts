"""End-to-end tests for `sweep`, `gen-windows`, and `rank`.

Builds a small Parquet partition of normalized events, generates a
walk-forward windows TOML, runs a parameter sweep on the
example_threshold strategy across that grid in serial mode (so workers
are easy to debug), and checks the rank CLI parses the resulting
Parquet.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from eventcontracts.cli import main as _main_fn
from eventcontracts.domain.events import EventProvenance, TradeEvent
from eventcontracts.domain.ids import EventId
from eventcontracts.domain.models import InstrumentId, OutcomeSide, Trade, Venue
from eventcontracts.storage import ParquetEventStore
from tests.conftest import REPO_ROOT


def cli(argv: list[str]) -> int:
    return _main_fn(argv)


def _seed_partition(root: Path) -> None:
    store = ParquetEventStore(root)
    for i, price in enumerate(["0.46", "0.44", "0.42", "0.40", "0.38", "0.36"]):
        ts = datetime(2026, 1, 1, second=i, tzinfo=UTC)
        store.append_normalized(
            TradeEvent(
                event_id=EventId(f"t-{i:03d}"),
                trade=Trade(
                    instrument_id=InstrumentId(
                        venue=Venue.KALSHI, market_id="M-1", outcome_id=None
                    ),
                    side=OutcomeSide.YES,
                    price=Decimal(price),
                    quantity=Decimal("10"),
                    trade_id=f"tv-{i}",
                    exchange_ts=ts,
                    received_at=ts,
                ),
                provenance=EventProvenance(
                    source="fixture", channel="trade", venue=Venue.KALSHI
                ),
            )
        )
    store.flush()


def test_gen_windows_emits_walk_forward_toml(tmp_path: Path) -> None:
    out = tmp_path / "windows.toml"
    rc = cli(
        [
            "gen-windows",
            "--start",
            "2026-01-01T00:00:00Z",
            "--end",
            "2026-02-01T00:00:00Z",
            "--train-days",
            "7",
            "--validate-days",
            "0",
            "--test-days",
            "7",
            "--out",
            str(out),
        ]
    )
    assert rc == 0
    body = out.read_text()
    assert "[[windows]]" in body
    assert 'kind = "train"' in body
    assert 'kind = "test"' in body
    # Window TOML must be loadable by `sweep`.
    import tomllib

    parsed = tomllib.loads(body)
    assert isinstance(parsed["windows"], list)
    assert parsed["windows"]


def test_sweep_runs_grid_and_rank_reads_results(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    data_root = tmp_path / "data"
    _seed_partition(data_root)

    params_path = tmp_path / "params.toml"
    params_path.write_text(
        """
[grid]
buy_below = ["0.43", "0.41"]
size = ["3", "5"]
""".strip()
    )

    windows_path = tmp_path / "windows.toml"
    windows_path.write_text(
        """
[[windows]]
name = "fixture_all"
kind = "train"
start = "2026-01-01T00:00:00Z"
end = "2026-01-02T00:00:00Z"
""".strip()
    )

    results_path = tmp_path / "results.parquet"
    rc = cli(
        [
            "sweep",
            "--strategy",
            str(REPO_ROOT / "configs/strategies/example-threshold.toml"),
            "--sleeve",
            str(REPO_ROOT / "configs/sleeves/example-kalshi-paper.toml"),
            "--params",
            str(params_path),
            "--windows",
            str(windows_path),
            "--data",
            str(data_root),
            "--out",
            str(results_path),
            "--workers",
            "1",  # Serial keeps subprocess-import surface predictable in tests.
        ]
    )
    assert rc == 0
    assert results_path.exists()

    table = pq.read_table(results_path)
    rows = table.to_pylist()
    # 2 buy_below × 2 size × 1 window = 4 rows.
    assert len(rows) == 4
    assert all(row["window_name"] == "fixture_all" for row in rows)
    assert all(row["error"] == "" for row in rows)
    seen_params: set[str] = set()
    for row in rows:
        params = json.loads(row["params_json"])
        assert "buy_below" in params and "size" in params
        seen_params.add(row["params_hash"])
    assert len(seen_params) == 4  # all combos unique

    rc = cli(
        [
            "rank",
            "--results",
            str(results_path),
            "--sort-by",
            "fills",
            "--top",
            "4",
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "window_name" in out
    assert "fills" in out


def test_rank_aggregate_groups_by_params_hash(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    _seed_partition(data_root)

    params_path = tmp_path / "params.toml"
    params_path.write_text(
        """
[grid]
buy_below = ["0.43"]
size = ["3"]
""".strip()
    )

    windows_path = tmp_path / "windows.toml"
    windows_path.write_text(
        """
[[windows]]
name = "w1"
kind = "train"
start = "2026-01-01T00:00:00Z"
end = "2026-01-02T00:00:00Z"

[[windows]]
name = "w2"
kind = "test"
start = "2026-01-01T00:00:00Z"
end = "2026-01-02T00:00:00Z"
""".strip()
    )

    results_path = tmp_path / "results.parquet"
    cli(
        [
            "sweep",
            "--strategy",
            str(REPO_ROOT / "configs/strategies/example-threshold.toml"),
            "--sleeve",
            str(REPO_ROOT / "configs/sleeves/example-kalshi-paper.toml"),
            "--params",
            str(params_path),
            "--windows",
            str(windows_path),
            "--data",
            str(data_root),
            "--out",
            str(results_path),
            "--workers",
            "1",
        ]
    )

    rc = cli(
        [
            "rank",
            "--results",
            str(results_path),
            "--sort-by",
            "fills",
            "--top",
            "5",
            "--aggregate",
        ]
    )
    assert rc == 0
