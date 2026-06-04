"""Pre-round golf top-N research path tests."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from eventcontracts.research.golf_live_paper import (
    build_historical_golf_dataset,
    fixture_historical_inputs,
    fixture_market_payloads,
    fixture_shadow_inputs,
    map_kalshi_golf_markets,
    render_shadow_fill_summary_markdown,
    summarize_shadow_fill_ledger,
    write_shadow_fill_ledger,
)
from eventcontracts.research.golf_preround import (
    ReferenceTopNConfig,
    devig_decimal_odds,
    evaluate_reference_topn,
    fixture_kalshi_market_snapshots,
    fixture_reference_topn_inputs,
    load_preround_rows_csv,
    render_reference_topn_markdown,
    run_preround_research,
    select_best_market_structure,
    summarize_kalshi_golf_markets,
    synthetic_preround_fixture,
    write_reference_topn_outputs,
)
from eventcontracts.research.golf_preround_data import (
    build_preround_topn_dataset,
    fixture_input_rows,
    write_snapshot_rows,
)
from tests.conftest import REPO_ROOT


def test_devig_decimal_odds_normalizes_overround() -> None:
    board = devig_decimal_odds({"Scottie Scheffler": 5.5, "Rory McIlroy": 11.0, "Xander Schauffele": 23.0})

    assert board.overround > 0.0
    assert sum(board.probabilities.values()) == pytest.approx(1.0)
    assert board.probabilities["Scottie Scheffler"] > board.probabilities["Rory McIlroy"]


def test_market_discovery_selects_tournament_top_n_fixture() -> None:
    summaries = summarize_kalshi_golf_markets(fixture_kalshi_market_snapshots())
    selection = select_best_market_structure(summaries)

    assert selection.chosen.structure == "tournament_top_n"
    assert selection.chosen.quoted_markets >= 2
    assert selection.chosen.median_spread == pytest.approx(0.01)
    assert any(item.structure == "make_miss_cut" for item in selection.rejected)


def test_preround_research_runs_chronological_oos_candidates() -> None:
    report = run_preround_research(
        synthetic_preround_fixture(),
        target_top_n=20,
        simulations=400,
        seed=3,
        provider_status={"DATAGOLF_API_KEY": False, "THE_ODDS_API_KEY": False},
    )

    assert report.train_events > 0
    assert report.test_events > 0
    assert report.train_rows + report.test_rows == len(synthetic_preround_fixture())
    assert {
        "market_implied",
        "calibrated_logistic",
        "score_distribution_simulation",
        "market_odds_residual",
    }.issubset(report.metrics)
    assert report.metrics["calibrated_logistic"].n == report.test_rows
    assert "edge" not in report.decision.lower() or "no edge" in report.decision.lower()
    assert report.mutual_information
    assert report.permutation_importance


def test_preround_data_builder_enforces_point_in_time(tmp_path: Path) -> None:
    features, labels, snapshots, odds = fixture_input_rows()
    features[0] = {**features[0], "feature_as_of": "2030-01-01T00:00:00+00:00"}

    with pytest.raises(ValueError, match="after scheduled_start"):
        build_preround_topn_dataset(
            feature_rows=features,
            label_rows=labels,
            snapshot_rows=snapshots,
            odds_rows=odds,
            out=tmp_path / "bad.csv",
        )


def test_preround_data_builder_outputs_research_csv(tmp_path: Path) -> None:
    features, labels, snapshots, odds = fixture_input_rows()
    features[0] = {
        **features[0],
        "injury_strokes_per_round": 0.25,
        "rest_fatigue_strokes_per_round": 0.15,
        "caddie_absence_strokes_per_round": 0.1,
        "withdrawal_risk": 0.02,
    }
    out = tmp_path / "preround_top20.csv"

    build_report = build_preround_topn_dataset(
        feature_rows=features,
        label_rows=labels,
        snapshot_rows=snapshots,
        odds_rows=odds,
        out=out,
    )
    rows = load_preround_rows_csv(out)
    report = run_preround_research(rows, simulations=250, fixture_mode=False)

    assert build_report.rows_written == len(synthetic_preround_fixture())
    assert rows[0].market_bid is not None
    assert rows[0].odds_probability is not None
    assert rows[0].feature_value("injury_strokes_per_round") == pytest.approx(0.25)
    assert rows[0].feature_value("rest_fatigue_strokes_per_round") == pytest.approx(0.15)
    assert rows[0].feature_value("caddie_absence_strokes_per_round") == pytest.approx(0.1)
    assert rows[0].feature_value("withdrawal_risk") == pytest.approx(0.02)
    assert report.data_source == "fixture"
    assert "fixture/no-network" in report.decision


def test_golf_snapshot_writer_preserves_player_identity_fields(tmp_path: Path) -> None:
    out = tmp_path / "snapshots.csv"

    write_snapshot_rows(
        out,
        [
            {
                "captured_at": "2026-06-03T19:44:36+00:00",
                "tournament_id": "KXPGATOP20-USO26",
                "player_id": "SCHEF",
                "player_name": "Scottie Scheffler",
                "market_ticker": "KXPGATOP20-USO26-SCHEF",
                "yes_sub_title": "Scottie Scheffler",
                "no_sub_title": "Scottie Scheffler",
                "title": "Will Scottie Scheffler finish top 20?",
                "rules_primary": "Settles yes if Scottie Scheffler finishes top 20.",
                "yes_bid": 0.42,
                "yes_ask": 0.45,
            }
        ],
    )

    text = out.read_text(encoding="utf-8")
    assert "player_name" in text
    assert "Scottie Scheffler" in text
    assert "rules_primary" in text


def test_golf_preround_script_no_network_writes_report(tmp_path: Path) -> None:
    report_path = tmp_path / "report.md"
    json_path = tmp_path / "report.json"
    command = [
        sys.executable,
        str(REPO_ROOT / "python" / "scripts" / "golf_preround_research.py"),
        "--no-network",
        "--simulations",
        "250",
        "--report",
        str(report_path),
        "--json-out",
        str(json_path),
    ]

    completed = subprocess.run(command, cwd=REPO_ROOT, check=False, capture_output=True, text=True)

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["market_selection"]["chosen"]["structure"] == "tournament_top_n"
    assert "Golf Pre-Round Top-N Research" in report_path.read_text(encoding="utf-8")


def test_reference_topn_filters_tournament_and_emits_intents(tmp_path: Path) -> None:
    snapshots, odds = fixture_reference_topn_inputs()
    snapshots.append(
        {
            **snapshots[0],
            "tournament_id": "KXPGATOP5-THMTPBW26",
            "market_ticker": "KXPGATOP5-THMTPBW26-SCOTT",
            "title": "The Memorial Tournament: Will Scottie Scheffler finish top 5?",
        }
    )

    report = evaluate_reference_topn(
        snapshot_rows=snapshots,
        odds_rows=odds,
        config=ReferenceTopNConfig(simulations=700, seed=3, min_net_edge=0.0),
    )
    write_reference_topn_outputs(
        report,
        report_json=tmp_path / "reference.json",
        report_md=tmp_path / "reference.md",
        intents_jsonl=tmp_path / "intents.jsonl",
    )

    assert report.filtered_market_rows == len(snapshots) - 1
    assert report.matched_market_rows == len(snapshots) - 1
    assert report.candidate_count >= 1
    assert all("USO26" in candidate.market_ticker for candidate in report.candidates)
    assert "Golf Reference Top-N Inference" in render_reference_topn_markdown(report)
    assert (tmp_path / "intents.jsonl").read_text(encoding="utf-8").strip()


def test_reference_topn_drops_high_overround_reference_rows() -> None:
    captured_at = "2026-06-03T18:00:00+00:00"
    odds_as_of = "2026-06-03T17:30:00+00:00"
    players = [
        ("Player A", 0.14, 0.35, 0.38),
        ("Player B", 0.13, 0.33, 0.36),
        ("Player C", 0.12, 0.31, 0.34),
        ("Player D", 0.11, 0.29, 0.32),
        ("Player E", 0.10, 0.27, 0.30),
        ("Long Shot", 0.001, 0.05, 0.07),
    ]
    snapshots: list[dict[str, object]] = []
    odds: list[dict[str, object]] = []
    for name, win_prob, bid, ask in players:
        player_id = name.replace(" ", "").upper()
        snapshots.append(
            {
                "captured_at": captured_at,
                "tournament_id": "KXPGATOP5-USO26",
                "player_id": player_id,
                "player_name": name,
                "market_ticker": f"KXPGATOP5-USO26-{player_id}",
                "title": f"U.S. Open: Will {name} finish top 5?",
                "yes_bid": bid,
                "yes_ask": ask,
                "yes_bid_size": 500.0,
                "yes_ask_size": 500.0,
            }
        )
        for source in ("book-a", "book-b"):
            odds.append(
                {
                    "source": source,
                    "player_name": name,
                    "odds_as_of": odds_as_of,
                    "market_type": "outright_reference",
                    "odds_probability": win_prob,
                    "overround": 1.2,
                }
            )
    odds.append(
        {
            "source": "polluted-exchange-board",
            "player_name": "Long Shot",
            "odds_as_of": odds_as_of,
            "market_type": "outright_reference",
            "odds_probability": 0.85,
            "overround": 4.4,
        }
    )

    strict = evaluate_reference_topn(
        snapshot_rows=snapshots,
        odds_rows=odds,
        config=ReferenceTopNConfig(simulations=2000, seed=7, min_net_edge=0.0, max_book_overround=1.8),
    )
    permissive = evaluate_reference_topn(
        snapshot_rows=snapshots,
        odds_rows=odds,
        config=ReferenceTopNConfig(simulations=2000, seed=7, min_net_edge=0.0, max_book_overround=5.0),
    )

    strict_long_shot = next(item for item in strict.candidates if item.player_name == "Long Shot")
    permissive_long_shot = next(item for item in permissive.candidates if item.player_name == "Long Shot")
    assert strict_long_shot.odds_sources == 2
    assert permissive_long_shot.odds_sources == 3
    assert strict_long_shot.fair_yes_probability < permissive_long_shot.fair_yes_probability - 0.5
    assert "max_book_overround=1.800" in strict.tournament_filter


def test_golf_reference_topn_script_no_network(tmp_path: Path) -> None:
    command = [
        sys.executable,
        str(REPO_ROOT / "python" / "scripts" / "golf_reference_topn.py"),
        "evaluate",
        "--no-network",
        "--simulations",
        "700",
        "--min-net-edge",
        "0",
        "--report-json",
        str(tmp_path / "reference.json"),
        "--report-md",
        str(tmp_path / "reference.md"),
        "--intents-jsonl",
        str(tmp_path / "intents.jsonl"),
    ]

    completed = subprocess.run(command, cwd=REPO_ROOT, check=False, capture_output=True, text=True)

    assert completed.returncode == 0, completed.stderr
    payload = json.loads((tmp_path / "reference.json").read_text(encoding="utf-8"))
    assert payload["candidate_count"] >= 1
    assert "Golf Reference Top-N Inference" in (tmp_path / "reference.md").read_text(encoding="utf-8")


def test_golf_preround_data_script_no_network_builds_csv(tmp_path: Path) -> None:
    out = tmp_path / "preround_top20.csv"
    report_json = tmp_path / "build_report.json"
    command = [
        sys.executable,
        str(REPO_ROOT / "python" / "scripts" / "golf_preround_data.py"),
        "build-csv",
        "--no-network",
        "--out",
        str(out),
        "--report-json",
        str(report_json),
    ]

    completed = subprocess.run(command, cwd=REPO_ROOT, check=False, capture_output=True, text=True)

    assert completed.returncode == 0, completed.stderr
    build_payload = json.loads(report_json.read_text(encoding="utf-8"))
    rows = load_preround_rows_csv(out)
    assert build_payload["rows_written"] == len(rows)
    assert rows


def test_golf_market_mapper_handles_topn_makecut_and_cutline() -> None:
    rows = map_kalshi_golf_markets(fixture_market_payloads())

    families = {row.market_family for row in rows}
    top20 = next(row for row in rows if row.market_family == "top_n")
    cutline = next(row for row in rows if row.market_family == "cut_line")

    assert {"top_n", "make_cut", "cut_line"} <= families
    assert top20.top_n == 20
    assert top20.subject_id == "scottiescheffler"
    assert cutline.cut_line == 2
    assert cutline.cut_line_relation == "exact"


@pytest.mark.parametrize("family", ("make_cut", "cut_line"))
def test_golf_historical_importers_build_no_network_rows(tmp_path: Path, family: str) -> None:
    features, labels, snapshots = fixture_historical_inputs(family)
    out = tmp_path / f"{family}.csv"

    report = build_historical_golf_dataset(
        feature_rows=features,
        label_rows=labels,
        snapshot_rows=snapshots,
        out=out,
        market_family=family,
    )

    text = out.read_text(encoding="utf-8")
    assert report.rows_written == 1
    assert family in text
    assert "market_bid" in text


def test_golf_historical_importer_rejects_future_feature(tmp_path: Path) -> None:
    features, labels, snapshots = fixture_historical_inputs("make_cut")
    features[0] = {**features[0], "feature_as_of": "2030-01-01T00:00:00+00:00"}

    with pytest.raises(ValueError, match="after decision_time"):
        build_historical_golf_dataset(
            feature_rows=features,
            label_rows=labels,
            snapshot_rows=snapshots,
            out=tmp_path / "bad.csv",
            market_family="make_cut",
        )


def test_golf_shadow_fill_ledger_records_hypothetical_fill(tmp_path: Path) -> None:
    intents, quotes, trades, settlements = fixture_shadow_inputs()
    out = tmp_path / "shadow.jsonl"

    report = write_shadow_fill_ledger(
        intents=intents,
        quotes=quotes,
        trades=trades,
        settlements=settlements,
        out=out,
        min_net_edge=0.05,
    )
    row = json.loads(out.read_text(encoding="utf-8").splitlines()[0])

    assert report.rows_written == 1
    assert row["would_fill_now"] is True
    assert row["candidate"] is True
    assert row["fee"] > 0
    assert row["settlement_value"] == 1.0


def test_golf_shadow_fill_summary_keeps_fixture_gate(tmp_path: Path) -> None:
    intents, quotes, trades, settlements = fixture_shadow_inputs()
    out = tmp_path / "shadow.jsonl"
    write_shadow_fill_ledger(
        intents=intents,
        quotes=quotes,
        trades=trades,
        settlements=settlements,
        out=out,
        min_net_edge=0.05,
    )

    summary = summarize_shadow_fill_ledger(ledger_path=out, fixture_mode=True)
    markdown = render_shadow_fill_summary_markdown(summary)

    assert summary.rows_read == 1
    assert summary.candidate_rows == 1
    assert summary.filled_rows == 1
    assert summary.avg_markout_5m == pytest.approx(0.03)
    assert summary.avg_settlement_value == pytest.approx(1.0)
    assert "fixture/no-network" in summary.decision_gate
    assert "Golf Shadow-Fill Summary" in markdown
    assert "no-trade evidence only" in markdown


def test_golf_live_paper_script_no_network_bridge(tmp_path: Path) -> None:
    command = [
        sys.executable,
        str(REPO_ROOT / "python" / "scripts" / "golf_live_paper.py"),
        "bridge-once",
        "--no-network",
        "--out",
        str(tmp_path / "bridge"),
    ]

    completed = subprocess.run(command, cwd=REPO_ROOT, check=False, capture_output=True, text=True)

    assert completed.returncode == 0, completed.stderr
    manifest = json.loads((tmp_path / "bridge" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["no_trade"] is True
    assert (tmp_path / "bridge" / "shadow_fills.jsonl").exists()


def test_golf_live_paper_script_summarizes_shadow_fill_fixture(tmp_path: Path) -> None:
    bridge_dir = tmp_path / "bridge"
    bridge = [
        sys.executable,
        str(REPO_ROOT / "python" / "scripts" / "golf_live_paper.py"),
        "bridge-once",
        "--no-network",
        "--out",
        str(bridge_dir),
    ]
    summarize = [
        sys.executable,
        str(REPO_ROOT / "python" / "scripts" / "golf_live_paper.py"),
        "summarize-shadow-fill",
        "--ledger",
        str(bridge_dir / "shadow_fills.jsonl"),
        "--fixture-mode",
        "--report-json",
        str(tmp_path / "summary.json"),
        "--report-md",
        str(tmp_path / "summary.md"),
    ]

    bridge_result = subprocess.run(bridge, cwd=REPO_ROOT, check=False, capture_output=True, text=True)
    summary_result = subprocess.run(summarize, cwd=REPO_ROOT, check=False, capture_output=True, text=True)

    assert bridge_result.returncode == 0, bridge_result.stderr
    assert summary_result.returncode == 0, summary_result.stderr
    payload = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert payload["fixture_mode"] is True
    assert payload["candidate_rows"] == 1
    assert "fixture/no-network" in payload["decision_gate"]
    assert "Golf Shadow-Fill Summary" in (tmp_path / "summary.md").read_text(encoding="utf-8")
