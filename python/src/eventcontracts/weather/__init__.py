"""Weather-domain clients and predictive models."""

from eventcontracts.weather.calibration import (
    ForecastActual,
    StationCalibration,
    brier_score,
    expected_calibration_error,
    fit_station,
    load_calibration_meta,
    load_calibrations,
    load_pairs_csv,
    log_loss,
    reliability_bins,
    save_calibrations,
)
from eventcontracts.weather.clients import NoaaCdoClient, NwsClient, OpenMeteoClient
from eventcontracts.weather.kxhigh import (
    KXHIGH_STATIONS,
    KalshiHighContract,
    daily_high_from_snapshot,
    parse_kxhigh_market,
)
from eventcontracts.weather.temperature import (
    HourlyWeatherPoint,
    TemperatureForecastSnapshot,
    TemperatureThresholdMarket,
    TemperatureThresholdModel,
    TemperatureThresholdPrediction,
    WeatherLocation,
    snapshot_from_open_meteo_payload,
)

__all__ = [
    "KXHIGH_STATIONS",
    "ForecastActual",
    "HourlyWeatherPoint",
    "KalshiHighContract",
    "NoaaCdoClient",
    "NwsClient",
    "OpenMeteoClient",
    "StationCalibration",
    "TemperatureForecastSnapshot",
    "TemperatureThresholdMarket",
    "TemperatureThresholdModel",
    "TemperatureThresholdPrediction",
    "WeatherLocation",
    "brier_score",
    "daily_high_from_snapshot",
    "expected_calibration_error",
    "fit_station",
    "load_calibration_meta",
    "load_calibrations",
    "parse_kxhigh_market",
    "load_pairs_csv",
    "log_loss",
    "reliability_bins",
    "save_calibrations",
    "snapshot_from_open_meteo_payload",
]
