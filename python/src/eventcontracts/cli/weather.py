"""Historical weather-model research commands."""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Any, TypeVar, cast

import httpx

from eventcontracts.adapters.venues.kalshi import KalshiFeeModel
from eventcontracts.adapters.venues.kalshi.client import KalshiPublicClient
from eventcontracts.cli.backtest import run_backtest
from eventcontracts.config import load_sleeve_spec, load_strategy_spec
from eventcontracts.domain.decisions import IntentEnvelope, PlaceOrder, decision_kind, decision_priority
from eventcontracts.domain.events import (
    ExternalSignalEvent,
    NormalizedEvent,
    OrderBookEvent,
    QuoteEvent,
    SettlementResolvedEvent,
)
from eventcontracts.domain.ids import CorrelationId, EventId
from eventcontracts.domain.lifecycle import SettlementEvent
from eventcontracts.domain.models import InstrumentId, OrderBook, OrderBookLevel, OutcomeSide, Quote, Venue
from eventcontracts.env import load_default_env
from eventcontracts.execution import (
    ConstantLatency,
    FractionalQueueEstimator,
    MarketPaperSimulator,
    PnLTracker,
    intent_to_order,
)
from eventcontracts.replay import NormalizedReplaySource
from eventcontracts.risk import DailyLossLedger, SleeveRiskGate
from eventcontracts.storage import ParquetEventStore
from eventcontracts.strategy import create_from_spec
from eventcontracts.testing import InMemoryContext
from eventcontracts.weather import (
    OpenMeteoClient,
    TemperatureForecastSnapshot,
    TemperatureThresholdMarket,
    TemperatureThresholdModel,
    WeatherLocation,
    snapshot_from_open_meteo_payload,
)
from eventcontracts.weather.clients import OPEN_METEO_BASE_URL, OPEN_METEO_HISTORICAL_FORECAST_BASE_URL
from eventcontracts.weather.temperature import ThresholdDirection

DEFAULT_CONFIG_ROOT = Path("configs")
DEFAULT_OUTPUT_ROOT = Path("data/weather-historical")
DEFAULT_WEATHER_SERIES = ("KXTEMPNYCH",)
DEFAULT_SYNTHETIC_CANDLE_DEPTH = Decimal("250")
KALSHI_WEATHER_LOCATIONS = {
    "KXTEMPNYCH": WeatherLocation(
        name="New York City",
        latitude=40.7128,
        longitude=-74.0060,
        timezone="UTC",
    ),
}
_KALSHI_TEMPERATURE_TICKER_RE = re.compile(
    r"^(?P<series>KXTEMP[A-Z]+)-(?P<date>\d{2}[A-Z]{3}\d{2})(?P<hour>\d{2})-T(?P<threshold>-?\d+(?:\.\d+)?)$"
)
_T = TypeVar("_T")


@dataclass(frozen=True)
class KalshiTemperatureContract:
    ticker: str
    series_ticker: str
    title: str
    status: str
    threshold_f: float
    direction: ThresholdDirection
    target_time: datetime
    open_time: datetime
    close_time: datetime
    location: WeatherLocation
    result: str | None = None


@dataclass(frozen=True)
class CandleFetchResult:
    contract: KalshiTemperatureContract
    payload: dict[str, Any]
    candle_count: int
    error: str | None = None


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    preflight = subparsers.add_parser(
        "weather-preflight",
        help="Check weather historical research key/config readiness.",
    )
    preflight.add_argument("--configs-root", type=Path, default=DEFAULT_CONFIG_ROOT)
    preflight.set_defaults(handler=_handle_preflight)

    historical = subparsers.add_parser(
        "weather-historical",
        help="Run historical paper weather model test from Kalshi candles and weather history.",
    )
    historical.add_argument("--ticker", required=True, help="Kalshi market ticker / market_id.")
    historical.add_argument("--threshold-f", type=float, required=True)
    historical.add_argument("--direction", choices=("above", "below"), default="above")
    historical.add_argument("--lat", type=float, required=True)
    historical.add_argument("--lon", type=float, required=True)
    historical.add_argument("--location-name", default="weather-location")
    historical.add_argument("--timezone", default="UTC")
    historical.add_argument("--start", required=True, help="ISO datetime lower bound.")
    historical.add_argument("--end", required=True, help="ISO datetime upper bound.")
    historical.add_argument(
        "--target-day",
        default=None,
        help="YYYY-MM-DD settlement/target date. Defaults to --end date.",
    )
    historical.add_argument(
        "--target-time",
        default=None,
        help="ISO target timestamp for hourly temperature-at-time contracts.",
    )
    historical.add_argument("--period-interval", type=int, default=1, choices=(1, 60, 1440))
    historical.add_argument("--signal-interval-minutes", type=int, default=60)
    historical.add_argument(
        "--synthetic-candle-depth",
        type=str,
        default=str(DEFAULT_SYNTHETIC_CANDLE_DEPTH),
        help=(
            "Fallback top-of-book contracts per REST candle when Kalshi does "
            "not include volume/open-interest depth."
        ),
    )
    historical.add_argument("--out", type=Path, default=DEFAULT_OUTPUT_ROOT)
    historical.add_argument("--configs-root", type=Path, default=DEFAULT_CONFIG_ROOT)
    historical.add_argument("--starting-equity", type=str, default="10000")
    historical.add_argument("--kalshi-candles-fixture", type=Path, default=None)
    historical.add_argument("--weather-fixture", type=Path, default=None)
    historical.set_defaults(handler=_handle_historical)

    sweep = subparsers.add_parser(
        "weather-historical-sweep",
        help="Discover Kalshi weather markets and run all historical paper tests with available candles.",
    )
    sweep.add_argument("--series-ticker", action="append", default=None)
    sweep.add_argument("--status", action="append", choices=("open", "closed", "settled"), default=None)
    sweep.add_argument("--limit", type=int, default=1000)
    sweep.add_argument("--max-pages-per-status", type=int, default=25)
    sweep.add_argument("--period-interval", type=int, default=1, choices=(1, 60, 1440))
    sweep.add_argument("--signal-interval-minutes", type=int, default=60)
    sweep.add_argument(
        "--synthetic-candle-depth",
        type=str,
        default=str(DEFAULT_SYNTHETIC_CANDLE_DEPTH),
        help=(
            "Fallback top-of-book contracts per REST candle when Kalshi does "
            "not include volume/open-interest depth."
        ),
    )
    sweep.add_argument("--min-candles", type=int, default=1)
    sweep.add_argument("--concurrency", type=int, default=8)
    sweep.add_argument("--retry-attempts", type=int, default=5)
    sweep.add_argument("--retry-base-sleep-seconds", type=float, default=1.0)
    sweep.add_argument("--request-spacing-ms", type=int, default=0)
    sweep.add_argument("--max-candle-fetch-errors", type=int, default=100)
    sweep.add_argument("--out", type=Path, default=DEFAULT_OUTPUT_ROOT)
    sweep.add_argument("--configs-root", type=Path, default=DEFAULT_CONFIG_ROOT)
    sweep.add_argument("--starting-equity", type=str, default="10000")
    sweep.set_defaults(handler=_handle_historical_sweep)


