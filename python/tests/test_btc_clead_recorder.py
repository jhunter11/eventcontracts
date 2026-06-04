from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "python" / "scripts" / "btc_clead_recorder.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("btc_clead_recorder_script", SCRIPT)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_no_network_writes_timing_fixture_with_lead_label(tmp_path: Path) -> None:
    script = _load_script()
    ledger = tmp_path / "btc_clead_selftest.jsonl"

    exit_code = script.main(["--no-network", "--ledger", str(ledger)])

    assert exit_code == 0
    rows = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["row_type"] == "no_network_self_test"
    assert rows[0]["lead_label"] == "official_rti_fixture"
    assert rows[0]["lead_reason"] == "measured"
    assert rows[0]["decision"]["reason"] == "paper_candidate"
