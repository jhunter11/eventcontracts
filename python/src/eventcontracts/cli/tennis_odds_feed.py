"""CLI: merge operator-supplied bookmaker odds into the tennis matches table.

Closes the live-sleeve odds gap (audit F8). The live tennis sleeve sets
``require_odds_present = true``, so a snapshot without both players' decimal
odds never trades. This command takes the upcoming-matches CSV and a
vendor-neutral ``player,decimal_odds`` odds file and writes an enriched matches
CSV whose ``p1_decimal_odds`` / ``p2_decimal_odds`` columns are populated by
name match — a drop-in ``--matches`` input for ``tennis-xgboost-score``.

The command reports the per-match coverage and, with ``--min-match-rate``,
fails loudly when too few matches received odds, so a thin feed cannot quietly
turn the live sleeve into a no-op.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from eventcontracts.research.tennis_odds_feed import merge_odds_file


def register(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "tennis-merge-odds",
        help=(
            "Populate p1/p2_decimal_odds on an upcoming-matches CSV from a "
            "vendor-neutral player,decimal_odds odds file (closes the live "
            "odds gate)."
        ),
    )
    parser.add_argument(
        "--matches",
        type=Path,
        required=True,
        help="Upcoming-matches CSV with player_1/player_2 columns.",
    )
    parser.add_argument(
        "--odds",
        type=Path,
        required=True,
        help="Operator odds CSV with player and decimal_odds columns (one row per player).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        required=True,
        help="Destination CSV (input columns preserved; odds columns filled).",
    )
    parser.add_argument(
        "--min-match-rate",
        type=float,
        default=0.0,
        help=(
            "Fail (exit 2) if the fraction of matches that received both "
            "players' odds is below this threshold. Default 0.0 (report only)."
        ),
    )
    parser.set_defaults(handler=_handle)


def _handle(args: argparse.Namespace) -> int:
    report = merge_odds_file(args.matches, args.odds, args.out)
    print(report.summary())
    print(f"wrote {args.out}")
    if report.unmatched:
        print(f"unmatched matches ({len(report.unmatched)}):")
        for p1, p2 in report.unmatched:
            print(f"  - {p1 or '?'} vs {p2 or '?'}")
    if report.match_rate < args.min_match_rate:
        print(
            f"ERROR: odds match rate {report.match_rate:.1%} is below the "
            f"required {args.min_match_rate:.1%}; the live sleeve would be a "
            f"near no-op. Supply a fuller odds file before go-live."
        )
        return 2
    return 0