def _handle_preflight(args: argparse.Namespace) -> int:
    payload = weather_preflight(_resolve_configs_root(args.configs_root))
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def _handle_historical(args: argparse.Namespace) -> int:
    end = _parse_datetime(args.end)
    target_time = _parse_datetime(args.target_time) if args.target_time else None
    target_day = (
        _parse_date(args.target_day)
        if args.target_day
        else (target_time.date() if target_time else end.date())
    )
    summary = asyncio.run(
        run_weather_historical(
            ticker=args.ticker,
            threshold_f=args.threshold_f,
            direction=cast(ThresholdDirection, args.direction),
            latitude=args.lat,
            longitude=args.lon,
            location_name=args.location_name,
            timezone=args.timezone,
            start=_parse_datetime(args.start),
            end=end,
            target_day=target_day,
            target_time=target_time,
            period_interval=args.period_interval,
            signal_interval_minutes=args.signal_interval_minutes,
            synthetic_candle_depth=Decimal(str(args.synthetic_candle_depth)),
            out=args.out,
            configs_root=_resolve_configs_root(args.configs_root),
            starting_equity=Decimal(str(args.starting_equity)),
            kalshi_candles_fixture=args.kalshi_candles_fixture,
            weather_fixture=args.weather_fixture,
        )
    )
    print(json.dumps(summary, indent=2, sort_keys=True, default=str))
    return 0


def _handle_historical_sweep(args: argparse.Namespace) -> int:
    summary = asyncio.run(
        run_weather_historical_sweep(
            series_tickers=tuple(args.series_ticker or DEFAULT_WEATHER_SERIES),
            statuses=tuple(args.status or ("open", "closed", "settled")),
            limit=args.limit,
            max_pages_per_status=args.max_pages_per_status,
            period_interval=args.period_interval,
            signal_interval_minutes=args.signal_interval_minutes,
            synthetic_candle_depth=Decimal(str(args.synthetic_candle_depth)),
            min_candles=args.min_candles,
            concurrency=args.concurrency,
            retry_attempts=args.retry_attempts,
            retry_base_sleep_seconds=args.retry_base_sleep_seconds,
            request_spacing_ms=args.request_spacing_ms,
            max_candle_fetch_errors=args.max_candle_fetch_errors,
            out=args.out,
            configs_root=_resolve_configs_root(args.configs_root),
            starting_equity=Decimal(str(args.starting_equity)),
        )
    )
    print(json.dumps(summary, indent=2, sort_keys=True, default=str))
    return 0


def weather_preflight(configs_root: Path) -> dict[str, Any]:
    env_path = load_default_env()
    strategy = load_strategy_spec(configs_root / "strategies" / "weather-temperature-arbitrage.toml")
    sleeve = load_sleeve_spec(configs_root / "sleeves" / "weather-kalshi-paper-a.toml")
    return {
        "env_path": str(env_path) if env_path is not None else None,
        "kalshi_market_data": {
            "KALSHI_ENV": bool(os.getenv("KALSHI_ENV")),
            "KALSHI_API_KEY_ID": bool(os.getenv("KALSHI_API_KEY_ID")),
            "KALSHI_PRIVATE_KEY_PATH": bool(os.getenv("KALSHI_PRIVATE_KEY_PATH")),
        },
        "weather_sources": {
            "open_meteo_forecast_endpoint": os.getenv("OPEN_METEO_BASE_URL") or OPEN_METEO_BASE_URL,
            "open_meteo_historical_forecast_endpoint": (
                os.getenv("OPEN_METEO_HISTORICAL_FORECAST_BASE_URL")
                or OPEN_METEO_HISTORICAL_FORECAST_BASE_URL
            ),
            "NOAA_TOKEN": bool(os.getenv("NOAA_TOKEN")),
        },
        "strategy": {
            "path": str(configs_root / "strategies" / "weather-temperature-arbitrage.toml"),
            "strategy_id": str(strategy.strategy_id),
            "loaded": True,
        },
        "sleeve": {
            "path": str(configs_root / "sleeves" / "weather-kalshi-paper-a.toml"),
            "sleeve_id": str(sleeve.sleeve_id),
            "loaded": True,
        },
    }


