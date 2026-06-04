from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from eventcontracts.research.baseball_rfi import (
    RfiContextFeature,
    RfiEvaluationConfig,
    build_early_price_samples,
    evaluate_rfi_execution_filters,
    evaluate_rfi_level_edge,
    evaluate_rfi_live_markouts,
    evaluate_rfi_live_touch,
    fixture_rfi_inputs,
    read_context_csv,
    read_live_quotes_csv,
    read_live_touch_candidates_jsonl,
    read_markets_csv,
    read_trades_jsonl,
    read_ws_live_quote_timeline_jsonl,
    read_ws_live_quotes_jsonl,
    write_fixture_inputs,
    write_fixture_live_inputs,
    write_fixture_markout_inputs,
)
from eventcontracts.research.golf_surface import read_surface_markets_csv, read_surface_quotes_csv

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "baseball_rfi_edge.py"
spec = importlib.util.spec_from_file_location("baseball_rfi_edge_script", SCRIPT_PATH)
assert spec is not None and spec.loader is not None
script = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = script
spec.loader.exec_module(script)


def test_early_price_samples_are_weighted_and_chronological() -> None:
    markets, trades = fixture_rfi_inputs(n_markets=4, yes_rate=0.5, market_yes_price=0.60)
    samples = build_early_price_samples(markets, trades, config=RfiEvaluationConfig(min_train=1))

    assert len(samples) == 4
    assert samples[0].observed_at <= samples[-1].observed_at
    assert samples[0].sample_trade_count == 8
    assert 0.59 < samples[0].yes_price < 0.61


def test_rfi_level_fixture_finds_positive_nrfi_edge() -> None:
    markets, trades = fixture_rfi_inputs(n_markets=80, yes_rate=0.48, market_yes_price=0.60)
    report, bets, _samples = evaluate_rfi_level_edge(
        markets,
        trades,
        config=RfiEvaluationConfig(min_train=20, min_net_edge=0.01),
    )

    assert bets
    assert any(bet.side == "NO" for bet in bets)
    assert report.summaries[0].positive
    assert report.summaries[0].ev_ci_low is not None
    assert "positive mean EV" in report.decision_gate


def test_rfi_level_efficient_fixture_has_no_bets() -> None:
    markets, trades = fixture_rfi_inputs(n_markets=80, yes_rate=0.50, market_yes_price=0.50)
    report, bets, _samples = evaluate_rfi_level_edge(
        markets,
        trades,
        config=RfiEvaluationConfig(min_train=20, min_net_edge=0.03),
    )

    assert not bets
    assert all(summary.bets == 0 for summary in report.summaries)


def test_rfi_context_features_adjust_walk_forward_probability() -> None:
    markets, trades = fixture_rfi_inputs(n_markets=6, yes_rate=0.50, market_yes_price=0.45)
    contexts = [
        RfiContextFeature(
            market_id=market.market_id,
            feature_as_of=trades[idx * 8].created_at,
            injury_probability_delta=0.04,
            rest_probability_delta=0.01,
            roster_absence_probability_delta=-0.01,
            source="fixture-injury-rest-roster",
        )
        for idx, market in enumerate(markets)
    ]

    report, bets, _samples = evaluate_rfi_level_edge(
        markets,
        trades,
        config=RfiEvaluationConfig(min_train=1, min_net_edge=0.0, crosses=(0.0,)),
        context_features=contexts,
    )

    assert report.context_market_count == len(markets)
    assert report.context_coverage == pytest.approx(1.0)
    assert report.mean_context_probability_delta == pytest.approx(0.04)
    assert bets
    assert bets[0].model_yes_probability == pytest.approx(min(1.0 - 1e-9, bets[0].base_yes_probability + 0.04))
    assert bets[0].context_feature_count == 3


def test_rfi_context_features_reject_future_rows() -> None:
    markets, trades = fixture_rfi_inputs(n_markets=3, yes_rate=0.50, market_yes_price=0.50)
    future_context = RfiContextFeature(
        market_id=markets[1].market_id,
        feature_as_of=trades[8].created_at.replace(year=2030),
        injury_probability_delta=0.05,
    )

    with pytest.raises(ValueError, match="after the early price observation"):
        evaluate_rfi_level_edge(
            markets,
            trades,
            config=RfiEvaluationConfig(min_train=1, min_net_edge=0.0, crosses=(0.0,)),
            context_features=(future_context,),
        )


