"""Station-level calibration for the temperature-threshold model.

The raw Open-Meteo daily-high forecast is biased versus the NWS station that a
Kalshi weather contract actually settles on (e.g. Chicago Midway runs ~+1.2 F
cold in the model), and its true day-to-day error (sd ~1.2-1.5 F) is much
tighter than the model's hand-tuned `base_uncertainty_f`. Trading an uncalibrated
fair value mostly trades that model error, not real edge.

This module fits, per station, from a paired (forecast_high, actual_high)
history:

  * ``bias_f``  = mean(actual - forecast)        -> additive correction
  * ``sigma_f`` = std(actual - forecast - bias)  -> the honest forecast error
  * optional per-calendar-month bias when the seasonal swing is material.

It then prices a threshold market with a Normal(forecast + bias, sigma) CDF —
the same distributional form the existing model uses, but with parameters fit to
ground truth instead of guessed. Everything here is pure/numpy-free so it stays
deterministic and unit-testable; live wiring reads the persisted JSON fit.
"""

from __future__ import annotations

import json
import math
import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ForecastActual:
    """One paired day: model forecast high vs the settled station high (F)."""

    day: date
    forecast_high_f: float
    actual_high_f: float

    @property
    def error_f(self) -> float:
        """actual - forecast (positive = model ran cold)."""
        return self.actual_high_f - self.forecast_high_f