async def run_weather_historical(
    *,
    ticker: str,
    threshold_f: float,
    direction: ThresholdDirection,
    latitude: float,
    longitude: float,
    location_name: str,
    timezone: str,
    start: datetime,
    end: datetime,
    target_day: date,
    target_time: datetime | None,
    period_interval: int,
    signal_interval_minutes: int,
    synthetic_candle_depth: Decimal,
    out: Path,
    configs_root: Path,
    starting_equity: Decimal,
    kalshi_candles_fixture: Path | None,
    weather_fixture: Path | None,
) -> dict[str, Any]:
    if signal_interval_minutes <= 0:
        raise ValueError("signal_interval_minutes must be positive")
    if synthetic_candle_depth <= 0:
        raise ValueError("synthetic_candle_depth must be positive")
    run_root = out / datetime.now(UTC).strftime("run-%Y%m%dT%H%M%S%fZ")
    data_root = run_root / "event_lake"
    reports_root = run_root / "reports"
    reports_root.mkdir(parents=True, exist_ok=True)

    instrument = InstrumentId(venue=Venue.KALSHI, market_id=ticker)
    location = WeatherLocation(name=location_name, latitude=latitude, longitude=longitude, timezone=timezone)
    market = TemperatureThresholdMarket(
        instrument_id=instrument,
        threshold_f=threshold_f,
        target_day=target_day,
        direction=direction,
        target_time=target_time,
    )
    candles_payload, weather_payload = await _load_historical_payloads(
        ticker=ticker,
        latitude=latitude,
        longitude=longitude,
        timezone=timezone,
        start=start,
        end=end,
        period_interval=period_interval,
        kalshi_candles_fixture=kalshi_candles_fixture,
        weather_fixture=weather_fixture,
    )
    snapshot = snapshot_from_open_meteo_payload(weather_payload, location=location, as_of=start)
    quote_events = tuple(
        candlesticks_to_quote_events(
            candles_payload,
            instrument=instrument,
            synthetic_depth=synthetic_candle_depth,
        )
    )
    book_events = tuple(
        candlesticks_to_book_events(
            candles_payload,
            instrument=instrument,
            synthetic_depth=synthetic_candle_depth,
        )
    )
    settlement_events: tuple[SettlementResolvedEvent, ...] = ()
    signal_events = tuple(
        historical_temperature_signals(
            snapshot=snapshot,
            market=market,
            signal_times=_signal_times(start, end, signal_interval_minutes),
        )
    )
    store = ParquetEventStore(data_root)
    events: tuple[NormalizedEvent, ...] = (*book_events, *quote_events, *signal_events, *settlement_events)
    for event in sorted(events, key=_event_sort_key):
        store.append_normalized(event)
    store.flush()

    report, summary = run_backtest(
        load_strategy_spec(configs_root / "strategies" / "weather-temperature-arbitrage.toml"),
        load_sleeve_spec(configs_root / "sleeves" / "weather-kalshi-paper-a.toml"),
        data_root,
        starting_equity=starting_equity,
        latency_ms=1000.0,
        queue_fraction="1.0",
    )
    report_path = reports_root / "weather-temperature-arbitrage.json"
    report_path.write_text(json.dumps(report.to_dict(), indent=2, default=str) + "\n", encoding="utf-8")
    diagnostics = write_weather_replay_diagnostics(
        data_root=data_root,
        reports_root=reports_root,
        strategy_spec_path=configs_root / "strategies" / "weather-temperature-arbitrage.toml",
        sleeve_spec_path=configs_root / "sleeves" / "weather-kalshi-paper-a.toml",
        starting_equity=starting_equity,
        outcomes_by_ticker={},
    )
    manifest = {
        "ticker": ticker,
        "threshold_f": threshold_f,
        "direction": direction,
        "location": {"name": location_name, "latitude": latitude, "longitude": longitude, "timezone": timezone},
        "start": start.isoformat(),
        "end": end.isoformat(),
        "target_day": target_day.isoformat(),
        "target_time": target_time.isoformat() if target_time is not None else None,
        "synthetic_candle_depth": str(synthetic_candle_depth),
        "data_root": str(data_root),
        "report": str(report_path),
        "quote_events": len(quote_events),
        "book_events": len(book_events),
        "settlement_events": len(settlement_events),
        "signal_events": len(signal_events),
        "decisions_emitted": summary.decisions_emitted,
        "diagnostics": diagnostics,
    }
    manifest_path = run_root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    return {"manifest": str(manifest_path), **manifest}