def test_rfi_live_touch_fixture_marks_candidate_without_edge_claim(tmp_path: Path) -> None:
    paths = write_fixture_live_inputs(tmp_path)
    markets = read_markets_csv(Path(paths["markets_csv"]))
    trades = read_trades_jsonl(Path(paths["trades_jsonl"]))
    live_quotes = read_live_quotes_csv(Path(paths["live_quotes_csv"]))

    report = evaluate_rfi_live_touch(
        markets,
        trades,
        live_quotes,
        config=RfiEvaluationConfig(min_train=20, min_net_edge=0.01),
        as_of=live_quotes[0].received_at,
    )

    assert report.training_sample_count == 80
    assert report.quote_count == 2
    assert report.candidate_count >= 1
    assert "markout" in report.decision_gate
    assert report.candidates[0].candidate is True
    assert report.candidates[0].reason == "fee_net_live_touch_candidate_needs_markout_settlement"


def test_rfi_live_touch_rejects_future_context(tmp_path: Path) -> None:
    paths = write_fixture_live_inputs(tmp_path)
    markets = read_markets_csv(Path(paths["markets_csv"]))
    trades = read_trades_jsonl(Path(paths["trades_jsonl"]))
    live_quotes = read_live_quotes_csv(Path(paths["live_quotes_csv"]))
    future_context = RfiContextFeature(
        market_id=live_quotes[0].market_id,
        feature_as_of=live_quotes[0].received_at.replace(year=2030),
        injury_probability_delta=0.03,
    )

    with pytest.raises(ValueError, match="after the live quote observation"):
        evaluate_rfi_live_touch(
            markets,
            trades,
            live_quotes,
            config=RfiEvaluationConfig(min_train=20),
            context_features=(future_context,),
            as_of=live_quotes[0].received_at,
        )


def test_rfi_fixture_inputs_round_trip(tmp_path: Path) -> None:
    paths = write_fixture_inputs(tmp_path)
    markets = read_markets_csv(Path(paths["markets_csv"]))
    trades = read_trades_jsonl(Path(paths["trades_jsonl"]))
    contexts = read_context_csv(Path(paths["context_csv"]))

    assert len(markets) == 80
    assert len(trades) == 640
    assert len(contexts) == 80


def test_rfi_live_fixture_inputs_round_trip(tmp_path: Path) -> None:
    paths = write_fixture_live_inputs(tmp_path)
    live_quotes = read_live_quotes_csv(Path(paths["live_quotes_csv"]))

    assert len(live_quotes) == 2
    assert live_quotes[0].book_verified is True
    assert live_quotes[0].yes_ask == pytest.approx(0.46)


def test_rfi_ws_orderbook_rows_convert_to_book_verified_quotes(tmp_path: Path) -> None:
    raw_path = tmp_path / "raw.jsonl"
    raw_path.write_text(json.dumps(_ws_orderbook_row("KXMLBRFI-WS-FIXTURE")) + "\n", encoding="utf-8")

    quotes = read_ws_live_quotes_jsonl(raw_path)

    assert len(quotes) == 1
    assert quotes[0].market_id == "KXMLBRFI-WS-FIXTURE"
    assert quotes[0].yes_bid == pytest.approx(0.39)
    assert quotes[0].yes_ask == pytest.approx(0.40)
    assert quotes[0].book_verified is True


def test_rfi_ws_quote_timeline_applies_orderbook_deltas(tmp_path: Path) -> None:
    raw_path = tmp_path / "raw.jsonl"
    start = datetime(2026, 6, 3, 20, 0, tzinfo=UTC)
    raw_rows = [
        _ws_orderbook_row("KXMLBRFI-WS-FIXTURE", received_at=start, yes_bid=0.39, no_bid=0.58),
        _ws_orderbook_delta_row(
            "KXMLBRFI-WS-FIXTURE",
            received_at=start.replace(minute=1),
            side="yes",
            price=0.41,
            delta=7.0,
        ),
    ]
    raw_path.write_text("".join(json.dumps(row) + "\n" for row in raw_rows), encoding="utf-8")

    quotes = read_ws_live_quote_timeline_jsonl(raw_path)

    assert len(quotes) == 2
    assert quotes[0].yes_bid == pytest.approx(0.39)
    assert quotes[1].yes_bid == pytest.approx(0.41)
    assert quotes[1].yes_ask == pytest.approx(0.42)


