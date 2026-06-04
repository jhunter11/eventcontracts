"""Validate the model-improvement pass: static full-history bias vs recency-
weighted bias, on the same walk-forward, no-look-ahead harness as the gate.

Two kinds of metric, because aggregate Brier alone hides the problem:
  * AGGREGATE calibration (the original gate): Brier / log-loss / ECE on every
    integer-threshold prediction. The recency fit must not materially regress.
  * DRIFT error (what broke live trading): the bias-corrected forecast error
    err = actual - corrected_high at each test day. We report MAE, p95|err|, and
    the WORST 30-day rolling mean |err| — i.e. how badly the model's center
    drifts in its worst stretch. Recency weighting should shrink that tail.

For each station we walk forward (expanding window, 120-day warmup) and score
the static fit and recency fits at several half-lives, then print a table and
pick the half-life with the best worst-window drift without hurting Brier.
"""

from __future__ import annotations

import io
import statistics
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "python" / "src"))

from eventcontracts.weather.calibration import (  # noqa: E402
    brier_score,
    expected_calibration_error,
    fit_station,
    load_pairs_csv,
    log_loss,
    reliability_bins,
)

CALIB_DIR = ROOT / "data" / "weather-calib"
WARMUP = 120
HALF_LIVES = [None, 45.0, 30.0, 21.0, 14.0]  # None = static full-history (baseline)


def _grid(actual: float) -> range:
    a = round(actual)
    return range(a - 8, a + 9)


def _worst_rolling(abs_errs: list[float], window: int = 30) -> float:
    if len(abs_errs) < window:
        return statistics.fmean(abs_errs) if abs_errs else float("nan")
    worst = 0.0
    run = sum(abs_errs[:window])
    worst = run / window
    for i in range(window, len(abs_errs)):
        run += abs_errs[i] - abs_errs[i - window]
        worst = max(worst, run / window)
    return worst


def walk(pairs, half_life):
    pairs = sorted(pairs, key=lambda p: p.day)
    pred: list[float] = []
    obs: list[int] = []
    abs_center_err: list[float] = []
    signed: list[float] = []
    for i in range(WARMUP, len(pairs)):
        train = pairs[:i]
        test = pairs[i]
        cal = fit_station(test.station if hasattr(test, "station") else "S", train,
                          monthly=(half_life is None), half_life_days=half_life)
        fc, actual, month = test.forecast_high_f, test.actual_high_f, test.day.month
        center = cal.corrected_high(fc, month)
        err = actual - center
        abs_center_err.append(abs(err))
        signed.append(err)
        for thr in _grid(actual):
            pred.append(cal.p_high_at_least(float(thr), fc, month))
            obs.append(1 if round(actual) >= thr else 0)
    return {
        "brier": brier_score(pred, obs),
        "logloss": log_loss(pred, obs),
        "ece": expected_calibration_error(reliability_bins(pred, obs)),
        "mae": statistics.fmean(abs_center_err),
        "p95": sorted(abs_center_err)[int(0.95 * len(abs_center_err))],
        "worst30": _worst_rolling(abs_center_err),
        "meansigned": statistics.fmean(signed),
        "n": len(abs_center_err),
    }


def main() -> int:
    results: dict[str, dict] = {}
    for code in ("NY", "CHI", "MIA"):
        path = CALIB_DIR / f"{code}.csv"
        if not path.exists():
            print(f"missing {path}; run weather_build_calibration_dataset.py first")
            return 2
        pairs = load_pairs_csv(path)
        # load_pairs_csv has no station attr; tag for clarity only
        results[code] = {hl: walk(pairs, hl) for hl in HALF_LIVES}

    label = lambda hl: "static" if hl is None else f"hl={int(hl)}d"  # noqa: E731
    print("=== STATIC vs RECENCY-WEIGHTED (walk-forward, no look-ahead) ===\n")
    for code in ("NY", "CHI", "MIA"):
        print(f"[{code}]")
        print(
            f"  {'fit':8s} {'brier':>7s} {'logloss':>8s} {'ece':>7s} | "
            f"{'MAE':>5s} {'p95':>5s} {'worst30':>7s} {'meanErr':>7s}"
        )
        base = results[code][None]
        for hl in HALF_LIVES:
            r = results[code][hl]
            mark = ""
            if hl is not None:
                better_tail = r["worst30"] < base["worst30"]
                ok_brier = r["brier"] <= base["brier"] * 1.03
                mark = "  <- better drift, brier ok" if (better_tail and ok_brier) else ""
            print(f"  {label(hl):8s} {r['brier']:7.4f} {r['logloss']:8.4f} {r['ece']:7.4f} | "
                  f"{r['mae']:5.2f} {r['p95']:5.2f} {r['worst30']:7.2f} {r['meansigned']:+7.2f}{mark}")
        print()

    # Pick a single global half-life: best mean worst30 improvement that keeps
    # pooled brier within 3% of static.
    print("=== PICK ===")
    best_hl = None
    best_score = -1e9
    for hl in HALF_LIVES:
        if hl is None:
            continue
        worst_impr = statistics.fmean(
            results[c][None]["worst30"] - results[c][hl]["worst30"] for c in ("NY", "CHI", "MIA")
        )
        brier_ratio = statistics.fmean(
            results[c][hl]["brier"] / results[c][None]["brier"] for c in ("NY", "CHI", "MIA")
        )
        ok = brier_ratio <= 1.03
        verdict = "OK" if ok else "brier regressed"
        print(
            f"  hl={int(hl)}d: mean worst30 improvement={worst_impr:+.3f}F  "
            f"brier_ratio={brier_ratio:.3f}  {verdict}"
        )
        if ok and worst_impr > best_score:
            best_score = worst_impr
            best_hl = hl
    print(f"\n  recommended half_life_days = {int(best_hl) if best_hl else 'NONE (keep static)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
