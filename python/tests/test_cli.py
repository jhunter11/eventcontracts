"""CLI smoke tests: check-config, validate-config, validate-bundle, replay."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from eventcontracts.cli import main as _main_fn
from eventcontracts.domain.events import EventProvenance, TradeEvent
from eventcontracts.domain.ids import EventId
from eventcontracts.domain.models import InstrumentId, OutcomeSide, Trade, Venue
from eventcontracts.storage import ParquetEventStore
from tests.conftest import REPO_ROOT


def cli(argv: list[str]) -> int:
    return _main_fn(argv)


def test_check_config_runs(capsys: pytest.CaptureFixture[str]) -> None:
    rc = cli(
        ["check-config", str(REPO_ROOT / "configs/strategies/example-threshold.toml")]
    )
    assert rc == 0
    assert "example_threshold" in capsys.readouterr().out


def test_validate_config_strategy(capsys: pytest.CaptureFixture[str]) -> None:
    rc = cli(
        [
            "validate-config",
            "strategy",
            str(REPO_ROOT / "configs/strategies/example-threshold.toml"),
        ]
    )
    assert rc == 0


def test_validate_bundle_accepts_weather_threshold(
    capsys: pytest.CaptureFixture[str],
) -> None:
    bundle = REPO_ROOT / "contracts/examples/weather_threshold"
    rc = cli(["validate-bundle", str(bundle)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "OK" in out


def test_validate_bundle_rejects_missing_files(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    rc = cli(["validate-bundle", str(tmp_path)])
    assert rc == 1
    out = capsys.readouterr().out
    assert "missing" in out


def test_validate_bundle_rejects_checksum_mismatch(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    (tmp_path / "manifest.toml").write_text(
        """
schema_version = "1"
bundle_id = "bad/checksum"
created_at = "2026-01-01T00:00:00Z"

[strategy]
name = "example_threshold"
version = "0.1.0"
strategy_spec = "strategy_spec.toml"

[features]
schema = "feature_schema.json"
schema_id = "bad_features"
schema_version = "1"
sha256 = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

[[files]]
path = "manifest.toml"
sha256 = "0000000000000000000000000000000000000000000000000000000000000000"

[[files]]
path = "strategy_spec.toml"
sha256 = "0000000000000000000000000000000000000000000000000000000000000000"

[[files]]
path = "sleeve_spec.toml"
sha256 = "0000000000000000000000000000000000000000000000000000000000000000"

[[files]]
path = "feature_schema.json"
sha256 = "0000000000000000000000000000000000000000000000000000000000000000"
""".strip()
    )
    (tmp_path / "strategy_spec.toml").write_text(
        """
strategy_id = "example-threshold-v1"
name = "example_threshold"
version = "0.1.0"
description = "checksum mismatch fixture"

[subscription]
venues = ["kalshi"]
instrument_patterns = ["*"]
event_kinds = ["trade"]

[parameters]
buy_below = "0.50"
""".strip()
    )
    (tmp_path / "sleeve_spec.toml").write_text(
        """
sleeve_id = "paper-a"
strategy_id = "example-threshold-v1"
strategy_version = "0.1.0"
venue = "kalshi"
capital_allocation = "10000"
currency = "USD"

[risk]
max_order_notional = "100"
max_position_notional = "500"
max_daily_loss = "50"
max_open_orders = 10
max_gross_exposure = "1000"
currency = "USD"
""".strip()
    )
    (tmp_path / "feature_schema.json").write_text(
        json.dumps(
            {
                "schema_id": "bad_features",
                "schema_version": "1",
                "features": [{"name": "x", "dtype": "float64"}],
            }
        )
    )

    rc = cli(["validate-bundle", str(tmp_path)])

    assert rc == 1
    assert "checksum mismatch" in capsys.readouterr().out


def test_replay_streams_normalized_events(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    store = ParquetEventStore(tmp_path)
    for i in range(3):
        store.append_normalized(
            TradeEvent(
                event_id=EventId(f"t-{i}"),
                trade=Trade(
                    instrument_id=InstrumentId(
                        venue=Venue.KALSHI,
                        market_id="M-1",
                        outcome_id=None,
                    ),
                    side=OutcomeSide.YES,
                    price=Decimal("0.50"),
                    quantity=Decimal("1"),
                    trade_id=None,
                    exchange_ts=datetime(2026, 1, 1, second=i, tzinfo=UTC),
                    received_at=datetime(2026, 1, 1, second=i, tzinfo=UTC),
                ),
                provenance=EventProvenance(source="k", channel="trade", venue=Venue.KALSHI),
            )
        )
    store.flush()

    rc = cli(["replay", "--data", str(tmp_path), "--limit", "2"])
    assert rc == 0
    lines = [
        line for line in capsys.readouterr().out.splitlines() if line.strip()
    ]
    assert len(lines) == 2
    parsed = json.loads(lines[0])
    assert parsed["kind"] == "trade"


def test_backtest_emits_full_report_and_writes_out_file(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    # Seed a tiny normalized partition with falling-price trades that trigger
    # the example_threshold strategy's PlaceOrder branch.
    data_dir = tmp_path / "data"
    store = ParquetEventStore(data_dir)
    for i, price in enumerate(["0.46", "0.44", "0.42"]):
        trade_at = datetime(2026, 1, 1, second=i, tzinfo=UTC)
        store.append_normalized(
            TradeEvent(
                event_id=EventId(f"t-{i}"),
                trade=Trade(
                    instrument_id=InstrumentId(
                        venue=Venue.KALSHI, market_id="M-1", outcome_id=None
                    ),
                    side=OutcomeSide.YES,
                    price=Decimal(price),
                    quantity=Decimal("20"),
                    trade_id=f"tv-{i}",
                    exchange_ts=trade_at,
                    received_at=trade_at,
                ),
                provenance=EventProvenance(source="fixture", channel="trade", venue=Venue.KALSHI),
            )
        )
    store.flush()

    out_path = tmp_path / "report.json"
    rc = cli(
        [
            "backtest",
            "--strategy",
            str(REPO_ROOT / "configs/strategies/example-threshold.toml"),
            "--sleeve",
            str(REPO_ROOT / "configs/sleeves/example-kalshi-paper.toml"),
            "--data",
            str(data_dir),
            "--starting-equity",
            "1000",
            "--out",
            str(out_path),
        ]
    )
    assert rc == 0

    out_payload = json.loads(out_path.read_text())
    # Full BacktestReport surface area.
    for field in (
        "strategy_id",
        "sleeve_id",
        "events_processed",
        "intents_dispatched",
        "fills",
        "fill_rate",
        "realized_pnl",
        "unrealized_pnl",
        "total_pnl",
        "total_fees_paid",
        "peak_equity",
        "trough_equity",
        "max_drawdown",
        "open_positions",
        "rejection_reasons",
    ):
        assert field in out_payload, f"missing field: {field}"

    assert out_payload["events_processed"] == 3
    assert out_payload["intents_dispatched"] >= 1
