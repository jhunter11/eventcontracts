"""Weather reference-data adapters."""

from __future__ import annotations


class WeatherReferenceDataClient:
    """Placeholder for NWS, METAR, and NOAA climate inputs."""

    def stream_observations(self) -> None:
        raise NotImplementedError
