"""Totals / duration model: can we predict match LENGTH (total games)?

The winner market is sharp and unbeatable; the TOTALS market (over/under total
games) is softer and is where an ML model can add value. This builds a leak-free
pre-match model of total games and tests, walk-forward, whether it beats a strong
baseline -- the prerequisite for a totals-market edge.

Leakage discipline: the favourite/underdog split is derived from the PRE-MATCH
odds (lower decimal odds = favourite), never from who actually won. Total games
is outcome-symmetric, so there is no winner-leak in the target.

Data: tennis-data.co.uk (set-by-set games W1..W5/L1..L5, ranks, surface, court,
best-of, Pinnacle odds for the closeness signal).
"""

from __future__ import annotations

import io
import json
import sys
import warnings
from pathlib import Path

import numpy as np

warnings.filterwarnings("ignore")
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

ROOT = Path(__file__).resolve().parents[2]
ODDS = ROOT / "data" / "tennis" / "tennis_data_odds"
FIRST_TEST_YEAR = 2018


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def load_rows():
    import openpyxl

    recs = []
    for f in sorted(ODDS.glob("*.xlsx")):
        if not (f.stem.isdigit() and int(f.stem) >= 2013):
            continue
        year = int(f.stem)
        wb = openpyxl.load_workbook(f, read_only=True, data_only=True)
        ws = wb.active
        it = ws.iter_rows(values_only=True)
        hdr = list(next(it))
        col = {}
        for i, n in enumerate(hdr):
            if n not in col and n is not None:
                col[n] = i
        if not all(k in col for k in ("PSW", "PSL", "W1", "L1", "Best of")):
            wb.close()
            continue

        def g(r, name):
            i = col.get(name)
            return r[i] if (i is not None and i < len(r)) else None

        for r in it:
            psw, psl = _num(g(r, "PSW")), _num(g(r, "PSL"))
            if not psw or not psl or psw <= 1 or psl <= 1:
                continue
            best_of = _num(g(r, "Best of")) or 3
            # total games across all played sets
            tg, sets_played = 0.0, 0
            for s in range(1, 6):
                ws_, ls_ = _num(g(r, f"W{s}")), _num(g(r, f"L{s}"))
                if ws_ is not None and ls_ is not None:
                    tg += ws_ + ls_
                    sets_played += 1
            if sets_played == 0 or tg < 6:  # walkovers / retirements / missing
                continue
            comment = g(r, "Comment")
            if comment is not None and str(comment).strip().lower() not in ("completed", ""):
                continue  # drop retirements/walkovers when annotated
            wr, lr = _num(g(r, "WRank")), _num(g(r, "LRank"))
            surface = str(g(r, "Surface") or "Hard")
            court = str(g(r, "Court") or "Outdoor")
            # favourite by ODDS (pre-match), not by who won
            fav_is_w = psw <= psl
            fav_rank = wr if fav_is_w else lr
            dog_rank = lr if fav_is_w else wr
            raw = 1 / psw + 1 / psl
            fav_prob = (1 / min(psw, psl)) / raw  # de-vigged favourite prob
            recs.append(
                {
                    "year": year,
                    "total_games": tg,
                    "best_of": best_of,
                    "surface": surface,
                    "indoor": 1.0 if court.lower().startswith("indoor") else 0.0,
                    "fav_rank": fav_rank if fav_rank and fav_rank > 0 else 200.0,
                    "dog_rank": dog_rank if dog_rank and dog_rank > 0 else 200.0,
                    "fav_prob": fav_prob,
                }
            )
        wb.close()
    return recs


def featurize(recs):
    surfaces = ["Hard", "Clay", "Grass", "Carpet"]
    X, yreg, year, best_of = [], [], [], []
    for r in recs:
        fav_rank = max(r["fav_rank"], 1.0)
        dog_rank = max(r["dog_rank"], 1.0)
        row = [
            1.0 if r["best_of"] >= 5 else 0.0,
            r["indoor"],
            r["fav_prob"],
            abs(0.5 - r["fav_prob"]),  # closeness: small = even match (more games)
            np.log(fav_rank),
            np.log(dog_rank),
            np.log(dog_rank) - np.log(fav_rank),  # rank gap
        ]
        row += [1.0 if r["surface"] == s else 0.0 for s in surfaces]
        X.append(row)
        yreg.append(r["total_games"])
        year.append(r["year"])
        best_of.append(int(r["best_of"]))
    names = (
        ["best_of_5", "indoor", "fav_prob", "closeness", "log_fav_rank", "log_dog_rank", "rank_gap"]
        + [f"surface_{s}" for s in surfaces]
    )
    return np.array(X, float), np.array(yreg, float), np.array(year), np.array(best_of), names


