"""Weather-domain clients and predictive models."""

from eventcontracts.weather.clients import NoaaCdoClient, NwsClient, OpenMeteoClient
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
    "HourlyWeatherPoint",
    "NoaaCdoClient",
    "NwsClient",
    "OpenMeteoClient",
    "TemperatureForecastSnapshot",
    "TemperatureThresholdMarket",
    "TemperatureThresholdModel",
    "TemperatureThresholdPrediction",
    "WeatherLocation",
    "snapshot_from_open_meteo_payload",
]
