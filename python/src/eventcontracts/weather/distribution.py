"""Distributional KXHIGH pricing with intraday high-so-far support.

The existing :mod:`eventcontracts.weather.kxhigh` path prices a Kalshi bracket
from a single calibrated forecast high. This module keeps that settlement logic
but exposes the whole terminal daily-high distribution so live/paper workflows
can add:

* high-so-far constraints from station observations, and
* optional ensemble members for empirical tails when a weather provider returns
  multiple plausible highs.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from eventcontracts.domain.validation import require_aware_datetime, require_non_empty
from eventcontracts.weather.calibration import StationCalibration
from eventcontracts.weather.kxhigh import KalshiHighContract
from eventcontracts.weather.temperature import TemperatureForecastSnapshot


@dataclass(frozen=True)
class StationObservationSnapshot:
    """Observed station high-so-far for a settlement day."""

    station_code: str
    target_day: date
    observed_high_f: float
    as_of: datetime
    source: str

    def __post_init__(self) -> None:
        require_non_empty(self.station_code, "station_code")
        require_aware_datetime(self.as_of, "as_of")
        require_non_empty(self.source, "source")
        if not math.isfinite(self.observed_high_f):
            raise ValueError("observed_high_f must be finite")


@dataclass(frozen=True)
class EnsembleMemberHigh:
    """One provider/member daily-high forecast before station calibration."""

    source: str
    member_id: str
    forecast_high_f: float
    weight: float = 1.0

    def __post_init__(self) -> None:
        require_non_empty(self.source, "source")
        require_non_empty(self.member_id, "member_id")
        if not math.isfinite(self.forecast_high_f):
            raise ValueError("forecast_high_f must be finite")
        if not math.isfinite(self.weight) or self.weight <= 0:
            raise ValueError("weight must be finite and positive")


@dataclass(frozen=True)
class DailyHighDistribution:
    """Terminal daily-high distribution used to price a full KXHIGH ladder."""

    station_code: str
    target_day: date
    as_of: datetime
    method: str
    mean_f: float
    sigma_f: float | None
    high_so_far_f: float | None
    latent_mean_f: float | None = None
    values_f: tuple[float, ...] = ()
    weights: tuple[float, ...] = ()
    feature_hash: str = ""
    schema_version: str = "weather-daily-high-distribution-v1"

    def __post_init__(self) -> None:
        require_non_empty(self.station_code, "station_code")
        require_aware_datetime(self.as_of, "as_of")
        require_non_empty(self.method, "method")
        if not math.isfinite(self.mean_f):
            raise ValueError("mean_f must be finite")
        if self.latent_mean_f is not None and not math.isfinite(self.latent_mean_f):
            raise ValueError("latent_mean_f must be finite")
        if self.sigma_f is not None and (not math.isfinite(self.sigma_f) or self.sigma_f <= 0):
            raise ValueError("sigma_f must be finite and positive")
        if self.high_so_far_f is not None and not math.isfinite(self.high_so_far_f):
            raise ValueError("high_so_far_f must be finite")
        if self.values_f or self.weights:
            if len(self.values_f) != len(self.weights):
                raise ValueError("values_f and weights must have the same length")
            if not self.values_f:
                raise ValueError("empirical distribution needs at least one value")
            if any(not math.isfinite(v) for v in self.values_f):
                raise ValueError("values_f must be finite")
            if any((not math.isfinite(w) or w <= 0) for w in self.weights):
                raise ValueError("weights must be finite and positive")
            if not math.isclose(sum(self.weights), 1.0, abs_tol=1e-9):
                raise ValueError("weights must sum to 1")
        require_non_empty(self.feature_hash, "feature_hash")
        require_non_empty(self.schema_version, "schema_version")


@dataclass(frozen=True)
class KxhighBracketValuation:
    """YES fair value for one parsed KXHIGH bracket."""

    ticker: str
    yes_probability: float
    fair_yes_cents: float
    distribution: DailyHighDistribution
    strike_type: str
    floor_strike: float | None
    cap_strike: float | None

    def __post_init__(self) -> None:
        require_non_empty(self.ticker, "ticker")
        if not math.isfinite(self.yes_probability) or not 0.0 <= self.yes_probability <= 1.0:
            raise ValueError("yes_probability must be finite and in [0, 1]")
        if not math.isfinite(self.fair_yes_cents) or not 0.0 <= self.fair_yes_cents <= 100.0:
            raise ValueError("fair_yes_cents must be finite and in [0, 100]")


def build_daily_high_distribution(
    snapshot: TemperatureForecastSnapshot,
    target_day: date,
    calibration: StationCalibration,
    *,
    observation: StationObservationSnapshot | None = None,
    ensemble_members: Sequence[EnsembleMemberHigh] | None = None,
) -> DailyHighDistribution:
    """Build a calibrated terminal daily-high distribution.

    Without ensemble members this is a Normal distribution over the final
    integer-settled high. With members it is a weighted empirical distribution
    over calibrated member highs. In both cases, an observed high-so-far is a
    lower bound on the terminal high.
    """

    if observation is not None:
        if observation.station_code != calibration.station:
            raise ValueError("observation station does not match calibration")
        if observation.target_day != target_day:
            raise ValueError("observation target_day does not match distribution target_day")

    raw_high = snapshot.forecast_high_f(target_day)
    high_so_far_f = observation.observed_high_f if observation is not None else None
    as_of = _max_datetime(snapshot.as_of, observation.as_of if observation is not None else None)
    month = target_day.month

    if ensemble_members:
        calibrated_values = tuple(
            _apply_high_so_far(calibration.corrected_high(member.forecast_high_f, month), high_so_far_f)
            for member in ensemble_members
        )
        weights = _normalize_weights(tuple(member.weight for member in ensemble_members))
        mean_f = sum(v * w for v, w in zip(calibrated_values, weights, strict=True))
        feature_hash = _stable_hash(
            {
                "station": calibration.station,
                "target_day": target_day,
                "method": "empirical_ensemble_calibrated",
                "members": [
                    {
                        "source": member.source,
                        "member_id": member.member_id,
                        "forecast_high_f": member.forecast_high_f,
                        "weight": member.weight,
                    }
                    for member in ensemble_members
                ],
                "high_so_far_f": high_so_far_f,
                "calibration": calibration.to_dict(),
            }
        )
        return DailyHighDistribution(
            station_code=calibration.station,
            target_day=target_day,
            as_of=as_of,
            method="empirical_ensemble_calibrated",
            mean_f=mean_f,
            sigma_f=None,
            high_so_far_f=high_so_far_f,
            values_f=calibrated_values,
            weights=weights,
            feature_hash=feature_hash,
        )

    mu = calibration.corrected_high(raw_high, month)
    sigma = calibration.effective_sigma()
    mean_f = _terminal_normal_mean(mu, sigma, high_so_far_f)
    feature_hash = _stable_hash(
        {
            "station": calibration.station,
            "target_day": target_day,
            "method": "normal_calibrated",
            "raw_forecast_high_f": raw_high,
            "high_so_far_f": high_so_far_f,
            "calibration": calibration.to_dict(),
        }
    )
    return DailyHighDistribution(
        station_code=calibration.station,
        target_day=target_day,
        as_of=as_of,
        method="normal_calibrated",
        mean_f=mean_f,
        sigma_f=sigma,
        high_so_far_f=high_so_far_f,
        latent_mean_f=mu,
        feature_hash=feature_hash,
    )


def probability_for_contract(contract: KalshiHighContract, distribution: DailyHighDistribution) -> float:
    """YES probability for a parsed KXHIGH contract under a distribution."""

    _validate_contract_distribution(contract, distribution)
    if contract.strike_type == "greater":
        if contract.floor_strike is None:
            raise ValueError(f"greater bracket {contract.ticker} missing floor_strike")
        return probability_at_least(distribution, contract.floor_strike + 1.0)
    if contract.strike_type == "less":
        if contract.cap_strike is None:
            raise ValueError(f"less bracket {contract.ticker} missing cap_strike")
        return _clip_probability(1.0 - probability_at_least(distribution, contract.cap_strike))
    if contract.strike_type == "between":
        if contract.floor_strike is None or contract.cap_strike is None:
            raise ValueError(f"between bracket {contract.ticker} missing floor/cap")
        return probability_between(distribution, contract.floor_strike, contract.cap_strike)
    raise ValueError(f"unknown strike_type {contract.strike_type!r} for {contract.ticker}")


def price_kxhigh_ladder(
    contracts: Sequence[KalshiHighContract],
    distribution: DailyHighDistribution,
) -> tuple[KxhighBracketValuation, ...]:
    """Price a sequence of parsed KXHIGH contracts."""

    valuations: list[KxhighBracketValuation] = []
    for contract in contracts:
        yes_probability = probability_for_contract(contract, distribution)
        valuations.append(
            KxhighBracketValuation(
                ticker=contract.ticker,
                yes_probability=yes_probability,
                fair_yes_cents=yes_probability * 100.0,
                distribution=distribution,
                strike_type=contract.strike_type,
                floor_strike=contract.floor_strike,
                cap_strike=contract.cap_strike,
            )
        )
    return tuple(sorted(valuations, key=lambda valuation: valuation.ticker))


def probability_between(distribution: DailyHighDistribution, floor_f: float, cap_f: float) -> float:
    """P(floor <= terminal integer high <= cap)."""

    if cap_f < floor_f:
        raise ValueError("cap_f must be >= floor_f")
    if distribution.values_f:
        return _weighted_share(
            distribution,
            lambda high_f: _rounded_high(high_f) >= floor_f and _rounded_high(high_f) <= cap_f,
        )
    return _clip_probability(
        probability_at_least(distribution, floor_f) - probability_at_least(distribution, cap_f + 1.0)
    )


def probability_at_least(distribution: DailyHighDistribution, threshold_f: float) -> float:
    """P(terminal integer high >= threshold)."""

    if distribution.high_so_far_f is not None and _rounded_high(distribution.high_so_far_f) >= threshold_f:
        return 1.0
    if distribution.values_f:
        return _weighted_share(distribution, lambda high_f: _rounded_high(high_f) >= threshold_f)
    if distribution.sigma_f is None:
        raise ValueError("normal distribution requires sigma_f")
    mean_f = distribution.latent_mean_f if distribution.latent_mean_f is not None else distribution.mean_f
    z = (threshold_f - 0.5 - mean_f) / distribution.sigma_f
    return _clip_probability(1.0 - _norm_cdf(z))


def _validate_contract_distribution(
    contract: KalshiHighContract,
    distribution: DailyHighDistribution,
) -> None:
    if contract.station_code != distribution.station_code:
        raise ValueError("contract station does not match distribution")
    if contract.target_day != distribution.target_day:
        raise ValueError("contract target_day does not match distribution")


def _weighted_share(distribution: DailyHighDistribution, predicate: Callable[[float], bool]) -> float:
    return _clip_probability(
        sum(
            weight
            for value, weight in zip(distribution.values_f, distribution.weights, strict=True)
            if predicate(value)
        )
    )


def _normalize_weights(weights: tuple[float, ...]) -> tuple[float, ...]:
    total = sum(weights)
    if total <= 0:
        raise ValueError("weights must sum to a positive number")
    return tuple(weight / total for weight in weights)


def _apply_high_so_far(value_f: float, high_so_far_f: float | None) -> float:
    return value_f if high_so_far_f is None else max(value_f, high_so_far_f)


def _terminal_normal_mean(mu: float, sigma: float, high_so_far_f: float | None) -> float:
    if high_so_far_f is None:
        return mu
    alpha = (high_so_far_f - mu) / sigma
    return mu + sigma * _norm_pdf(alpha) + (high_so_far_f - mu) * _norm_cdf(alpha)


def _rounded_high(value_f: float) -> int:
    # NWS/Kalshi settlement is an integer daily high; half-up matches the
    # existing 0.5 continuity correction in StationCalibration.
    return math.floor(value_f + 0.5)


def _max_datetime(first: datetime, second: datetime | None) -> datetime:
    require_aware_datetime(first, "first")
    if second is None:
        return first
    require_aware_datetime(second, "second")
    return max(first, second)


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _clip_probability(p: float) -> float:
    return max(0.0, min(1.0, p))


def _stable_hash(value: Any) -> str:
    payload = json.dumps(_jsonable(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(item) for item in value]
    return value
