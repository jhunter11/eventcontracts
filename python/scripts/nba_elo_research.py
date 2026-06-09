#!/usr/bin/env python3
"""Run the NBA Elo calibration research pass.

Exit codes:
- 0 when calibrated custom Elo clears the configured significance bar.
- 2 when the run completes but remains CONTINUE_RESEARCH.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "python" / "src"))

from eventcontracts.research.nba_elo import (  # noqa: E402
    load_fivethirtyeight_games,
    load_pregame_features_csv,
    render_markdown,
    report_to_dict,
    run_enhanced_research,
    run_research,
)

FIVETHIRTYEIGHT_NBAALLELO_URL = "https://raw.githubusercontent.com/fivethirtyeight/data/master/nba-elo/nbaallelo.csv"


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    data_path = args.data_csv
    if data_path is None:
        data_path = args.cache_dir / "nbaallelo.csv"
        if args.refresh_data or not data_path.exists():
            _download(FIVETHIRTYEIGHT_NBAALLELO_URL, data_path)

    games = load_fivethirtyeight_games(data_path)
    pregame_features = load_pregame_features_csv(args.pregame_features_csv) if args.pregame_features_csv else None
    common = {
        "data_source": FIVETHIRTYEIGHT_NBAALLELO_URL,
        "downloaded_path": str(data_path),
        "train_years": tuple(args.train_years),
        "validation_years": tuple(args.validation_years),
        "test_years": tuple(args.test_years),
        "bootstrap_samples": args.bootstrap_samples,
        "require_fivethirtyeight_edge": not args.allow_home_prior_only,
    }
    if args.model == "baseline":
        report = run_research(
            games,
            calibration_candidates=args.calibration_candidates,
            **common,  # type: ignore[arg-type]
        )
    else:
        report = run_enhanced_research(
            games,
            pregame_features=pregame_features,
            **common,  # type: ignore[arg-type]
        )

    payload = report_to_dict(report)
    args.report_json.parent.mkdir(parents=True, exist_ok=True)
    args.report_json.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    args.report_md.parent.mkdir(parents=True, exist_ok=True)
    args.report_md.write_text(render_markdown(report), encoding="utf-8")

    print(
        json.dumps(
            {
                "decision": report.decision,
                "ok": report.ok,
                "reason": report.reason,
                "report_json": str(args.report_json),
                "report_md": str(args.report_md),
                "model": args.model,
                "selected_calibration": report.selected_calibration,
                "test_brier_calibrated": report.test_metrics_calibrated.brier,
                "test_ece_calibrated": report.test_metrics_calibrated.ece,
                "significant_vs_home_prior": report.significance_vs_home_prior.significant,
                "significant_vs_fivethirtyeight": (
                    report.significance_vs_fivethirtyeight.significant
                    if report.significance_vs_fivethirtyeight is not None
                    else None
                ),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if report.ok else 2


def _download(url: str, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "eventcontracts-nba-elo-research/1.0"})
    with urllib.request.urlopen(req, timeout=60) as response:
        path.write_bytes(response.read())


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run point-in-time NBA Elo calibration research")
    parser.add_argument("--data-csv", type=Path, default=None, help="Existing nbaallelo.csv path")
    parser.add_argument("--cache-dir", type=Path, default=ROOT / "data" / "research" / "nba_elo")
    parser.add_argument("--refresh-data", action="store_true", help="Redownload the source CSV")
    parser.add_argument(
        "--report-json",
        type=Path,
        default=ROOT / "artifacts" / "research" / "nba_elo" / "nba_elo_report.json",
    )
    parser.add_argument(
        "--report-md",
        type=Path,
        default=ROOT / "artifacts" / "research" / "nba_elo" / "nba_elo_report.md",
    )
    parser.add_argument("--train-years", type=int, nargs=2, default=(1947, 2008), metavar=("START", "END"))
    parser.add_argument("--validation-years", type=int, nargs=2, default=(2009, 2011), metavar=("START", "END"))
    parser.add_argument("--test-years", type=int, nargs=2, default=(2012, 2015), metavar=("START", "END"))
    parser.add_argument("--bootstrap-samples", type=int, default=2000)
    parser.add_argument("--calibration-candidates", type=int, default=25)
    parser.add_argument("--model", choices=("enhanced", "baseline"), default="enhanced")
    parser.add_argument(
        "--pregame-features-csv",
        type=Path,
        default=None,
        help="Optional pregame feature CSV with game_id, team, available_at, player, market, and matchup columns.",
    )
    parser.add_argument(
        "--allow-home-prior-only",
        action="store_true",
        help="Treat home-prior significance as sufficient when no strong reference edge exists.",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
