from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "noncrypto_ev_lab.py"
spec = importlib.util.spec_from_file_location("noncrypto_ev_lab", SCRIPT_PATH)
assert spec is not None and spec.loader is not None
lab = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = lab
spec.loader.exec_module(lab)


def test_tennis_fixture_scores_fee_net_candidate(tmp_path: Path) -> None:
    out = tmp_path / "result.json"
    ledger = tmp_path / "ledger.jsonl"
    exit_code = lab.main(
        [
            "tennis-bundle",
            "--no-network",
            "--out",
            str(out),
            "--ledger",
            str(ledger),
            "--min-net-edge",
            "0.015",
        ]
    )

    assert exit_code == 0
    assert out.exists()
    assert ledger.exists()
    result = out.read_text(encoding="utf-8")
    assert '"tick_logging_recommended": true' in result
    assert "paper_candidate" in ledger.read_text(encoding="utf-8")


def test_tennis_objectives_collapse_to_sharp_only_without_model(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "manifest.json").write_text(
        """
        {"matches": [{
          "market_id": "KXTENNIS-FIXTURE-A",
          "p1": "Carlos Alcaraz",
          "p2": "Novak Djokovic",
          "p1_odds": 1.60,
          "p2_odds": 2.40,
          "commence_time": "2026-06-03T18:00:00Z",
          "kalshi_yes_bid": 0.58,
          "kalshi_yes_ask": 0.60
        }]}
        """,
        encoding="utf-8",
    )
    (bundle / "snapshots.jsonl").write_text(
        '{"market_id":"KXTENNIS-FIXTURE-A","match_id":"m","match_date":"2026-06-03",'
        '"p1_id":"1","p2_id":"2","p1_name":"Carlos Alcaraz","p2_name":"Novak Djokovic",'
        '"p1_decimal_odds":1.6,"p2_decimal_odds":2.4}\n',
        encoding="utf-8",
    )

    result = lab.score_tennis_bundle(bundle, model_path=None)

    assert result["summary"]["objectives"] == 1
    assert result["rows"][0]["objective"] == "sharp_only"
