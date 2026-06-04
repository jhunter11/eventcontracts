"""Phase 1 TEST: walk-forward calibration gate for the weather model.

Proves, on the real 731-day forecast/actual dataset, that calibrating the daily-
high distribution (per-station bias + fitted sigma) makes THRESHOLD predictions
materially better-calibrated than the uncalibrated model — the prerequisite for
trusting any market edge.

Method (honest, no look-ahead):
  * Walk forward day by day. For each test day, fit calibration ONLY on prior
    days (expanding window, min 120 days warmup), then predict every integer
    threshold bracket's P(high>=k) and P(in [k,k+1]) for that day.
  * Compare two models on identical (prediction, realized-outcome) pairs:
      A) UNCALIBRATED: Normal(forecast_high, sigma=2.1)  (the repo default)
      B) CALIBRATED:   Normal(forecast_high+bias_fit, sigma_fit)
  * Score Brier, log-loss, ECE, and a reliability table. Lower Brier/LL + lower
    ECE = better calibrated. This is the deployment gate.

Reads data/weather-calib/{NY,CHI,MIA}.csv (from
weather_build_calibration_dataset.py). Persists the FINAL full-history fit to
configs/weather/station_calibrations.json for live use.
"""

from __future__ import annotations

import io
import math
import sys
from datetime import UTC, datetime
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
    save_calibrations,
)

CALIB_DIR = ROOT / "data" / "weather-calib"
OUT_DIR = ROOT / "configs" / "weather"
WARMUP = 120
UNCAL_SIGMA = 2.1  # repo default base_uncertainty_f
HALF_LIFE_DAYS = 21.0  # recency-weighted bias; see weather_calibration_compare.py


def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _clip(p, lo=1e-6, hi=1 - 1e-6):
    return max(lo, min(hi, p))


def p_at_least_uncal(thr, fc, sigma=UNCAL_SIGMA):
    return _clip(1.0 - _ncdf((thr - 0.5 - fc) / sigma))


def threshold_grid(actual_high: float):
    """Integer thresholds spanning the realized high +/- 8 F (covers live brackets)."""
    lo = int(round(actual_high)) - 8
    hi = int(round(actual_high)) + 8
    return list(range(lo, hi + 1))


