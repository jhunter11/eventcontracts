"""Falsification battery: throw every reasonable lever at the v2 win-model and
check whether ANY configuration beats the closing line on the 2025-26 holdout.

This exists to *validate the negative* (the win-model isn't tradeable vs the line,
per the CLV-not-win-model finding). Selection bias works in our favour: if even the
best of N configs — hand-picked on the test set — fails to beat the line, the
conclusion is robust. We sweep one axis at a time around a default:

  * recency half-life (how hard to favour recent matches)
  * Elo base-K and the layoff boost (rating dynamics)
  * training-history start year
  * model capacity (tree depth) and seed-bagging
  * feature set: +odds (line is an input) / no-odds (pure skill) / +experimental
  * isotonic recalibration of the headline model

For each config we report dlogloss / dbrier vs MARKET and a beats-line flag. For
the headline (MODEL+odds, default) and the single BEST config found we add a
bootstrap 95% CI on the per-match logloss difference vs the line: an edge requires
that CI to sit strictly below 0. Reuses tennis_holdout_eval's exact train/metrics.

Read-only research; prints a table (+ optional --json), writes no model artifacts.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
from datetime import UTC, date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "python" / "src"))
sys.path.insert(0, str(ROOT / "python" / "scripts"))

import polars as pl  # noqa: E402
import tennis_holdout_eval as hev  # noqa: E402  (reuse its load/train/feature helpers)

from eventcontracts.research import tennis_v2 as t2  # noqa: E402

_BASE_PARAMS = {
    "objective": "binary:logistic",
    "eval_metric": ["logloss", "auc"],
    "max_depth": 5,
    "eta": 0.04,
    "subsample": 0.85,
    "colsample_bytree": 0.8,
    "min_child_weight": 5,
    "seed": 42,
}
_ODDS_FEATURES = {"p1_implied_prob", "implied_prob_diff", "odds_overround", "odds_present"}


def _train(train, validation, feats, rhl, *, params_override=None, nrounds=600):
    xgb = hev._xgb()
    params = dict(_BASE_PARAMS)
    if params_override:
        params.update(params_override)
    try:
        weight = t2._recency_weights(train, "match_date", rhl)  # noqa: SLF001
    except Exception:  # noqa: BLE001
        weight = None
    dtrain = xgb.DMatrix(train.select(feats).to_numpy(), label=train["label"].to_numpy(), weight=weight)
    evals = [(dtrain, "train")]
    dval = None
    if validation.height:
        dval = xgb.DMatrix(validation.select(feats).to_numpy(), label=validation["label"].to_numpy())
        evals.append((dval, "validation"))
    return xgb.train(params, dtrain, num_boost_round=nrounds, evals=evals,
                     early_stopping_rounds=50 if dval is not None else None, verbose_eval=False)


def _bootstrap_dll_ci(y, p_model, p_market, *, n=2000, seed=1):
    """95% CI on mean(per-match logloss_model - logloss_market). <0 => model better."""
    eps = 1e-6
    def ll(p, o):
        p = min(1 - eps, max(eps, p))
        return -(o * math.log(p) + (1 - o) * math.log(1 - p))
    diffs = [ll(pm, o) - ll(pk, o) for pm, pk, o in zip(p_model, p_market, y, strict=True)]
    rng = random.Random(seed)
    m = len(diffs)
    means = []
    for _ in range(n):
        s = 0.0
        for _ in range(m):
            s += diffs[rng.randrange(m)]
        means.append(s / m)
    means.sort()
    return means[int(0.025 * n)], sum(diffs) / m, means[int(0.975 * n)]


_MATCH_CACHE: dict[int, pl.DataFrame] = {}
_FRAME_CACHE: dict[tuple, dict] = {}


def _matches(since_year: int) -> pl.DataFrame:
    if since_year not in _MATCH_CACHE:
        paths = hev._match_paths(hev.DEFAULT_DATA, include_challengers=False, since_year=since_year, through_year=None)
        m = hev._load_matches(pl, paths, since_year=since_year, through_year=None, max_matches=None)
        m = hev.merge_odds_into_matches(m, hev.load_tennis_data_odds(hev.DEFAULT_ODDS))
        _MATCH_CACHE[since_year] = m
    return _MATCH_CACHE[since_year]


def _prep(since_year: int, base_k: float, layoff: float, holdout: date) -> dict:
    key = (since_year, base_k, layoff, holdout.isoformat())
    if key in _FRAME_CACHE:
        return _FRAME_CACHE[key]
    frame = t2.build_v2_training_frame(
        _matches(since_year), include_mirrored=True, recent_window=14, elo_base_k=base_k, elo_layoff_boost=layoff
    )
    pre = frame.filter(pl.col("match_date") < holdout)
    test = frame.filter(pl.col("match_date") >= holdout)
    dates = pre.select("match_date").unique().sort("match_date")["match_date"].to_list()
    val_cut = dates[int(len(dates) * 0.9)]
    train = pre.filter(pl.col("match_date") < val_cut)
    validation = pre.filter(pl.col("match_date") >= val_cut)
    test_mkt = test.filter(pl.col("odds_present") == 1.0)
    y = test_mkt["label"].to_list()
    p_market = test_mkt["p1_implied_prob"].to_list()
    out = {
        "train": train, "validation": validation, "test_mkt": test_mkt, "y": y,
        "p_market": p_market, "m_market": t2.evaluate_v2_probabilities(y, p_market),
    }
    _FRAME_CACHE[key] = out
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--holdout-start", default="2025-01-01")
    ap.add_argument("--json", type=Path, default=None)
    ap.add_argument("--quick", action="store_true", help="tiny smoke run (few configs, recent data)")
    args = ap.parse_args()
    holdout = date.fromisoformat(args.holdout_start)

    feats_all = list(t2.TENNIS_V2_FEATURE_NAMES)
    feats_no = [f for f in feats_all if f not in _ODDS_FEATURES]

    # (label, since_year, base_k, layoff, recency_hl, feats, params_override, nrounds)
    if args.quick:
        base_year = 2021
        configs = [
            ("DEFAULT (+odds)", base_year, 250.0, 0.0, 3.0, feats_all, None, 600),
            ("recency=off", base_year, 250.0, 0.0, None, feats_all, None, 600),
            ("no-odds (skill)", base_year, 250.0, 0.0, 3.0, feats_no, None, 600),
        ]
    else:
        base_year = 2013
        configs = [
            ("DEFAULT (+odds, depth5, rec3)", base_year, 250.0, 0.0, 3.0, feats_all, None, 600),
            ("recency=off",                   base_year, 250.0, 0.0, None, feats_all, None, 600),
            ("recency=1y",                    base_year, 250.0, 0.0, 1.0, feats_all, None, 600),
            ("recency=2y",                    base_year, 250.0, 0.0, 2.0, feats_all, None, 600),
            ("recency=5y",                    base_year, 250.0, 0.0, 5.0, feats_all, None, 600),
            ("recency=10y",                   base_year, 250.0, 0.0, 10.0, feats_all, None, 600),
            ("elo_base_k=120",                base_year, 120.0, 0.0, 3.0, feats_all, None, 600),
            ("elo_base_k=400",                base_year, 400.0, 0.0, 3.0, feats_all, None, 600),
            ("elo_layoff=0.5",                base_year, 250.0, 0.5, 3.0, feats_all, None, 600),
            ("elo_layoff=1.0",                base_year, 250.0, 1.0, 3.0, feats_all, None, 600),
            ("since_year=2010",               2010,      250.0, 0.0, 3.0, feats_all, None, 600),
            ("since_year=2018",               2018,      250.0, 0.0, 3.0, feats_all, None, 600),
            ("depth=4",                       base_year, 250.0, 0.0, 3.0, feats_all, {"max_depth": 4}, 600),
            ("depth=7",                       base_year, 250.0, 0.0, 3.0, feats_all, {"max_depth": 7}, 600),
            ("min_child_weight=20",           base_year, 250.0, 0.0, 3.0, feats_all, {"min_child_weight": 20}, 600),
            ("nrounds=1500/eta=0.02",         base_year, 250.0, 0.0, 3.0, feats_all, {"eta": 0.02}, 1500),
            ("no-odds (pure skill)",          base_year, 250.0, 0.0, 3.0, feats_no, None, 600),
        ]

    rows = []
    best = None  # (dll, label, p_model)
    default_pred = None
    for label, sy, bk, lo, rhl, feats, override, nrounds in configs:
        d = _prep(sy, bk, lo, holdout)
        booster = _train(d["train"], d["validation"], feats, rhl, params_override=override, nrounds=nrounds)
        p = hev._predict_subset(booster, d["test_mkt"], feats)
        m = t2.evaluate_v2_probabilities(d["y"], p)
        mk = d["m_market"]
        dll, db = m.log_loss - mk.log_loss, m.brier_score - mk.brier_score
        beats = m.log_loss < mk.log_loss and m.brier_score < mk.brier_score
        rows.append((label, m, dll, db, beats))
        if label.startswith("DEFAULT"):
            default_pred = (d["y"], p, d["p_market"])
        if best is None or dll < best[0]:
            best = (dll, label, d["y"], p, d["p_market"])
        print(f"  {label:32s} ll={m.log_loss:.4f} brier={m.brier_score:.4f} auc={m.roc_auc:.4f} "
              f"dll={dll:+.4f} dbrier={db:+.4f} -> {'BEATS LINE' if beats else 'no'}")

    # seed-bagging on DEFAULT (variance reduction)
    d0 = _prep(base_year, 250.0, 0.0, holdout)
    bag = [hev._predict_subset(_train(d0["train"], d0["validation"], feats_all, 3.0,
           params_override={"seed": s}), d0["test_mkt"], feats_all) for s in (42, 7, 123, 2024)]
    p_bag = [sum(col) / len(col) for col in zip(*bag, strict=True)]
    m_bag = t2.evaluate_v2_probabilities(d0["y"], p_bag)
    mk0 = d0["m_market"]
    print(f"  {'seed-bag(4) +odds':32s} ll={m_bag.log_loss:.4f} brier={m_bag.brier_score:.4f} "
          f"auc={m_bag.roc_auc:.4f} dll={m_bag.log_loss - mk0.log_loss:+.4f} "
          f"-> {'BEATS LINE' if (m_bag.log_loss < mk0.log_loss and m_bag.brier_score < mk0.brier_score) else 'no'}")
    if m_bag.log_loss - mk0.log_loss < best[0]:
        best = (m_bag.log_loss - mk0.log_loss, "seed-bag(4)", d0["y"], p_bag, d0["p_market"])

    # isotonic recalibration of the DEFAULT model (does fixing calibration help logloss?)
    try:
        from sklearn.isotonic import IsotonicRegression
        b0 = _train(d0["train"], d0["validation"], feats_all, 3.0)
        pv = hev._predict_subset(b0, d0["validation"], feats_all)
        iso = IsotonicRegression(out_of_bounds="clip").fit(pv, d0["validation"]["label"].to_numpy())
        p_iso = list(iso.predict(hev._predict_subset(b0, d0["test_mkt"], feats_all)))
        m_iso = t2.evaluate_v2_probabilities(d0["y"], p_iso)
        print(f"  {'isotonic-recalibrated +odds':32s} ll={m_iso.log_loss:.4f} brier={m_iso.brier_score:.4f} "
              f"auc={m_iso.roc_auc:.4f} dll={m_iso.log_loss - mk0.log_loss:+.4f} "
              f"-> {'BEATS LINE' if (m_iso.log_loss < mk0.log_loss and m_iso.brier_score < mk0.brier_score) else 'no'}")
    except Exception as exc:  # noqa: BLE001
        print(f"  isotonic-recalibrated: skipped ({exc})")

    # experimental orthogonal features: marginal vs same-config baseline + vs line
    lookup = hev.build_experimental_lookup(_matches(base_year))
    tr_x = hev.add_experimental_columns(d0["train"], lookup)
    va_x = hev.add_experimental_columns(d0["validation"], lookup)
    te_x = hev.add_experimental_columns(d0["test_mkt"], lookup)
    feats_exp = feats_all + hev.EXPERIMENTAL_FEATURES
    b_base = _train(d0["train"], d0["validation"], feats_all, 3.0)
    m_base = t2.evaluate_v2_probabilities(d0["y"], hev._predict_subset(b_base, d0["test_mkt"], feats_all))
    b_exp = _train(tr_x, va_x, feats_exp, 3.0)
    m_exp = t2.evaluate_v2_probabilities(d0["y"], hev._predict_subset(b_exp, te_x, feats_exp))
    print(f"\n  EXPERIMENTAL marginal: dll(+exp vs same-config base)={m_exp.log_loss - m_base.log_loss:+.4f} "
          f"({'helps' if m_exp.log_loss < m_base.log_loss else 'no help'}); "
          f"+exp vs LINE dll={m_exp.log_loss - mk0.log_loss:+.4f}")

    # bootstrap CIs (default headline + best config found)
    print("\n=== VERDICT (bootstrap 95% CI on mean per-match logloss vs LINE; <0 => model beats line) ===")
    out_json = {"generated_at": datetime.now(UTC).isoformat(), "holdout": args.holdout_start,
                "market_logloss": mk0.log_loss, "market_brier": mk0.brier_score, "market_auc": mk0.roc_auc,
                "n_test_with_odds": d0["test_mkt"].height, "configs": []}
    for lbl, m, dll, db, beats in rows:
        out_json["configs"].append({"label": lbl, "logloss": m.log_loss, "brier": m.brier_score,
                                    "auc": m.roc_auc, "dll": dll, "dbrier": db, "beats_line": beats})
    if default_pred:
        lo, mid, hi = _bootstrap_dll_ci(*default_pred)
        print(f"  DEFAULT +odds:  mean dll={mid:+.4f}  95% CI [{lo:+.4f}, {hi:+.4f}]  "
              f"-> {'edge' if hi < 0 else 'NO edge (CI not below 0)'}")
        out_json["default_ci"] = [lo, mid, hi]
    lo, mid, hi = _bootstrap_dll_ci(best[2], best[3], best[4])
    print(f"  BEST config ({best[1]}): mean dll={mid:+.4f}  95% CI [{lo:+.4f}, {hi:+.4f}]  "
          f"-> {'edge' if hi < 0 else 'NO edge (CI not below 0)'}")
    n_beats = sum(1 for *_x, b in rows if b)
    print(f"\n  configs that beat the line (logloss AND brier): {n_beats} / {len(rows)}")
    skill_auc = next((m.roc_auc for lbl, m, *_ in rows if "no-odds" in lbl), float("nan"))
    print(f"  market AUC={mk0.roc_auc:.4f} vs best no-odds skill AUC={skill_auc:.4f} "
          f"(discrimination gap = the model can't even rank as well as the line)")
    out_json["best"] = {"label": best[1], "ci": [lo, mid, hi]}
    out_json["n_beats"] = n_beats
    if args.json:
        args.json.write_text(json.dumps(out_json, indent=2))
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
