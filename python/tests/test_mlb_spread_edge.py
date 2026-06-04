from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "mlb_spread_edge.py"


def _load_script() -> Any:
    spec = importlib.util.spec_from_file_location("mlb_spread_edge_test_module", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_bench_no_network_writes_compute_summary(tmp_path: Path) -> None:
    script: ModuleType = _load_script()
    report_json = tmp_path / "bench.json"
    report_md = tmp_path / "bench.md"

    exit_code = script.main(
        [
            "bench",
            "--no-network",
            "--compute-iterations",
            "5",
            "--report-json",
            str(report_json),
            "--report-md",
            str(report_md),
        ]
    )

    assert exit_code == 0
    payload = json.loads(report_json.read_text(encoding="utf-8"))
    assert payload["summary"]["compute_iterations"] == 5
    assert payload["summary"]["compute_eval_median_ms"] >= 0.0
    assert payload["summary"]["conclusion"] == "compute_measured_no_network"
    assert report_md.exists()


def test_markout_no_network_writes_horizon_metadata_and_ledger(tmp_path: Path) -> None:
    script: ModuleType = _load_script()
    entry_json = tmp_path / "entry.json"
    entry_md = tmp_path / "entry.md"
    signals_jsonl = tmp_path / "signals.jsonl"
    markout_json = tmp_path / "markout.json"
    markout_md = tmp_path / "markout.md"
    ledger_jsonl = tmp_path / "markout-ledger.jsonl"

    assert script.main(
        [
            "validate-once",
            "--no-network",
            "--min-net-edge",
            "0.0",
            "--min-executable-size",
            "0",
            "--max-source-age-seconds",
            "999999",
            "--report-json",
            str(entry_json),
            "--report-md",
            str(entry_md),
            "--signals-jsonl-out",
            str(signals_jsonl),
        ]
    ) == 0

    assert script.main(
        [
            "markout",
            "--no-network",
            "--entry-report-json",
            str(entry_json),
            "--horizon-seconds",
            "60",
            "--markout-label",
            "plus_60s",
            "--markout-ledger-jsonl-out",
            str(ledger_jsonl),
            "--report-json",
            str(markout_json),
            "--report-md",
            str(markout_md),
        ]
    ) == 0

    payload = json.loads(markout_json.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "mlb-spread-markout-v2"
    assert payload["summary"]["label"] == "plus_60s"
    assert payload["summary"]["target_horizon_seconds"] == 60.0
    assert payload["summary"]["entry_age_seconds"] >= 0.0
    assert ledger_jsonl.exists()
    ledger_rows = [json.loads(line) for line in ledger_jsonl.read_text(encoding="utf-8").splitlines()]
    assert len(ledger_rows) == payload["summary"]["markout_rows"]
    assert ledger_rows[0]["label"] == "plus_60s"


def test_markout_horizons_no_network_writes_bundle_and_reports(tmp_path: Path) -> None:
    script: ModuleType = _load_script()
    entry_json = tmp_path / "entry.json"
    entry_md = tmp_path / "entry.md"
    signals_jsonl = tmp_path / "signals.jsonl"
    report_dir = tmp_path / "horizons"
    bundle_json = tmp_path / "horizons.json"
    bundle_md = tmp_path / "horizons.md"
    ledger_jsonl = tmp_path / "horizons-ledger.jsonl"

    assert script.main(
        [
            "validate-once",
            "--no-network",
            "--min-net-edge",
            "0.0",
            "--min-executable-size",
            "0",
            "--max-source-age-seconds",
            "999999",
            "--report-json",
            str(entry_json),
            "--report-md",
            str(entry_md),
            "--signals-jsonl-out",
            str(signals_jsonl),
        ]
    ) == 0

    assert script.main(
        [
            "markout-horizons",
            "--no-network",
            "--entry-report-json",
            str(entry_json),
            "--horizons-seconds",
            "0,0.001",
            "--max-wait-seconds",
            "1",
            "--markout-ledger-jsonl-out",
            str(ledger_jsonl),
            "--report-dir",
            str(report_dir),
            "--report-prefix",
            "fixture-markout",
            "--report-json",
            str(bundle_json),
            "--report-md",
            str(bundle_md),
        ]
    ) == 0

    bundle = json.loads(bundle_json.read_text(encoding="utf-8"))
    assert bundle["summary"]["horizons_requested"] == 2
    assert bundle["summary"]["horizons_collected"] == 2
    report_paths = [Path(row["report_json"]) for row in bundle["reports"]]
    assert all(path.exists() for path in report_paths)
    assert ledger_jsonl.exists()
    assert bundle_md.exists()


def test_orderbook_fetch_dedupes_tickers_with_concurrency() -> None:
    script: Any = _load_script()
    calls: list[str] = []

    def fake_fetch_json(url: str) -> dict[str, object]:
        calls.append(url)
        ticker = url.rsplit("/", 2)[-2]
        return {"orderbook": {"ticker": ticker}}

    original = script._fetch_json
    try:
        script._fetch_json = fake_fetch_json
        orderbooks = script._fetch_orderbooks(
            ["KXMLBSPREAD-A", "KXMLBSPREAD-B", "KXMLBSPREAD-A", ""],
            pause_seconds=0.0,
            concurrency=4,
        )
    finally:
        script._fetch_json = original

    assert set(orderbooks) == {"KXMLBSPREAD-A", "KXMLBSPREAD-B"}
    assert len(calls) == 2


def test_entry_candidate_tickers_dedupes_validation_payload() -> None:
    script: Any = _load_script()
    payload = {
        "reports": [
            {
                "decisions": [
                    {"ticker": "KXMLBSPREAD-A", "candidate": True},
                    {"ticker": "KXMLBSPREAD-B", "candidate": False},
                    {"ticker": "KXMLBSPREAD-A", "candidate": True},
                ]
            },
            {"decisions": [{"ticker": "KXMLBSPREAD-C", "candidate": True}]},
        ]
    }

    assert script._entry_candidate_tickers(payload) == ("KXMLBSPREAD-A", "KXMLBSPREAD-C")


def test_timestamp_audit_no_network_reports_proxy_only(tmp_path: Path) -> None:
    script: ModuleType = _load_script()
    report_json = tmp_path / "timestamp-audit.json"
    report_md = tmp_path / "timestamp-audit.md"

    exit_code = script.main(
        [
            "timestamp-audit",
            "--no-network",
            "--report-json",
            str(report_json),
            "--report-md",
            str(report_md),
        ]
    )

    assert exit_code == 0
    payload = json.loads(report_json.read_text(encoding="utf-8"))
    assert payload["summary"]["upstream_timestamp_fields"] == 0
    assert payload["summary"]["decision"] == "proxy_only:no_upstream_odds_timestamp_field"
    assert report_md.exists()


def test_readiness_blocks_missing_source_timestamp_and_negative_markout(tmp_path: Path) -> None:
    script: ModuleType = _load_script()
    validation_json = tmp_path / "validation.json"
    markout_json = tmp_path / "markout.json"
    settlement_json = tmp_path / "settlement.json"
    bench_json = tmp_path / "bench.json"
    signals_jsonl = tmp_path / "signals.jsonl"
    readiness_json = tmp_path / "readiness.json"
    readiness_md = tmp_path / "readiness.md"

    _write_json(
        validation_json,
        {
            "summary": {"candidates": 1, "candidate_expected_profit_dollars": 12.5},
            "reports": [
                {
                    "config": {"require_source_timestamp": False},
                    "game": {"completed": False, "status_state": "in"},
                    "decisions": [
                        {
                            "candidate": True,
                            "source_age_seconds": None,
                            "source_timestamp_basis": "espn_api_received_at_no_odds_last_modified",
                        }
                    ],
                }
            ],
        },
    )
    _write_json(
        markout_json,
        {
            "summary": {
                "markout_rows": 17,
                "positive_markouts": 0,
                "mean_markout_after_entry_fee": -0.036,
            }
        },
    )
    _write_json(
        settlement_json,
        {"summary": {"settled_rows": 17, "mean_pnl_after_entry_fee": 0.081}},
    )
    _write_json(
        bench_json,
        {"summary": {"end_to_end_ms": 350.0, "compute_eval_median_ms": 0.01}},
    )
    signals_jsonl.write_text(
        json.dumps(
            {
                "as_of": "2026-06-04T00:00:00+00:00",
                "market_id": "KXMLBSPREAD-FIXTURE",
                "probability": 0.62,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    exit_code = script.main(
        [
            "readiness",
            "--validation-report-json",
            str(validation_json),
            "--markout-report-json",
            str(markout_json),
            "--settlement-report-json",
            str(settlement_json),
            "--bench-report-json",
            str(bench_json),
            "--signals-jsonl",
            str(signals_jsonl),
            "--report-json",
            str(readiness_json),
            "--report-md",
            str(readiness_md),
            "--allow-not-ready",
        ]
    )

    assert exit_code == 0
    payload = json.loads(readiness_json.read_text(encoding="utf-8"))
    assert payload["summary"]["production_ready"] is False
    assert "source_timestamp_required_by_validation" in payload["summary"]["blockers"]
    assert "upstream_source_timestamps_present" in payload["summary"]["blockers"]
    assert "positive_markout" in payload["summary"]["blockers"]
    assert "positive_settlement" in payload["summary"]["blockers"]
    assert "external_signal_payload_compatible" not in payload["summary"]["blockers"]
    assert readiness_md.exists()


def test_readiness_accepts_empty_signals_for_zero_candidate_source_gated_packet(tmp_path: Path) -> None:
    script: ModuleType = _load_script()
    validation_json = tmp_path / "validation.json"
    bench_json = tmp_path / "bench.json"
    signals_jsonl = tmp_path / "signals.jsonl"
    readiness_json = tmp_path / "readiness.json"
    readiness_md = tmp_path / "readiness.md"

    _write_json(
        validation_json,
        {
            "summary": {"candidates": 0, "candidate_expected_profit_dollars": 0.0},
            "reports": [
                {
                    "config": {"require_source_timestamp": True},
                    "game": {"completed": False, "status_state": "in"},
                    "decisions": [
                        {
                            "candidate": False,
                            "net_edge": 0.08,
                            "reason": "source_timestamp_missing",
                            "source_age_seconds": None,
                            "source_timestamp_basis": "espn_api_received_at_no_odds_last_modified",
                        }
                    ],
                }
            ],
        },
    )
    _write_json(
        bench_json,
        {
            "counts": {"orderbooks": 0, "orderbooks_requested": 0, "preliminary_candidates": 0},
            "summary": {"end_to_end_ms": 745.0, "compute_eval_median_ms": 0.3},
        },
    )
    signals_jsonl.write_text("", encoding="utf-8")

    exit_code = script.main(
        [
            "readiness",
            "--validation-report-json",
            str(validation_json),
            "--markout-report-json",
            str(tmp_path / "missing-markout.json"),
            "--settlement-report-json",
            str(tmp_path / "missing-settlement.json"),
            "--bench-report-json",
            str(bench_json),
            "--signals-jsonl",
            str(signals_jsonl),
            "--report-json",
            str(readiness_json),
            "--report-md",
            str(readiness_md),
            "--allow-not-ready",
        ]
    )

    assert exit_code == 0
    payload = json.loads(readiness_json.read_text(encoding="utf-8"))
    blockers = set(payload["summary"]["blockers"])
    assert "fee_net_candidates_present" in blockers
    assert "upstream_source_timestamps_present" in blockers
    assert "external_signal_payload_compatible" not in blockers
    assert "executable_depth_bench_present" not in blockers
    assert "source_timestamp_required_by_validation" not in blockers
