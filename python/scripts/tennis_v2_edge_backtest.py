"""Live-capital gate: does the v2 tennis WINNER model beat the Kalshi line after
the REAL Kalshi fee?

Walk-forward on canonical data/tennis/. For each test season Y the model trains
on all prior seasons and is evaluated only on odds-present matches (the live
sleeve's require_odds_present=true universe).

Fee model (source of truth: rust/crates/fees/src/lib.rs):
    fee_per_contract = 0.07 * P * (1 - P)   (P = backed side's price, dollars)
Buy 1 contract per qualifying signal at the de-vigged market price P:
    win  -> +(1 - P) - fee ; lose -> -P - fee
ROI is on CAPITAL DEPLOYED (sum of P paid) -- the honest Kalshi-native return.
"""

from __future__ import annotations

import io
import json
import re
import sys
from pathlib import Path

import numpy as np

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

MAIN_FILE = re.compile(r"atp_matches_(\d{4})\.csv$")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "python" / "src"))

from eventcontracts.research import tennis_odds as todds  # noqa: E402
from eventcontracts.research import tennis_v2  # noqa: E402
from eventcontracts.research import tennis_xgboost as tx  # noqa: E402

SACKMANN = ROOT / "data" / "tennis" / "tennis_atp" / "tennis_atp-master"
ODDS = ROOT / "data" / "tennis" / "tennis_data_odds"
SINCE_YEAR = 2005
ODDS_FROM = 2013
FIRST_TEST_YEAR = 2018
FEE_RATE = 0.07
SWEEP_BPS = (150, 250, 400, 600, 800, 1000)
SWEEP_CONF = (0.0, 0.60, 0.65, 0.70)


def _log_loss(y: np.ndarray, p: np.ndarray) -> float:
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def _fee(price: float) -> float:
    return FEE_RATE * price * (1.0 - price)


def load_frame():
    pl = tx._polars()
    files = [
        f
        for f in sorted(SACKMANN.glob("atp_matches_*.csv"))
        if (m := MAIN_FILE.search(f.name)) and int(m.group(1)) >= SINCE_YEAR
    ]
    frames = [
        pl.read_csv(f, infer_schema_length=20000, ignore_errors=True, truncate_ragged_lines=True)
        for f in files
    ]
    matches = pl.concat(frames, how="diagonal_relaxed")
    odds_files = [
        f for f in sorted(ODDS.glob("*.xlsx")) if f.stem.isdigit() and int(f.stem) >= ODDS_FROM
    ]
    odds = todds.load_tennis_data_odds(odds_files)
    matches = todds.merge_odds_into_matches(matches, odds)
    print(
        f"loaded {matches.height} matches from {len(files)} files | "
        f"odds_files={len(odds_files)} odds_match_rate={todds.odds_match_rate(matches):.3f}"
    )
    frame = tennis_v2.build_v2_training_frame(matches, include_mirrored=True, recent_window=14)
    odds_flag = "odds_present" if "odds_present" in frame.columns else "odds_present_flag"
    frame = frame.filter(
        (pl.col(odds_flag) > 0.5)
        & pl.col("p1_implied_prob").is_not_null()
        & (pl.col("p1_implied_prob") > 0.01)
        & (pl.col("p1_implied_prob") < 0.99)
    )
    print(f"odds-present feature rows={frame.height}")
    return pl, frame


