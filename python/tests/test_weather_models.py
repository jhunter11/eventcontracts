"""Weather model and historical replay tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from eventcontracts.cli import main as cli_main
from eventcontracts.cli.weather import (
    candlesticks_to_book_events,
    candlesticks_to_quote_events,
    parse_kalshi_temperature_contract,
)
from eventcontracts.domain.models import InstrumentId, Venue
from eventcontracts.weather import (
    OpenMeteoClient,
    TemperatureThresholdMarket,
    TemperatureThresholdModel,
    WeatherLocation,
    snapshot_from_open_meteo_payload,
)
from tests.conftest import REPO_ROOT


def test_temperature_threshold_model_builds_strategy_signal() -> None:
    as_of = datetime(2026, 5, 24, 12, tzinfo=UTC)
    location = WeatherLocation(name="NYC", latitude=40.7128, longitude=-74.006)
    snapshot = snapshot_from_open_meteo_payload(_weather_payload(), location=location, as_of=as_of)
    market = TemperatureThresholdMarket(
        instrument_id=InstrumentId(venue=Venue.KALSHI, market_id="KXHIGHNY-26MAY24-B75"),
        threshold_f=75.0,
        target_day=as_of.date(),
        target_time=datetime(2026, 5, 24, 14, tzinfo=UTC),
    )

    prediction = TemperatureThresholdModel().predict(snapshot, market)
    signal = prediction.to_external_signal()

    assert prediction.implied_probability > 0.50
    assert signal.source == "open-meteo"
    assert signal.payload["implied_prob"] == prediction.implied_probability
    assert signal.payload["instrument_id"]["market_id"] == "KXHIGHNY-26MAY24-B75"
    assert signal.payload["temperature_basis"] == "target_time"
    assert signal.payload["target_time"] == "2026-05-24T14:00:00+00:00"


def test_parse_kalshi_temperature_contract_for_nyc_hourly_market() -> None:
    contract = parse_kalshi_temperature_contract(
        {
            "ticker": "KXTEMPNYCH-26MAY2516-T79.99",
            "title": "Will the temp in New York City be above 79.99° on May 25, 2026 at 4pm EDT?",
            "status": "active",
            "open_time": "2026-05-25T18:34:11Z",
            "close_time": "2026-05-25T20:00:00Z",
        }
    )

    assert contract is not None
    assert contract.series_ticker == "KXTEMPNYCH"
    assert contract.threshold_f == 79.99
    assert contract.direction == "above"
    assert contract.target_time == datetime(2026, 5, 25, 20, tzinfo=UTC)


def test_open_meteo_historical_forecast_client_uses_historical_endpoint() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_weather_payload())

    async def run() -> dict[str, Any]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            client = OpenMeteoClient(
                historical_forecast_base_url="https://historical.example.test",
                http_client=http_client,
            )
            return await client.get_historical_forecast_payload(
                latitude=40.7128,
                longitude=-74.006,
                start_date=datetime(2026, 5, 24, tzinfo=UTC).date(),
                end_date=datetime(2026, 5, 24, tzinfo=UTC).date(),
            )

    import asyncio

    payload = asyncio.run(run())

    assert payload["hourly"]["temperature_2m"][2] == 82.0
    assert requests
    assert str(requests[0].url).startswith("https://historical.example.test/v1/forecast")
    assert "temperature_2m" in str(requests[0].url)


def test_weather_historical_cli_runs_fixture_backtest(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    candles = tmp_path / "candles.json"
    weather = tmp_path / "weather.json"
    candles.write_text(json.dumps(_candles_payload()), encoding="utf-8")
    weather.write_text(json.dumps(_weather_payload()), encoding="utf-8")

    rc = cli_main(
        [
            "weather-historical",
            "--ticker",
            "KXHIGHNY-26MAY24-B75",
            "--threshold-f",
            "75",
            "--lat",
            "40.7128",
            "--lon",
            "-74.0060",
            "--location-name",
            "NYC",
            "--start",
            "2026-05-24T12:00:00+00:00",
            "--end",
            "2026-05-24T15:00:00+00:00",
            "--target-day",
            "2026-05-24",
            "--signal-interval-minutes",
            "60",
            "--configs-root",
            str(REPO_ROOT / "configs"),
            "--out",
            str(tmp_path / "weather-historical"),
            "--kalshi-candles-fixture",
            str(candles),
            "--weather-fixture",
            str(weather),
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert Path(payload["manifest"]).exists()
    assert Path(payload["report"]).exists()
    assert payload["quote_events"] == 4
    assert payload["book_events"] == 4
    assert payload["signal_events"] == 4
    assert payload["decisions_emitted"] > 0
    report = json.loads(Path(payload["report"]).read_text(encoding="utf-8"))
    assert report["intents_dispatched"] > 0
    assert report["fills_are_hypothetical"] is True
    assert report["liquidity_assumption"] == "historical_candle_synthetic_top_of_book"


def test_kalshi_candle_volume_sets_synthetic_book_depth() -> None:
    instrument = InstrumentId(venue=Venue.KALSHI, market_id="KXHIGHNY-26MAY24-B75")

    event = next(iter(candlesticks_to_book_events(_candles_payload(), instrument=instrument)))
    book = event.book

    assert book.yes_bids[0].quantity == 10
    assert book.yes_asks[0].quantity == 10
    assert event.provenance.metadata["synthetic_liquidity"] is True


def test_kalshi_candle_quotes_mark_synthetic_liquidity() -> None:
    instrument = InstrumentId(venue=Venue.KALSHI, market_id="KXHIGHNY-26MAY24-B75")

    event = next(iter(candlesticks_to_quote_events(_candles_payload(), instrument=instrument)))

    assert event.provenance.source_sequence == "KXHIGHNY-26MAY24-B75:0"
    assert event.provenance.metadata["synthetic_liquidity"] is True


def _weather_payload() -> dict[str, object]:
    return {
        "hourly": {
            "time": [
                "2026-05-24T12:00",
                "2026-05-24T13:00",
                "2026-05-24T14:00",
                "2026-05-24T15:00",
            ],
            "temperature_2m": [72.0, 78.0, 82.0, 80.0],
            "cloud_cover": [20.0, 30.0, 35.0, 45.0],
            "precipitation_probability": [0.0, 5.0, 10.0, 10.0],
            "wind_speed_10m": [5.0, 7.0, 8.0, 8.0],
            "relative_humidity_2m": [50.0, 52.0, 54.0, 58.0],
        }
    }


def _candles_payload() -> dict[str, object]:
    start = int(datetime(2026, 5, 24, 12, tzinfo=UTC).timestamp())
    candles = []
    for offset, bid, ask in (
        (0, "0.3500", "0.3900"),
        (3600, "0.3600", "0.4000"),
        (7200, "0.3700", "0.4100"),
        (10800, "0.3800", "0.4200"),
    ):
        candles.append(
            {
                "end_period_ts": start + offset,
                "yes_bid": {"close": bid},
                "yes_ask": {"close": ask},
                "volume": "10.00",
                "open_interest": "20.00",
            }
        )
    return {"ticker": "KXHIGHNY-26MAY24-B75", "candlesticks": candles}
