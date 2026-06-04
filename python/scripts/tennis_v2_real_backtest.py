"""Walk-forward (rolling-origin) backtest of the v2 tennis model on REAL data.

Loads the downloaded Sackmann ATP matches + tennis-data.co.uk decimal odds,
merges them, builds the v2 feature frame, and runs the rolling-origin backtest.
Prints honest per-fold and aggregate accuracy / log-loss / Brier plus the
bookmaker-vs-model comparison and a post-fee edge simulation. Real numbers only.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from eventcontracts.research import tennis_odds as todds  # noqa: E402
from eventcontracts.research import tennis_v2  # noqa: E402
from eventcontracts.research import tennis_xgboost as tx  # noqa: E402

SACKMANN = Path(__file__).resolve().parents[2] / "data" / "sackmann"
ODDS = Path(__file__).resolve().parents[2] / "data" / "odds"
SINCE_YEAR = 2005
ODDS_FROM = 2010
THROUGH = 2024


def main() -> int:
    pl = tx._polars()
    match_files = sorted(SACKMANN.glob("atp_matches_*.csv"))
    frames = [
        pl.read_csv(f, infer_schema_length=20000, ignore_errors=True) for f in match_files
    ]
    matches = pl.concat(frames, how="diagonal_relaxed")
    print(f"loaded {matches.height} matches from {len(match_files)} files")

    odds_files = sorted(ODDS.glob("*.xlsx"))
    odds = todds.load_tennis_data_odds(odds_files)
    matches = todds.merge_odds_into_matches(matches, odds)
    rate = todds.odds_match_rate(matches)
    print(f"odds merged: {len(odds_files)} files, match_rate={rate:.3f}")

    frame = tennis_v2.build_v2_training_frame(
        matches, include_mirrored=True, recent_window=14
    )
    print(f"v2 feature frame rows={frame.height} cols={len(frame.columns)}")

    # 5-fold walk-forward, odds-present only, with a post-fee betting edge sim.
    report = tennis_v2.rolling_origin_backtest(
        frame,
        n_splits=5,
        num_boost_round=400,
        early_stopping_rounds=40,
        use_monotone=True,
        edge_threshold=0.0,
        odds_required=True,
    )
    print("=== ROLLING-ORIGIN BACKTEST (odds-present) ===")
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