def main() -> int:
    pl, frame = load_frame()
    years = sorted({d.year for d in frame["match_date"].to_list()})

    sweep = {
        (b, c): {"profit": 0.0, "cost": 0.0, "bets": 0, "wins": 0}
        for b in SWEEP_BPS
        for c in SWEEP_CONF
    }
    agg = {"mc": 0, "bc": 0, "n": 0, "mll": [], "bll": []}
    rows = []

    for year in [y for y in years if y >= FIRST_TEST_YEAR]:
        train = frame.filter(pl.col("match_date").dt.year() < year)
        test = frame.filter(pl.col("match_date").dt.year() == year)
        if train.height < 1000 or test.height < 100:
            continue
        model = tennis_v2.train_v2(
            train, None, num_boost_round=400, early_stopping_rounds=40, use_monotone=True
        )
        p_model = np.asarray(tennis_v2.predict_v2_antisymmetric(model, test), dtype=float)
        y = test["label"].to_numpy().astype(int)
        p_book = test["p1_implied_prob"].to_numpy().astype(float)

        agg["mc"] += int(round(float(np.mean((p_model >= 0.5).astype(int) == y)) * test.height))
        agg["bc"] += int(round(float(np.mean((p_book >= 0.5).astype(int) == y)) * test.height))
        agg["n"] += test.height
        m_ll, b_ll = _log_loss(y, p_model), _log_loss(y, p_book)
        agg["mll"].append((m_ll, test.height))
        agg["bll"].append((b_ll, test.height))
        rows.append(
            {
                "season": year,
                "test_rows": test.height,
                "model_acc": round(float(np.mean((p_model >= 0.5).astype(int) == y)), 4),
                "book_acc": round(float(np.mean((p_book >= 0.5).astype(int) == y)), 4),
                "model_logloss": round(m_ll, 4),
                "book_logloss": round(b_ll, 4),
            }
        )

        edge1 = p_model - p_book
        conf = np.maximum(p_model, 1.0 - p_model)
        for b in SWEEP_BPS:
            t = b / 10000.0
            for c in SWEEP_CONF:
                cell = sweep[(b, c)]
                for i in range(test.height):
                    if conf[i] < c:
                        continue
                    if edge1[i] >= t and p_book[i] > 0.01:
                        price, won = float(p_book[i]), (y[i] == 1)
                    elif -edge1[i] >= t and (1 - p_book[i]) > 0.01:
                        price, won = float(1 - p_book[i]), (y[i] == 0)
                    else:
                        continue
                    cell["profit"] += ((1.0 - price) if won else -price) - _fee(price)
                    cell["cost"] += price
                    cell["bets"] += 1
                    cell["wins"] += int(won)

    def _wmean(pairs):
        den = sum(n for _, n in pairs)
        return sum(v * n for v, n in pairs) / den if den else float("nan")

    print("\n=== PER-SEASON (calibration vs the line) ===")
    for r in rows:
        print(json.dumps(r))
    print("\n=== AGGREGATE CALIBRATION ===")
    print(
        json.dumps(
            {
                "total_test_rows": agg["n"],
                "model_accuracy": round(agg["mc"] / agg["n"], 4),
                "bookmaker_accuracy": round(agg["bc"] / agg["n"], 4),
                "model_logloss": round(_wmean(agg["mll"]), 4),
                "bookmaker_logloss": round(_wmean(agg["bll"]), 4),
            },
            indent=2,
        )
    )

    print("\n=== OPERATING-POINT SWEEP (REAL Kalshi fee 0.07*P*(1-P); ROI on capital deployed) ===")
    best = None
    for b in SWEEP_BPS:
        parts = []
        for c in SWEEP_CONF:
            cell = sweep[(b, c)]
            roi = cell["profit"] / cell["cost"] if cell["cost"] else float("nan")
            wr = cell["wins"] / cell["bets"] if cell["bets"] else float("nan")
            parts.append(f"conf>={c:.2f}: roi={roi:+.4f} wr={wr:.3f} n={cell['bets']}")
            if cell["bets"] >= 300 and (best is None or roi > best[0]):
                best = (roi, b, c, cell["bets"], wr)
        print(f"edge>={b:>4}bps | " + " | ".join(parts))
    if best:
        print(
            f"\nBEST (>=300 bets): roi_on_capital={best[0]:+.4f} at edge>={best[1]}bps "
            f"conf>={best[2]:.2f} | win_rate={best[4]:.3f} over {best[3]} bets"
        )
        print(
            "WINNER-MARKET VERDICT:",
            "TRADEABLE" if best[0] > 0.01 else "NOT tradeable (<=1% ROI / likely noise)",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
