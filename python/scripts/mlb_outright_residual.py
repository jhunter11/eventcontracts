"""Run the research-only MLB outright residual validator.

No-trade only. This script evaluates fixture or supplied public/read-only inputs
for the ``mlb-outright-residual-v1`` external-edge producer and writes reports
plus ExternalSignal-shaped shadow payloads. It never submits, cancels, replaces,
or live-submits orders.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "python" / "src"))

from eventcontracts.research.mlb_outright_residual import (  # noqa: E402
    MlbOutrightValidationConfig,
    evaluate_mlb_outright_residual,
    fixture_quotes,
    fixture_references,
    fixture_signals,
    read_model_signals_jsonl,
    read_quotes_csv,
    read_references_csv,
    read_settlements_csv,
    render_markdown,
    write_fixture_inputs,
    write_report_outputs,
)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    return int(args.handler(args))


def _handle_validate_once(args: argparse.Namespace) -> int:
    _require_no_network(args.no_network)
    config = _config_from_args(args)
    signals = read_model_signals_jsonl(args.signals_jsonl) if args.signals_jsonl else fixture_signals()
    references = read_references_csv(args.references_csv) if args.references_csv else fixture_references()
    quotes = read_quotes_csv(args.quotes_csv) if args.quotes_csv else fixture_quotes()
    settlements = read_settlements_csv(args.settlements_csv) if args.settlements_csv else ()
    report = evaluate_mlb_outright_residual(
        signals,
        references,
        quotes,
        settlements=settlements,
        config=config,
    )
    write_report_outputs(
        report,
        report_json=args.report_json,
        report_md=args.report_md,
        signals_jsonl=args.signals_jsonl_out,
    )
    print(json.dumps(report.as_dict(), indent=2, sort_keys=True))
    return 0


def _handle_write_fixture_inputs(args: argparse.Namespace) -> int:
    _require_no_network(args.no_network)
    payload = write_fixture_inputs(args.out_dir)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def _config_from_args(args: argparse.Namespace) -> MlbOutrightValidationConfig:
    return MlbOutrightValidationConfig(
        min_net_edge=args.min_net_edge,
        min_reference_residual=args.min_reference_residual,
        max_signal_age_ms=args.max_signal_age_ms,
        max_quote_age_ms=args.max_quote_age_ms,
        min_confidence=args.min_confidence,
        quantity=args.quantity,
        capital_annual_rate=args.capital_annual_rate,
        max_group_candidates=args.max_group_candidates,
        min_settlement_evidence=args.min_settlement_evidence,
    )


def _require_no_network(no_network: bool) -> None:
    if not no_network:
        raise SystemExit("only --no-network is implemented for this research validator")


def _add_common(subparser: argparse.ArgumentParser) -> None:
    subparser.add_argument("--no-network", action="store_true")
    subparser.add_argument("--min-net-edge", type=float, default=0.03)
    subparser.add_argument("--min-reference-residual", type=float, default=0.01)
    subparser.add_argument("--max-signal-age-ms", type=int, default=24 * 60 * 60 * 1000)
    subparser.add_argument("--max-quote-age-ms", type=int, default=60 * 60 * 1000)
    subparser.add_argument("--min-confidence", type=float, default=0.0)
    subparser.add_argument("--quantity", type=int, default=1)
    subparser.add_argument("--capital-annual-rate", type=float, default=0.05)
    subparser.add_argument("--max-group-candidates", type=int, default=2)
    subparser.add_argument("--min-settlement-evidence", type=int, default=20)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    once = subparsers.add_parser("validate-once", help="Run one MLB outright residual validation pass.")
    _add_common(once)
    once.add_argument("--report-json", type=Path, default=ROOT / "live-test" / "mlb-outright-residual.json")
    once.add_argument("--report-md", type=Path, default=ROOT / "live-test" / "mlb-outright-residual.md")
    once.add_argument(
        "--signals-jsonl-out",
        type=Path,
        default=ROOT / "live-test" / "mlb-outright-residual-signals.jsonl",
    )
    once.add_argument("--signals-jsonl", type=Path, default=None)
    once.add_argument("--references-csv", type=Path, default=None)
    once.add_argument("--quotes-csv", type=Path, default=None)
    once.add_argument("--settlements-csv", type=Path, default=None)
    once.set_defaults(handler=_handle_validate_once)

    fixtures = subparsers.add_parser("write-fixture-inputs", help="Write reusable no-network fixture inputs.")
    fixtures.add_argument("--no-network", action="store_true")
    fixtures.add_argument("--out-dir", type=Path, default=ROOT / "live-test" / "mlb-outright-fixture-inputs")
    fixtures.set_defaults(handler=_handle_write_fixture_inputs)

    report = subparsers.add_parser("render-latest", help="Render markdown from the default JSON report.")
    report.add_argument("--no-network", action="store_true")
    report.set_defaults(handler=lambda args: _render_latest(args))
    return parser.parse_args(argv)


def _render_latest(args: argparse.Namespace) -> int:
    _require_no_network(args.no_network)
    report = evaluate_mlb_outright_residual(fixture_signals(), fixture_references(), fixture_quotes())
    print(render_markdown(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