def test_rfi_live_markout_fixture_finds_positive_clv(tmp_path: Path) -> None:
    paths = write_fixture_markout_inputs(tmp_path)
    candidates = read_live_touch_candidates_jsonl(Path(paths["candidates_jsonl"]))
    quote_timeline = read_ws_live_quote_timeline_jsonl(Path(paths["ws_raw_jsonl"]))

    report = evaluate_rfi_live_markouts(
        candidates,
        quote_timeline,
        horizons_seconds=(300,),
        min_markout_rows=1,
    )

    assert report.candidate_count == 1
    assert report.markout_count == 1
    assert report.summaries[0].mean_clv is not None
    assert report.summaries[0].mean_clv > 0.0
    assert report.markouts[0].candidate_spread > 0.0
    assert report.markouts[0].candidate_yes_ask_size > 0.0
    assert report.markouts[0].candidate_source
    assert "settlement" in report.decision_gate


def test_rfi_execution_filter_fixture_finds_positive_slice(tmp_path: Path) -> None:
    paths = write_fixture_markout_inputs(tmp_path)
    candidates = read_live_touch_candidates_jsonl(Path(paths["candidates_jsonl"]))
    quote_timeline = read_ws_live_quote_timeline_jsonl(Path(paths["ws_raw_jsonl"]))
    markout_report = evaluate_rfi_live_markouts(
        candidates,
        quote_timeline,
        horizons_seconds=(300,),
        min_markout_rows=1,
    )

    report = evaluate_rfi_execution_filters(markout_report.markouts, horizon_seconds=300, min_rows=1)

    assert report.evaluated_rows == 1
    assert report.summaries[0].rule_name == "all"
    assert report.summaries[0].positive is True
    assert "positive mean" in report.decision_gate


def test_rfi_cli_no_network_writes_report(tmp_path: Path) -> None:
    exit_code = script.main(
        [
            "evaluate",
            "--no-network",
            "--report-json",
            str(tmp_path / "report.json"),
            "--report-md",
            str(tmp_path / "report.md"),
            "--bets-jsonl",
            str(tmp_path / "bets.jsonl"),
            "--samples-jsonl",
            str(tmp_path / "samples.jsonl"),
        ]
    )

    assert exit_code == 0
    result = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert result["sample_count"] == 80
    assert "positive mean EV" in result["decision_gate"]


def test_rfi_cli_live_touch_no_network_writes_report(tmp_path: Path) -> None:
    exit_code = script.main(
        [
            "live-touch",
            "--no-network",
            "--report-json",
            str(tmp_path / "live-touch.json"),
            "--report-md",
            str(tmp_path / "live-touch.md"),
            "--candidates-jsonl",
            str(tmp_path / "live-touch-candidates.jsonl"),
        ]
    )

    assert exit_code == 0
    payload = json.loads((tmp_path / "live-touch.json").read_text(encoding="utf-8"))
    assert payload["quote_count"] == 2
    assert payload["candidate_count"] >= 1
    assert "markout" in payload["decision_gate"]


def test_rfi_cli_live_touch_accepts_ws_raw_jsonl(tmp_path: Path) -> None:
    fixture_dir = tmp_path / "inputs"
    paths = write_fixture_live_inputs(fixture_dir)
    raw_path = tmp_path / "raw.jsonl"
    raw_path.write_text(json.dumps(_ws_orderbook_row("KXMLBRFI-WS-FIXTURE")) + "\n", encoding="utf-8")

    exit_code = script.main(
        [
            "live-touch",
            "--markets-csv",
            paths["markets_csv"],
            "--trades-jsonl",
            paths["trades_jsonl"],
            "--ws-raw-jsonl",
            str(raw_path),
            "--report-json",
            str(tmp_path / "ws-live-touch.json"),
            "--report-md",
            str(tmp_path / "ws-live-touch.md"),
            "--candidates-jsonl",
            str(tmp_path / "ws-live-touch-candidates.jsonl"),
        ]
    )

    assert exit_code == 0
    payload = json.loads((tmp_path / "ws-live-touch.json").read_text(encoding="utf-8"))
    assert payload["quote_count"] == 1
    assert payload["candidate_count"] == 1
    assert payload["candidates"][0]["book_verified"] is True


def test_rfi_cli_capture_live_no_network_writes_quotes(tmp_path: Path) -> None:
    fixture_dir = tmp_path / "live-capture"

    assert script.main(["capture-live", "--no-network", "--out-dir", str(fixture_dir)]) == 0

    assert (fixture_dir / "rfi_live_quotes.csv").exists()
    assert read_live_quotes_csv(fixture_dir / "rfi_live_quotes.csv")


