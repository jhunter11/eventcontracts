"""Free weather data clients used by the weather research path."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Any

import httpx

from eventcontracts.weather.temperature import (
    TemperatureForecastSnapshot,
    WeatherLocation,
    snapshot_from_open_meteo_payload,
)

OPEN_METEO_BASE_URL = "https://api.open-meteo.com"
OPEN_METEO_HISTORICAL_FORECAST_BASE_URL = "https://historical-forecast-api.open-meteo.com"
NWS_BASE_URL = "https://api.weather.gov"
NOAA_CDO_BASE_URL = "https://www.ncdc.noaa.gov/cdo-web/api/v2"


class OpenMeteoClient:
    """Async Open-Meteo forecast client."""

    def __init__(
        self,
        *,
        base_url: str = OPEN_METEO_BASE_URL,
        historical_forecast_base_url: str = OPEN_METEO_HISTORICAL_FORECAST_BASE_URL,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.historical_forecast_base_url = historical_forecast_base_url.rstrip("/")
        self._client = http_client

    @classmethod
    def from_env(cls) -> OpenMeteoClient:
        return cls(
            base_url=os.getenv("OPEN_METEO_BASE_URL") or OPEN_METEO_BASE_URL,
            historical_forecast_base_url=(
                os.getenv("OPEN_METEO_HISTORICAL_FORECAST_BASE_URL")
                or OPEN_METEO_HISTORICAL_FORECAST_BASE_URL
            ),
        )

    async def get_forecast_payload(
        self,
        *,
        latitude: float,
        longitude: float,
        forecast_days: int = 2,
        timezone: str = "UTC",
    ) -> dict[str, Any]:
        params: dict[str, str | int | float] = {
            "latitude": latitude,
            "longitude": longitude,
            "hourly": ",".join(
                (
                    "temperature_2m",
                    "cloud_cover",
                    "precipitation_probability",
                    "wind_speed_10m",
                    "relative_humidity_2m",
                )
            ),
            "temperature_unit": "fahrenheit",
            "wind_speed_unit": "mph",
            "timezone": timezone,
            "forecast_days": forecast_days,
        }
        return await self._get("/v1/forecast", params=params)

    async def get_historical_forecast_payload(
        self,
        *,
        latitude: float,
        longitude: float,
        start_date: date,
        end_date: date,
        timezone: str = "UTC",
    ) -> dict[str, Any]:
        params: dict[str, str | int | float] = {
            "latitude": latitude,
            "longitude": longitude,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "hourly": ",".join(
                (
                    "temperature_2m",
                    "cloud_cover",
                    "precipitation_probability",
                    "wind_speed_10m",
                    "relative_humidity_2m",
                )
            ),
            "temperature_unit": "fahrenheit",
            "wind_speed_unit": "mph",
            "timezone": timezone,
        }
        return await self._get_absolute(f"{self.historical_forecast_base_url}/v1/forecast", params=params)

    async def temperature_snapshot(
        self,
        *,
        location: WeatherLocation,
        as_of: datetime,
        forecast_days: int = 2,
    ) -> TemperatureForecastSnapshot:
        payload = await self.get_forecast_payload(
            latitude=location.latitude,
            longitude=location.longitude,
            timezone=location.timezone,
            forecast_days=forecast_days,
        )
        return snapshot_from_open_meteo_payload(payload, location=location, as_of=as_of)

    async def _get(self, path: str, params: Mapping[str, str | int | float]) -> dict[str, Any]:
        return await self._get_absolute(f"{self.base_url}{path}", params=params)

    async def _get_absolute(self, url: str, params: Mapping[str, str | int | float]) -> dict[str, Any]:
        if self._client is not None:
            response = await self._client.get(url, params=params)
            response.raise_for_status()
            return _as_dict(response.json())
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            return _as_dict(response.json())


class NwsClient:
    """Minimal National Weather Service API client."""

    def __init__(
        self,
        *,
        base_url: str = NWS_BASE_URL,
        user_agent: str = "eventcontracts/0.1 research",
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.user_agent = user_agent
        self._client = http_client

    @classmethod
    def from_env(cls) -> NwsClient:
        return cls(
            base_url=os.getenv("NWS_BASE_URL") or NWS_BASE_URL,
            user_agent=os.getenv("NWS_USER_AGENT") or "eventcontracts/0.1 research",
        )

    async def point_metadata(self, *, latitude: float, longitude: float) -> dict[str, Any]:
        return await self._get(f"/points/{latitude:.4f},{longitude:.4f}")

    async def observation_stations(self, *, latitude: float, longitude: float) -> dict[str, Any]:
        point = await self.point_metadata(latitude=latitude, longitude=longitude)
        stations_url = _nested_str(point, ("properties", "observationStations"))
        if not stations_url:
            raise ValueError("NWS point metadata missing observationStations URL")
        return await self._get_absolute(stations_url)

    async def latest_station_observations(self, station_id: str, *, limit: int = 24) -> dict[str, Any]:
        return await self._get(f"/stations/{station_id}/observations", params={"limit": limit})

    async def _get(self, path: str, params: Mapping[str, str | int | float] | None = None) -> dict[str, Any]:
        return await self._get_absolute(f"{self.base_url}{path}", params=params)

    async def _get_absolute(
        self,
        url: str,
        params: Mapping[str, str | int | float] | None = None,
    ) -> dict[str, Any]:
        headers = {"User-Agent": self.user_agent, "Accept": "application/geo+json, application/json"}
        if self._client is not None:
            response = await self._client.get(url, params=params, headers=headers)
            response.raise_for_status()
            return _as_dict(response.json())
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get(url, params=params, headers=headers)
            response.raise_for_status()
            return _as_dict(response.json())


class NoaaCdoClient:
    """NOAA Climate Data Online v2 client."""

    def __init__(
        self,
        *,
        token: str,
        base_url: str = NOAA_CDO_BASE_URL,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        if not token:
            raise ValueError("NOAA CDO token is required")
        self.token = token
        self.base_url = base_url.rstrip("/")
        self._client = http_client

    @classmethod
    def from_env(cls) -> NoaaCdoClient:
        token = os.getenv("NOAA_TOKEN") or ""
        return cls(token=token, base_url=os.getenv("NOAA_CDO_BASE_URL") or NOAA_CDO_BASE_URL)

    async def daily_summaries(
        self,
        *,
        station_id: str,
        start_date: date,
        end_date: date,
        datatype_ids: Sequence[str] = ("TMAX", "TMIN", "PRCP"),
        units: str = "standard",
        limit: int = 1000,
    ) -> dict[str, Any]:
        params: dict[str, str | int] = {
            "datasetid": "GHCND",
            "stationid": station_id,
            "startdate": start_date.isoformat(),
            "enddate": end_date.isoformat(),
            "datatypeid": ",".join(datatype_ids),
            "units": units,
            "limit": limit,
        }
        return await self._get("/data", params=params)

    async def _get(self, path: str, params: Mapping[str, str | int]) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        headers = {"token": self.token}
        if self._client is not None:
            response = await self._client.get(url, params=params, headers=headers)
            response.raise_for_status()
            return _as_dict(response.json())
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get(url, params=params, headers=headers)
            response.raise_for_status()
            return _as_dict(response.json())


def _as_dict(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("expected JSON object")
    return value


def _nested_str(payload: Mapping[str, Any], path: tuple[str, ...]) -> str | None:
    cursor: Any = payload
    for key in path:
        if not isinstance(cursor, Mapping):
            return None
        cursor = cursor.get(key)
    return cursor if isinstance(cursor, str) and cursor else None