def main() -> int:
    import xgboost as xgb

    recs = load_rows()
    X, y, year, best_of, names = featurize(recs)
    print(f"matches with set scores + odds = {len(recs)}  features={len(names)}")
    print(f"total_games: mean={y.mean():.2f} std={y.std():.2f} min={y.min():.0f} max={y.max():.0f}")

    mae_model, mae_base, rmse_model, rmse_base = [], [], [], []
    auc_parts = []
    n_total = 0
    for ty in [t for t in sorted(set(year.tolist())) if t >= FIRST_TEST_YEAR]:
        tr = year < ty
        te = year == ty
        if tr.sum() < 2000 or te.sum() < 200:
            continue
        dtr = xgb.DMatrix(X[tr], label=y[tr], feature_names=names)
        dte = xgb.DMatrix(X[te], feature_names=names)
        params = {
            "objective": "reg:squarederror",
            "eta": 0.03,
            "max_depth": 4,
            "min_child_weight": 20,
            "subsample": 0.8,
            "colsample_bytree": 0.8,
            "reg_lambda": 5.0,
            "tree_method": "hist",
        }
        model = xgb.train(params, dtr, num_boost_round=500, verbose_eval=False)
        pred = model.predict(dte)

        # Strong baseline: mean total games per (best_of, surface) from TRAIN.
        base = np.empty(te.sum())
        Xte_bo = best_of[te]
        # surface one-hot columns start after the 7 numeric features
        surf_cols = {s: 7 + i for i, s in enumerate(["Hard", "Clay", "Grass", "Carpet"])}
        tr_idx = np.where(tr)[0]
        te_idx = np.where(te)[0]
        for j, gi in enumerate(te_idx):
            bo = best_of[gi]
            surf = None
            for s, c in surf_cols.items():
                if X[gi, c] > 0.5:
                    surf = s
            sel = tr & (best_of == bo)
            if surf is not None:
                sel = sel & (X[:, surf_cols[surf]] > 0.5)
            base[j] = y[sel].mean() if sel.sum() > 30 else y[tr].mean()

        yte = y[te]
        mae_model.append((np.mean(np.abs(pred - yte)), te.sum()))
        mae_base.append((np.mean(np.abs(base - yte)), te.sum()))
        rmse_model.append((np.sqrt(np.mean((pred - yte) ** 2)), te.sum()))
        rmse_base.append((np.sqrt(np.mean((base - yte) ** 2)), te.sum()))

        # Over/under the TRAIN median line (per best_of): can the model call it?
        med = {bo: np.median(y[tr & (best_of == bo)]) for bo in (3, 5)}
        line = np.array([med.get(bo, np.median(y[tr])) for bo in Xte_bo])
        over = (yte > line).astype(int)
        # use model's predicted total vs the line as the score
        score = pred - line
        # AUC for predicting 'over'
        order = np.argsort(score)
        ranks = np.empty_like(order, float)
        ranks[order] = np.arange(1, len(score) + 1)
        n_pos = over.sum()
        n_neg = len(over) - n_pos
        if n_pos and n_neg:
            auc = (ranks[over == 1].sum() - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg)
            auc_parts.append((auc, te.sum()))
        n_total += te.sum()

    def w(parts):
        d = sum(int(n) for _, n in parts)
        return float(sum(float(v) * int(n) for v, n in parts) / d) if d else float("nan")

    def _np_safe(o):  # numpy scalars -> native python for json
        return o.item() if hasattr(o, "item") else str(o)

    print("\n=== WALK-FORWARD TOTALS MODEL vs BASELINE (lower MAE/RMSE better) ===")
    print(
        json.dumps(
            {
                "test_matches": int(n_total),
                "model_MAE": round(w(mae_model), 4),
                "baseline_MAE": round(w(mae_base), 4),
                "model_RMSE": round(w(rmse_model), 4),
                "baseline_RMSE": round(w(rmse_base), 4),
                "MAE_improvement_pct": round(100 * (1 - w(mae_model) / w(mae_base)), 2),
                "over_under_AUC": round(w(auc_parts), 4),
            },
            indent=2,
            default=_np_safe,
        )
    )
    print(
        "\nINTERPRETATION: over_under_AUC > 0.55 and positive MAE improvement => the model"
        "\ncarries genuine pre-match signal on match length, so a totals-market edge is"
        "\nplausible (needs totals odds to monetize, which tennis-data lacks)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
