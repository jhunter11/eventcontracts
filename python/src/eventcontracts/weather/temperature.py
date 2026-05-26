"""Temperature threshold models for Kalshi-style weather contracts."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Literal

from eventcontracts.domain.events import EventProvenance, ExternalSignalEvent
from eventcontracts.domain.ids import EventId
from eventcontracts.domain.models import InstrumentId
from eventcontracts.domain.validation import require_aware_datetime, require_non_empty

ThresholdDirection = Literal["above", "below"]


@dataclass(frozen=True)
class WeatherLocation:
    """Point location for a weather market."""

    name: str
    latitude: float
    longitude: float
    timezone: str = "UTC"
    station_id: str | None = None

    def __post_init__(self) -> None:
        require_non_empty(self.name, "name")
        if not -90 <= self.latitude <= 90:
            raise ValueError("latitude must be in [-90, 90]")
        if not -180 <= self.longitude <= 180:
            raise ValueError("longitude must be in [-180, 180]")
        require_non_empty(self.timezone, "timezone")
        if self.station_id is not None:
            require_non_empty(self.station_id, "station_id")


@dataclass(frozen=True)
class HourlyWeatherPoint:
    """One point-in-time hourly weather forecast/observation."""

    timestamp: datetime
    temperature_f: float
    cloud_cover_pct: float | None = None
    precipitation_probability_pct: float | None = None
    wind_speed_mph: float | None = None
    relative_humidity_pct: float | None = None

    def __post_init__(self) -> None:
        require_aware_datetime(self.timestamp, "timestamp")
        _optional_percent(self.cloud_cover_pct, "cloud_cover_pct")
        _optional_percent(self.precipitation_probability_pct, "precipitation_probability_pct")
        _optional_percent(self.relative_humidity_pct, "relative_humidity_pct")
        if self.wind_speed_mph is not None and self.wind_speed_mph < 0:
            raise ValueError("wind_speed_mph must be >= 0")


@dataclass(frozen=True)
class TemperatureForecastSnapshot:
    """Forecast state used to price a daily high/low threshold."""

    location: WeatherLocation
    as_of: datetime
    hourly: tuple[HourlyWeatherPoint, ...]
    source: str = "open-meteo"
    schema_version: str = "weather-temperature-forecast-v1"

    def __post_init__(self) -> None:
        require_aware_datetime(self.as_of, "as_of")
        require_non_empty(self.source, "source")
        require_non_empty(self.schema_version, "schema_version")
        if not self.hourly:
            raise ValueError("hourly forecast must not be empty")
        object.__setattr__(self, "hourly", tuple(sorted(self.hourly, key=lambda point: point.timestamp)))

    def points_for_day(self, target_day: date) -> tuple[HourlyWeatherPoint, ...]:
        return tuple(point for point in self.hourly if point.timestamp.date() == target_day)

    def forecast_high_f(self, target_day: date) -> float:
        points = self.points_for_day(target_day)
        if not points:
            raise ValueError(f"no hourly forecast points for {target_day.isoformat()}")
        return max(point.temperature_f for point in points)

    def nearest_point(self, target_time: datetime, *, max_distance_seconds: int = 5400) -> HourlyWeatherPoint:
        require_aware_datetime(target_time, "target_time")
        nearest = min(self.hourly, key=lambda point: abs((point.timestamp - target_time).total_seconds()))
        distance = abs((nearest.timestamp - target_time).total_seconds())
        if distance > max_distance_seconds:
            raise ValueError(
                "no hourly forecast point within "
                f"{max_distance_seconds} seconds of {target_time.isoformat()}"
            )
        return nearest


@dataclass(frozen=True)
class TemperatureThresholdMarket:
    """Tradable temperature threshold market."""

    instrument_id: InstrumentId
    threshold_f: float
    target_day: date
    direction: ThresholdDirection = "above"
    target_time: datetime | None = None

    def __post_init__(self) -> None:
        if self.direction not in {"above", "below"}:
            raise ValueError("direction must be 'above' or 'below'")
        if self.target_time is not None:
            require_aware_datetime(self.target_time, "target_time")


@dataclass(frozen=True)
class TemperatureThresholdPrediction:
    """Model-implied probability for one threshold market."""

    market: TemperatureThresholdMarket
    location: WeatherLocation
    as_of: datetime
    implied_probability: float
    expected_high_f: float
    raw_forecast_high_f: float
    uncertainty_f: float
    source: str
    target_time: datetime | None = None
    temperature_basis: Literal["target_time", "daily_high"] = "daily_high"
    model_family: str = "gaussian_rules_v2"
    features: Mapping[str, float] | None = None
    schema_version: str = "weather-temperature-probability-v1"

    def __post_init__(self) -> None:
        require_aware_datetime(self.as_of, "as_of")
        if self.target_time is not None:
            require_aware_datetime(self.target_time, "target_time")
        if self.temperature_basis not in {"target_time", "daily_high"}:
            raise ValueError("temperature_basis must be target_time or daily_high")
        require_non_empty(self.model_family, "model_family")
        if not math.isfinite(self.implied_probability) or not 0 <= self.implied_probability <= 1:
            raise ValueError("implied_probability must be finite and in [0, 1]")
        if self.uncertainty_f <= 0:
            raise ValueError("uncertainty_f must be positive")

    def to_external_signal(
        self,
        *,
        received_at: datetime | None = None,
        event_id: EventId | None = None,
    ) -> ExternalSignalEvent:
        event_time = received_at or self.as_of
        return ExternalSignalEvent(
            event_id=event_id
            or EventId(
                "weather-temperature-"
                f"{self.market.instrument_id.market_id}-{self.market.target_day.isoformat()}-{_event_ts(self.as_of)}"
            ),
            source=self.source,
            exchange_ts=self.as_of,
            received_at=event_time,
            schema_version=self.schema_version,
            payload={
                "implied_prob": self.implied_probability,
                "instrument_id": {
                    "venue": self.market.instrument_id.venue.value,
                    "market_id": self.market.instrument_id.market_id,
                    "outcome_id": self.market.instrument_id.outcome_id,
                },
                "location": {
                    "name": self.location.name,
                    "latitude": self.location.latitude,
                    "longitude": self.location.longitude,
                    "timezone": self.location.timezone,
                    "station_id": self.location.station_id,
                },
                "target_day": self.market.target_day.isoformat(),
                "target_time": self.target_time.isoformat() if self.target_time is not None else None,
                "threshold_f": self.market.threshold_f,
                "direction": self.market.direction,
                "temperature_basis": self.temperature_basis,
                "expected_temperature_f": self.expected_high_f,
                "raw_forecast_temperature_f": self.raw_forecast_high_f,
                "expected_high_f": self.expected_high_f,
                "raw_forecast_high_f": self.raw_forecast_high_f,
                "uncertainty_f": self.uncertainty_f,
                "model_family": self.model_family,
                "features": dict(self.features or {}),
            },
            provenance=EventProvenance(
                source=self.source,
                channel="temperature_threshold_model",
                schema_version=self.schema_version,
                venue=self.market.instrument_id.venue,
                metadata={"model_family": self.model_family},
            ),
        )


class TemperatureThresholdModel:
    """Rules-mode probabilistic model for temperature threshold markets.

    This is intentionally simple and auditable for first-pass research. It uses
    either the forecast temperature nearest the market's target timestamp or
    the forecast daily high as the mean, then prices the threshold with a
    Gaussian error distribution whose width grows with time-to-target, wind,
    humidity, and cloud uncertainty.
    """

    def __init__(
        self,
        *,
        base_uncertainty_f: float = 2.1,
        hours_to_peak_weight: float = 0.035,
        cloud_cap_f_per_pct_above_60: float = 0.015,
        wind_uncertainty_weight: float = 0.015,
        humidity_uncertainty_weight: float = 0.004,
        precip_uncertainty_weight: float = 0.006,
        trend_uncertainty_weight: float = 0.050,
        daytime_cloud_bias_weight: float = 0.006,
        bias_f: float = 0.0,
        min_uncertainty_f: float = 0.75,
        model_family: str = "gaussian_rules_v2",
    ) -> None:
        if base_uncertainty_f <= 0:
            raise ValueError("base_uncertainty_f must be positive")
        if min_uncertainty_f <= 0:
            raise ValueError("min_uncertainty_f must be positive")
        require_non_empty(model_family, "model_family")
        self.base_uncertainty_f = base_uncertainty_f
        self.hours_to_peak_weight = hours_to_peak_weight
        self.cloud_cap_f_per_pct_above_60 = cloud_cap_f_per_pct_above_60
        self.wind_uncertainty_weight = wind_uncertainty_weight
        self.humidity_uncertainty_weight = humidity_uncertainty_weight
        self.precip_uncertainty_weight = precip_uncertainty_weight
        self.trend_uncertainty_weight = trend_uncertainty_weight
        self.daytime_cloud_bias_weight = daytime_cloud_bias_weight
        self.bias_f = bias_f
        self.min_uncertainty_f = min_uncertainty_f
        self.model_family = model_family

    def predict(
        self,
        snapshot: TemperatureForecastSnapshot,
        market: TemperatureThresholdMarket,
    ) -> TemperatureThresholdPrediction:
        points = snapshot.points_for_day(market.target_day)
        if not points:
            raise ValueError(f"no forecast points for target day {market.target_day.isoformat()}")
        if market.target_time is not None:
            reference = snapshot.nearest_point(market.target_time)
            raw_temperature = reference.temperature_f
            features = self._features(points, reference, snapshot.as_of)
            expected_temperature = raw_temperature + self.bias_f + self._hourly_bias_adjustment(features)
            basis: Literal["target_time", "daily_high"] = "target_time"
        else:
            reference = max(points, key=lambda point: point.temperature_f)
            raw_temperature = reference.temperature_f
            features = self._features(points, reference, snapshot.as_of)
            expected_temperature = raw_temperature + self.bias_f - self._cloud_cap_adjustment(points, reference)
            basis = "daily_high"
        uncertainty = self._uncertainty(points, reference, snapshot.as_of, features)
        z = (market.threshold_f - expected_temperature) / uncertainty
        probability_above = 1.0 - _normal_cdf(z)
        implied = probability_above if market.direction == "above" else 1.0 - probability_above
        return TemperatureThresholdPrediction(
            market=market,
            location=snapshot.location,
            as_of=snapshot.as_of,
            implied_probability=_clip_probability(implied),
            expected_high_f=expected_temperature,
            raw_forecast_high_f=raw_temperature,
            uncertainty_f=uncertainty,
            source=snapshot.source,
            target_time=market.target_time,
            temperature_basis=basis,
            model_family=self.model_family,
            features=features,
        )

    def _cloud_cap_adjustment(self, points: Sequence[HourlyWeatherPoint], peak: HourlyWeatherPoint) -> float:
        nearby = [
            point.cloud_cover_pct
            for point in points
            if point.cloud_cover_pct is not None and abs((point.timestamp - peak.timestamp).total_seconds()) <= 7200
        ]
        if not nearby:
            return 0.0
        avg_cloud = sum(nearby) / len(nearby)
        return max(0.0, avg_cloud - 60.0) * self.cloud_cap_f_per_pct_above_60

    def _uncertainty(
        self,
        points: Sequence[HourlyWeatherPoint],
        peak: HourlyWeatherPoint,
        as_of: datetime,
        features: Mapping[str, float],
    ) -> float:
        hours_to_peak = max((peak.timestamp - as_of).total_seconds() / 3600.0, 0.0)
        winds = [point.wind_speed_mph for point in points if point.wind_speed_mph is not None]
        humidity = [point.relative_humidity_pct for point in points if point.relative_humidity_pct is not None]
        clouds = [point.cloud_cover_pct for point in points if point.cloud_cover_pct is not None]
        wind_component = (sum(winds) / len(winds)) * self.wind_uncertainty_weight if winds else 0.0
        humidity_component = _stddev(humidity) * self.humidity_uncertainty_weight if len(humidity) >= 2 else 0.0
        cloud_component = _stddev(clouds) * 0.004 if len(clouds) >= 2 else 0.0
        precip_component = features["precipitation_probability_pct"] * self.precip_uncertainty_weight
        trend_component = abs(features["temperature_delta_3h_f"]) * self.trend_uncertainty_weight
        uncertainty = self.base_uncertainty_f + hours_to_peak * self.hours_to_peak_weight
        uncertainty += wind_component + humidity_component + cloud_component + precip_component + trend_component
        return max(self.min_uncertainty_f, uncertainty)

    def _features(
        self,
        points: Sequence[HourlyWeatherPoint],
        reference: HourlyWeatherPoint,
        as_of: datetime,
    ) -> dict[str, float]:
        previous_1h = _nearest_before(points, reference.timestamp, hours=1)
        previous_3h = _nearest_before(points, reference.timestamp, hours=3)
        next_1h = _nearest_after(points, reference.timestamp, hours=1)
        temperatures = [point.temperature_f for point in points]
        cloud = reference.cloud_cover_pct or 0.0
        humidity = reference.relative_humidity_pct or 0.0
        precip = reference.precipitation_probability_pct or 0.0
        wind = reference.wind_speed_mph or 0.0
        hour_angle = 2.0 * math.pi * reference.timestamp.hour / 24.0
        year_angle = 2.0 * math.pi * reference.timestamp.timetuple().tm_yday / 366.0
        return {
            "target_hour_utc": float(reference.timestamp.hour),
            "target_day_of_year": float(reference.timestamp.timetuple().tm_yday),
            "target_hour_sin": math.sin(hour_angle),
            "target_hour_cos": math.cos(hour_angle),
            "day_of_year_sin": math.sin(year_angle),
            "day_of_year_cos": math.cos(year_angle),
            "hours_to_target": max((reference.timestamp - as_of).total_seconds() / 3600.0, 0.0),
            "raw_temperature_f": reference.temperature_f,
            "daily_high_f": max(temperatures),
            "daily_low_f": min(temperatures),
            "daily_range_f": max(temperatures) - min(temperatures),
            "temperature_delta_1h_f": reference.temperature_f - previous_1h.temperature_f,
            "temperature_delta_3h_f": reference.temperature_f - previous_3h.temperature_f,
            "forward_temperature_delta_1h_f": next_1h.temperature_f - reference.temperature_f,
            "cloud_cover_pct": cloud,
            "precipitation_probability_pct": precip,
            "wind_speed_mph": wind,
            "relative_humidity_pct": humidity,
            "cloud_cover_delta_3h_pct": cloud - (previous_3h.cloud_cover_pct or cloud),
            "humidity_delta_3h_pct": humidity - (previous_3h.relative_humidity_pct or humidity),
        }

    def _hourly_bias_adjustment(self, features: Mapping[str, float]) -> float:
        target_hour = int(features["target_hour_utc"])
        daylight_utc = 14 <= target_hour <= 23
        if not daylight_utc:
            return 0.0
        cloud_excess = max(0.0, features["cloud_cover_pct"] - 65.0)
        return -cloud_excess * self.daytime_cloud_bias_weight


def snapshot_from_open_meteo_payload(
    payload: Mapping[str, Any],
    *,
    location: WeatherLocation,
    as_of: datetime,
) -> TemperatureForecastSnapshot:
    """Parse an Open-Meteo `/v1/forecast` response into a model snapshot."""

    require_aware_datetime(as_of, "as_of")
    hourly = payload.get("hourly")
    if not isinstance(hourly, Mapping):
        raise ValueError("Open-Meteo payload missing hourly object")
    times = hourly.get("time")
    temperatures = hourly.get("temperature_2m")
    if not isinstance(times, list) or not isinstance(temperatures, list):
        raise ValueError("Open-Meteo hourly time and temperature_2m must be lists")
    points: list[HourlyWeatherPoint] = []
    for index, raw_time in enumerate(times):
        if index >= len(temperatures):
            break
        points.append(
            HourlyWeatherPoint(
                timestamp=_parse_open_meteo_time(str(raw_time)),
                temperature_f=float(temperatures[index]),
                cloud_cover_pct=_optional_float_at(hourly, "cloud_cover", index),
                precipitation_probability_pct=_optional_float_at(hourly, "precipitation_probability", index),
                wind_speed_mph=_optional_float_at(hourly, "wind_speed_10m", index),
                relative_humidity_pct=_optional_float_at(hourly, "relative_humidity_2m", index),
            )
        )
    return TemperatureForecastSnapshot(
        location=location,
        as_of=as_of,
        hourly=tuple(points),
        source="open-meteo",
        schema_version="open-meteo-forecast-v1",
    )


def _optional_float_at(payload: Mapping[str, Any], key: str, index: int) -> float | None:
    values = payload.get(key)
    if not isinstance(values, list) or index >= len(values):
        return None
    value = values[index]
    if value is None:
        return None
    return float(value)


def _parse_open_meteo_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _optional_percent(value: float | None, name: str) -> None:
    if value is not None and not 0 <= value <= 100:
        raise ValueError(f"{name} must be in [0, 100]")


def _stddev(values: Sequence[float]) -> float:
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / len(values)
    return math.sqrt(variance)


def _nearest_before(
    points: Sequence[HourlyWeatherPoint],
    timestamp: datetime,
    *,
    hours: int,
) -> HourlyWeatherPoint:
    target_seconds = hours * 3600
    candidates = [point for point in points if 0 <= (timestamp - point.timestamp).total_seconds() <= target_seconds]
    if not candidates:
        return min(points, key=lambda point: abs((point.timestamp - timestamp).total_seconds()))
    return min(candidates, key=lambda point: abs((timestamp - point.timestamp).total_seconds() - target_seconds))


def _nearest_after(
    points: Sequence[HourlyWeatherPoint],
    timestamp: datetime,
    *,
    hours: int,
) -> HourlyWeatherPoint:
    target_seconds = hours * 3600
    candidates = [point for point in points if 0 <= (point.timestamp - timestamp).total_seconds() <= target_seconds]
    if not candidates:
        return min(points, key=lambda point: abs((point.timestamp - timestamp).total_seconds()))
    return min(candidates, key=lambda point: abs((point.timestamp - timestamp).total_seconds() - target_seconds))


def _normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def _clip_probability(value: float) -> float:
    return min(0.999, max(0.001, value))


def _event_ts(value: datetime) -> str:
    return value.isoformat().replace("+", "p").replace(":", "").replace("-", "").replace(".", "")
