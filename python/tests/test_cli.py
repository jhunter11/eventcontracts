"""CLI smoke tests: check-config, validate-config, validate-bundle, replay."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from eventcontracts.cli import main as _main_fn
from eventcontracts.domain.events import EventProvenance, QuoteEvent, TradeEvent
from eventcontracts.domain.ids import EventId
from eventcontracts.domain.models import (
    InstrumentId,
    OrderBookLevel,
    OutcomeSide,
    Quote,
    Trade,
    Venue,
)
from eventcontracts.storage import ParquetEventStore
from tests.conftest import REPO_ROOT


def cli(argv: list[str]) -> int:
    return _main_fn(argv)


def _quote_event(i: int, ts: datetime) -> QuoteEvent:
    instrument = InstrumentId(venue=Venue.KALSHI, market_id="M-1", outcome_id=None)
    return QuoteEvent(
        event_id=EventId(f"q-{i}"),
        quote=Quote(
            instrument_id=instrument,
            side=OutcomeSide.YES,
            bid=OrderBookLevel(price=Decimal("0.39"), quantity=Decimal("100")),
            ask=OrderBookLevel(price=Decimal("0.40"), quantity=Decimal("100")),
            exchange_ts=ts,
            received_at=ts,
        ),
        provenance=EventProvenance(
            source="fixture",
            channel="quote",
            venue=Venue.KALSHI,
            source_sequence=f"q-{i}",
        ),
    )


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


def test_new_strategy_scaffold_and_verify(tmp_path: Path) -> None:
    rc = cli(
        [
            "new-strategy",
            "demo-edge",
            "--archetype",
            "external_edge",
            "--root",
            str(tmp_path),
        ]
    )
    assert rc == 0

    assert (tmp_path / "configs/strategies/demo-edge.toml").is_file()
    assert (tmp_path / "configs/sleeves/demo-edge-kalshi-paper-a.toml").is_file()
    assert (tmp_path / "contracts/parity/demo_edge").is_dir()

    # A freshly-scaffolded strategy has NO parity cases, so it is not promotable
    # yet — verify must FAIL rather than give a false green on an empty dir.
    assert cli(["verify-strategy", "demo-edge", "--root", str(tmp_path), "--skip-parity"]) == 1

    # Adding a parity case file satisfies the structural promotion checks. The
    # placeholder case is not a runnable event stream, so the no-trade smoke is
    # skipped here (it has dedicated coverage in test_live_readiness_smoke.py).
    (tmp_path / "contracts/parity/demo_edge/01_case.json").write_text("{}", encoding="utf-8")
    assert (
        cli(
            [
                "verify-strategy",
                "demo-edge",
                "--root",
                str(tmp_path),
                "--skip-parity",
                "--skip-smoke",
            ]
        )
        == 0
    )


def test_verify_strategy_rejects_non_promotable_archetype(tmp_path: Path) -> None:
    # `scalper` is scaffoldable but has no Rust runtime, so it cannot be promoted
    # to live even with a parity case present.
    assert (
        cli(["new-strategy", "demo-scalp", "--archetype", "scalper", "--root", str(tmp_path)]) == 0
    )
    (tmp_path / "contracts/parity/demo_scalp/01_case.json").write_text("{}", encoding="utf-8")
    assert cli(["verify-strategy", "demo-scalp", "--root", str(tmp_path), "--skip-parity"]) == 1


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


def test_sports_golf_preflight_reports_free_key_status(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NOAA_TOKEN", "token")
    monkeypatch.delenv("DATAGOLF_API_KEY", raising=False)

    rc = cli(["sports-golf-preflight", "--strict", "--configs-root", str(REPO_ROOT / "configs")])
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert payload["required_missing"] == []
    assert payload["free_research_keys"]["NOAA_TOKEN"] is True
    assert payload["sports_golf_optional_provider_keys"]["DATAGOLF_API_KEY"] is False
    assert payload["configs"]["strategy:player_cut"]["loaded"] is True


def test_sports_golf_preflight_can_require_sports_provider(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    for key in ("DATAGOLF_API_KEY", "PGA_TOUR_API_KEY", "SHOTLINK_API_KEY"):
        monkeypatch.delenv(key, raising=False)

    rc = cli(["sports-golf-preflight", "--require-sports-provider", "--configs-root", str(REPO_ROOT / "configs")])
    payload = json.loads(capsys.readouterr().out)

    assert rc == 2
    assert not any(payload["sports_golf_optional_provider_keys"].values())


def test_sports_golf_smoke_generates_reports(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)

    rc = cli(
        [
            "sports-golf-smoke",
            "--configs-root",
            str(REPO_ROOT / "configs"),
            "--out",
            str(tmp_path / "sports-smoke"),
            "--simulations",
            "50",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert Path(payload["manifest"]).exists()
    assert Path(payload["player_report"]).exists()
    assert Path(payload["cut_line_report"]).exists()
    assert payload["player_cut_orders"] > 0
    assert payload["cut_line_orders"] > 0


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
        store.append_normalized(_quote_event(i, trade_at))
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

    assert out_payload["events_processed"] == 6
    assert out_payload["intents_dispatched"] >= 1
    assert out_payload["started_at"] == "2026-01-01T00:00:00+00:00"
    assert out_payload["ended_at"] == "2026-01-01T00:00:02+00:00"
    assert out_payload["duration_seconds"] == 2.0
