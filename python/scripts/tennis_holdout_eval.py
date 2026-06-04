"""Held-out tennis eval: does the v2 model beat the closing line on 2025-26?

Trains chronologically on matches BEFORE --holdout-start and evaluates on the
held-out tail, comparing three predictors on the SAME held-out matches (only
where bookmaker odds are present, so the market benchmark is defined):

  * MARKET        — overround-normalized bookmaker implied prob (p1_implied_prob)
  * MODEL+odds    — the full 34-feature v2 model (odds are among its inputs)
  * MODEL-no-odds — the 30 non-odds features only (pure model skill vs market)

The metric that decides tradeability is **log-loss / Brier vs the market**, not
accuracy: a model can be "accurate" and still lose to the line. Per the project's
tradeability finding the prior expectation is the model does NOT beat the line;
this re-tests that on fresh 2025-26 data and with retraining.

Read-only research; writes nothing but a printed report (+ optional --json).
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path

import polars as pl

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "python" / "src"))

from eventcontracts.cli.tennis_xgboost import _load_matches, _match_paths  # noqa: E402
from eventcontracts.research import tennis_v2 as t2  # noqa: E402
from eventcontracts.research.tennis_odds import (  # noqa: E402
    load_tennis_data_odds,
    merge_odds_into_matches,
    odds_match_rate,
)

DEFAULT_DATA = ROOT / "data" / "tennis" / "tennis_atp" / "tennis_atp-master"
DEFAULT_ODDS = ROOT / "data" / "tennis" / "tennis_data_odds"
ODDS_FEATURES = {"p1_implied_prob", "implied_prob_diff", "odds_overround", "odds_present"}


def _xgb():
    from importlib import import_module

    return import_module("xgboost")


def _train_subset(train: pl.DataFrame, validation: pl.DataFrame, features: list[str], rhl: float | None):
    """Train an XGBoost booster on an arbitrary feature subset, mirroring
    train_v2's params (no monotone constraints — those are index-aligned to the
    full 34-vector). Recency-weighted to match the production fit."""
    xgb = _xgb()
    params = {
        "objective": "binary:logistic",
        "eval_metric": ["logloss", "auc"],
        "max_depth": 5,
        "eta": 0.04,
        "subsample": 0.85,
        "colsample_bytree": 0.8,
        "min_child_weight": 5,
        "seed": 42,
    }
    try:
        weight = t2._recency_weights(train, "match_date", rhl)  # noqa: SLF001
    except Exception:  # noqa: BLE001
        weight = None
    dtrain = xgb.DMatrix(train.select(features).to_numpy(), label=train["label"].to_numpy(), weight=weight)
    evals = [(dtrain, "train")]
    dval = None
    if validation.height:
        dval = xgb.DMatrix(validation.select(features).to_numpy(), label=validation["label"].to_numpy())
        evals.append((dval, "validation"))
    booster = xgb.train(
        params, dtrain, num_boost_round=600, evals=evals,
        early_stopping_rounds=50 if dval is not None else None, verbose_eval=False,
    )
    return booster


def _predict_subset(booster, frame: pl.DataFrame, features: list[str]) -> list[float]:
    xgb = _xgb()
    return [float(p) for p in booster.predict(xgb.DMatrix(frame.select(features).to_numpy()))]


def _fmt(m) -> str:
    return (f"acc={m.accuracy:.4f}  logloss={m.log_loss:.4f}  brier={m.brier_score:.4f}  "
            f"auc={m.roc_auc:.4f}  ece={m.expected_calibration_error:.4f}")


# --------------------------------------------------------------------------- #
# Experimental orthogonal features (leak-safe: each uses only strictly-prior
# matches). Targets signals a sharp line may underweight — fatigue/workload,
# surface transition, injury proxy, and head-to-head (absent from the 34).
# --------------------------------------------------------------------------- #
EXPERIMENTAL_FEATURES = [
    "exp_tourn_minutes_so_far_diff",
    "exp_tourn_sets_so_far_diff",
    "exp_surface_switch_diff",
    "exp_recent_retirement_diff",
    "exp_h2h_smoothed_edge",
    "exp_h2h_surface_smoothed_edge",
]
_ROUND_RANK = {"R128": 0, "RR": 0, "R64": 1, "R32": 2, "R16": 3, "QF": 4, "SF": 5, "BR": 5, "F": 6}


def _n_sets(score: str | None) -> int:
    if not score:
        return 0
    return sum(1 for tok in str(score).split() if "-" in tok and tok.upper() not in {"W/O", "RET"})


def _to_date(yyyymmdd: int):
    s = int(yyyymmdd)
    return date(s // 10000, (s // 100) % 100, s % 100)


def build_experimental_lookup(matches: pl.DataFrame) -> dict:
    """One chronological pass: for each match, compute experimental features from
    state containing ONLY earlier matches (then update state). Keyed by
    (match_date, frozenset(player_ids)) so the mirrored v2 frame can join either
    orientation."""
    from collections import defaultdict

    hist: dict[str, list] = defaultdict(list)  # pid -> [(date, tourney_id, minutes, nsets, surface, retired)]
    h2h: dict[tuple, int] = defaultdict(int)  # (a,b) -> times a beat b
    h2h_s: dict[tuple, int] = defaultdict(int)  # (a,b,surface)
    lookup: dict = {}
    rows = matches.with_columns(
        pl.col("round").replace_strict(_ROUND_RANK, default=0).alias("_rr")
    ).sort(["tourney_date", "_rr", "match_num"])
    for r in rows.iter_rows(named=True):
        wid, lid = str(r["winner_id"]), str(r["loser_id"])
        d = _to_date(r["tourney_date"])
        tid, surf = r["tourney_id"], r["surface"]
        mins = float(r["minutes"]) if r["minutes"] is not None else 0.0
        nsets = _n_sets(r["score"])

        def _tourn(pid: str) -> tuple[float, float]:
            ms = [h for h in hist[pid] if h[1] == tid]  # noqa: B023 (tid stable per row)
            return sum(h[2] for h in ms), float(sum(h[3] for h in ms))

        def _switch(pid: str) -> float:
            prev = [h for h in hist[pid] if h[1] != tid]  # noqa: B023
            return 1.0 if (prev and prev[-1][4] != surf) else 0.0  # noqa: B023

        def _ret30(pid: str) -> float:
            return 1.0 if any(h[5] and (d - h[0]).days <= 30 for h in hist[pid]) else 0.0  # noqa: B023

        wtmin, wtsets = _tourn(wid)
        ltmin, ltsets = _tourn(lid)
        lookup[(d, frozenset((wid, lid)))] = {
            "by": {
                wid: (wtmin, wtsets, _switch(wid), _ret30(wid)),
                lid: (ltmin, ltsets, _switch(lid), _ret30(lid)),
            },
            "h2h": {wid: (h2h[(wid, lid)], h2h_s[(wid, lid, surf)]),
                    lid: (h2h[(lid, wid)], h2h_s[(lid, wid, surf)])},
        }
        retired = "RET" in str(r["score"] or "")
        hist[wid].append((d, tid, mins, nsets, surf, False))
        hist[lid].append((d, tid, mins, nsets, surf, retired))
        h2h[(wid, lid)] += 1
        h2h_s[(wid, lid, surf)] += 1
    return lookup


def add_experimental_columns(frame: pl.DataFrame, lookup: dict) -> pl.DataFrame:
    cols: dict[str, list[float]] = {name: [] for name in EXPERIMENTAL_FEATURES}
    for row in frame.select(["match_date", "p1_id", "p2_id"]).iter_rows(named=True):
        p1, p2 = str(row["p1_id"]), str(row["p2_id"])
        e = lookup.get((row["match_date"], frozenset((p1, p2))))
        if e is None or p1 not in e["by"] or p2 not in e["by"]:
            for name in EXPERIMENTAL_FEATURES:
                cols[name].append(0.0)
            continue
        b1, b2 = e["by"][p1], e["by"][p2]
        p1w, p1s = e["h2h"][p1]
        p2w, p2s = e["h2h"][p2]
        cols["exp_tourn_minutes_so_far_diff"].append(b1[0] - b2[0])
        cols["exp_tourn_sets_so_far_diff"].append(b1[1] - b2[1])
        cols["exp_surface_switch_diff"].append(b1[2] - b2[2])
        cols["exp_recent_retirement_diff"].append(b1[3] - b2[3])
        cols["exp_h2h_smoothed_edge"].append((p1w + 2) / (p1w + p2w + 4) - 0.5)
        cols["exp_h2h_surface_smoothed_edge"].append((p1s + 2) / (p1s + p2s + 4) - 0.5)
    return frame.with_columns([pl.Series(name, vals, dtype=pl.Float64) for name, vals in cols.items()])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", type=Path, default=DEFAULT_DATA)
    ap.add_argument("--odds-dir", type=Path, default=DEFAULT_ODDS)
    ap.add_argument("--since-year", type=int, default=2013)
    ap.add_argument("--holdout-start", default="2025-01-01", help="test = matches on/after this date")
    ap.add_argument("--recency-half-life-years", type=float, default=3.0)
    ap.add_argument("--elo-layoff-boost", type=float, default=0.0,
                    help="dynamic-K inactivity sensitivity (0 disables the layoff term; production default)")
    ap.add_argument("--experimental", action="store_true",
                    help="also train MODEL+odds+EXPERIMENTAL (orthogonal fatigue/h2h/transition feats)")
    ap.add_argument("--json", type=Path, default=None)
    args = ap.parse_args()

    holdout = date.fromisoformat(args.holdout_start)
    rhl = args.recency_half_life_years if args.recency_half_life_years > 0 else None

    paths = _match_paths(args.data_dir, include_challengers=False, since_year=args.since_year, through_year=None)
    if not paths:
        print(f"ERROR: no atp_matches_YYYY.csv under {args.data_dir}")
        return 2
    matches = _load_matches(pl, paths, since_year=args.since_year, through_year=None, max_matches=None)
    odds = load_tennis_data_odds(args.odds_dir)
    matches = merge_odds_into_matches(matches, odds)
    print(f"loaded {matches.height} matches (since {args.since_year}); odds match_rate={odds_match_rate(matches):.1%}")

    frame = t2.build_v2_training_frame(
        matches, include_mirrored=True, recent_window=14, elo_layoff_boost=args.elo_layoff_boost
    )
    test = frame.filter(pl.col("match_date") >= holdout)
    pre = frame.filter(pl.col("match_date") < holdout)
    if not test.height or not pre.height:
        print(f"ERROR: empty partition (pre={pre.height}, test={test.height})")
        return 2
    # validation = last ~10% of pre-holdout dates (for early stopping)
    cut_dates = pre.select("match_date").unique().sort("match_date")["match_date"].to_list()
    val_cut = cut_dates[int(len(cut_dates) * 0.9)]
    train = pre.filter(pl.col("match_date") < val_cut)
    validation = pre.filter(pl.col("match_date") >= val_cut)
    print(f"train<{val_cut}: {train.height} | val: {validation.height} | test>={holdout}: {test.height} (mirrored)")

    # Restrict the held-out comparison to rows with a real market price.
    test_mkt = test.filter(pl.col("odds_present") == 1.0)
    print(f"held-out rows with bookmaker odds (market benchmark defined): {test_mkt.height} / {test.height}")
    if not test_mkt.height:
        print("ERROR: no held-out rows carry odds; cannot benchmark vs the line")
        return 1
    y = test_mkt["label"].to_list()

    feats_all = list(t2.TENNIS_V2_FEATURE_NAMES)
    feats_no_odds = [f for f in feats_all if f not in ODDS_FEATURES]

    print("\ntraining MODEL+odds (34 feats, monotone+recency)...")
    model_odds = t2.train_v2(train, validation, recency_half_life_years=rhl, use_monotone=True)
    p_model_odds = list(t2.predict_v2(model_odds, test_mkt))

    print(f"training MODEL-no-odds ({len(feats_no_odds)} feats)...")
    model_no = _train_subset(train, validation, feats_no_odds, rhl)
    p_model_no = _predict_subset(model_no, test_mkt, feats_no_odds)

    # Experimental: clean marginal test — same training method (no monotone) with
    # vs without the orthogonal batch, so any delta is the features, not config.
    m_base_nm = m_model_exp = None
    if args.experimental:
        lookup = build_experimental_lookup(matches)
        tr_x = add_experimental_columns(train, lookup)
        va_x = add_experimental_columns(validation, lookup)
        te_x = add_experimental_columns(test_mkt, lookup)
        feats_exp = feats_all + EXPERIMENTAL_FEATURES
        print(f"training MODEL+odds no-mono baseline ({len(feats_all)}) and +EXPERIMENTAL ({len(feats_exp)})...")
        base_nm = _train_subset(train, validation, feats_all, rhl)
        m_base_nm = t2.evaluate_v2_probabilities(y, _predict_subset(base_nm, test_mkt, feats_all))
        model_exp = _train_subset(tr_x, va_x, feats_exp, rhl)
        m_model_exp = t2.evaluate_v2_probabilities(y, _predict_subset(model_exp, te_x, feats_exp))

    p_market = test_mkt["p1_implied_prob"].to_list()

    m_market = t2.evaluate_v2_probabilities(y, p_market)
    m_model_odds = t2.evaluate_v2_probabilities(y, p_model_odds)
    m_model_no = t2.evaluate_v2_probabilities(y, p_model_no)

    print("\n=== HELD-OUT (2025-26) — predictors on identical matches ===")
    print(f"  MARKET (line)        {_fmt(m_market)}")
    print(f"  MODEL + odds         {_fmt(m_model_odds)}")
    print(f"  MODEL - no odds      {_fmt(m_model_no)}")
    if m_base_nm is not None and m_model_exp is not None:
        print(f"  MODEL+odds (no-mono) {_fmt(m_base_nm)}")
        print(f"  MODEL+odds+EXPERMNTL {_fmt(m_model_exp)}")

    beats = m_model_odds.log_loss < m_market.log_loss and m_model_odds.brier_score < m_market.brier_score
    print("\n=== VERDICT ===")
    dll = m_model_odds.log_loss - m_market.log_loss
    db = m_model_odds.brier_score - m_market.brier_score
    print(f"  MODEL+odds vs MARKET: dlogloss={dll:+.4f} dbrier={db:+.4f}  "
          f"-> {'MODEL BEATS LINE' if beats else 'does NOT beat the line'}")
    print(f"  pure model skill (no-odds) vs MARKET: dlogloss={m_model_no.log_loss - m_market.log_loss:+.4f}")
    if m_model_exp is not None and m_base_nm is not None:
        marg = m_model_exp.log_loss - m_base_nm.log_loss
        exp_beats = m_model_exp.log_loss < m_market.log_loss and m_model_exp.brier_score < m_market.brier_score
        print(f"  EXPERIMENTAL marginal (vs same-config baseline): dlogloss={marg:+.4f} "
              f"({'helps' if marg < 0 else 'no help'})")
        print(f"  MODEL+odds+EXPERIMENTAL vs MARKET: dlogloss={m_model_exp.log_loss - m_market.log_loss:+.4f}  "
              f"-> {'BEATS LINE' if exp_beats else 'does NOT beat the line'}")
    if not beats:
        print("  => no tradeable win-model edge on this holdout; consistent with the CLV-not-win-model")
        print("     finding. Better/experimental features must beat the line here to matter.")

    if args.json:
        args.json.write_text(json.dumps({
            "generated_at": datetime.now(UTC).isoformat(),
            "holdout_start": args.holdout_start,
            "n_test_with_odds": test_mkt.height,
            "market": dataclasses_asdict(m_market),
            "model_with_odds": dataclasses_asdict(m_model_odds),
            "model_no_odds": dataclasses_asdict(m_model_no),
            "model_beats_line": beats,
        }, indent=2), encoding="utf-8")
        print(f"\nwrote {args.json}")
    return 0


def dataclasses_asdict(m) -> dict:
    return {
        "samples": m.samples, "accuracy": m.accuracy, "log_loss": m.log_loss,
        "brier_score": m.brier_score, "roc_auc": m.roc_auc,
        "expected_calibration_error": m.expected_calibration_error,
    }


if __name__ == "__main__":
    raise SystemExit(main())
