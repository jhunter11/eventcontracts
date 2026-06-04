"""KXHIGH daily-high market pricing with station calibration.

Kalshi ``KXHIGH{NY,CHI,MIA}`` markets settle on the **NWS Climatological Report
(Daily)** high temperature for a fixed station — the exact ground truth the
:mod:`eventcontracts.weather.calibration` fits were built on (NOAA GHCND TMAX is
the archive of those reports). This module is the missing seam that lets the
*proven* calibration actually price those markets:

  * parse a live Kalshi KXHIGH market payload into a structured bracket
    (``greater`` / ``less`` / ``between``, with integer floor/cap strikes), and
  * price the YES probability of that bracket from a forecast daily-high using
    :class:`StationCalibration` **directly** — i.e. via ``p_high_at_least`` /
    ``p_between`` with the same 0.5 continuity correction the walk-forward
    calibration gate validated.

Pricing through the calibration primitives (rather than the heuristic-shaped
``TemperatureThresholdModel.predict``) is deliberate: it guarantees the live
fair value equals the distribution that scored Brier 0.0395 / ECE 0.0056 in the
gate, and it natively handles the 2°-wide ``between`` brackets the single-
threshold model cannot express.

The per-series station registry mirrors
``python/scripts/weather_build_calibration_dataset.py`` (same GHCND id, coords,
and IANA tz) so the forecast input matches what the calibration was fit against.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime

from eventcontracts.weather.calibration import StationCalibration
from eventcontracts.weather.temperature import TemperatureForecastSnapshot, WeatherLocation

StrikeType = str  # "greater" | "less" | "between"

# series_ticker -> (station_code, settlement WeatherLocation). Coords/tz/GHCND id
# match the calibration dataset builder so the Open-Meteo daily-high forecast is
# computed at the same point and local calendar day the fit used.
KXHIGH_STATIONS: dict[str, tuple[str, WeatherLocation]] = {
    "KXHIGHNY": (
        "NY",
        WeatherLocation(
            name="NYC Central Park",
            latitude=40.7790,
            longitude=-73.9693,
            timezone="America/New_York",
            station_id="USW00094728",
        ),
    ),
    "KXHIGHCHI": (
        "CHI",
        WeatherLocation(
            name="Chicago Midway",
            latitude=41.7860,
            longitude=-87.7524,
            timezone="America/Chicago",
            station_id="USW00014819",
        ),
    ),
    "KXHIGHMIA": (
        "MIA",
        WeatherLocation(
            name="Miami Intl",
            latitude=25.7906,
            longitude=-80.3164,
            timezone="America/New_York",
            station_id="USW00012839",
        ),
    ),
}

# KXHIGHNY-26MAY31-T79  /  KXHIGHNY-26MAY31-B72.5
_KXHIGH_TICKER_RE = re.compile(
    r"^(?P<series>KXHIGH[A-Z]+)-(?P<date>\d{2}[A-Z]{3}\d{2})-(?P<strike>[TB]-?\d+(?:\.\d+)?)$"
)


@dataclass(frozen=True)
class KalshiHighContract:
    """A parsed KXHIGH daily-high bracket for one settlement station/day."""

    ticker: str
    series_ticker: str
    station_code: str
    location: WeatherLocation
    target_day: date
    strike_type: StrikeType  # greater | less | between
    floor_strike: float | None
    cap_strike: float | None
    close_time: datetime | None = None  # market close (UTC); used by the live runner

    def yes_probability(self, forecast_high_f: float, calibration: StationCalibration) -> float:
        """P(this bracket settles YES) given a forecast daily-high (F).

        Uses the integer-settlement YES regions:
          * greater floor=F  -> high >= F+1
          * less    cap=C    -> high <= C-1
          * between [F, C]   -> F <= high <= C
        """
        month = self.target_day.month
        if self.strike_type == "greater":
            if self.floor_strike is None:
                raise ValueError(f"greater bracket {self.ticker} missing floor_strike")
            return calibration.p_high_at_least(self.floor_strike + 1.0, forecast_high_f, month)
        if self.strike_type == "less":
            if self.cap_strike is None:
                raise ValueError(f"less bracket {self.ticker} missing cap_strike")
            return _clip(1.0 - calibration.p_high_at_least(self.cap_strike, forecast_high_f, month))
        if self.strike_type == "between":
            if self.floor_strike is None or self.cap_strike is None:
                raise ValueError(f"between bracket {self.ticker} missing floor/cap")
            return calibration.p_between(self.floor_strike, self.cap_strike, forecast_high_f, month)
        raise ValueError(f"unknown strike_type {self.strike_type!r} for {self.ticker}")


def parse_kxhigh_market(payload: dict[str, object]) -> KalshiHighContract | None:
    """Parse a Kalshi ``/markets`` KXHIGH entry. Returns None if it is not a
    recognized KXHIGH bracket for a station we have calibration for."""
    ticker = payload.get("ticker")
    if not isinstance(ticker, str):
        return None
    match = _KXHIGH_TICKER_RE.match(ticker)
    if match is None:
        return None
    series = match.group("series")
    station = KXHIGH_STATIONS.get(series)
    if station is None:
        return None
    station_code, location = station
    try:
        target_day = datetime.strptime(match.group("date"), "%y%b%d").date()
    except ValueError:
        return None

    strike_type = payload.get("strike_type")
    if strike_type not in {"greater", "less", "between"}:
        return None
    floor_strike = _opt_float(payload.get("floor_strike"))
    cap_strike = _opt_float(payload.get("cap_strike"))
    close_time = _parse_market_time(payload.get("close_time") or payload.get("close_ts"))
    return KalshiHighContract(
        ticker=ticker,
        series_ticker=series,
        station_code=station_code,
        location=location,
        target_day=target_day,
        strike_type=str(strike_type),
        floor_strike=floor_strike,
        cap_strike=cap_strike,
        close_time=close_time,
    )


def daily_high_from_snapshot(snapshot: TemperatureForecastSnapshot, target_day: date) -> float:
    """Local-calendar-day max of the hourly forecast — the same daily-high the
    calibration was fit against. Raises if the day has no forecast points."""
    points = snapshot.points_for_day(target_day)
    if not points:
        raise ValueError(f"no forecast points for {target_day.isoformat()}")
    return max(p.temperature_f for p in points)


def _parse_market_time(value: object) -> datetime | None:
    """Parse a Kalshi market time (ISO-8601 string or unix seconds) to aware UTC."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return datetime.fromtimestamp(value, tz=UTC)
    if not isinstance(value, str) or not value:
        return None
    if value.isdigit():
        return datetime.fromtimestamp(int(value), tz=UTC)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _opt_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _clip(p: float, lo: float = 1e-6, hi: float = 1.0 - 1e-6) -> float:
    return max(lo, min(hi, p))
