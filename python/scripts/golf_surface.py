"""Run the research-only golf multi-outcome surface.

No-trade only. The script prices fixture or supplied public-market state and
writes model reports plus hypothetical shadow-fill intents. It never submits,
cancels, replaces, or live-submits orders.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "python" / "src"))

from eventcontracts.research.golf_surface import (  # noqa: E402
    GolfMultiOutcomeSurfaceModel,
    GolfSurfaceConfig,
    GolfTopNArbConfig,
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
    read_surface_ws_quotes_jsonl,
    render_surface_markdown,
    run_async_surface_fixture,
    scan_golf_topn_arbitrage,
    write_fixture_surface_inputs,
    write_fixture_surface_markout_inputs,
    write_golf_topn_arb_outputs,
    write_predictions_jsonl,
    write_surface_markout_outputs,
    write_surface_outputs,
)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    return int(args.handler(args))


def _handle_surface_once(args: argparse.Namespace) -> int:
    _require_no_network(args.no_network)
    config = _config_from_args(args)
    state = read_surface_state_json(args.state_json) if args.state_json is not None else fixture_surface_state()
    markets = read_surface_markets_csv(args.markets_csv) if args.markets_csv is not None else fixture_surface_markets()
    quotes = (
        read_surface_quotes_csv(args.quotes_csv)
        if args.quotes_csv is not None
        else fixture_surface_quotes(state.as_of)
    )
    prediction = GolfMultiOutcomeSurfaceModel(config).predict(
        state,
        markets=markets,
        quotes=quotes,
    )
    write_surface_outputs(
        prediction,
        report_json=args.report_json,
        report_md=args.report_md,
        intents_jsonl=args.intents_jsonl,
    )
    print(json.dumps(prediction.as_dict(), indent=2, sort_keys=True))
    return 0


def _handle_write_fixture_inputs(args: argparse.Namespace) -> int:
    _require_no_network(args.no_network)
    payload = write_fixture_surface_inputs(args.out_dir)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def _handle_rolling_loop(args: argparse.Namespace) -> int:
    _require_no_network(args.no_network)
    config = _config_from_args(args)
    predictions = asyncio.run(run_async_surface_fixture(iterations=args.iterations, config=config))
    args.out_dir.mkdir(parents=True, exist_ok=True)
    write_predictions_jsonl(args.out_dir / "surface_predictions.jsonl", predictions)
    if predictions:
        latest = predictions[-1]
        write_surface_outputs(
            latest,
            report_json=args.out_dir / "latest_surface.json",
            report_md=args.out_dir / "latest_surface.md",
            intents_jsonl=args.out_dir / "latest_shadow_intents.jsonl",
        )
        print(render_surface_markdown(latest))
    return 0


def _handle_markout(args: argparse.Namespace) -> int:
    _require_no_network(args.no_network)
    if args.intents_jsonl is None or args.ws_raw_jsonl is None:
        paths = write_fixture_surface_markout_inputs(args.report_json.parent / "fixture-inputs")
        intents_jsonl = Path(paths["intents_jsonl"])
        ws_raw_jsonl = Path(paths["ws_raw_jsonl"])
    else:
        intents_jsonl = args.intents_jsonl
        ws_raw_jsonl = args.ws_raw_jsonl
    candidates = read_surface_intents_jsonl(intents_jsonl)
    quote_timeline = read_surface_ws_quote_timeline_jsonl(
        ws_raw_jsonl,
        market_tickers=tuple(candidate.market_ticker for candidate in candidates),
    )
    horizons = tuple(int(item) for item in args.horizons_seconds.split(",") if item.strip())
    report = evaluate_surface_markouts(
        candidates,
        quote_timeline,
        horizons_seconds=horizons,
        min_markout_rows=args.min_markout_rows,
    )
    write_surface_markout_outputs(
        report,
        report_json=args.report_json,
        report_md=args.report_md,
        markouts_jsonl=args.markouts_jsonl,
    )
    print(json.dumps(report.as_dict(), indent=2, sort_keys=True))
    return 0


def _handle_topn_arb(args: argparse.Namespace) -> int:
    _require_no_network(args.no_network)
    if args.markets_csv is None and args.quotes_csv is None and args.ws_raw_jsonl is None:
        markets, quotes = fixture_topn_arb_inputs()
    else:
        if args.markets_csv is None:
            raise SystemExit("--markets-csv is required when scanning supplied quotes")
        if args.quotes_csv is None and args.ws_raw_jsonl is None:
            raise SystemExit("one of --quotes-csv or --ws-raw-jsonl is required")
        if args.quotes_csv is not None and args.ws_raw_jsonl is not None:
            raise SystemExit("use either --quotes-csv or --ws-raw-jsonl, not both")
        markets = read_surface_markets_csv(args.markets_csv)
        market_tickers = tuple(market.market_ticker for market in markets if market.market_family == "top_n")
        quotes = (
            read_surface_ws_quotes_jsonl(args.ws_raw_jsonl, market_tickers=market_tickers)
            if args.ws_raw_jsonl is not None
            else read_surface_quotes_csv(args.quotes_csv)
        )
    report = scan_golf_topn_arbitrage(
        markets,
        quotes,
        config=GolfTopNArbConfig(
            min_net_edge=args.min_net_edge,
            min_executable_size=args.min_executable_size,
            max_quote_age_seconds=args.max_quote_age_seconds,
        ),
    )
    write_golf_topn_arb_outputs(
        report,
        report_json=args.report_json,
        report_md=args.report_md,
        candidates_jsonl=args.candidates_jsonl,
    )
    print(json.dumps(report.as_dict(), indent=2, sort_keys=True))
    return 0


def _config_from_args(args: argparse.Namespace) -> GolfSurfaceConfig:
    return GolfSurfaceConfig(
        simulations=args.simulations,
        seed=args.seed,
        min_net_edge=args.min_net_edge,
        max_quote_age_ms=args.max_quote_age_ms,
    )


def _require_no_network(no_network: bool) -> None:
    if not no_network:
        raise SystemExit("only --no-network is implemented for this research surface")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_common(subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument("--no-network", action="store_true")
        subparser.add_argument("--simulations", type=int, default=4000)
        subparser.add_argument("--seed", type=int, default=17)
        subparser.add_argument("--min-net-edge", type=float, default=0.03)
        subparser.add_argument("--max-quote-age-ms", type=int, default=60_000)

    once = subparsers.add_parser("surface-once", help="Run one fixture surface recompute.")
    add_common(once)
    once.add_argument("--report-json", type=Path, default=ROOT / "live-test" / "golf-surface-once.json")
    once.add_argument("--report-md", type=Path, default=ROOT / "live-test" / "golf-surface-once.md")
    once.add_argument("--intents-jsonl", type=Path, default=ROOT / "live-test" / "golf-surface-intents.jsonl")
    once.add_argument("--state-json", type=Path, default=None)
    once.add_argument("--markets-csv", type=Path, default=None)
    once.add_argument("--quotes-csv", type=Path, default=None)
    once.set_defaults(handler=_handle_surface_once)

    fixtures = subparsers.add_parser("write-fixture-inputs", help="Write reusable no-network fixture inputs.")
    fixtures.add_argument("--no-network", action="store_true")
    fixtures.add_argument("--out-dir", type=Path, default=ROOT / "live-test" / "golf-surface-fixture-inputs")
    fixtures.set_defaults(handler=_handle_write_fixture_inputs)

    rolling = subparsers.add_parser("rolling-loop", help="Run deterministic async fixture recomputes.")
    add_common(rolling)
    rolling.add_argument("--iterations", type=int, default=3)
    rolling.add_argument("--out-dir", type=Path, default=ROOT / "live-test" / "golf-surface-rolling-fixture")
    rolling.set_defaults(handler=_handle_rolling_loop)

    markout = subparsers.add_parser("markout", help="Mark out surface intents against local WS book rows.")
    markout.add_argument("--no-network", action="store_true")
    markout.add_argument("--intents-jsonl", type=Path, default=None)
    markout.add_argument("--ws-raw-jsonl", type=Path, default=None)
    markout.add_argument("--horizons-seconds", default="300,900,1800")
    markout.add_argument("--min-markout-rows", type=int, default=10)
    markout.add_argument("--report-json", type=Path, default=ROOT / "live-test" / "golf-surface-markout.json")
    markout.add_argument("--report-md", type=Path, default=ROOT / "live-test" / "golf-surface-markout.md")
    markout.add_argument(
        "--markouts-jsonl",
        type=Path,
        default=ROOT / "live-test" / "golf-surface-markouts.jsonl",
    )
    markout.set_defaults(handler=_handle_markout)

    topn_arb = subparsers.add_parser("topn-arb", help="Scan top-N books for logical dominance violations.")
    topn_arb.add_argument("--no-network", action="store_true")
    topn_arb.add_argument("--markets-csv", type=Path, default=None)
    topn_arb.add_argument("--quotes-csv", type=Path, default=None)
    topn_arb.add_argument("--ws-raw-jsonl", type=Path, default=None)
    topn_arb.add_argument("--min-net-edge", type=float, default=0.0)
    topn_arb.add_argument("--min-executable-size", type=float, default=1.0)
    topn_arb.add_argument("--max-quote-age-seconds", type=float, default=600.0)
    topn_arb.add_argument("--report-json", type=Path, default=ROOT / "live-test" / "golf-topn-arb.json")
    topn_arb.add_argument("--report-md", type=Path, default=ROOT / "live-test" / "golf-topn-arb.md")
    topn_arb.add_argument(
        "--candidates-jsonl",
        type=Path,
        default=ROOT / "live-test" / "golf-topn-arb-candidates.jsonl",
    )
    topn_arb.set_defaults(handler=_handle_topn_arb)

    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
