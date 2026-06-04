"""Decisive experiment: can the v2 tennis model MATCH-OR-BEAT the closing line?

The plain v2 model takes the de-vigged line (`p1_implied_prob`) as a feature yet
posts a WORSE out-of-sample log-loss than the line itself — it overfits the other
features and drifts off a strong prior. The principled fix (standard "beat the
closing line" construction) is to learn the RESIDUAL over the market:

    final_logit = logit(line) + residual(features)

implemented with XGBoost `base_margin = logit(line)`. With no real signal the
residual collapses to ~0 and the model reproduces the line, so it can only help.
Antisymmetry (f(a,b) = 1 - f(b,a)) is preserved exactly by averaging the raw
margins of a row and its swapped mirror:

    final_logit = (margin(x) - margin(swap(x))) / 2

Walk-forward, real data, odds-present universe. Reports, per season and pooled,
the line / plain-model / anchored-model log-loss + Brier, and a post-fee
(0.07*P*(1-P)) betting sweep of the anchored model vs the line.
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
from eventcontracts.research import tennis_v2 as tv2  # noqa: E402
from eventcontracts.research import tennis_xgboost as tx  # noqa: E402

SACKMANN = ROOT / "data" / "tennis" / "tennis_atp" / "tennis_atp-master"
ODDS = ROOT / "data" / "tennis" / "tennis_data_odds"
SINCE_YEAR = 2005
ODDS_FROM = 2013
FIRST_TEST_YEAR = 2018
FEE_RATE = 0.07
IMPLIED_COL = "p1_implied_prob"


def _logit(p: np.ndarray) -> np.ndarray:
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return np.log(p / (1 - p))


def _sigmoid(z: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-z))


def _log_loss(y: np.ndarray, p: np.ndarray) -> float:
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def _brier(y: np.ndarray, p: np.ndarray) -> float:
    return float(np.mean((p - y) ** 2))


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
    frame = tv2.build_v2_training_frame(matches, include_mirrored=True, recent_window=14)
    flag = "odds_present" if "odds_present" in frame.columns else "odds_present_flag"
    frame = frame.filter(
        (pl.col(flag) > 0.5)
        & pl.col(IMPLIED_COL).is_not_null()
        & (pl.col(IMPLIED_COL) > 0.02)
        & (pl.col(IMPLIED_COL) < 0.98)
    )
    print(f"odds-present feature rows={frame.height}")
    return pl, frame


def _train_anchored(xgb, train_frame, *, num_boost_round=600, eta=0.03, max_depth=3):
    """XGBoost residual-over-line model: base_margin = logit(line)."""
    matrix = np.asarray(train_frame.select(tv2.TENNIS_V2_FEATURE_NAMES).to_numpy(), dtype=float)
    label = train_frame["label"].to_numpy().astype(int)
    line = train_frame[IMPLIED_COL].to_numpy().astype(float)
    dtrain = xgb.DMatrix(matrix, label=label, feature_names=list(tv2.TENNIS_V2_FEATURE_NAMES))
    dtrain.set_base_margin(_logit(line))
    params = {
        "objective": "binary:logistic",
        "eval_metric": "logloss",
        "eta": eta,
        "max_depth": max_depth,
        "min_child_weight": 20,  # strong regularization: only learn robust residuals
        "subsample": 0.8,
        "colsample_bytree": 0.7,
        "reg_lambda": 5.0,
        "reg_alpha": 1.0,
        "tree_method": "hist",
    }
    return xgb.train(params, dtrain, num_boost_round=num_boost_round, verbose_eval=False)


def _predict_anchored(xgb, model, test_frame):
    matrix = np.asarray(test_frame.select(tv2.TENNIS_V2_FEATURE_NAMES).to_numpy(), dtype=float)
    line = test_frame[IMPLIED_COL].to_numpy().astype(float)
    swapped = np.asarray(tv2._swap_features(matrix), dtype=float)

    d = xgb.DMatrix(matrix, feature_names=list(tv2.TENNIS_V2_FEATURE_NAMES))
    d.set_base_margin(_logit(line))
    d_sw = xgb.DMatrix(swapped, feature_names=list(tv2.TENNIS_V2_FEATURE_NAMES))
    d_sw.set_base_margin(_logit(1.0 - line))

    margin_ab = model.predict(d, output_margin=True)
    margin_ba = model.predict(d_sw, output_margin=True)
    return _sigmoid((margin_ab - margin_ba) / 2.0)


def main() -> int:
    pl, frame = load_frame()
    xgb = tx._xgb() if hasattr(tx, "_xgb") else __import__("xgboost")
    years = sorted({d.year for d in frame["match_date"].to_list()})

    pooled = {"y": [], "line": [], "plain": [], "anch": []}
    per_season = []

    for year in [y for y in years if y >= FIRST_TEST_YEAR]:
        train = frame.filter(pl.col("match_date").dt.year() < year)
        test = frame.filter(pl.col("match_date").dt.year() == year)
        if train.height < 1000 or test.height < 100:
            continue
        y = test["label"].to_numpy().astype(int)
        line = test[IMPLIED_COL].to_numpy().astype(float)

        plain_model = tv2.train_v2(train, None, num_boost_round=400, early_stopping_rounds=40, use_monotone=True)
        p_plain = np.asarray(tv2.predict_v2_antisymmetric(plain_model, test), dtype=float)

        anch_model = _train_anchored(xgb, train)
        p_anch = _predict_anchored(xgb, anch_model, test)

        per_season.append(
            {
                "season": year,
                "n": test.height,
                "line_ll": round(_log_loss(y, line), 4),
                "plain_ll": round(_log_loss(y, p_plain), 4),
                "anchored_ll": round(_log_loss(y, p_anch), 4),
                "line_brier": round(_brier(y, line), 4),
                "anchored_brier": round(_brier(y, p_anch), 4),
                "anchored_beats_line": bool(_log_loss(y, p_anch) < _log_loss(y, line)),
            }
        )
        pooled["y"].append(y)
        pooled["line"].append(line)
        pooled["plain"].append(p_plain)
        pooled["anch"].append(p_anch)

    print("\n=== PER-SEASON LOG-LOSS (lower is better) ===")
    for r in per_season:
        print(json.dumps(r))

    Y = np.concatenate(pooled["y"])
    L = np.concatenate(pooled["line"])
    PL = np.concatenate(pooled["plain"])
    AN = np.concatenate(pooled["anch"])

    print("\n=== POOLED OUT-OF-SAMPLE (37k+ odds-present matches) ===")
    print(
        json.dumps(
            {
                "n": int(Y.size),
                "line_logloss": round(_log_loss(Y, L), 5),
                "plain_model_logloss": round(_log_loss(Y, PL), 5),
                "anchored_model_logloss": round(_log_loss(Y, AN), 5),
                "line_brier": round(_brier(Y, L), 5),
                "anchored_brier": round(_brier(Y, AN), 5),
                "anchored_beats_line_pooled": bool(_log_loss(Y, AN) < _log_loss(Y, L)),
                "seasons_anchored_beats_line": sum(r["anchored_beats_line"] for r in per_season),
                "seasons_total": len(per_season),
            },
            indent=2,
        )
    )

    # Post-fee betting sweep: anchored model vs line, ROI on capital deployed.
    print("\n=== ANCHORED-MODEL BETTING SWEEP (real Kalshi fee; ROI on capital) ===")
    edge1 = AN - L
    conf = np.maximum(AN, 1.0 - AN)
    best = None
    for b in (100, 150, 200, 300, 500):
        t = b / 10000.0
        for c in (0.0, 0.60, 0.65):
            profit = cost = bets = wins = 0.0
            bets = 0
            for i in range(Y.size):
                if conf[i] < c:
                    continue
                if edge1[i] >= t and L[i] > 0.02:
                    price, won = float(L[i]), (Y[i] == 1)
                elif -edge1[i] >= t and (1 - L[i]) > 0.02:
                    price, won = float(1 - L[i]), (Y[i] == 0)
                else:
                    continue
                profit += ((1.0 - price) if won else -price) - _fee(price)
                cost += price
                bets += 1
                wins += int(won)
            roi = profit / cost if cost else float("nan")
            wr = wins / bets if bets else float("nan")
            flag = " *" if (bets >= 300 and roi > 0.01) else ""
            print(f"edge>={b:>3}bps conf>={c:.2f}: roi={roi:+.4f} wr={wr:.3f} n={bets}{flag}")
            if bets >= 300 and (best is None or roi > best[0]):
                best = (roi, b, c, bets, wr)
    if best:
        print(
            f"\nBEST (>=300 bets): roi={best[0]:+.4f} edge>={best[1]}bps "
            f"conf>={best[2]:.2f} wr={best[4]:.3f} n={best[3]}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
