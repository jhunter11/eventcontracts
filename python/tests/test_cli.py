"""CLI smoke tests: check-config, validate-config, validate-bundle, replay."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from decimal import Decimal
from io import StringIO
from pathlib import Path

import pytest

from eventcontracts.cli import main as _main_fn
from eventcontracts.cli import build_parser


def cli(argv: list[str]) -> int:
    return _main_fn(argv)
from eventcontracts.domain.events import EventProvenance, TradeEvent
from eventcontracts.domain.ids import EventId
from eventcontracts.domain.models import InstrumentId, OutcomeSide, Trade, Venue
from eventcontracts.storage import ParquetEventStore

from tests.conftest import REPO_ROOT


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


def test_replay_streams_normalized_events(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    store = ParquetEventStore(tmp_path)
    for i in range(3):
        store.append_normalized(
            TradeEvent(
                event_id=EventId(f"t-{i}"),
                trade=Trade(
                    instrument_id=InstrumentId(venue=Venue.KALSHI, market_id="M-1", outcome_id=None),
                    side=OutcomeSide.YES,
                    price=Decimal("0.50"),
                    quantity=Decimal("1"),
                    trade_id=None,
                    exchange_ts=datetime(2026, 1, 1, second=i, tzinfo=timezone.utc),
                    received_at=datetime(2026, 1, 1, second=i, tzinfo=timezone.utc),
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
