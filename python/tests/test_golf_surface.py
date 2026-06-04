"""Golf multi-outcome surface tests."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from eventcontracts.research.golf_surface import (
    GolfMultiOutcomeSurfaceModel,
    GolfSurfaceConfig,
    GolfTopNArbConfig,
    SurfaceShadowIntent,
    apply_surface_update,
    evaluate_surface_markouts,
    fixture_surface_markets,
    fixture_surface_quotes,
    fixture_surface_state,
    fixture_topn_arb_inputs,
    read_surface_intents_jsonl,
    read_surface_markets_csv,
    read_surface_quotes_csv,
    read_surface_state_json,
    read_surface_ws_quote_timeline_jsonl,
    run_async_surface_fixture,
    scan_golf_topn_arbitrage,
    write_fixture_surface_inputs,
    write_fixture_surface_markout_inputs,
    write_golf_topn_arb_outputs,
    write_surface_markout_outputs,
    write_surface_outputs,
)
from tests.conftest import REPO_ROOT


def test_golf_surface_probabilities_are_coherent() -> None:
    state = fixture_surface_state()
    config = GolfSurfaceConfig(simulations=1200, seed=7, top_n_values=(1, 3, 5))
    prediction = GolfMultiOutcomeSurfaceModel(config).predict(state)

    assert sum(prediction.cut_line_probabilities.values()) == pytest.approx(1.0)
    assert prediction.make_cut_probabilities["scottie"] > prediction.make_cut_probabilities["longshot"]
    for player_id in prediction.make_cut_probabilities:
        top1 = prediction.top_n_probabilities[1][player_id]
        top3 = prediction.top_n_probabilities[3][player_id]
        top5 = prediction.top_n_probabilities[5][player_id]
        assert 0.0 <= top1 <= top3 <= top5 <= 1.0


def test_golf_surface_scans_fee_net_candidates() -> None:
    state = fixture_surface_state()
    config = GolfSurfaceConfig(simulations=1000, seed=3, min_net_edge=0.01)

    prediction = GolfMultiOutcomeSurfaceModel(config).predict(
        state,
        markets=fixture_surface_markets(),
        quotes=fixture_surface_quotes(state.as_of),
    )

    assert prediction.probabilities
    assert prediction.candidates
    assert any(candidate.candidate for candidate in prediction.candidates)
    assert "shadow" in prediction.decision_gate.lower()


def test_golf_surface_availability_context_moves_projection() -> None:
    state = fixture_surface_state()
    hurt_state = replace(
        state,
        players=tuple(
            replace(
                player,
                injury_strokes_per_round=1.1,
                rest_fatigue_strokes_per_round=0.3,
                caddie_absence_strokes_per_round=0.2,
            )
            if player.player_id == "scottie"
            else player
            for player in state.players
        ),
    )
    config = GolfSurfaceConfig(simulations=1200, seed=19, top_n_values=(5, 10, 20))

    base = GolfMultiOutcomeSurfaceModel(config).predict(state)
    hurt = GolfMultiOutcomeSurfaceModel(config).predict(hurt_state)

    assert hurt.player_context_strokes_per_round["scottie"] == pytest.approx(1.6)
    assert hurt.expected_finish_scores["scottie"] > base.expected_finish_scores["scottie"] + 3.0
    assert hurt.top_n_probabilities[5]["scottie"] < base.top_n_probabilities[5]["scottie"]


def test_golf_surface_rejects_label_fields_in_updates() -> None:
    state = fixture_surface_state()

    with pytest.raises(ValueError, match="label or settlement"):
        apply_surface_update(state, {"as_of": state.as_of.isoformat(), "settlement": 1})


def test_async_golf_surface_fixture_recomputes() -> None:
    predictions = asyncio.run(
        run_async_surface_fixture(iterations=3, config=GolfSurfaceConfig(simulations=700, seed=11))
    )

    assert len(predictions) == 3
    assert predictions[0].as_of < predictions[-1].as_of
    assert predictions[-1].probabilities


def test_golf_surface_writes_reports_and_intents(tmp_path: Path) -> None:
    state = fixture_surface_state()
    prediction = GolfMultiOutcomeSurfaceModel(GolfSurfaceConfig(simulations=700, seed=5, min_net_edge=0.01)).predict(
        state,
        markets=fixture_surface_markets(),
        quotes=fixture_surface_quotes(state.as_of),
    )

    write_surface_outputs(
        prediction,
        report_json=tmp_path / "surface.json",
        report_md=tmp_path / "surface.md",
        intents_jsonl=tmp_path / "intents.jsonl",
    )

    payload = json.loads((tmp_path / "surface.json").read_text(encoding="utf-8"))
    assert payload["tournament_id"] == "USO26"
    assert "Golf Multi-Outcome Surface" in (tmp_path / "surface.md").read_text(encoding="utf-8")
    assert (tmp_path / "intents.jsonl").read_text(encoding="utf-8").strip()


def test_golf_surface_cli_no_network(tmp_path: Path) -> None:
    command = [
        sys.executable,
        str(REPO_ROOT / "python" / "scripts" / "golf_surface.py"),
        "surface-once",
        "--no-network",
        "--simulations",
        "700",
        "--report-json",
        str(tmp_path / "surface.json"),
        "--report-md",
        str(tmp_path / "surface.md"),
        "--intents-jsonl",
        str(tmp_path / "intents.jsonl"),
    ]

    completed = subprocess.run(command, cwd=REPO_ROOT, check=False, capture_output=True, text=True)

    assert completed.returncode == 0, completed.stderr
    payload = json.loads((tmp_path / "surface.json").read_text(encoding="utf-8"))
    assert payload["probabilities"]
    assert "Golf Multi-Outcome Surface" in (tmp_path / "surface.md").read_text(encoding="utf-8")


def test_golf_surface_cli_accepts_file_inputs(tmp_path: Path) -> None:
    paths = write_fixture_surface_inputs(tmp_path / "inputs")
    state = read_surface_state_json(Path(paths["state_json"]))
    markets = read_surface_markets_csv(Path(paths["markets_csv"]))
    quotes = read_surface_quotes_csv(Path(paths["quotes_csv"]))
    assert state.players
    assert markets
    assert quotes
    command = [
        sys.executable,
        str(REPO_ROOT / "python" / "scripts" / "golf_surface.py"),
        "surface-once",
        "--no-network",
        "--simulations",
        "700",
        "--state-json",
        paths["state_json"],
        "--markets-csv",
        paths["markets_csv"],
        "--quotes-csv",
        paths["quotes_csv"],
        "--report-json",
        str(tmp_path / "surface.json"),
        "--report-md",
        str(tmp_path / "surface.md"),
        "--intents-jsonl",
        str(tmp_path / "intents.jsonl"),
    ]

    completed = subprocess.run(command, cwd=REPO_ROOT, check=False, capture_output=True, text=True)

    assert completed.returncode == 0, completed.stderr
    payload = json.loads((tmp_path / "surface.json").read_text(encoding="utf-8"))
    assert payload["probabilities"]
    assert payload["candidates"]


def test_golf_surface_ws_rows_convert_to_book_quotes(tmp_path: Path) -> None:
    raw_path = tmp_path / "ws.jsonl"
    raw_path.write_text(
        json.dumps(_ws_orderbook_row("KXPGATOP10-FIXTURE-PLAYER", yes_bid=0.27, no_bid=0.61)) + "\n",
        encoding="utf-8",
    )

    quotes = read_surface_ws_quote_timeline_jsonl(raw_path)

    assert len(quotes) == 1
    assert quotes[0].market_ticker == "KXPGATOP10-FIXTURE-PLAYER"
    assert quotes[0].yes_bid == pytest.approx(0.27)
    assert quotes[0].yes_ask == pytest.approx(0.39)
    assert quotes[0].source == "kalshi_ws_orderbook_snapshot"


def test_golf_surface_markout_fixture_finds_positive_clv(tmp_path: Path) -> None:
    paths = write_fixture_surface_markout_inputs(tmp_path / "fixture")
    candidates = read_surface_intents_jsonl(Path(paths["intents_jsonl"]))
    quotes = read_surface_ws_quote_timeline_jsonl(Path(paths["ws_raw_jsonl"]))

    report = evaluate_surface_markouts(
        candidates,
        quotes,
        horizons_seconds=(300,),
        min_markout_rows=1,
    )

    assert report.candidate_count >= 1
    assert report.markout_count >= 1
    assert report.summaries[0].mean_clv is not None and report.summaries[0].mean_clv > 0.0
    assert "settlement" in report.decision_gate


def test_golf_surface_markout_writes_reports(tmp_path: Path) -> None:
    intent = SurfaceShadowIntent(
        market_ticker="KXPGATOP10-FIXTURE-PLAYER",
        decision_time=fixture_surface_state().as_of,
        side="YES",
        executable_price=0.30,
        fee=0.02,
        expected_net_edge=0.04,
        fair_yes_probability=0.37,
    )
    quotes_path = tmp_path / "ws.jsonl"
    quotes_path.write_text(
        "\n".join(
            [
                json.dumps(_ws_orderbook_row(intent.market_ticker, yes_bid=0.25, no_bid=0.68)),
                json.dumps(
                    _ws_orderbook_row(
                        intent.market_ticker,
                        received_at=300,
                        yes_bid=0.36,
                        no_bid=0.62,
                    )
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    report = evaluate_surface_markouts(
        (intent,),
        read_surface_ws_quote_timeline_jsonl(quotes_path),
        horizons_seconds=(300,),
        min_markout_rows=1,
    )

    write_surface_markout_outputs(
        report,
        report_json=tmp_path / "markout.json",
        report_md=tmp_path / "markout.md",
        markouts_jsonl=tmp_path / "markouts.jsonl",
    )

    payload = json.loads((tmp_path / "markout.json").read_text(encoding="utf-8"))
    assert payload["markout_count"] == 1
    assert "Golf Surface Markout" in (tmp_path / "markout.md").read_text(encoding="utf-8")
    assert (tmp_path / "markouts.jsonl").read_text(encoding="utf-8").strip()


def test_golf_surface_cli_markout_no_network(tmp_path: Path) -> None:
    command = [
        sys.executable,
        str(REPO_ROOT / "python" / "scripts" / "golf_surface.py"),
        "markout",
        "--no-network",
        "--horizons-seconds",
        "300",
        "--min-markout-rows",
        "1",
        "--report-json",
        str(tmp_path / "markout.json"),
        "--report-md",
        str(tmp_path / "markout.md"),
        "--markouts-jsonl",
        str(tmp_path / "markouts.jsonl"),
    ]

    completed = subprocess.run(command, cwd=REPO_ROOT, check=False, capture_output=True, text=True)

    assert completed.returncode == 0, completed.stderr
    payload = json.loads((tmp_path / "markout.json").read_text(encoding="utf-8"))
    assert payload["markout_count"] >= 1


def test_golf_topn_arb_fixture_detects_dominance_candidate() -> None:
    markets, quotes = fixture_topn_arb_inputs()

    report = scan_golf_topn_arbitrage(
        markets,
        quotes,
        config=GolfTopNArbConfig(min_net_edge=0.01, min_executable_size=10.0),
    )

    assert report.pair_count == 1
    assert report.candidate_count == 1
    candidate = report.candidates[0]
    assert candidate.lower_top_n == 5
    assert candidate.higher_top_n == 20
    assert candidate.fee_net_floor > 0.01
    assert candidate.executable_size == pytest.approx(80.0)
    assert "paper" in report.decision_gate


def test_golf_topn_arb_coherent_fixture_has_no_candidate() -> None:
    state = fixture_surface_state()

    report = scan_golf_topn_arbitrage(
        fixture_surface_markets(),
        fixture_surface_quotes(state.as_of),
        config=GolfTopNArbConfig(min_net_edge=0.0),
        as_of=state.as_of,
    )

    assert report.pair_count == 3
    assert report.candidate_count == 0
    assert report.max_fee_net_floor is not None and report.max_fee_net_floor < 0.0


def test_golf_topn_arb_writes_reports(tmp_path: Path) -> None:
    markets, quotes = fixture_topn_arb_inputs()
    report = scan_golf_topn_arbitrage(markets, quotes, config=GolfTopNArbConfig(min_net_edge=0.01))

    write_golf_topn_arb_outputs(
        report,
        report_json=tmp_path / "topn.json",
        report_md=tmp_path / "topn.md",
        candidates_jsonl=tmp_path / "topn.jsonl",
    )

    payload = json.loads((tmp_path / "topn.json").read_text(encoding="utf-8"))
    assert payload["candidate_count"] == 1
    assert "Golf Top-N Dominance Scan" in (tmp_path / "topn.md").read_text(encoding="utf-8")
    assert (tmp_path / "topn.jsonl").read_text(encoding="utf-8").strip()


def test_golf_topn_arb_cli_no_network(tmp_path: Path) -> None:
    command = [
        sys.executable,
        str(REPO_ROOT / "python" / "scripts" / "golf_surface.py"),
        "topn-arb",
        "--no-network",
        "--min-net-edge",
        "0.01",
        "--report-json",
        str(tmp_path / "topn.json"),
        "--report-md",
        str(tmp_path / "topn.md"),
        "--candidates-jsonl",
        str(tmp_path / "topn.jsonl"),
    ]

    completed = subprocess.run(command, cwd=REPO_ROOT, check=False, capture_output=True, text=True)

    assert completed.returncode == 0, completed.stderr
    payload = json.loads((tmp_path / "topn.json").read_text(encoding="utf-8"))
    assert payload["candidate_count"] == 1
    assert "Golf Top-N Dominance Scan" in (tmp_path / "topn.md").read_text(encoding="utf-8")


def _ws_orderbook_row(
    market_ticker: str,
    *,
    received_at: int = 0,
    yes_bid: float,
    no_bid: float,
) -> dict[str, object]:
    base = fixture_surface_state().as_of
    return {
        "channel": "orderbook_snapshot",
        "received_at": (base + timedelta(seconds=received_at)).isoformat(),
        "schema_version": "kalshi-ws-v1",
        "source": "kalshi-ws",
        "venue": "kalshi",
        "metadata": {"ws_type": "orderbook_snapshot"},
        "payload": {
            "type": "orderbook_snapshot",
            "msg": {
                "type": "orderbook_snapshot",
                "market_ticker": market_ticker,
                "yes_dollars_fp": [[f"{yes_bid:.4f}", "100.00"]],
                "no_dollars_fp": [[f"{no_bid:.4f}", "100.00"]],
            },
        },
    }
