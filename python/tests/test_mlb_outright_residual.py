from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from eventcontracts.research.mlb_outright_residual import (
    KalshiOutrightQuote,
    MlbOutrightValidationConfig,
    MlbSettlementOutcome,
    devig_futures_board,
    evaluate_mlb_outright_residual,
    fixture_quotes,
    fixture_references,
    fixture_signals,
    read_model_signals_jsonl,
)

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "mlb_outright_residual.py"
spec = importlib.util.spec_from_file_location("mlb_outright_residual_script", SCRIPT_PATH)
assert spec is not None and spec.loader is not None
script = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = script
spec.loader.exec_module(script)


def test_devig_futures_board_normalizes_by_group() -> None:
    refs = fixture_references()
    board = devig_futures_board(refs)
    total = sum(row.probability for row in board.values() if row.group_id == "world_series_2026")

    assert abs(total - 1.0) < 1e-9
    assert board[("world_series_2026", "dodgers")].overround > 1.0


def test_fixture_finds_fee_net_candidates_but_not_proven_without_settlement() -> None:
    report = evaluate_mlb_outright_residual(
        fixture_signals(),
        fixture_references(),
        fixture_quotes(),
        config=MlbOutrightValidationConfig(min_settlement_evidence=2),
    )

    assert sum(1 for row in report.candidates if row.candidate) == 2
    assert report.evidence.proven is False
    assert report.decision_gate.startswith("start or continue tick logging")


def test_stale_quote_blocks_candidate() -> None:
    now = datetime(2026, 6, 3, 16, 0, tzinfo=UTC)
    stale_quotes = [
        KalshiOutrightQuote(
            quote.market_id,
            now - timedelta(hours=2),
            quote.yes_bid,
            quote.yes_ask,
            quote.yes_bid_size,
            quote.yes_ask_size,
        )
        for quote in fixture_quotes(now)
    ]

    report = evaluate_mlb_outright_residual(
        fixture_signals(now),
        fixture_references(now),
        stale_quotes,
        as_of=now,
        config=MlbOutrightValidationConfig(max_quote_age_ms=1_000),
    )

    assert all(not row.candidate for row in report.candidates)
    assert {row.reason for row in report.candidates} == {"stale_quote"}


def test_model_signal_reader_rejects_settlement_fields(tmp_path: Path) -> None:
    path = tmp_path / "signals.jsonl"
    payload = {
        "market_id": "KXMLBSERIES-26-WS-DODGERS",
        "outcome_id": "dodgers",
        "group_id": "world_series_2026",
        "as_of": "2026-06-03T16:00:00Z",
        "yes_probability": 0.21,
        "confidence": 0.6,
        "days_to_settlement": 120,
        "settled_yes": True,
    }
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    try:
        read_model_signals_jsonl(path)
    except ValueError as exc:
        assert "settled_yes" in str(exc)
    else:
        raise AssertionError("expected label/settlement field rejection")


def test_synthetic_settlement_evidence_can_prove_after_sample_gate() -> None:
    signals = fixture_signals()
    quotes = fixture_quotes()
    report = evaluate_mlb_outright_residual(
        signals,
        fixture_references(),
        quotes,
        config=MlbOutrightValidationConfig(min_settlement_evidence=2),
    )
    candidate_markets = [row.market_id for row in report.candidates if row.candidate]
    settlements = [
        MlbSettlementOutcome(candidate_markets[0], "dodgers", True, closing_yes_mid=0.24),
        MlbSettlementOutcome(candidate_markets[1], "mets", False, closing_yes_mid=0.04),
    ]

    with_evidence = evaluate_mlb_outright_residual(
        signals,
        fixture_references(),
        quotes,
        settlements=settlements,
        config=MlbOutrightValidationConfig(min_settlement_evidence=2),
    )

    assert with_evidence.evidence.proven
    assert with_evidence.decision_gate.startswith("paper only")


def test_no_network_cli_writes_report_and_shadow_signals(tmp_path: Path) -> None:
    out = tmp_path / "report.json"
    md = tmp_path / "report.md"
    signals = tmp_path / "signals.jsonl"

    exit_code = script.main(
        [
            "validate-once",
            "--no-network",
            "--report-json",
            str(out),
            "--report-md",
            str(md),
            "--signals-jsonl-out",
            str(signals),
        ]
    )

    assert exit_code == 0
    assert out.exists()
    assert md.exists()
    assert signals.exists()
    result = json.loads(out.read_text(encoding="utf-8"))
    assert result["candidate_count"] == 2
    assert "markout" in result["decision_gate"]


def test_file_input_cli_scores_fixture_inputs(tmp_path: Path) -> None:
    fixture_dir = tmp_path / "fixtures"
    assert script.main(["write-fixture-inputs", "--no-network", "--out-dir", str(fixture_dir)]) == 0

    out = tmp_path / "file-report.json"
    exit_code = script.main(
        [
            "validate-once",
            "--no-network",
            "--signals-jsonl",
            str(fixture_dir / "model_signals.jsonl"),
            "--references-csv",
            str(fixture_dir / "futures_references.csv"),
            "--quotes-csv",
            str(fixture_dir / "kalshi_quotes.csv"),
            "--report-json",
            str(out),
            "--report-md",
            str(tmp_path / "file-report.md"),
            "--signals-jsonl-out",
            str(tmp_path / "file-signals.jsonl"),
        ]
    )

    assert exit_code == 0
    result = json.loads(out.read_text(encoding="utf-8"))
    assert result["reference_count"] == 9
