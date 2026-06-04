from __future__ import annotations

import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType

from eventcontracts.domain.models import Venue
from eventcontracts.storage.interfaces import EventEnvelope

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "python" / "scripts" / "kalshi_ws_capture.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("kalshi_ws_capture_script", SCRIPT)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_envelope_to_row_preserves_raw_ws_payload() -> None:
    script = _load_script()
    now = datetime.now(UTC) - timedelta(seconds=1)
    envelope = EventEnvelope(
        venue=Venue.KALSHI,
        source="kalshi-ws",
        channel="trade",
        received_at=now,
        exchange_ts=now,
        payload={"type": "trade", "msg": {"ticker": "KXBTC15M-TEST", "price": 50}},
        schema_version="kalshi-ws-v1",
        metadata={"sid": 1, "source_sequence": "2"},
    )

    row = script.envelope_to_row(envelope)

    assert row["venue"] == "kalshi"
    assert row["channel"] == "trade"
    assert row["payload"] == envelope.payload
    assert row["metadata"] == envelope.metadata


def test_no_network_writes_fixture_jsonl(tmp_path: Path) -> None:
    script = _load_script()

    exit_code = script.main(["--no-network", "--out", str(tmp_path), "--series-tickers", "KXBTC15M"])

    assert exit_code == 0
    raw_files = list(tmp_path.glob("run-*/raw.jsonl"))
    assert len(raw_files) == 1
    assert "KXBTC15M-SELFTEST" in raw_files[0].read_text(encoding="utf-8")