def main() -> int:
    stations = {}
    for code in ("NY", "CHI", "MIA"):
        path = CALIB_DIR / f"{code}.csv"
        if not path.exists():
            print(f"missing {path}; run weather_build_calibration_dataset.py first")
            return 2
        stations[code] = load_pairs_csv(path)

    overall = {"uncal": {"pred": [], "obs": []}, "cal": {"pred": [], "obs": []}}
    print("=== WALK-FORWARD CALIBRATION GATE (expanding window, no look-ahead) ===\n")
    print(
        f"{'station':8s} {'days':>5s} {'A_brier':>8s} {'B_brier':>8s} "
        f"{'A_LL':>7s} {'B_LL':>7s} {'A_ECE':>7s} {'B_ECE':>7s}  verdict"
    )

    for code, pairs in stations.items():
        pairs = sorted(pairs, key=lambda p: p.day)
        a_pred, a_obs, b_pred, b_obs = [], [], [], []
        scored_days = 0
        for i in range(WARMUP, len(pairs)):
            train = pairs[:i]
            test = pairs[i]
            cal = fit_station(code, train, half_life_days=HALF_LIFE_DAYS)
            fc = test.forecast_high_f
            actual = test.actual_high_f
            month = test.day.month
            for thr in threshold_grid(actual):
                outcome = 1 if round(actual) >= thr else 0
                # A) uncalibrated
                a_pred.append(p_at_least_uncal(thr, fc))
                a_obs.append(outcome)
                # B) calibrated
                b_pred.append(cal.p_high_at_least(float(thr), fc, month))
                b_obs.append(outcome)
            scored_days += 1

        a_brier, b_brier = brier_score(a_pred, a_obs), brier_score(b_pred, b_obs)
        a_ll, b_ll = log_loss(a_pred, a_obs), log_loss(b_pred, b_obs)
        a_ece = expected_calibration_error(reliability_bins(a_pred, a_obs))
        b_ece = expected_calibration_error(reliability_bins(b_pred, b_obs))
        verdict = "CAL BETTER" if (b_brier < a_brier and b_ece < a_ece) else "no improvement"
        print(
            f"{code:8s} {scored_days:5d} {a_brier:8.4f} {b_brier:8.4f} "
            f"{a_ll:7.4f} {b_ll:7.4f} {a_ece:7.4f} {b_ece:7.4f}  {verdict}"
        )
        overall["uncal"]["pred"] += a_pred
        overall["uncal"]["obs"] += a_obs
        overall["cal"]["pred"] += b_pred
        overall["cal"]["obs"] += b_obs

    ap, ao = overall["uncal"]["pred"], overall["uncal"]["obs"]
    bp, bo = overall["cal"]["pred"], overall["cal"]["obs"]
    print("\n=== POOLED ===")
    print(f"  predictions scored: {len(ap):,}")
    uncal_ece = expected_calibration_error(reliability_bins(ap, ao))
    cal_ece = expected_calibration_error(reliability_bins(bp, bo))
    print(f"  UNCAL : brier={brier_score(ap, ao):.4f} logloss={log_loss(ap, ao):.4f} ece={uncal_ece:.4f}")
    print(f"  CAL   : brier={brier_score(bp, bo):.4f} logloss={log_loss(bp, bo):.4f} ece={cal_ece:.4f}")
    bimp = (brier_score(ap, ao) - brier_score(bp, bo)) / brier_score(ap, ao) * 100
    print(f"  Brier improvement: {bimp:+.1f}%")

    print("\n  CAL reliability (pred vs observed frequency):")
    for b in reliability_bins(bp, bo, n_bins=10):
        bar = "#" * int(b.mean_obs * 40)
        print(f"    [{b.lo:.1f},{b.hi:.1f}) n={b.n:5d} pred={b.mean_pred:.3f} obs={b.mean_obs:.3f} {bar}")

    # Persist FINAL recency-weighted fit (through latest actual) for live use.
    # This is a trailing-window snapshot and is meant to be regenerated each day
    # as new GHCND actuals land.
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    calibs = [fit_station(code, pairs, half_life_days=HALF_LIFE_DAYS) for code, pairs in stations.items()]
    out_path = OUT_DIR / "station_calibrations.json"
    # Stamp provenance so the committed artifact records how it was produced — fit
    # method, lead semantics, and data window — for reproducibility / leakage review.
    provenance = {
        "generated_at": datetime.now(UTC).isoformat(),
        "generated_by": "python/scripts/weather_calibration_report.py",
        "fit_method": "recency_weighted",
        "half_life_days": HALF_LIFE_DAYS,
        "warmup_days": WARMUP,
        "lead_semantics": (
            "sigma fit on the Open-Meteo historical-forecast archive at ~nowcast lead; "
            "lead>=1 brackets are over-confident (see "
            "docs/weather-kxhigh-validation-and-edge-spec.md Phase 2). The live producer "
            "and offline recorder both emit only lead==0 signals."
        ),
        "source_dataset": "data/weather-calib/{NY,CHI,MIA}.csv",
        "source_api": "historical-forecast-api.open-meteo.com",
        "stations": {
            code: {
                "n": len(pairs),
                "first_day": min(p.day for p in pairs).isoformat(),
                "last_day": max(p.day for p in pairs).isoformat(),
            }
            for code, pairs in stations.items()
        },
    }
    save_calibrations(calibs, out_path, provenance=provenance)
    print(f"\n  wrote final calibrations (half_life={HALF_LIFE_DAYS:.0f}d) -> {out_path.relative_to(ROOT)}")
    for c in calibs:
        print(f"    {c.station}: bias={c.bias_f:+.2f}F sigma={c.sigma_f:.2f}F n={c.n}")

    gate_pass = brier_score(bp, bo) < brier_score(ap, ao) and expected_calibration_error(
        reliability_bins(bp, bo)
    ) < expected_calibration_error(reliability_bins(ap, ao))
    print(f"\nCALIBRATION GATE: {'PASS' if gate_pass else 'FAIL'}")
    return 0 if gate_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