def test_rfi_cli_markout_no_network_writes_report(tmp_path: Path) -> None:
    exit_code = script.main(
        [
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
    )

    assert exit_code == 0
    payload = json.loads((tmp_path / "markout.json").read_text(encoding="utf-8"))
    assert payload["candidate_count"] == 1
    assert payload["markout_count"] == 1
    assert "settlement" in payload["decision_gate"]


def test_rfi_cli_execution_filter_no_network_writes_report(tmp_path: Path) -> None:
    exit_code = script.main(
        [
            "execution-filter",
            "--no-network",
            "--horizon-seconds",
            "300",
            "--min-rows",
            "1",
            "--report-json",
            str(tmp_path / "execution-filter.json"),
            "--report-md",
            str(tmp_path / "execution-filter.md"),
        ]
    )

    assert exit_code == 0
    payload = json.loads((tmp_path / "execution-filter.json").read_text(encoding="utf-8"))
    assert payload["evaluated_rows"] == 1
    assert payload["summaries"][0]["positive"] is True
    assert "Execution Filter" in (tmp_path / "execution-filter.md").read_text(encoding="utf-8")


def test_rfi_cli_file_inputs(tmp_path: Path) -> None:
    fixture_dir = tmp_path / "inputs"
    assert script.main(["capture-kalshi", "--no-network", "--out-dir", str(fixture_dir)]) == 0
    out = tmp_path / "file-report.json"

    exit_code = script.main(
        [
            "evaluate",
            "--markets-csv",
            str(fixture_dir / "rfi_markets.csv"),
            "--trades-jsonl",
            str(fixture_dir / "rfi_trades.jsonl"),
            "--context-csv",
            str(fixture_dir / "rfi_context.csv"),
            "--report-json",
            str(out),
            "--report-md",
            str(tmp_path / "file-report.md"),
            "--bets-jsonl",
            str(tmp_path / "file-bets.jsonl"),
            "--samples-jsonl",
            str(tmp_path / "file-samples.jsonl"),
        ]
    )

    assert exit_code == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["market_count"] == 80
    assert payload["context_market_count"] == 80


def test_golf_surface_reads_captured_kalshi_topn_shape(tmp_path: Path) -> None:
    csv_path = tmp_path / "golf_capture.csv"
    csv_path.write_text(
        "captured_at,tournament_id,player_id,market_ticker,yes_bid,yes_ask,yes_bid_size,yes_ask_size\n"
        "2026-06-03T19:44:36+00:00,KXPGATOP20-USO26,SCHEF,KXPGATOP20-USO26-SCHEF,"
        "0.42,0.45,10,11\n",
        encoding="utf-8",
    )

    markets = read_surface_markets_csv(csv_path)
    quotes = read_surface_quotes_csv(csv_path)

    assert markets[0].market_family == "top_n"
    assert markets[0].top_n == 20
    assert markets[0].subject_id == "SCHEF"
    assert quotes[0].yes_ask == 0.45


def _ws_orderbook_row(
    market_id: str,
    *,
    received_at: datetime | None = None,
    yes_bid: float = 0.39,
    no_bid: float = 0.60,
) -> dict[str, object]:
    now = received_at or datetime.now(UTC)
    return {
        "venue": "kalshi",
        "source": "kalshi-ws",
        "channel": "orderbook_snapshot",
        "received_at": now.isoformat(),
        "exchange_ts": None,
        "schema_version": "kalshi-ws-v1",
        "metadata": {"ws_type": "orderbook_snapshot"},
        "payload": {
            "type": "orderbook_snapshot",
            "msg": {
                "market_ticker": market_id,
                "yes_dollars_fp": [["0.37", "4"], [f"{yes_bid:.2f}", "10"]],
                "no_dollars_fp": [["0.58", "3"], [f"{no_bid:.2f}", "12"]],
            },
        },
    }


def _ws_orderbook_delta_row(
    market_id: str,
    *,
    received_at: datetime,
    side: str,
    price: float,
    delta: float,
) -> dict[str, object]:
    return {
        "venue": "kalshi",
        "source": "kalshi-ws",
        "channel": "orderbook_delta",
        "received_at": received_at.isoformat(),
        "exchange_ts": received_at.isoformat(),
        "schema_version": "kalshi-ws-v1",
        "metadata": {"ws_type": "orderbook_delta"},
        "payload": {
            "type": "orderbook_delta",
            "msg": {
                "market_ticker": market_id,
                "side": side,
                "price_dollars": f"{price:.4f}",
                "delta_fp": f"{delta:.2f}",
            },
        },
    }