@dataclass(frozen=True)
class StationCalibration:
    """Fitted additive bias + residual sigma for one settlement station."""

    station: str
    bias_f: float
    sigma_f: float
    n: int
    monthly_bias_f: Mapping[int, float] = field(default_factory=dict)
    min_sigma_f: float = 0.75

    def corrected_high(self, forecast_high_f: float, month: int | None = None) -> float:
        bias = self.bias_f
        if month is not None and month in self.monthly_bias_f:
            bias = self.monthly_bias_f[month]
        return forecast_high_f + bias

    def effective_sigma(self) -> float:
        return max(self.min_sigma_f, self.sigma_f)

    # ---- threshold pricing (integer-degree Kalshi brackets) ----
    def p_high_at_least(self, threshold_f: float, forecast_high_f: float, month: int | None = None) -> float:
        """P(settled integer high >= threshold_f)."""
        mu = self.corrected_high(forecast_high_f, month)
        z = (threshold_f - 0.5 - mu) / self.effective_sigma()
        return _clip(1.0 - _norm_cdf(z))

    def p_between(
        self, floor_f: float, cap_f: float, forecast_high_f: float, month: int | None = None
    ) -> float:
        """P(floor_f <= settled integer high <= cap_f) for a 'between' bracket."""
        return _clip(
            self.p_high_at_least(floor_f, forecast_high_f, month)
            - self.p_high_at_least(cap_f + 1.0, forecast_high_f, month)
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "station": self.station,
            "bias_f": round(self.bias_f, 4),
            "sigma_f": round(self.sigma_f, 4),
            "n": self.n,
            "monthly_bias_f": {str(k): round(v, 4) for k, v in self.monthly_bias_f.items()},
            "min_sigma_f": self.min_sigma_f,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> StationCalibration:
        return cls(
            station=str(d["station"]),
            bias_f=float(d["bias_f"]),
            sigma_f=float(d["sigma_f"]),
            n=int(d["n"]),
            monthly_bias_f={int(k): float(v) for k, v in dict(d.get("monthly_bias_f", {})).items()},
            min_sigma_f=float(d.get("min_sigma_f", 0.75)),
        )


def fit_station(
    station: str,
    pairs: Sequence[ForecastActual],
    *,
    monthly: bool = True,
    monthly_min_n: int = 20,
    min_sigma_f: float = 0.75,
    half_life_days: float | None = None,
) -> StationCalibration:
    """Fit bias + residual sigma. Sigma is computed around the bias actually used,
    so it reflects post-correction error.

    ``half_life_days`` switches on **recency weighting**: errors are weighted by
    ``0.5 ** (age_in_days / half_life)`` where age is measured from the most
    recent pair. This tracks the *current* forecast regime — the full-history
    monthly bias is blind to drift (e.g. a late-May warm spell where the model
    runs hot for weeks), which is what made the static fit's live fair values
    untradeable. With recency on, the trailing window *is* the season, so the
    per-calendar-month term is dropped (``monthly_bias_f`` left empty) and the
    fit is meant to be regenerated each day on data through the latest actual."""
    if not pairs:
        raise ValueError("need at least one forecast/actual pair to fit")

    if half_life_days is not None:
        return _fit_recency(station, pairs, half_life_days=half_life_days, min_sigma_f=min_sigma_f)

    errors = [p.error_f for p in pairs]
    bias = statistics.fmean(errors)

    monthly_bias: dict[int, float] = {}
    if monthly:
        by_month: dict[int, list[float]] = {}
        for p in pairs:
            by_month.setdefault(p.day.month, []).append(p.error_f)
        for m, errs in by_month.items():
            if len(errs) >= monthly_min_n:
                monthly_bias[m] = statistics.fmean(errs)

    residuals = []
    for p in pairs:
        b = monthly_bias.get(p.day.month, bias) if monthly else bias
        residuals.append(p.error_f - b)
    sigma = statistics.pstdev(residuals) if len(residuals) > 1 else min_sigma_f
    return StationCalibration(
        station=station,
        bias_f=bias,
        sigma_f=sigma,
        n=len(pairs),
        monthly_bias_f=monthly_bias,
        min_sigma_f=min_sigma_f,
    )


def _fit_recency(
    station: str,
    pairs: Sequence[ForecastActual],
    *,
    half_life_days: float,
    min_sigma_f: float,
) -> StationCalibration:
    """Exponentially recency-weighted bias + residual sigma."""
    if half_life_days <= 0:
        raise ValueError("half_life_days must be positive")
    ordered = sorted(pairs, key=lambda p: p.day)
    last_day = ordered[-1].day
    decay = math.log(2.0) / half_life_days
    weights = [math.exp(-decay * (last_day - p.day).days) for p in ordered]
    wsum = sum(weights)
    if wsum <= 0:
        raise ValueError("degenerate recency weights")
    bias = sum(w * p.error_f for w, p in zip(weights, ordered, strict=True)) / wsum
    var = sum(w * (p.error_f - bias) ** 2 for w, p in zip(weights, ordered, strict=True)) / wsum
    sigma = math.sqrt(var)
    return StationCalibration(
        station=station,
        bias_f=bias,
        sigma_f=sigma,
        n=len(ordered),
        monthly_bias_f={},
        min_sigma_f=min_sigma_f,
    )


# ---------- evaluation: reliability + Brier on threshold predictions ----------
@dataclass(frozen=True)
class ReliabilityBin:
    lo: float
    hi: float
    n: int
    mean_pred: float
    mean_obs: float


def brier_score(pred: Sequence[float], obs: Sequence[int]) -> float:
    if not pred:
        return float("nan")
    return statistics.fmean((p - o) ** 2 for p, o in zip(pred, obs, strict=True))


def log_loss(pred: Sequence[float], obs: Sequence[int]) -> float:
    if not pred:
        return float("nan")
    return statistics.fmean(
        -(o * math.log(_clip(p)) + (1 - o) * math.log(_clip(1.0 - p)))
        for p, o in zip(pred, obs, strict=True)
    )


def reliability_bins(pred: Sequence[float], obs: Sequence[int], n_bins: int = 10) -> list[ReliabilityBin]:
    bins: list[ReliabilityBin] = []
    for i in range(n_bins):
        lo, hi = i / n_bins, (i + 1) / n_bins
        sel = [
            (p, o)
            for p, o in zip(pred, obs, strict=True)
            if (lo <= p < hi or (i == n_bins - 1 and p == 1.0))
        ]
        if not sel:
            continue
        ps = [p for p, _ in sel]
        os_ = [o for _, o in sel]
        bins.append(
            ReliabilityBin(lo, hi, len(sel), statistics.fmean(ps), statistics.fmean(os_))
        )
    return bins


def expected_calibration_error(bins: Sequence[ReliabilityBin]) -> float:
    total = sum(b.n for b in bins)
    if total == 0:
        return float("nan")
    return sum(b.n * abs(b.mean_pred - b.mean_obs) for b in bins) / total


# ---------- IO ----------
def load_pairs_csv(path: str | Path) -> list[ForecastActual]:
    import csv

    out: list[ForecastActual] = []
    with Path(path).open(newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            out.append(
                ForecastActual(
                    day=date.fromisoformat(r["date"]),
                    forecast_high_f=float(r["om_archive_high_f"]),
                    actual_high_f=float(r["ghcnd_tmax_f"]),
                )
            )
    return out


def save_calibrations(
    calibs: Iterable[StationCalibration],
    path: str | Path,
    *,
    provenance: Mapping[str, Any] | None = None,
) -> None:
    """Persist per-station fits as JSON.

    An optional ``provenance`` block is stored under the reserved ``_meta`` key so a
    committed fit records *how/when* it was produced — fit method, lead semantics,
    data window — for reproducibility and leakage review. Keys beginning with ``_``
    are skipped by :func:`load_calibrations`, so ``_meta`` never parses as a station.
    Provenance is intentionally kept out of :meth:`StationCalibration.to_dict` so it
    cannot perturb the pricing ``feature_hash`` that consumes ``to_dict()``.
    """
    payload: dict[str, Any] = {c.station: c.to_dict() for c in calibs}
    if provenance is not None:
        payload["_meta"] = dict(provenance)
    Path(path).write_text(json.dumps(payload, indent=2), encoding="utf-8")


def load_calibrations(path: str | Path) -> dict[str, StationCalibration]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return {k: StationCalibration.from_dict(v) for k, v in raw.items() if not k.startswith("_")}


def load_calibration_meta(path: str | Path) -> dict[str, Any]:
    """Return the persisted ``_meta`` provenance block, or ``{}`` if none is present."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    meta = raw.get("_meta")
    return dict(meta) if isinstance(meta, Mapping) else {}


# ---------- math ----------
def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _clip(p: float, lo: float = 1e-6, hi: float = 1.0 - 1e-6) -> float:
    return max(lo, min(hi, p))