async def _load_historical_payloads(
    *,
    ticker: str,
    latitude: float,
    longitude: float,
    timezone: str,
    start: datetime,
    end: datetime,
    period_interval: int,
    kalshi_candles_fixture: Path | None,
    weather_fixture: Path | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if kalshi_candles_fixture is not None:
        candles_payload = _read_json_object(kalshi_candles_fixture)
    else:
        candles_payload = await KalshiPublicClient.from_env().get_market_candlesticks_payload(
            tickers=(ticker,),
            start_ts=int(start.timestamp()),
            end_ts=int(end.timestamp()),
            period_interval=period_interval,
        )
    if weather_fixture is not None:
        weather_payload = _read_json_object(weather_fixture)
    else:
        weather_payload = await OpenMeteoClient.from_env().get_historical_forecast_payload(
            latitude=latitude,
            longitude=longitude,
            start_date=start.date(),
            end_date=end.date(),
            timezone=timezone,
        )
    return candles_payload, weather_payload


async def run_weather_historical_sweep(
    *,
    series_tickers: Sequence[str],
    statuses: Sequence[str],
    limit: int,
    max_pages_per_status: int,
    period_interval: int,
    signal_interval_minutes: int,
    synthetic_candle_depth: Decimal,
    min_candles: int,
    concurrency: int,
    retry_attempts: int,
    retry_base_sleep_seconds: float,
    request_spacing_ms: int,
    max_candle_fetch_errors: int,
    out: Path,
    configs_root: Path,
    starting_equity: Decimal,
) -> dict[str, Any]:
    if limit <= 0:
        raise ValueError("limit must be positive")
    if max_pages_per_status <= 0:
        raise ValueError("max_pages_per_status must be positive")
    if signal_interval_minutes <= 0:
        raise ValueError("signal_interval_minutes must be positive")
    if synthetic_candle_depth <= 0:
        raise ValueError("synthetic_candle_depth must be positive")
    if min_candles <= 0:
        raise ValueError("min_candles must be positive")
    if concurrency <= 0:
        raise ValueError("concurrency must be positive")
    if retry_attempts < 0:
        raise ValueError("retry_attempts must be non-negative")
    if retry_base_sleep_seconds < 0:
        raise ValueError("retry_base_sleep_seconds must be non-negative")
    if request_spacing_ms < 0:
        raise ValueError("request_spacing_ms must be non-negative")
    if max_candle_fetch_errors < 0:
        raise ValueError("max_candle_fetch_errors must be non-negative")

    run_root = out / datetime.now(UTC).strftime("sweep-%Y%m%dT%H%M%S%fZ")
    data_root = run_root / "event_lake"
    reports_root = run_root / "reports"
    reports_root.mkdir(parents=True, exist_ok=True)

    kalshi = KalshiPublicClient.from_env()
    contracts, discovery = await _discover_kalshi_temperature_contracts(
        kalshi,
        series_tickers=series_tickers,
        statuses=statuses,
        limit=limit,
        max_pages_per_status=max_pages_per_status,
    )
    candle_results = await _fetch_sweep_candles(
        kalshi,
        contracts,
        period_interval=period_interval,
        concurrency=concurrency,
        retry_attempts=retry_attempts,
        retry_base_sleep_seconds=retry_base_sleep_seconds,
        request_spacing_ms=request_spacing_ms,
        max_candle_fetch_errors=max_candle_fetch_errors,
    )
    runnable = [result for result in candle_results if result.error is None and result.candle_count >= min_candles]
    summary: dict[str, Any] = {
        "run_root": str(run_root),
        "kalshi_base_url": kalshi.base_url,
        "series_tickers": list(series_tickers),
        "statuses": list(statuses),
        "markets_seen": discovery["markets_seen"],
        "contracts_parsed": len(contracts),
        "synthetic_candle_depth": str(synthetic_candle_depth),
        "contracts_with_candles": len(runnable),
        "contracts_without_candles": sum(
            1 for result in candle_results if result.error is None and result.candle_count == 0
        ),
        "contracts_below_min_candles": sum(
            1 for result in candle_results if result.error is None and 0 < result.candle_count < min_candles
        ),
        "candle_fetch_errors": sum(1 for result in candle_results if result.error is not None),
        "candle_fetch_aborted": any(result.error == "skipped_after_candle_error_limit" for result in candle_results),
        "candle_fetch_error_limit": max_candle_fetch_errors,
        "discovery": discovery,
        "contract_samples": [_contract_summary(contract) for contract in contracts[:10]],
        "candle_error_samples": [
            {"ticker": result.contract.ticker, "error": result.error}
            for result in candle_results
            if result.error is not None
        ][:10],
    }

    catalog_path = run_root / "catalog.json"
    catalog_path.write_text(
        json.dumps([_contract_summary(contract) for contract in contracts], indent=2, sort_keys=True, default=str)
        + "\n",
        encoding="utf-8",
    )
    summary["catalog"] = str(catalog_path)

    if not runnable:
        summary_path = run_root / "summary.json"
        summary["status"] = "no_runnable_markets"
        if summary["candle_fetch_aborted"]:
            summary["reason"] = (
                "Kalshi rate/error limits stopped candle retrieval before all contracts could be tested."
            )
        elif summary["candle_fetch_errors"]:
            summary["reason"] = "Kalshi candle requests errored before a complete historical backtest could run."
        else:
            summary["reason"] = "Kalshi returned no historical candle bars for the discovered weather contracts."
        summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
        summary["summary"] = str(summary_path)
        return summary

    weather_client = OpenMeteoClient.from_env()
    weather_cache: dict[tuple[str, date, date], dict[str, Any]] = {}
    quote_events: list[NormalizedEvent] = []
    book_events: list[NormalizedEvent] = []
    settlement_events: list[NormalizedEvent] = []
    signal_events: list[NormalizedEvent] = []
    skipped_weather: list[dict[str, str]] = []
    for result in runnable:
        contract = result.contract
        instrument = InstrumentId(venue=Venue.KALSHI, market_id=contract.ticker)
        candles = _candlesticks_for_payload(result.payload, ticker=contract.ticker)
        if not candles:
            continue
        quote_events.extend(
            candlesticks_to_quote_events(
                result.payload,
                instrument=instrument,
                synthetic_depth=synthetic_candle_depth,
            )
        )
        book_events.extend(
            candlesticks_to_book_events(
                result.payload,
                instrument=instrument,
                synthetic_depth=synthetic_candle_depth,
            )
        )
        settlement = settlement_event_from_contract(contract)
        if settlement is not None:
            settlement_events.append(settlement)
        start, end = _event_window_from_candles(candles)
        weather_start = min(start.date(), contract.target_time.date())
        weather_end = max(end.date(), contract.target_time.date())
        cache_key = (contract.series_ticker, weather_start, weather_end)
        try:
            weather_payload = weather_cache.get(cache_key)
            if weather_payload is None:
                weather_payload = await weather_client.get_historical_forecast_payload(
                    latitude=contract.location.latitude,
                    longitude=contract.location.longitude,
                    start_date=weather_start,
                    end_date=weather_end,
                    timezone=contract.location.timezone,
                )
                weather_cache[cache_key] = weather_payload
            snapshot = snapshot_from_open_meteo_payload(weather_payload, location=contract.location, as_of=start)
            market = TemperatureThresholdMarket(
                instrument_id=instrument,
                threshold_f=contract.threshold_f,
                target_day=contract.target_time.date(),
                direction=contract.direction,
                target_time=contract.target_time,
            )
            signal_events.extend(
                historical_temperature_signals(
                    snapshot=snapshot,
                    market=market,
                    signal_times=_signal_times(start, end, signal_interval_minutes),
                )
            )
        except Exception as exc:  # pragma: no cover - live API failure path
            skipped_weather.append({"ticker": contract.ticker, "error": f"{type(exc).__name__}: {exc}"})

    store = ParquetEventStore(data_root)
    for event in sorted(
        (*book_events, *quote_events, *signal_events, *settlement_events),
        key=_event_sort_key,
    ):
        store.append_normalized(event)
    store.flush()

    report, backtest_summary = run_backtest(
        load_strategy_spec(configs_root / "strategies" / "weather-temperature-arbitrage.toml"),
        load_sleeve_spec(configs_root / "sleeves" / "weather-kalshi-paper-a.toml"),
        data_root,
        starting_equity=starting_equity,
        latency_ms=1000.0,
        queue_fraction="1.0",
    )
    report_path = reports_root / "weather-temperature-arbitrage-sweep.json"
    report_path.write_text(json.dumps(report.to_dict(), indent=2, default=str) + "\n", encoding="utf-8")
    outcomes_by_ticker = {contract.ticker: contract.result or "" for contract in contracts}
    diagnostics = write_weather_replay_diagnostics(
        data_root=data_root,
        reports_root=reports_root,
        strategy_spec_path=configs_root / "strategies" / "weather-temperature-arbitrage.toml",
        sleeve_spec_path=configs_root / "sleeves" / "weather-kalshi-paper-a.toml",
        starting_equity=starting_equity,
        outcomes_by_ticker=outcomes_by_ticker,
    )

    summary.update(
        {
            "status": "completed",
            "data_root": str(data_root),
            "report": str(report_path),
            "quote_events": len(quote_events),
            "book_events": len(book_events),
            "settlement_events": len(settlement_events),
            "signal_events": len(signal_events),
            "weather_fetch_errors": len(skipped_weather),
            "weather_error_samples": skipped_weather[:10],
            "decisions_emitted": backtest_summary.decisions_emitted,
            "report_payload": report.to_dict(),
            "diagnostics": diagnostics,
        }
    )
    summary_path = run_root / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    summary["summary"] = str(summary_path)
    return summary


def candlesticks_to_quote_events(
    payload: dict[str, Any],
    *,
    instrument: InstrumentId,
    synthetic_depth: Decimal = DEFAULT_SYNTHETIC_CANDLE_DEPTH,
) -> Iterable[QuoteEvent]:
    candles = _candlesticks_for_payload(payload, ticker=instrument.market_id)
    for index, candle in enumerate(candles):
        timestamp = _candlestick_time(candle)
        bid_price = _candle_close(candle, "yes_bid")
        ask_price = _candle_close(candle, "yes_ask")
        if bid_price is None or ask_price is None:
            price = _candle_close(candle, "price")
            if price is None:
                continue
            bid_price = max(Decimal("0.01"), price - Decimal("0.005"))
            ask_price = min(Decimal("0.99"), price + Decimal("0.005"))
        depth = _candle_depth(candle, fallback_depth=synthetic_depth)
        yield QuoteEvent(
            event_id=EventId(f"kalshi-candle-quote-{instrument.market_id}-{index}"),
            quote=Quote(
                instrument_id=instrument,
                side=OutcomeSide.YES,
                bid=OrderBookLevel(price=bid_price, quantity=depth),
                ask=OrderBookLevel(price=ask_price, quantity=depth),
                exchange_ts=timestamp,
                received_at=timestamp,
            ),
        )


def candlesticks_to_book_events(
    payload: dict[str, Any],
    *,
    instrument: InstrumentId,
    synthetic_depth: Decimal = DEFAULT_SYNTHETIC_CANDLE_DEPTH,
) -> Iterable[OrderBookEvent]:
    candles = _candlesticks_for_payload(payload, ticker=instrument.market_id)
    for index, candle in enumerate(candles):
        timestamp = _candlestick_time(candle)
        bid_price = _candle_close(candle, "yes_bid")
        ask_price = _candle_close(candle, "yes_ask")
        if bid_price is None or ask_price is None:
            continue
        depth = _candle_depth(candle, fallback_depth=synthetic_depth)
        yield OrderBookEvent(
            event_id=EventId(f"kalshi-candle-book-{instrument.market_id}-{index}"),
            book=OrderBook(
                instrument_id=instrument,
                yes_bids=(OrderBookLevel(price=bid_price, quantity=depth),),
                yes_asks=(OrderBookLevel(price=ask_price, quantity=depth),),
                no_bids=(OrderBookLevel(price=Decimal("1") - ask_price, quantity=depth),),
                no_asks=(OrderBookLevel(price=Decimal("1") - bid_price, quantity=depth),),
                exchange_ts=timestamp,
                received_at=timestamp,
            ),
        )


def write_weather_replay_diagnostics(
    *,
    data_root: Path,
    reports_root: Path,
    strategy_spec_path: Path,
    sleeve_spec_path: Path,
    starting_equity: Decimal,
    outcomes_by_ticker: Mapping[str, str],
) -> dict[str, object]:
    """Replay a weather run and persist fill/equity/attribution diagnostics."""

    strategy_spec = load_strategy_spec(strategy_spec_path)
    sleeve_spec = load_sleeve_spec(sleeve_spec_path)
    strategy = create_from_spec(strategy_spec)
    daily_loss = DailyLossLedger()
    pnl = PnLTracker(currency=sleeve_spec.currency, daily_loss_ledger=daily_loss)
    simulator = MarketPaperSimulator(
        fee_model=KalshiFeeModel(),
        latency=ConstantLatency(submit_ms=1000.0),
        queue_estimator=FractionalQueueEstimator(fraction=Decimal("1.0")),
        strategy_id=strategy_spec.strategy_id,
        sleeve_id=sleeve_spec.sleeve_id,
        fill_sink=pnl,
    )
    risk = SleeveRiskGate(sleeve=sleeve_spec, daily_loss=daily_loss)
    ctx = InMemoryContext(
        strategy_id_value=strategy_spec.strategy_id,
        sleeve_id_value=sleeve_spec.sleeve_id,
        clock_now=datetime(2026, 1, 1, tzinfo=UTC),
    )
    strategy.on_init(ctx)

    fill_rows: list[dict[str, str]] = []
    equity_rows: list[dict[str, str]] = []
    attribution: dict[str, dict[str, Decimal | int]] = {}
    calibration: dict[str, dict[str, Decimal | int]] = {}
    place_orders = 0
    no_actions = 0
    rejections = 0
    peak = starting_equity
    max_drawdown = Decimal("0")
    peak_at_max = starting_equity
    trough_at_max = starting_equity

    for event_index, event in enumerate(NormalizedReplaySource(ParquetEventStore(data_root)).stream(), start=1):
        event_time = _event_sort_key(event)[0]
        ctx.clock_now = event_time
        simulator.on_event(event)
        pnl.on_event(event)
        if isinstance(event, ExternalSignalEvent):
            _record_calibration(calibration, event, outcomes_by_ticker)
        for decision in strategy.on_event(event, ctx):
            if isinstance(decision, PlaceOrder):
                place_orders += 1
            else:
                no_actions += 1
                continue
            envelope = IntentEnvelope(
                decision=decision,
                strategy_id=strategy_spec.strategy_id,
                sleeve_id=sleeve_spec.sleeve_id,
                correlation_id=CorrelationId(f"weather-replay-{event_index}-{place_orders}-{no_actions}"),
                emitted_at=event_time,
                priority=decision_priority(decision, strategy_spec.default_execution_priority),
                triggered_by_event_id=getattr(event, "event_id", None),
                metadata={"decision_kind": decision_kind(decision)},
            )
            verdict = risk.evaluate(envelope, ctx)
            if not verdict.allowed:
                rejections += 1
                continue
            intent = intent_to_order(envelope)
            if intent is None:
                continue
            fills = simulator.submit(intent, event_time)
            for fill in fills:
                ladder_key = str(
                    decision.metadata.get("ladder_key") or _ticker_ladder_key(fill.instrument_id.market_id)
                )
                outcome = (outcomes_by_ticker.get(fill.instrument_id.market_id) or "").lower()
                is_winner = outcome == fill.outcome_side.value
                notional = fill.price * fill.quantity
                _record_attribution(
                    attribution,
                    ladder_key=ladder_key,
                    notional=notional,
                    fee=fill.fee_amount,
                    quantity=fill.quantity,
                    payout=fill.quantity if is_winner else Decimal("0"),
                )
                fill_rows.append(
                    {
                        "fill_index": str(len(fill_rows) + 1),
                        "event_index": str(event_index),
                        "event_time": event_time.isoformat(),
                        "filled_at": fill.filled_at.isoformat(),
                        "ticker": fill.instrument_id.market_id,
                        "ladder_key": ladder_key,
                        "outcome_side": fill.outcome_side.value,
                        "order_side": fill.order_side.value,
                        "settled_result": outcome,
                        "is_winner": str(is_winner),
                        "price": str(fill.price),
                        "quantity": str(fill.quantity),
                        "notional": str(notional),
                        "fee_amount": str(fill.fee_amount),
                        "liquidity": fill.liquidity.value,
                        "decision_reason": decision.reason,
                        "expected_edge_bps": str(decision.expected_edge_bps),
                    }
                )

        equity = starting_equity + pnl.total_pnl(now=event_time)
        if equity > peak:
            peak = equity
        drawdown = peak - equity
        if drawdown > max_drawdown:
            max_drawdown = drawdown
            peak_at_max = peak
            trough_at_max = equity
        equity_rows.append(
            {
                "event_index": str(event_index),
                "event_time": event_time.isoformat(),
                "event_type": type(event).__name__,
                "equity": str(equity),
                "realized_pnl": str(pnl.cumulative_realized),
                "total_pnl": str(pnl.total_pnl(now=event_time)),
                "fees_paid": str(pnl.total_fees_paid),
                "open_positions": str(sum(1 for _ in pnl.positions(now=event_time))),
                "drawdown": str(drawdown),
            }
        )

    fill_log_path = reports_root / "weather-fill-log.csv"
    equity_curve_path = reports_root / "weather-equity-curve.csv"
    attribution_path = reports_root / "weather-ladder-attribution.json"
    calibration_path = reports_root / "weather-calibration.json"
    validation_path = reports_root / "weather-validation-replay.json"
    _write_csv(fill_log_path, fill_rows)
    _write_csv(equity_curve_path, equity_rows)
    attribution_payload = _render_attribution(attribution)
    calibration_payload = _render_calibration(calibration)
    attribution_path.write_text(json.dumps(attribution_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    calibration_path.write_text(json.dumps(calibration_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    validation = {
        "fill_log": str(fill_log_path),
        "equity_curve": str(equity_curve_path),
        "ladder_attribution": str(attribution_path),
        "calibration": str(calibration_path),
        "fills": len(fill_rows),
        "place_orders": place_orders,
        "no_actions": no_actions,
        "rejections": rejections,
        "path_peak_equity": str(max(Decimal(row["equity"]) for row in equity_rows) if equity_rows else starting_equity),
        "path_trough_equity": str(
            min(Decimal(row["equity"]) for row in equity_rows) if equity_rows else starting_equity
        ),
        "path_max_drawdown": str(max_drawdown),
        "path_max_drawdown_peak": str(peak_at_max),
        "path_max_drawdown_trough": str(trough_at_max),
        "ending_total_pnl": str(pnl.total_pnl(now=ctx.clock_now)),
        "ending_realized_pnl": str(pnl.cumulative_realized),
        "fees_paid": str(pnl.total_fees_paid),
        "open_positions": sum(1 for _ in pnl.positions(now=ctx.clock_now)),
    }
    validation_path.write_text(json.dumps(validation, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return validation


def _write_csv(path: Path, rows: Sequence[Mapping[str, str]]) -> None:
    if not rows:
        path.write_text("empty\n", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def _record_attribution(
    attribution: dict[str, dict[str, Decimal | int]],
    *,
    ladder_key: str,
    notional: Decimal,
    fee: Decimal,
    quantity: Decimal,
    payout: Decimal,
) -> None:
    row = attribution.setdefault(
        ladder_key,
        {
            "fills": 0,
            "quantity": Decimal("0"),
            "notional": Decimal("0"),
            "fees": Decimal("0"),
            "payout": Decimal("0"),
        },
    )
    row["fills"] = int(row["fills"]) + 1
    row["quantity"] = Decimal(row["quantity"]) + quantity
    row["notional"] = Decimal(row["notional"]) + notional
    row["fees"] = Decimal(row["fees"]) + fee
    row["payout"] = Decimal(row["payout"]) + payout


def _render_attribution(attribution: Mapping[str, Mapping[str, Decimal | int]]) -> list[dict[str, str | int]]:
    rows: list[dict[str, str | int]] = []
    for ladder_key, raw in attribution.items():
        notional = Decimal(raw["notional"])
        fees = Decimal(raw["fees"])
        payout = Decimal(raw["payout"])
        pnl = payout - notional - fees
        rows.append(
            {
                "ladder_key": ladder_key,
                "fills": int(raw["fills"]),
                "quantity": str(raw["quantity"]),
                "notional": str(notional),
                "fees": str(fees),
                "payout": str(payout),
                "pnl": str(pnl),
            }
        )
    return sorted(rows, key=lambda row: Decimal(str(row["pnl"])))


def _record_calibration(
    calibration: dict[str, dict[str, Decimal | int]],
    event: ExternalSignalEvent,
    outcomes_by_ticker: Mapping[str, str],
) -> None:
    payload = event.payload
    implied = payload.get("implied_prob") if isinstance(payload, Mapping) else None
    instrument = payload.get("instrument_id") if isinstance(payload, Mapping) else None
    if implied is None or not isinstance(instrument, Mapping):
        return
    ticker = instrument.get("market_id")
    if not isinstance(ticker, str):
        return
    outcome = (outcomes_by_ticker.get(ticker) or "").lower()
    if outcome not in {"yes", "no"}:
        return
    probability = Decimal(str(implied))
    bucket_floor = int(min(9, max(0, int(probability * Decimal("10")))))
    bucket = f"{bucket_floor / 10:.1f}-{(bucket_floor + 1) / 10:.1f}"
    row = calibration.setdefault(
        bucket,
        {"signals": 0, "probability_sum": Decimal("0"), "yes_results": 0},
    )
    row["signals"] = int(row["signals"]) + 1
    row["probability_sum"] = Decimal(row["probability_sum"]) + probability
    row["yes_results"] = int(row["yes_results"]) + (1 if outcome == "yes" else 0)


def _render_calibration(calibration: Mapping[str, Mapping[str, Decimal | int]]) -> list[dict[str, str | int]]:
    rows: list[dict[str, str | int]] = []
    for bucket, raw in calibration.items():
        signals = int(raw["signals"])
        probability_sum = Decimal(raw["probability_sum"])
        yes_results = int(raw["yes_results"])
        rows.append(
            {
                "bucket": bucket,
                "signals": signals,
                "avg_predicted_probability": str(probability_sum / Decimal(signals)),
                "realized_yes_rate": str(Decimal(yes_results) / Decimal(signals)),
                "yes_results": yes_results,
            }
        )
    return sorted(rows, key=lambda row: str(row["bucket"]))


def _ticker_ladder_key(ticker: str) -> str:
    return ticker.split("-T", maxsplit=1)[0]


def historical_temperature_signals(
    *,
    snapshot: TemperatureForecastSnapshot,
    market: TemperatureThresholdMarket,
    signal_times: Sequence[datetime],
) -> Iterable[ExternalSignalEvent]:
    model = TemperatureThresholdModel()
    for index, signal_time in enumerate(signal_times):
        point_in_time = TemperatureForecastSnapshot(
            location=snapshot.location,
            as_of=signal_time,
            hourly=snapshot.hourly,
            source="open-meteo",
            schema_version="open-meteo-historical-forecast-v1",
        )
        prediction = model.predict(point_in_time, market)
        yield prediction.to_external_signal(
            received_at=signal_time,
            event_id=EventId(f"weather-historical-signal-{market.instrument_id.market_id}-{index}"),
        )


def _signal_times(start: datetime, end: datetime, interval_minutes: int) -> tuple[datetime, ...]:
    values: list[datetime] = []
    cursor = start
    delta = timedelta(minutes=interval_minutes)
    while cursor <= end:
        values.append(cursor)
        cursor += delta
    return tuple(values)


async def _discover_kalshi_temperature_contracts(
    client: KalshiPublicClient,
    *,
    series_tickers: Sequence[str],
    statuses: Sequence[str],
    limit: int,
    max_pages_per_status: int,
) -> tuple[list[KalshiTemperatureContract], dict[str, Any]]:
    contracts: dict[str, KalshiTemperatureContract] = {}
    markets_seen = 0
    parse_skips: dict[str, int] = {}
    pages: list[dict[str, Any]] = []
    for series_ticker in series_tickers:
        for status in statuses:
            cursor: str | None = None
            for page_index in range(max_pages_per_status):
                payload = await client.get_markets_payload(
                    limit=limit,
                    cursor=cursor,
                    status=status,
                    series_ticker=series_ticker,
                )
                markets = payload.get("markets", [])
                market_count = len(markets) if isinstance(markets, list) else 0
                markets_seen += market_count
                parsed_on_page = 0
                if isinstance(markets, list):
                    for raw_market in markets:
                        if not isinstance(raw_market, dict):
                            _increment(parse_skips, "payload_not_object")
                            continue
                        contract = parse_kalshi_temperature_contract(raw_market)
                        if contract is None:
                            _increment(parse_skips, "not_supported_temperature_contract")
                            continue
                        contracts[contract.ticker] = contract
                        parsed_on_page += 1
                cursor_value = payload.get("cursor")
                cursor = str(cursor_value) if cursor_value else None
                pages.append(
                    {
                        "series_ticker": series_ticker,
                        "status": status,
                        "page": page_index + 1,
                        "markets": market_count,
                        "parsed": parsed_on_page,
                        "has_next_cursor": bool(cursor),
                    }
                )
                if not cursor:
                    break
    discovery = {
        "markets_seen": markets_seen,
        "parse_skips": parse_skips,
        "pages": pages,
    }
    return sorted(contracts.values(), key=lambda contract: contract.ticker), discovery


def parse_kalshi_temperature_contract(payload: dict[str, Any]) -> KalshiTemperatureContract | None:
    ticker = payload.get("ticker")
    if not isinstance(ticker, str):
        return None
    match = _KALSHI_TEMPERATURE_TICKER_RE.match(ticker)
    if match is None:
        return None
    series_ticker = match.group("series")
    location = KALSHI_WEATHER_LOCATIONS.get(series_ticker)
    if location is None:
        return None
    title = str(payload.get("title") or "")
    direction = _temperature_direction_from_title(title)
    try:
        close_time = _payload_datetime(payload, "close_time")
    except ValueError:
        return None
    try:
        open_time = _payload_datetime(payload, "open_time")
    except ValueError:
        open_time = close_time - timedelta(hours=24)
    return KalshiTemperatureContract(
        ticker=ticker,
        series_ticker=series_ticker,
        title=title,
        status=str(payload.get("status") or ""),
        threshold_f=float(match.group("threshold")),
        direction=direction,
        target_time=close_time,
        open_time=open_time,
        close_time=close_time,
        location=location,
        result=str(payload.get("result") or "") or None,
    )


def settlement_event_from_contract(contract: KalshiTemperatureContract) -> SettlementResolvedEvent | None:
    result = (contract.result or "").lower()
    if result not in {"yes", "no"}:
        return None
    resolved_side = OutcomeSide.YES if result == "yes" else OutcomeSide.NO
    settled_at = contract.close_time + timedelta(seconds=1)
    return SettlementResolvedEvent(
        event_id=EventId(f"kalshi-settlement-{contract.ticker}"),
        settlement=SettlementEvent(
            instrument_id=InstrumentId(venue=Venue.KALSHI, market_id=contract.ticker),
            resolved_side=resolved_side,
            payout_per_contract=Decimal("1"),
            currency="USD",
            settled_at=settled_at,
            source="kalshi-market-result",
            metadata={"raw_result": result},
        ),
    )


async def _fetch_sweep_candles(
    client: KalshiPublicClient,
    contracts: Sequence[KalshiTemperatureContract],
    *,
    period_interval: int,
    concurrency: int,
    retry_attempts: int,
    retry_base_sleep_seconds: float,
    request_spacing_ms: int,
    max_candle_fetch_errors: int,
) -> list[CandleFetchResult]:
    semaphore = asyncio.Semaphore(concurrency)
    abort_event = asyncio.Event()
    error_count = 0
    grouped: dict[tuple[int, int], list[KalshiTemperatureContract]] = {}
    for contract in contracts:
        start, end = _contract_candle_window(contract)
        grouped.setdefault((int(start.timestamp()), int(end.timestamp())), []).append(contract)

    async def fetch_chunk(
        *,
        contracts_chunk: Sequence[KalshiTemperatureContract],
        start_ts: int,
        end_ts: int,
    ) -> list[CandleFetchResult]:
        nonlocal error_count
        async with semaphore:
            if abort_event.is_set():
                return [
                    CandleFetchResult(
                        contract=contract,
                        payload={},
                        candle_count=0,
                        error="skipped_after_candle_error_limit",
                    )
                    for contract in contracts_chunk
                ]
            if request_spacing_ms:
                await asyncio.sleep(request_spacing_ms / 1000.0)
            attempt = 0
            while True:
                try:
                    payload = await client.get_market_candlesticks_payload(
                        tickers=tuple(contract.ticker for contract in contracts_chunk),
                        start_ts=start_ts,
                        end_ts=end_ts,
                        period_interval=period_interval,
                    )
                    return [
                        CandleFetchResult(
                            contract=contract,
                            payload=payload,
                            candle_count=len(
                                _candlesticks_for_payload(
                                    payload,
                                    ticker=contract.ticker,
                                    allow_unlabeled=len(contracts_chunk) == 1,
                                )
                            ),
                        )
                        for contract in contracts_chunk
                    ]
                except Exception as exc:  # pragma: no cover - live API failure path
                    if not _should_retry_candle_error(exc, attempt=attempt, retry_attempts=retry_attempts):
                        error_count += len(contracts_chunk)
                        if max_candle_fetch_errors and error_count >= max_candle_fetch_errors:
                            abort_event.set()
                        return [
                            CandleFetchResult(
                                contract=contract,
                                payload={},
                                candle_count=0,
                                error=f"{type(exc).__name__}: {exc}",
                            )
                            for contract in contracts_chunk
                        ]
                    await asyncio.sleep(retry_base_sleep_seconds * (2**attempt))
                    attempt += 1

    tasks = [
        fetch_chunk(contracts_chunk=chunk, start_ts=start_ts, end_ts=end_ts)
        for (start_ts, end_ts), group in grouped.items()
        for chunk in _chunks(group, 50)
    ]
    nested_results = await asyncio.gather(*tasks)
    return [result for results in nested_results for result in results]


def _candlesticks_for_payload(
    payload: dict[str, Any],
    *,
    ticker: str,
    allow_unlabeled: bool = True,
) -> tuple[dict[str, Any], ...]:
    candles: list[dict[str, Any]] = []
    raw_candlesticks = payload.get("candlesticks")
    if isinstance(raw_candlesticks, list):
        _append_candlesticks(
            candles,
            raw_candlesticks,
            ticker=ticker,
            allow_unlabeled=allow_unlabeled,
        )
    raw_markets = payload.get("markets")
    if isinstance(raw_markets, list):
        for item in raw_markets:
            if not isinstance(item, dict):
                continue
            nested = item.get("candlesticks")
            item_ticker = item.get("ticker") or item.get("market_ticker")
            if item_ticker is not None and item_ticker != ticker:
                continue
            if isinstance(nested, list):
                candles.extend(nested_item for nested_item in nested if isinstance(nested_item, dict))
    if not isinstance(raw_candlesticks, list) and not isinstance(raw_markets, list):
        raise ValueError("Kalshi candlestick payload missing candlesticks list")
    return tuple(candles)


def _append_candlesticks(
    candles: list[dict[str, Any]],
    raw_candlesticks: Sequence[object],
    *,
    ticker: str,
    allow_unlabeled: bool,
) -> None:
    for item in raw_candlesticks:
        if not isinstance(item, dict):
            continue
        nested = item.get("candlesticks")
        item_ticker = item.get("ticker") or item.get("market_ticker")
        if isinstance(nested, list):
            if item_ticker is not None and item_ticker != ticker:
                continue
            candles.extend(nested_item for nested_item in nested if isinstance(nested_item, dict))
            continue
        if item_ticker is None and not allow_unlabeled:
            continue
        if item_ticker is not None and item_ticker != ticker:
            continue
        candles.append(item)


def _chunks(values: Sequence[_T], size: int) -> Iterable[Sequence[_T]]:
    if size <= 0:
        raise ValueError("size must be positive")
    for index in range(0, len(values), size):
        yield values[index : index + size]


def _event_window_from_candles(candles: Sequence[dict[str, Any]]) -> tuple[datetime, datetime]:
    timestamps = sorted(_candlestick_time(candle) for candle in candles)
    if not timestamps:
        raise ValueError("cannot build event window from empty candles")
    return timestamps[0], timestamps[-1]


def _contract_candle_window(contract: KalshiTemperatureContract) -> tuple[datetime, datetime]:
    now = datetime.now(UTC)
    end = min(contract.close_time, now) if contract.close_time > now else contract.close_time
    if end <= contract.open_time:
        end = contract.close_time
    return contract.open_time, end


def _contract_summary(contract: KalshiTemperatureContract) -> dict[str, Any]:
    return {
        "ticker": contract.ticker,
        "series_ticker": contract.series_ticker,
        "title": contract.title,
        "status": contract.status,
        "threshold_f": contract.threshold_f,
        "direction": contract.direction,
        "target_time": contract.target_time.isoformat(),
        "open_time": contract.open_time.isoformat(),
        "close_time": contract.close_time.isoformat(),
        "location": {
            "name": contract.location.name,
            "latitude": contract.location.latitude,
            "longitude": contract.location.longitude,
            "timezone": contract.location.timezone,
        },
        "result": contract.result,
    }


def _temperature_direction_from_title(title: str) -> ThresholdDirection:
    lowered = title.lower()
    if " below " in lowered or "below" in lowered:
        return "below"
    return "above"


def _payload_datetime(payload: dict[str, Any], key: str) -> datetime:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"market payload missing {key}")
    return _parse_datetime(value)


def _increment(counts: dict[str, int], key: str) -> None:
    counts[key] = counts.get(key, 0) + 1


def _should_retry_candle_error(exc: Exception, *, attempt: int, retry_attempts: int) -> bool:
    if attempt >= retry_attempts:
        return False
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in {429, 500, 502, 503, 504}
    return isinstance(exc, (httpx.TimeoutException, httpx.TransportError))


def _candlestick_time(candle: dict[str, Any]) -> datetime:
    raw = candle.get("end_period_ts") or candle.get("end_ts") or candle.get("ts")
    if raw is None:
        raise ValueError("candlestick missing end_period_ts")
    return datetime.fromtimestamp(int(raw), tz=UTC)


def _candle_close(candle: dict[str, Any], key: str) -> Decimal | None:
    raw = candle.get(key)
    if not isinstance(raw, dict):
        return None
    for field in ("close", "close_dollars", "mean", "mean_dollars"):
        value = raw.get(field)
        if value is not None:
            return Decimal(str(value))
    return None


def _candle_depth(candle: dict[str, Any], *, fallback_depth: Decimal) -> Decimal:
    """Estimate executable top-of-book depth from REST candle metadata.

    Kalshi historical candles do not provide point-in-time LOB depth. When
    volume is present, using it as the synthetic top level is more realistic
    than a hard-coded one-contract book while still capping fills to observed
    REST market activity for that interval.
    """

    fallback = max(fallback_depth.to_integral_value(rounding=ROUND_FLOOR), Decimal("1"))
    for field in ("volume", "yes_volume", "open_interest"):
        raw = candle.get(field)
        try:
            value = Decimal(str(raw)) if raw is not None else Decimal("0")
        except ArithmeticError:
            value = Decimal("0")
        if value > 0:
            return max(value.to_integral_value(rounding=ROUND_FLOOR), Decimal("1"))
    return fallback


def _event_sort_key(event: NormalizedEvent) -> tuple[datetime, str]:
    if isinstance(event, OrderBookEvent):
        return event.book.received_at, str(event.event_id)
    if isinstance(event, QuoteEvent):
        return event.quote.received_at, str(event.event_id)
    if isinstance(event, ExternalSignalEvent):
        return event.received_at, str(event.event_id)
    if isinstance(event, SettlementResolvedEvent):
        return event.settlement.settled_at, str(event.event_id)
    raise TypeError(f"unsupported weather historical event: {type(event).__name__}")


def _read_json_object(path: Path) -> dict[str, Any]:
    decoded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(decoded, dict):
        raise ValueError(f"expected JSON object in {path}")
    return decoded


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _parse_date(value: str) -> date:
    return datetime.fromisoformat(value).date()


def _resolve_configs_root(path: Path) -> Path:
    if path.exists():
        return path
    for directory in (Path.cwd(), *Path.cwd().parents):
        candidate = directory / path
        if candidate.exists():
            return candidate
    return path
