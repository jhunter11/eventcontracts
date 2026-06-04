"""Run read-only golf top-N inference from bookmaker outright references.

This script never submits, cancels, replaces, or live-submits orders. It turns
public/reference odds plus public Kalshi snapshots into hypothetical shadow
intents for CLV measurement only.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "python" / "src"))

from eventcontracts.research.golf_preround import (  # noqa: E402
    ReferenceTopNConfig,
    evaluate_reference_topn,
    fixture_reference_topn_inputs,
    write_reference_topn_outputs,
)
from eventcontracts.research.golf_preround_data import read_csv_rows  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    return int(args.handler(args))


def _handle_evaluate(args: argparse.Namespace) -> int:
    snapshots: Sequence[Mapping[str, object]]
    odds: Sequence[Mapping[str, object]]
    if args.no_network:
        snapshots, odds = fixture_reference_topn_inputs()
    else:
        if args.snapshots_csv is None or args.odds_csv is None:
            raise SystemExit("--snapshots-csv and --odds-csv are required unless --no-network is set")
        snapshots = read_csv_rows(args.snapshots_csv)
        odds = read_csv_rows(args.odds_csv)
    config = ReferenceTopNConfig(
        simulations=args.simulations,
        seed=args.seed,
        min_net_edge=args.min_net_edge,
        min_executable_size=args.min_executable_size,
        max_book_overround=args.max_book_overround,
        kalshi_event_token=args.kalshi_event_token,
        title_contains=args.title_contains,
    )
    report = evaluate_reference_topn(snapshot_rows=snapshots, odds_rows=odds, config=config)
    write_reference_topn_outputs(
        report,
        report_json=args.report_json,
        report_md=args.report_md,
        intents_jsonl=args.intents_jsonl,
    )
    print(json.dumps(report.as_dict(), indent=2, sort_keys=True))
    return 0


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    evaluate = subparsers.add_parser("evaluate", help="Evaluate reference top-N candidates.")
    evaluate.add_argument("--no-network", action="store_true")
    evaluate.add_argument("--snapshots-csv", type=Path, default=None)
    evaluate.add_argument("--odds-csv", type=Path, default=None)
    evaluate.add_argument("--kalshi-event-token", default="USO26")
    evaluate.add_argument("--title-contains", default="U.S. Open")
    evaluate.add_argument("--simulations", type=int, default=5000)
    evaluate.add_argument("--seed", type=int, default=23)
    evaluate.add_argument("--min-net-edge", type=float, default=0.03)
    evaluate.add_argument("--min-executable-size", type=float, default=100.0)
    evaluate.add_argument(
        "--max-book-overround",
        type=float,
        default=1.8,
        help="Drop reference odds rows whose source-board overround is above this threshold.",
    )
    evaluate.add_argument("--report-json", type=Path, default=ROOT / "live-test" / "golf-reference-topn.json")
    evaluate.add_argument("--report-md", type=Path, default=ROOT / "live-test" / "golf-reference-topn.md")
    evaluate.add_argument(
        "--intents-jsonl",
        type=Path,
        default=ROOT / "live-test" / "golf-reference-topn-intents.jsonl",
    )
    evaluate.set_defaults(handler=_handle_evaluate)

    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
