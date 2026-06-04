"""Run read-only MLB RFI level-edge validation.

This script either evaluates supplied settled market/trade files or captures
public Kalshi settled RFI tapes into local evidence files. It never submits,
cancels, replaces, or live-submits orders.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "python" / "src"))

from eventcontracts.research.baseball_rfi import (  # noqa: E402
    RfiEvaluationConfig,
    capture_kalshi_rfi_inputs,
    capture_kalshi_rfi_live_quotes,
    evaluate_rfi_execution_filters,
    evaluate_rfi_level_edge,
    evaluate_rfi_live_markouts,
    evaluate_rfi_live_touch,
    fixture_rfi_inputs,
    read_context_csv,
    read_live_markouts_jsonl,
    read_live_quotes_csv,
    read_live_touch_candidates_jsonl,
    read_markets_csv,
    read_trades_jsonl,
    read_ws_live_quote_timeline_jsonl,
    read_ws_live_quotes_jsonl,
    write_fixture_inputs,
    write_fixture_live_inputs,
    write_fixture_markout_inputs,
    write_rfi_execution_filter_outputs,
    write_rfi_live_markout_outputs,
    write_rfi_live_touch_outputs,
    write_rfi_outputs,
)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    return int(args.handler(args))


def _handle_evaluate(args: argparse.Namespace) -> int:
    config = _config_from_args(args)
    if args.no_network and (args.markets_csv is None or args.trades_jsonl is None):
        markets, trades = fixture_rfi_inputs()
    else:
        if args.markets_csv is None or args.trades_jsonl is None:
            raise SystemExit("--markets-csv and --trades-jsonl are required unless --no-network uses fixtures")
        markets = read_markets_csv(args.markets_csv)
        trades = read_trades_jsonl(args.trades_jsonl)
    context_features = read_context_csv(args.context_csv) if args.context_csv is not None else ()
    report, bets, samples = evaluate_rfi_level_edge(
        markets,
        trades,
        series_ticker=args.series_ticker,
        config=config,
        context_features=context_features,
    )
    write_rfi_outputs(
        report,
        bets,
        samples,
        report_json=args.report_json,
        report_md=args.report_md,
        bets_jsonl=args.bets_jsonl,
        samples_jsonl=args.samples_jsonl,
    )
    print(json.dumps(report.as_dict(), indent=2, sort_keys=True))
    return 0


def _handle_capture(args: argparse.Namespace) -> int:
    payload: Mapping[str, object]
    if args.no_network:
        payload = write_fixture_inputs(args.out_dir)
    else:
        payload = capture_kalshi_rfi_inputs(
            out_dir=args.out_dir,
            series_ticker=args.series_ticker,
            max_markets=args.max_markets,
            max_trade_pages=args.max_trade_pages,
        )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def _handle_capture_live(args: argparse.Namespace) -> int:
    payload: Mapping[str, object]
    if args.no_network:
        payload = write_fixture_live_inputs(args.out_dir)
    else:
        payload = capture_kalshi_rfi_live_quotes(
            out_dir=args.out_dir,
            series_ticker=args.series_ticker,
            max_markets=args.max_markets,
        )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def _handle_live_touch(args: argparse.Namespace) -> int:
    config = _config_from_args(args)
    if args.no_network and (
        args.markets_csv is None
        or args.trades_jsonl is None
        or (args.live_quotes_csv is None and args.ws_raw_jsonl is None)
    ):
        fixture_paths = write_fixture_live_inputs(args.report_json.parent / "fixture-inputs")
        markets = read_markets_csv(Path(fixture_paths["markets_csv"]))
        trades = read_trades_jsonl(Path(fixture_paths["trades_jsonl"]))
        live_quotes = read_live_quotes_csv(Path(fixture_paths["live_quotes_csv"]))
    else:
        if args.markets_csv is None or args.trades_jsonl is None:
            raise SystemExit(
                "--markets-csv and --trades-jsonl are required unless --no-network uses fixtures"
            )
        if args.live_quotes_csv is None and args.ws_raw_jsonl is None:
            raise SystemExit("--live-quotes-csv or --ws-raw-jsonl is required unless --no-network uses fixtures")
        markets = read_markets_csv(args.markets_csv)
        trades = read_trades_jsonl(args.trades_jsonl)
        live_quotes = (
            read_ws_live_quotes_jsonl(args.ws_raw_jsonl)
            if args.ws_raw_jsonl is not None
            else read_live_quotes_csv(args.live_quotes_csv)
        )
    context_features = read_context_csv(args.context_csv) if args.context_csv is not None else ()
    report = evaluate_rfi_live_touch(
        markets,
        trades,
        live_quotes,
        series_ticker=args.series_ticker,
        config=config,
        context_features=context_features,
    )
    write_rfi_live_touch_outputs(
        report,
        report_json=args.report_json,
        report_md=args.report_md,
        candidates_jsonl=args.candidates_jsonl,
    )
    print(json.dumps(report.as_dict(), indent=2, sort_keys=True))
    return 0


def _handle_markout(args: argparse.Namespace) -> int:
    if args.no_network and (args.candidates_jsonl is None or args.ws_raw_jsonl is None):
        fixture_paths = write_fixture_markout_inputs(args.report_json.parent / "fixture-inputs")
        candidates = read_live_touch_candidates_jsonl(Path(fixture_paths["candidates_jsonl"]))
        quote_timeline = read_ws_live_quote_timeline_jsonl(Path(fixture_paths["ws_raw_jsonl"]))
    else:
        if args.candidates_jsonl is None or args.ws_raw_jsonl is None:
            raise SystemExit("--candidates-jsonl and --ws-raw-jsonl are required unless --no-network uses fixtures")
        candidates = read_live_touch_candidates_jsonl(args.candidates_jsonl)
        quote_timeline = read_ws_live_quote_timeline_jsonl(args.ws_raw_jsonl)
    horizons = tuple(int(item) for item in args.horizons_seconds.split(",") if item.strip())
    report = evaluate_rfi_live_markouts(
        candidates,
        quote_timeline,
        horizons_seconds=horizons,
        min_markout_rows=args.min_markout_rows,
    )
    write_rfi_live_markout_outputs(
        report,
        report_json=args.report_json,
        report_md=args.report_md,
        markouts_jsonl=args.markouts_jsonl,
    )
    print(json.dumps(report.as_dict(), indent=2, sort_keys=True))
    return 0


def _handle_execution_filter(args: argparse.Namespace) -> int:
    if args.no_network and args.markouts_jsonl is None:
        fixture_paths = write_fixture_markout_inputs(args.report_json.parent / "fixture-inputs")
        candidates = read_live_touch_candidates_jsonl(Path(fixture_paths["candidates_jsonl"]))
        quote_timeline = read_ws_live_quote_timeline_jsonl(Path(fixture_paths["ws_raw_jsonl"]))
        markout_report = evaluate_rfi_live_markouts(
            candidates,
            quote_timeline,
            horizons_seconds=(args.horizon_seconds,),
            min_markout_rows=args.min_rows,
        )
        markouts = markout_report.markouts
    else:
        if args.markouts_jsonl is None:
            raise SystemExit("--markouts-jsonl is required unless --no-network uses fixtures")
        markouts = read_live_markouts_jsonl(args.markouts_jsonl)
    report = evaluate_rfi_execution_filters(
        markouts,
        horizon_seconds=args.horizon_seconds,
        min_rows=args.min_rows,
    )
    write_rfi_execution_filter_outputs(
        report,
        report_json=args.report_json,
        report_md=args.report_md,
    )
    print(json.dumps(report.as_dict(), indent=2, sort_keys=True))
    return 0


def _config_from_args(args: argparse.Namespace) -> RfiEvaluationConfig:
    crosses = tuple(float(item) for item in args.crosses.split(",") if item.strip())
    return RfiEvaluationConfig(
        min_train=args.min_train,
        min_net_edge=args.min_net_edge,
        early_trade_count=args.early_trade_count,
        early_window_seconds=args.early_window_seconds,
        crosses=crosses,
        max_context_probability_delta=args.max_context_probability_delta,
        max_quote_age_seconds=args.max_quote_age_seconds,
    )


def _add_config(subparser: argparse.ArgumentParser) -> None:
    subparser.add_argument("--min-train", type=int, default=20)
    subparser.add_argument("--min-net-edge", type=float, default=0.01)
    subparser.add_argument("--early-trade-count", type=int, default=20)
    subparser.add_argument("--early-window-seconds", type=int, default=900)
    subparser.add_argument("--crosses", default="0,0.01,0.02")
    subparser.add_argument("--max-context-probability-delta", type=float, default=0.15)
    subparser.add_argument("--max-quote-age-seconds", type=int, default=600)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    evaluate = subparsers.add_parser("evaluate", help="Evaluate supplied or fixture RFI tape.")
    evaluate.add_argument("--no-network", action="store_true")
    evaluate.add_argument("--series-ticker", default="KXMLBRFI")
    evaluate.add_argument("--markets-csv", type=Path, default=None)
    evaluate.add_argument("--trades-jsonl", type=Path, default=None)
    evaluate.add_argument("--context-csv", type=Path, default=None)
    evaluate.add_argument("--report-json", type=Path, default=ROOT / "live-test" / "baseball-rfi-edge.json")
    evaluate.add_argument("--report-md", type=Path, default=ROOT / "live-test" / "baseball-rfi-edge.md")
    evaluate.add_argument("--bets-jsonl", type=Path, default=ROOT / "live-test" / "baseball-rfi-bets.jsonl")
    evaluate.add_argument("--samples-jsonl", type=Path, default=ROOT / "live-test" / "baseball-rfi-samples.jsonl")
    _add_config(evaluate)
    evaluate.set_defaults(handler=_handle_evaluate)

    capture = subparsers.add_parser("capture-kalshi", help="Capture public settled Kalshi RFI tape.")
    capture.add_argument("--no-network", action="store_true")
    capture.add_argument("--series-ticker", default="KXMLBRFI")
    capture.add_argument("--out-dir", type=Path, default=ROOT / "live-test" / "baseball-rfi-capture")
    capture.add_argument("--max-markets", type=int, default=80)
    capture.add_argument("--max-trade-pages", type=int, default=2)
    capture.set_defaults(handler=_handle_capture)

    live_capture = subparsers.add_parser("capture-live", help="Capture public active Kalshi RFI quote snapshots.")
    live_capture.add_argument("--no-network", action="store_true")
    live_capture.add_argument("--series-ticker", default="KXMLBRFI")
    live_capture.add_argument("--out-dir", type=Path, default=ROOT / "live-test" / "baseball-rfi-live-touch")
    live_capture.add_argument("--max-markets", type=int, default=80)
    live_capture.set_defaults(handler=_handle_capture_live)

    live_touch = subparsers.add_parser("live-touch", help="Evaluate live quote touch against settled RFI prior.")
    live_touch.add_argument("--no-network", action="store_true")
    live_touch.add_argument("--series-ticker", default="KXMLBRFI")
    live_touch.add_argument("--markets-csv", type=Path, default=None)
    live_touch.add_argument("--trades-jsonl", type=Path, default=None)
    live_touch.add_argument("--live-quotes-csv", type=Path, default=None)
    live_touch.add_argument("--ws-raw-jsonl", type=Path, default=None)
    live_touch.add_argument("--context-csv", type=Path, default=None)
    live_touch.add_argument("--report-json", type=Path, default=ROOT / "live-test" / "baseball-rfi-live-touch.json")
    live_touch.add_argument("--report-md", type=Path, default=ROOT / "live-test" / "baseball-rfi-live-touch.md")
    live_touch.add_argument(
        "--candidates-jsonl",
        type=Path,
        default=ROOT / "live-test" / "baseball-rfi-live-touch-candidates.jsonl",
    )
    _add_config(live_touch)
    live_touch.set_defaults(handler=_handle_live_touch)

    markout = subparsers.add_parser("markout", help="Mark out live-touch candidates against later WS book rows.")
    markout.add_argument("--no-network", action="store_true")
    markout.add_argument("--candidates-jsonl", type=Path, default=None)
    markout.add_argument("--ws-raw-jsonl", type=Path, default=None)
    markout.add_argument("--horizons-seconds", default="300,900,1800")
    markout.add_argument("--min-markout-rows", type=int, default=10)
    markout.add_argument("--report-json", type=Path, default=ROOT / "live-test" / "baseball-rfi-markout.json")
    markout.add_argument("--report-md", type=Path, default=ROOT / "live-test" / "baseball-rfi-markout.md")
    markout.add_argument(
        "--markouts-jsonl",
        type=Path,
        default=ROOT / "live-test" / "baseball-rfi-markouts.jsonl",
    )
    markout.set_defaults(handler=_handle_markout)

    execution_filter = subparsers.add_parser(
        "execution-filter",
        help="Evaluate predeclared execution filters over local RFI markout rows.",
    )
    execution_filter.add_argument("--no-network", action="store_true")
    execution_filter.add_argument("--markouts-jsonl", type=Path, default=None)
    execution_filter.add_argument("--horizon-seconds", type=int, default=60)
    execution_filter.add_argument("--min-rows", type=int, default=10)
    execution_filter.add_argument(
        "--report-json",
        type=Path,
        default=ROOT / "live-test" / "baseball-rfi-execution-filter.json",
    )
    execution_filter.add_argument(
        "--report-md",
        type=Path,
        default=ROOT / "live-test" / "baseball-rfi-execution-filter.md",
    )
    execution_filter.set_defaults(handler=_handle_execution_filter)

    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
