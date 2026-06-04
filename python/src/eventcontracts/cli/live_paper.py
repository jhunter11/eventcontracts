"""Live no-trade paper runner.

Subscribes to live Kalshi market data, polls Open-Meteo for forecasts,
converts them to point-in-time `ExternalSignalEvent`s, and feeds both
streams into the strategy resolved from a TOML spec. Every emitted
`IntentEnvelope` is recorded to JSONL — **no orders are ever submitted to
the venue**. There is no live `VenueGateway` in this module by design.

Stage 1 of `docs/live-deployment-remaining-roadmap.md`. The MVP delivered
here:

- composes existing pieces (KalshiNormalizer, Open-Meteo client,
  TemperatureThresholdModel, WeatherTemperatureArbitrageStrategy, the
  InMemoryContext, and SleeveRiskGate) into a single async loop,
- discovers Kalshi weather contracts periodically,
- polls forecasts per unique location on a schedule,
- writes decisions / risk verdicts / forecast snapshots to a run directory,
- refreshes authenticated Kalshi account cash into `ctx.cash(...)` by default,
- prints stderr snapshots every N seconds,
- shuts down cleanly on Ctrl-C / SIGTERM.

Out of scope for this MVP (called out in the manifest under `limits`):
fill simulation, sequence-gap recovery on WS reconnect, persistent state
checkpoints, multi-strategy / multi-sleeve.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import fnmatch
import json
import os
import signal
import subprocess
import sys
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from eventcontracts.adapters.venues.kalshi.client import (
    KalshiPublicClient,
    KalshiWebSocketClient,
)
from eventcontracts.cli.weather import (
    KALSHI_WEATHER_LOCATIONS,
    KalshiTemperatureContract,
    parse_kalshi_temperature_contract,
)
from eventcontracts.config import load_sleeve_spec, load_strategy_spec
from eventcontracts.domain.decisions import (
    IntentEnvelope,
    PlaceOrder,
    decision_kind,
)
from eventcontracts.domain.events import EventProvenance, ExternalSignalEvent, NormalizedEvent
from eventcontracts.domain.ids import EventId, SleeveId, StrategyId
from eventcontracts.domain.positions import CashBalance
from eventcontracts.normalization.kalshi import KalshiNormalizer
from eventcontracts.risk.policy import SleeveRiskGate
from eventcontracts.runner import StrategyRunner
from eventcontracts.runner.ports import RiskDecision
from eventcontracts.strategy.registry import create_from_spec
from eventcontracts.testing.doubles import InMemoryContext
from eventcontracts.weather.calibration import (
    StationCalibration,
    load_calibration_meta,
    load_calibrations,
)
from eventcontracts.weather.clients import OpenMeteoClient
from eventcontracts.weather.distribution import (
    StationObservationSnapshot,
    build_daily_high_distribution,
    probability_for_contract,
)
from eventcontracts.weather.kxhigh import (
    KalshiHighContract,
    daily_high_from_snapshot,
    parse_kxhigh_market,
)
from eventcontracts.weather.temperature import (
    TemperatureThresholdMarket,
    TemperatureThresholdModel,
    WeatherLocation,
    snapshot_from_open_meteo_payload,
)

DEFAULT_KALSHI_CHANNELS: tuple[str, ...] = (
    "ticker",
    "trade",
    "orderbook_delta",
    "market_lifecycle_v2",
)

LiveWeatherContract = KalshiTemperatureContract | KalshiHighContract


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "live-paper",
        help="Live no-trade paper runner: feeds live Kalshi WS + Open-Meteo into a strategy.",
    )
    parser.add_argument("--strategy", type=Path, required=True, help="Strategy TOML spec.")
    parser.add_argument("--sleeve", type=Path, required=True, help="Sleeve TOML spec.")
    parser.add_argument(
        "--out",
        type=Path,
        required=True,
        help="Run directory. JSONL outputs + manifest land here.",
    )
    parser.add_argument(
        "--patterns",
        default="KXTEMP*,KXHIGH*,KXLOW*,KXWX*",
        help="Comma-separated weather ticker glob patterns.",
    )
    parser.add_argument(
        "--series-tickers",
        default="",
        help=(
            "Optional comma-separated Kalshi series tickers to discover directly "
            "(for example KXTEMPNYCH). Includes initialized markets so paper "
            "recording can start before the market turns active."
        ),
    )
    parser.add_argument(
        "--channels",
        default=",".join(DEFAULT_KALSHI_CHANNELS),
        help="Kalshi WS channels.",
    )
    parser.add_argument(
        "--max-duration-seconds",
        type=int,
        default=43200,
        help="Hard cap. Default 43200 = 12h.",
    )
    parser.add_argument(
        "--rediscover-interval-seconds",
        type=int,
        default=600,
        help="Re-list open weather markets every N seconds.",
    )
    parser.add_argument(
        "--forecast-interval-seconds",
        type=int,
        default=600,
        help="Poll Open-Meteo per unique location every N seconds.",
    )
    parser.add_argument(
        "--snapshot-interval-seconds",
        type=int,
        default=60,
        help="Print stderr progress every N seconds.",
    )
    parser.add_argument(
        "--discover-timeout-seconds",
        type=int,
        default=20,
    )
    parser.add_argument(
        "--discover-max-pages",
        type=int,
        default=5,
        help="Max REST pages per discovery (1000 markets/page).",
    )
    parser.add_argument(
        "--cash-source",
        choices=("account", "sleeve"),
        default="account",
        help=(
            "Source for ctx.cash(currency).available. "
            "Default account fetches Kalshi /portfolio/balance; sleeve uses the TOML allocation."
        ),
    )
    parser.add_argument(
        "--balance-refresh-seconds",
        type=int,
        default=60,
        help="Refresh Kalshi account balance every N seconds when --cash-source=account.",
    )
    parser.add_argument(
        "--kalshi-subaccount",
        type=int,
        default=0,
        help="Kalshi subaccount number for /portfolio/balance. Default 0 = primary.",
    )
    parser.add_argument(
        "--kxhigh-calibrations",
        type=Path,
        default=Path("configs/weather/station_calibrations.json"),
        help="Station calibration JSON for KXHIGH daily-high contracts.",
    )
    parser.set_defaults(handler=_handle)


# ---------- state ----------


@dataclass
class LiveStats:
    started_at: datetime
    raw_ws_events: int = 0
    normalized_events: int = 0
    normalize_skipped: int = 0
    forecast_snapshots: int = 0
    external_signals_emitted: int = 0
    kxhigh_signals_suppressed: int = 0
    decisions: int = 0
    decisions_by_kind: dict[str, int] = field(default_factory=dict)
    intents_dispatched: int = 0
    intents_rejected: int = 0
    risk_reject_reasons: dict[str, int] = field(default_factory=dict)
    discoveries: int = 0
    last_discovered_markets: int = 0
    cash_source: str = ""
    cash_available: Decimal | None = None
    cash_updated_at: datetime | None = None
    last_forecast_at: datetime | None = None
    last_event_at: datetime | None = None
    by_channel: dict[str, int] = field(default_factory=dict)


@dataclass
class RunFiles:
    run_dir: Path
    decisions: Path
    risk: Path
    signals: Path
    snapshots: Path

    @classmethod
    def open(cls, run_dir: Path) -> RunFiles:
        run_dir.mkdir(parents=True, exist_ok=True)
        return cls(
            run_dir=run_dir,
            decisions=run_dir / "decisions.jsonl",
            risk=run_dir / "risk_verdicts.jsonl",
            signals=run_dir / "external_signals.jsonl",
            snapshots=run_dir / "forecast_snapshots.jsonl",
        )

    def append(self, path: Path, payload: dict[str, Any]) -> None:
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, sort_keys=True, default=str) + "\n")


# ---------- handler ----------


def _handle(args: argparse.Namespace) -> int:
    strategy_spec = load_strategy_spec(args.strategy)
    sleeve_spec = load_sleeve_spec(args.sleeve)
    patterns = tuple(p.strip() for p in args.patterns.split(",") if p.strip())
    series_tickers = tuple(s.strip() for s in args.series_tickers.split(",") if s.strip())
    channels = tuple(c.strip() for c in args.channels.split(",") if c.strip())
    if not patterns:
        print("error: --patterns must include at least one entry", file=sys.stderr)
        return 2

    run_dir = args.out / datetime.now(UTC).strftime("run-%Y%m%dT%H%M%S%fZ")
    files = RunFiles.open(run_dir)
    stats = LiveStats(started_at=datetime.now(UTC))
    shutdown = asyncio.Event()

    def _on_signal(signum: int, _frame: object) -> None:
        signame = signal.Signals(signum).name
        print(f"[live-paper] received {signame}, shutting down...", file=sys.stderr, flush=True)
        try:
            asyncio.get_running_loop().call_soon_threadsafe(shutdown.set)
        except RuntimeError:
            shutdown.set()

    signal.signal(signal.SIGINT, _on_signal)
    with contextlib.suppress(AttributeError, ValueError):
        signal.signal(signal.SIGTERM, _on_signal)

    print(
        f"[live-paper] strategy={strategy_spec.name}@{strategy_spec.version} "
        f"sleeve={sleeve_spec.sleeve_id} out={run_dir}",
        file=sys.stderr,
        flush=True,
    )

    strategy = create_from_spec(strategy_spec)
    risk_gate = SleeveRiskGate(sleeve_spec)
    threshold_model = TemperatureThresholdModel()
    kxhigh_calibrations = _load_kxhigh_calibrations(args.kxhigh_calibrations)

    exit_code = 0
    try:
        asyncio.run(
            _run(
                strategy=strategy,
                strategy_spec=strategy_spec,
                sleeve_spec=sleeve_spec,
                risk_gate=risk_gate,
                threshold_model=threshold_model,
                kxhigh_calibrations=kxhigh_calibrations,
                patterns=patterns,
                series_tickers=series_tickers,
                channels=channels,
                files=files,
                stats=stats,
                args=args,
                shutdown=shutdown,
            )
        )
    except Exception as exc:  # noqa: BLE001
        exit_code = 1
        print(f"[live-paper] failed: {exc!r}", file=sys.stderr, flush=True)
    finally:
        manifest = _write_manifest(
            run_dir, stats, strategy_spec, sleeve_spec, patterns, series_tickers, channels, args
        )
        print(
            f"[live-paper] done; decisions={stats.decisions} "
            f"dispatched={stats.intents_dispatched} manifest={manifest}",
            file=sys.stderr,
            flush=True,
        )
    return exit_code


# ---------- async orchestration ----------


async def _run(
    *,
    strategy: Any,
    strategy_spec: Any,
    sleeve_spec: Any,
    risk_gate: SleeveRiskGate,
    threshold_model: TemperatureThresholdModel,
    kxhigh_calibrations: Mapping[str, StationCalibration],
    patterns: Sequence[str],
    series_tickers: Sequence[str],
    channels: Sequence[str],
    files: RunFiles,
    stats: LiveStats,
    args: argparse.Namespace,
    shutdown: asyncio.Event,
) -> None:
    deadline = stats.started_at + timedelta(seconds=args.max_duration_seconds)
    rest = KalshiPublicClient.from_env()
    open_meteo = OpenMeteoClient.from_env()
    event_queue: asyncio.Queue[NormalizedEvent] = asyncio.Queue(maxsize=10000)
    catalog = WeatherMarketCatalog()
    normalizer = KalshiNormalizer()
    handlers = normalizer.handlers()

    # Initial strategy lifecycle through the shared runner mechanics.
    cash_balance = await _initial_cash_balance(
        rest=rest,
        sleeve_spec=sleeve_spec,
        cash_source=args.cash_source,
        subaccount=args.kalshi_subaccount,
    )
    _record_cash_stats(stats, args.cash_source, cash_balance)
    ctx_provider = _ContextProvider(
        strategy_spec.strategy_id,
        sleeve_spec.sleeve_id,
        sleeve_spec.currency,
        cash_balance,
    )
    runner = StrategyRunner(
        spec=strategy_spec,
        sleeve=sleeve_spec,
        strategy=strategy,
        events=_EmptyEventSource(),
        sink=_LivePaperIntentSink(files=files, stats=stats),
        risk=risk_gate,
        clock=_LiveClock(),
        context_provider=ctx_provider,
        verdict_sink=_LivePaperRiskSink(files=files, stats=stats).on_verdict,
    )
    runner.start()

    tasks: list[asyncio.Task[Any]] = []

    tasks.append(
        asyncio.create_task(
            _ws_loop(
                rest=rest,
                catalog=catalog,
                channels=channels,
                patterns=patterns,
                series_tickers=series_tickers,
                event_queue=event_queue,
                handlers=handlers,
                stats=stats,
                shutdown=shutdown,
                deadline=deadline,
                rediscover_interval=args.rediscover_interval_seconds,
                discover_timeout=args.discover_timeout_seconds,
                discover_max_pages=args.discover_max_pages,
            )
        )
    )
    tasks.append(
        asyncio.create_task(
            _forecast_loop(
                open_meteo=open_meteo,
                catalog=catalog,
                threshold_model=threshold_model,
                kxhigh_calibrations=kxhigh_calibrations,
                event_queue=event_queue,
                files=files,
                stats=stats,
                shutdown=shutdown,
                deadline=deadline,
                interval=args.forecast_interval_seconds,
            )
        )
    )
    if args.cash_source == "account":
        tasks.append(
            asyncio.create_task(
                _balance_loop(
                    rest=rest,
                    ctx_provider=ctx_provider,
                    stats=stats,
                    currency=sleeve_spec.currency,
                    subaccount=args.kalshi_subaccount,
                    interval=args.balance_refresh_seconds,
                    shutdown=shutdown,
                    deadline=deadline,
                )
            )
        )
    tasks.append(
        asyncio.create_task(
            _snapshot_loop(
                stats=stats, interval=args.snapshot_interval_seconds, shutdown=shutdown
            )
        )
    )
    tasks.append(
        asyncio.create_task(
            _strategy_loop(
                runner=runner,
                event_queue=event_queue,
                shutdown=shutdown,
            )
        )
    )

    # Wait for shutdown or deadline.
    try:
        while not shutdown.is_set() and datetime.now(UTC) < deadline:
            try:
                await asyncio.wait_for(shutdown.wait(), timeout=2.0)
                break
            except TimeoutError:
                continue
    finally:
        shutdown.set()
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(BaseException):
                await task
        with contextlib.suppress(BaseException):
            runner.stop()


# ---------- WS loop ----------


async def _ws_loop(
    *,
    rest: KalshiPublicClient,
    catalog: WeatherMarketCatalog,
    channels: Sequence[str],
    patterns: Sequence[str],
    series_tickers: Sequence[str],
    event_queue: asyncio.Queue[NormalizedEvent],
    handlers: dict[Any, Any],
    stats: LiveStats,
    shutdown: asyncio.Event,
    deadline: datetime,
    rediscover_interval: int,
    discover_timeout: int,
    discover_max_pages: int,
) -> None:
    last_tickers: tuple[str, ...] = ()
    last_contracts: tuple[LiveWeatherContract, ...] = ()
    while not shutdown.is_set() and datetime.now(UTC) < deadline:
        try:
            tickers, contracts = await asyncio.wait_for(
                _discover_weather_markets(
                    rest,
                    patterns,
                    discover_max_pages,
                    series_tickers=series_tickers,
                ),
                timeout=discover_timeout,
            )
        except TimeoutError:
            print(
                f"[live-paper] discovery timed out; keeping {len(last_tickers)} previous markets",
                file=sys.stderr,
                flush=True,
            )
            tickers = last_tickers
            contracts = last_contracts
        stats.discoveries += 1
        stats.last_discovered_markets = len(tickers)
        catalog.update(contracts)

        if not tickers:
            print(
                f"[live-paper] no markets match {list(patterns)}; "
                f"sleeping {rediscover_interval}s before re-poll",
                file=sys.stderr,
                flush=True,
            )
            await _wait_or_shutdown(shutdown, rediscover_interval)
            continue
        last_tickers = tickers
        last_contracts = contracts

        print(
            f"[live-paper] subscribing to {len(tickers)} markets across "
            f"{len(catalog.locations())} locations",
            file=sys.stderr,
            flush=True,
        )

        ws = KalshiWebSocketClient.from_env()
        session_deadline = min(
            datetime.now(UTC) + timedelta(seconds=rediscover_interval), deadline
        )
        stream_task = asyncio.create_task(
            _stream_ws(ws, channels, tickers, event_queue, handlers, stats)
        )
        stop_task = asyncio.create_task(
            _wait_until_deadline(session_deadline, shutdown)
        )
        done, pending = await asyncio.wait(
            {stream_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        for task in pending:
            with contextlib.suppress(BaseException):
                await task
        for task in done:
            if task is stream_task:
                exc = task.exception()
                if exc is not None and not isinstance(exc, asyncio.CancelledError):
                    print(
                        f"[live-paper] ws stream error: {exc!r}; reconnecting",
                        file=sys.stderr,
                        flush=True,
                    )


async def _stream_ws(
    ws: KalshiWebSocketClient,
    channels: Sequence[str],
    tickers: Sequence[str],
    event_queue: asyncio.Queue[NormalizedEvent],
    handlers: dict[Any, Any],
    stats: LiveStats,
) -> None:
    async for envelope in ws.stream(channels=channels, market_tickers=list(tickers)):
        stats.raw_ws_events += 1
        chan = envelope.channel
        stats.by_channel[chan] = stats.by_channel.get(chan, 0) + 1
        handler = handlers.get((envelope.schema_version, chan))
        if handler is None:
            stats.normalize_skipped += 1
            continue
        try:
            normalized = handler(envelope)
        except Exception as exc:  # noqa: BLE001
            stats.normalize_skipped += 1
            if stats.normalize_skipped <= 5 or stats.normalize_skipped % 100 == 0:
                print(
                    f"[live-paper] normalize error on {chan}: {exc} "
                    f"(skipped={stats.normalize_skipped})",
                    file=sys.stderr,
                    flush=True,
                )
            continue
        events = normalized if isinstance(normalized, tuple) else (normalized,)
        stats.normalized_events += len(events)
        stats.last_event_at = datetime.now(UTC)
        for event in events:
            await event_queue.put(event)


async def _discover_weather_markets(
    rest: KalshiPublicClient,
    patterns: Sequence[str],
    max_pages: int,
    *,
    series_tickers: Sequence[str] = (),
) -> tuple[tuple[str, ...], tuple[LiveWeatherContract, ...]]:
    globs = [p for p in patterns if any(c in p for c in "*?[]")]
    exact = {p for p in patterns if not any(c in p for c in "*?[]")}
    matched_tickers: set[str] = set(exact)
    contracts: list[LiveWeatherContract] = []

    if series_tickers:
        for series_ticker in series_tickers:
            series_cursor: str | None = None
            for _ in range(max_pages):
                payload = await rest.get_markets_payload(
                    limit=1000,
                    cursor=series_cursor,
                    series_ticker=series_ticker,
                )
                for market in payload.get("markets", []) or []:
                    if not isinstance(market, dict) or not _is_live_or_upcoming_market(market):
                        continue
                    ticker = market.get("ticker") or market.get("market_ticker")
                    if not isinstance(ticker, str):
                        continue
                    if ticker in exact or any(fnmatch.fnmatchcase(ticker, g) for g in globs):
                        matched_tickers.add(ticker)
                        contract = _parse_live_weather_contract(market)
                        if contract is not None:
                            contracts.append(contract)
                cursor_value = payload.get("cursor")
                if not cursor_value:
                    break
                series_cursor = str(cursor_value)
        return tuple(sorted(matched_tickers)), tuple(contracts)

    cursor: str | None = None
    for _ in range(max_pages):
        payload = await rest.get_markets_payload(limit=1000, cursor=cursor, status="open")
        markets = payload.get("markets", []) or []
        for market in markets:
            if not isinstance(market, dict):
                continue
            ticker = market.get("ticker") or market.get("market_ticker")
            if not isinstance(ticker, str):
                continue
            if any(fnmatch.fnmatchcase(ticker, g) for g in globs):
                matched_tickers.add(ticker)
                contract = _parse_live_weather_contract(market)
                if contract is not None:
                    contracts.append(contract)
        cursor_value = payload.get("cursor")
        if not cursor_value:
            break
        cursor = str(cursor_value)
    return tuple(sorted(matched_tickers)), tuple(contracts)


def _is_live_or_upcoming_market(market: dict[str, object]) -> bool:
    status = market.get("status")
    return isinstance(status, str) and status.lower() in {"active", "initialized", "open"}


# ---------- forecast loop ----------


async def _forecast_loop(
    *,
    open_meteo: OpenMeteoClient,
    catalog: WeatherMarketCatalog,
    threshold_model: TemperatureThresholdModel,
    kxhigh_calibrations: Mapping[str, StationCalibration],
    event_queue: asyncio.Queue[NormalizedEvent],
    files: RunFiles,
    stats: LiveStats,
    shutdown: asyncio.Event,
    deadline: datetime,
    interval: int,
) -> None:
    while not shutdown.is_set() and datetime.now(UTC) < deadline:
        locations = catalog.locations()
        if not locations:
            await _wait_or_shutdown(shutdown, interval)
            continue
        for location in locations:
            if shutdown.is_set():
                break
            try:
                payload = await open_meteo.get_forecast_payload(
                    latitude=location.latitude,
                    longitude=location.longitude,
                    timezone=location.timezone,
                )
            except Exception as exc:  # noqa: BLE001
                print(
                    f"[live-paper] open-meteo error for {location.name}: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
                continue
            as_of = datetime.now(UTC)
            # Open-Meteo returns LOCAL wall-clock hourly times when `timezone=` is
            # set (parsed naive->UTC), so the daily-high "day" is the local calendar
            # day. Derive the local current day from the response's own
            # utc_offset_seconds so the KXHIGH lead filter matches points_for_day's
            # bucketing exactly, with no tz-database dependency.
            local_today = _payload_local_today(payload, as_of)
            try:
                snapshot = snapshot_from_open_meteo_payload(
                    payload, location=location, as_of=as_of
                )
            except Exception as exc:  # noqa: BLE001
                print(
                    f"[live-paper] snapshot parse error for {location.name}: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
                continue
            stats.forecast_snapshots += 1
            stats.last_forecast_at = as_of
            files.append(
                files.snapshots,
                {
                    "as_of": as_of.isoformat(),
                    "location": location.name,
                    "lat": location.latitude,
                    "lon": location.longitude,
                    "hourly_points": len(snapshot.hourly),
                },
            )

            for contract in catalog.contracts_for_location(location):
                try:
                    if isinstance(contract, KalshiHighContract):
                        emitted = _kxhigh_external_signal(
                            contract,
                            snapshot=snapshot,
                            as_of=as_of,
                            calibrations=kxhigh_calibrations,
                            local_today=local_today,
                            high_so_far_f=_payload_high_so_far_f(payload, contract.target_day, as_of),
                        )
                        if emitted is None:
                            # Suppressed: lead!=0 (nowcast-lead sigma is over-confident
                            # for future days / past days have settled) or the market
                            # has already closed. Don't emit a false/dead signal.
                            stats.kxhigh_signals_suppressed += 1
                            continue
                        signal_event, signal_row = emitted
                    else:
                        market = TemperatureThresholdMarket(
                            instrument_id=_kalshi_instrument(contract.ticker),
                            threshold_f=contract.threshold_f,
                            target_day=contract.target_time.date(),
                            direction=contract.direction,
                            target_time=contract.target_time,
                        )
                        prediction = threshold_model.predict(snapshot, market)
                        signal_event = prediction.to_external_signal()
                        signal_row = {
                            "as_of": as_of.isoformat(),
                            "instrument": contract.ticker,
                            "implied_prob": prediction.implied_probability,
                            "expected_temperature_f": prediction.expected_high_f,
                            "uncertainty_f": prediction.uncertainty_f,
                        }
                except Exception as exc:  # noqa: BLE001
                    print(
                        f"[live-paper] predict error for {contract.ticker}: {exc}",
                        file=sys.stderr,
                        flush=True,
                    )
                    continue
                stats.external_signals_emitted += 1
                files.append(files.signals, signal_row)
                await event_queue.put(signal_event)
        await _wait_or_shutdown(shutdown, interval)


# ---------- strategy loop ----------


async def _strategy_loop(
    *,
    runner: StrategyRunner,
    event_queue: asyncio.Queue[NormalizedEvent],
    shutdown: asyncio.Event,
) -> None:
    while not shutdown.is_set():
        try:
            event = await asyncio.wait_for(event_queue.get(), timeout=2.0)
        except TimeoutError:
            continue
        try:
            runner.process_event(event)
        except Exception as exc:  # noqa: BLE001
            print(f"[live-paper] strategy error: {exc!r}", file=sys.stderr, flush=True)
            continue


# ---------- helpers ----------


class _LiveClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class _EmptyEventSource:
    def stream(self) -> Iterator[NormalizedEvent]:
        return iter(())


@dataclass
class _LivePaperRiskSink:
    files: RunFiles
    stats: LiveStats

    def on_verdict(self, envelope: IntentEnvelope, verdict: RiskDecision) -> None:
        kind = decision_kind(envelope.decision)
        self.stats.decisions += 1
        self.stats.decisions_by_kind[kind] = self.stats.decisions_by_kind.get(kind, 0) + 1
        self.files.append(
            self.files.risk,
            {
                "correlation_id": str(envelope.correlation_id),
                "decision_kind": kind,
                "allowed": verdict.allowed,
                "reasons": list(verdict.reasons),
                "emitted_at": envelope.emitted_at.isoformat(),
            },
        )
        if not verdict.allowed:
            self.stats.intents_rejected += 1
            for reason in verdict.reasons or ("unspecified",):
                self.stats.risk_reject_reasons[reason] = (
                    self.stats.risk_reject_reasons.get(reason, 0) + 1
                )


@dataclass
class _LivePaperIntentSink:
    files: RunFiles
    stats: LiveStats

    def emit(self, envelope: IntentEnvelope) -> None:
        decision = envelope.decision
        kind = decision_kind(decision)
        self.stats.intents_dispatched += 1
        self.files.append(
            self.files.decisions,
            {
                "correlation_id": str(envelope.correlation_id),
                "decision_kind": kind,
                "instrument": _instrument_str(decision),
                "decision_repr": repr(decision),
                "emitted_at": envelope.emitted_at.isoformat(),
                "triggered_by_event_id": str(envelope.triggered_by_event_id)
                if envelope.triggered_by_event_id is not None
                else None,
            },
        )


@dataclass
class WeatherMarketCatalog:
    """Tracks open weather contracts and the unique locations to poll."""

    _by_ticker: dict[str, LiveWeatherContract] = field(default_factory=dict)

    def update(self, contracts: Sequence[LiveWeatherContract]) -> None:
        self._by_ticker = {c.ticker: c for c in contracts}

    def locations(self) -> tuple[WeatherLocation, ...]:
        seen: dict[tuple[float, float], WeatherLocation] = {}
        for contract in self._by_ticker.values():
            key = (contract.location.latitude, contract.location.longitude)
            seen.setdefault(key, contract.location)
        for location in KALSHI_WEATHER_LOCATIONS.values():
            key = (location.latitude, location.longitude)
            seen.setdefault(key, location)
        return tuple(seen.values())

    def contracts_for_location(
        self, location: WeatherLocation
    ) -> tuple[LiveWeatherContract, ...]:
        return tuple(
            contract
            for contract in self._by_ticker.values()
            if contract.location.latitude == location.latitude
            and contract.location.longitude == location.longitude
        )


def _calibration_staleness_warning(
    meta: Mapping[str, Any], *, now: datetime, max_age_days: float = 7.0
) -> str | None:
    """Warn if the persisted calibration lacks provenance or is older than
    ``max_age_days``. The KXHIGH fit is a recency-weighted trailing-window snapshot
    meant to be regenerated as new GHCND actuals land; a stale fit silently
    misprices the live brackets, so the live runner surfaces this at startup."""
    generated_at = meta.get("generated_at")
    if not generated_at:
        return (
            "KXHIGH calibration has no generated_at provenance; cannot verify "
            "freshness — regenerate via scripts/weather_calibration_report.py"
        )
    try:
        ts = datetime.fromisoformat(str(generated_at).replace("Z", "+00:00"))
    except ValueError:
        return f"KXHIGH calibration generated_at is unparsable: {generated_at!r}"
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    age_days = (now - ts).total_seconds() / 86_400.0
    if age_days > max_age_days:
        return (
            f"KXHIGH calibration is {age_days:.1f} days old (> {max_age_days:.0f}d); "
            "regenerate the trailing-window fit before relying on it"
        )
    return None


def _load_kxhigh_calibrations(path: Path) -> dict[str, StationCalibration]:
    if not path.exists():
        print(
            f"[live-paper] KXHIGH calibration file missing: {path}; "
            "KXHIGH forecast signals will be skipped",
            file=sys.stderr,
            flush=True,
        )
        return {}
    calibs = load_calibrations(path)
    warning = _calibration_staleness_warning(load_calibration_meta(path), now=datetime.now(UTC))
    if warning is not None:
        print(f"[live-paper] WARNING: {warning}", file=sys.stderr, flush=True)
    return calibs


def _parse_live_weather_contract(market: dict[str, Any]) -> LiveWeatherContract | None:
    return parse_kalshi_temperature_contract(market) or parse_kxhigh_market(market)


def _payload_local_today(payload: Mapping[str, Any], as_of: datetime) -> date:
    """Local calendar 'today' for an Open-Meteo response.

    Uses the API's own ``utc_offset_seconds`` so the result is in the same local
    wall-clock frame that :func:`snapshot_from_open_meteo_payload` buckets hourly
    points into. Falls back to the UTC date if the offset is absent/unparsable.
    """
    offset = payload.get("utc_offset_seconds")
    if offset is None:
        return as_of.date()
    try:
        local_dt: datetime = as_of + timedelta(seconds=int(offset))
    except (TypeError, ValueError):
        return as_of.date()
    return local_dt.date()


def _payload_high_so_far_f(payload: Mapping[str, Any], target_day: date, as_of: datetime) -> float | None:
    """Best-effort Open-Meteo hourly proxy for today's high so far.

    This is not the official NWS settlement print. It is a same-provider,
    same-location lower-bound proxy used to condition the KXHIGH distribution
    during the lead-0 window.
    """

    hourly = payload.get("hourly")
    if not isinstance(hourly, Mapping):
        return None
    times = hourly.get("time")
    temperatures = hourly.get("temperature_2m")
    if not isinstance(times, list) or not isinstance(temperatures, list):
        return None
    offset = payload.get("utc_offset_seconds")
    if offset is None:
        return None
    try:
        offset_seconds = int(offset)
    except (TypeError, ValueError):
        return None
    local_now = (as_of + timedelta(seconds=offset_seconds)).replace(tzinfo=None)
    if local_now.date() != target_day:
        return None
    high_so_far: float | None = None
    for raw_time, raw_temperature in zip(times, temperatures, strict=False):
        if raw_temperature is None:
            continue
        try:
            point_time = datetime.fromisoformat(str(raw_time).replace("Z", "+00:00"))
            temperature = float(raw_temperature)
        except (TypeError, ValueError):
            continue
        if point_time.tzinfo is None:
            point_day = point_time.date()
            is_observed_or_now = point_time <= local_now
        else:
            point_utc = point_time.astimezone(UTC)
            point_day = (point_utc + timedelta(seconds=offset_seconds)).date()
            is_observed_or_now = point_utc <= as_of
        if point_day == target_day and is_observed_or_now:
            high_so_far = temperature if high_so_far is None else max(high_so_far, temperature)
    return high_so_far


def _snapshot_local_today(snapshot: Any) -> date:
    """Fallback local 'today' when no payload offset is available: the earliest
    forecast day present. The live producer fetches with past_days=0, so the first
    local day in the snapshot is the current local day."""
    earliest: date = min(point.timestamp.date() for point in snapshot.hourly)
    return earliest


def _kxhigh_external_signal(
    contract: KalshiHighContract,
    *,
    snapshot: Any,
    as_of: datetime,
    calibrations: Mapping[str, StationCalibration],
    local_today: date | None = None,
    high_so_far_f: float | None = None,
) -> tuple[ExternalSignalEvent, dict[str, Any]] | None:
    """Price a KXHIGH bracket into an ``ExternalSignalEvent``, or ``None`` when the
    signal must be suppressed.

    The station calibration sigma is fit at ~nowcast lead (the historical-forecast
    archive), so brackets settling on a future local day (lead>=1) are
    systematically over-confident and manufacture wing "edges"
    (see ``docs/weather-kxhigh-validation-and-edge-spec.md`` Phase 2). Until a
    lead-aware sigma exists, only same-day (lead==0) signals are trustworthy;
    lead<0 brackets have already settled. This mirrors the lead==0 gate the offline
    recorder (``scripts/weather_kxhigh_paper.py``) already enforces, so the live
    trading path and the paper recorder agree.
    """
    calibration = calibrations.get(contract.station_code)
    if calibration is None:
        raise ValueError(f"missing KXHIGH calibration for station {contract.station_code}")
    if local_today is None:
        local_today = _snapshot_local_today(snapshot)
    lead_days = (contract.target_day - local_today).days
    if lead_days != 0:
        return None
    # Suppress markets that have already closed (a race between discovery and this
    # forecast tick). The absolute close_time is carried in the payload so the
    # strategy can apply a fresh "no new trades within N seconds of close" gate.
    seconds_to_close: float | None = None
    if contract.close_time is not None:
        seconds_to_close = (contract.close_time - as_of).total_seconds()
        if seconds_to_close <= 0:
            return None
    raw_high = daily_high_from_snapshot(snapshot, contract.target_day)
    observation = (
        StationObservationSnapshot(
            station_code=contract.station_code,
            target_day=contract.target_day,
            observed_high_f=high_so_far_f,
            as_of=as_of,
            source="open_meteo_hourly_proxy",
        )
        if high_so_far_f is not None
        else None
    )
    distribution = build_daily_high_distribution(
        snapshot,
        contract.target_day,
        calibration,
        observation=observation,
    )
    expected_high = distribution.mean_f
    implied_prob = probability_for_contract(contract, distribution)
    signal_event = ExternalSignalEvent(
        event_id=EventId(
            "weather-temperature-"
            f"{contract.ticker}-{contract.target_day.isoformat()}-{_event_ts(as_of)}"
        ),
        source=snapshot.source,
        exchange_ts=as_of,
        received_at=as_of,
        schema_version="weather-temperature-probability-v1",
        payload={
            "market_id": contract.ticker,
            "implied_prob": implied_prob,
            "lead_days": lead_days,
            "close_time": contract.close_time.isoformat() if contract.close_time is not None else None,
            "seconds_to_close": seconds_to_close,
            "instrument_id": {
                "venue": "kalshi",
                "market_id": contract.ticker,
                "outcome_id": None,
            },
            "location": {
                "name": contract.location.name,
                "latitude": contract.location.latitude,
                "longitude": contract.location.longitude,
                "timezone": contract.location.timezone,
                "station_id": contract.location.station_id,
            },
            "target_day": contract.target_day.isoformat(),
            "target_time": None,
            "threshold_f": contract.floor_strike,
            "cap_threshold_f": contract.cap_strike,
            "direction": contract.strike_type,
            "temperature_basis": "daily_high",
            "expected_temperature_f": expected_high,
            "raw_forecast_temperature_f": raw_high,
            "expected_high_f": expected_high,
            "raw_forecast_high_f": raw_high,
            "uncertainty_f": calibration.effective_sigma(),
            "model_family": f"kxhigh_distribution:{distribution.method}:{calibration.station}",
            "distribution_method": distribution.method,
            "distribution_feature_hash": distribution.feature_hash,
            "high_so_far_f": high_so_far_f,
            "high_so_far_source": "open_meteo_hourly_proxy" if high_so_far_f is not None else None,
            "latent_expected_high_f": distribution.latent_mean_f,
            "features": {
                "floor_strike": contract.floor_strike,
                "cap_strike": contract.cap_strike,
                "distribution_mean_f": distribution.mean_f,
                "high_so_far_f": high_so_far_f,
            },
        },
        provenance=EventProvenance(
            source=snapshot.source,
            channel="kxhigh_station_calibration",
            schema_version="weather-temperature-probability-v1",
            venue=_kalshi_instrument(contract.ticker).venue,
            metadata={"model_family": f"kxhigh_distribution:{distribution.method}:{calibration.station}"},
        ),
    )
    signal_row = {
        "as_of": as_of.isoformat(),
        "instrument": contract.ticker,
        "implied_prob": implied_prob,
        "lead_days": lead_days,
        "target_day": contract.target_day.isoformat(),
        "close_time": contract.close_time.isoformat() if contract.close_time is not None else None,
        "seconds_to_close": seconds_to_close,
        "expected_temperature_f": expected_high,
        "raw_forecast_temperature_f": raw_high,
        "uncertainty_f": calibration.effective_sigma(),
        "model_family": f"kxhigh_distribution:{distribution.method}:{calibration.station}",
        "distribution_method": distribution.method,
        "distribution_feature_hash": distribution.feature_hash,
        "high_so_far_f": high_so_far_f,
        "high_so_far_source": "open_meteo_hourly_proxy" if high_so_far_f is not None else None,
        "latent_expected_high_f": distribution.latent_mean_f,
    }
    return signal_event, signal_row


async def _initial_cash_balance(
    *,
    rest: KalshiPublicClient,
    sleeve_spec: Any,
    cash_source: str,
    subaccount: int,
) -> CashBalance:
    if cash_source == "account":
        try:
            balance = await rest.get_cash_balance(
                currency=sleeve_spec.currency,
                subaccount=subaccount,
            )
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                "could not read Kalshi account balance from /portfolio/balance; "
                "set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH for "
                "--cash-source account, or use --cash-source sleeve for dry paper"
            ) from exc
        print(
            f"[live-paper] account cash={balance.available} {balance.currency} "
            f"updated_at={balance.updated_at.isoformat()}",
            file=sys.stderr,
            flush=True,
        )
        return balance
    return _sleeve_cash_balance(sleeve_spec)


async def _balance_loop(
    *,
    rest: KalshiPublicClient,
    ctx_provider: _ContextProvider,
    stats: LiveStats,
    currency: str,
    subaccount: int,
    interval: int,
    shutdown: asyncio.Event,
    deadline: datetime,
) -> None:
    while not shutdown.is_set() and datetime.now(UTC) < deadline:
        await _wait_or_shutdown(shutdown, interval)
        if shutdown.is_set() or datetime.now(UTC) >= deadline:
            return
        try:
            balance = await rest.get_cash_balance(
                currency=currency,
                subaccount=subaccount,
            )
        except Exception as exc:  # noqa: BLE001
            print(
                f"[live-paper] balance refresh failed: {exc!r}",
                file=sys.stderr,
                flush=True,
            )
            continue
        ctx_provider.update_cash(balance)
        _record_cash_stats(stats, "account", balance)


def _sleeve_cash_balance(sleeve_spec: Any) -> CashBalance:
    now = datetime.now(UTC)
    available = Decimal(str(sleeve_spec.capital_allocation))
    return CashBalance(
        currency=sleeve_spec.currency,
        total=available,
        available=available,
        held_for_orders=Decimal("0"),
        settling=Decimal("0"),
        updated_at=now,
    )


def _record_cash_stats(stats: LiveStats, cash_source: str, balance: CashBalance) -> None:
    stats.cash_source = cash_source
    stats.cash_available = balance.available
    stats.cash_updated_at = balance.updated_at


@dataclass
class _ContextProvider:
    strategy_id: StrategyId
    sleeve_id: SleeveId
    currency: str
    cash_balance: CashBalance

    def update_cash(self, cash_balance: CashBalance) -> None:
        self.cash_balance = cash_balance

    def context(self) -> InMemoryContext:
        now = datetime.now(UTC)
        balance = self.cash_balance
        return InMemoryContext(
            strategy_id_value=self.strategy_id,
            sleeve_id_value=self.sleeve_id,
            clock_now=now,
            cash_by_ccy={
                self.currency: CashBalance(
                    currency=balance.currency,
                    total=balance.total,
                    available=balance.available,
                    held_for_orders=balance.held_for_orders,
                    settling=balance.settling,
                    updated_at=balance.updated_at,
                ),
            },
        )


def _kalshi_instrument(ticker: str) -> Any:
    from eventcontracts.domain.models import InstrumentId, Venue

    return InstrumentId(venue=Venue.KALSHI, market_id=ticker)


def _event_ts(value: datetime) -> str:
    return value.isoformat().replace("+", "p").replace(":", "").replace("-", "").replace(".", "")


def _instrument_str(decision: Any) -> str | None:
    if isinstance(decision, PlaceOrder):
        return f"{decision.instrument_id.venue.value}:{decision.instrument_id.market_id}"
    return None


async def _wait_or_shutdown(shutdown: asyncio.Event, seconds: int) -> None:
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(shutdown.wait(), timeout=seconds)


async def _wait_until_deadline(
    deadline: datetime, shutdown: asyncio.Event
) -> None:
    while not shutdown.is_set():
        remaining = (deadline - datetime.now(UTC)).total_seconds()
        if remaining <= 0:
            return
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=min(remaining, 2.0))
            return
        except TimeoutError:
            continue


async def _snapshot_loop(
    *, stats: LiveStats, interval: int, shutdown: asyncio.Event
) -> None:
    while not shutdown.is_set():
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=interval)
            return
        except TimeoutError:
            _print_snapshot(stats)


def _print_snapshot(stats: LiveStats) -> None:
    now = datetime.now(UTC)
    elapsed = max((now - stats.started_at).total_seconds(), 1e-9)
    rate = stats.normalized_events / elapsed
    last_ev = stats.last_event_at.isoformat() if stats.last_event_at else "-"
    last_fc = stats.last_forecast_at.isoformat() if stats.last_forecast_at else "-"
    cash = str(stats.cash_available) if stats.cash_available is not None else "-"
    print(
        f"[live-paper] elapsed={int(elapsed)}s "
        f"discoveries={stats.discoveries} markets={stats.last_discovered_markets} "
        f"raw_ws={stats.raw_ws_events} normalized={stats.normalized_events} "
        f"({rate:.1f}/s) signals={stats.external_signals_emitted} "
        f"forecasts={stats.forecast_snapshots} decisions={stats.decisions} "
        f"dispatched={stats.intents_dispatched} rejected={stats.intents_rejected} "
        f"cash={cash} source={stats.cash_source or '-'} "
        f"last_event={last_ev} last_forecast={last_fc}",
        file=sys.stderr,
        flush=True,
    )


def _write_manifest(
    run_dir: Path,
    stats: LiveStats,
    strategy_spec: Any,
    sleeve_spec: Any,
    patterns: Sequence[str],
    series_tickers: Sequence[str],
    channels: Sequence[str],
    args: argparse.Namespace,
) -> Path:
    ended_at = datetime.now(UTC)
    manifest = {
        "kind": "live-paper",
        "started_at": stats.started_at.isoformat(),
        "ended_at": ended_at.isoformat(),
        "elapsed_seconds": int((ended_at - stats.started_at).total_seconds()),
        "env": os.environ.get("EVENTCONTRACTS_ENV", "unknown"),
        "code_version": os.environ.get("EVENTCONTRACTS_CODE_VERSION")
        or _git_head_sha()
        or "unknown",
        "kalshi_env": os.environ.get("KALSHI_ENV", "unknown"),
        "strategy": {
            "strategy_id": str(strategy_spec.strategy_id),
            "name": strategy_spec.name,
            "version": strategy_spec.version,
        },
        "sleeve": {
            "sleeve_id": str(sleeve_spec.sleeve_id),
            "venue": sleeve_spec.venue.value
            if hasattr(sleeve_spec.venue, "value")
            else str(sleeve_spec.venue),
        },
        "patterns": list(patterns),
        "series_tickers": list(series_tickers),
        "channels": list(channels),
        "args": {
            "max_duration_seconds": args.max_duration_seconds,
            "rediscover_interval_seconds": args.rediscover_interval_seconds,
            "forecast_interval_seconds": args.forecast_interval_seconds,
            "snapshot_interval_seconds": args.snapshot_interval_seconds,
            "discover_timeout_seconds": args.discover_timeout_seconds,
            "discover_max_pages": args.discover_max_pages,
            "cash_source": args.cash_source,
            "balance_refresh_seconds": args.balance_refresh_seconds,
            "kalshi_subaccount": args.kalshi_subaccount,
        },
        "stats": {
            "discoveries": stats.discoveries,
            "last_discovered_markets": stats.last_discovered_markets,
            "raw_ws_events": stats.raw_ws_events,
            "normalized_events": stats.normalized_events,
            "normalize_skipped": stats.normalize_skipped,
            "by_channel": dict(stats.by_channel),
            "external_signals_emitted": stats.external_signals_emitted,
            "kxhigh_signals_suppressed": stats.kxhigh_signals_suppressed,
            "forecast_snapshots": stats.forecast_snapshots,
            "decisions": stats.decisions,
            "decisions_by_kind": dict(stats.decisions_by_kind),
            "intents_dispatched": stats.intents_dispatched,
            "intents_rejected": stats.intents_rejected,
            "risk_reject_reasons": dict(stats.risk_reject_reasons),
            "cash_source": stats.cash_source,
            "cash_available": str(stats.cash_available)
            if stats.cash_available is not None
            else None,
            "cash_updated_at": stats.cash_updated_at.isoformat()
            if stats.cash_updated_at
            else None,
            "last_event_at": stats.last_event_at.isoformat() if stats.last_event_at else None,
            "last_forecast_at": stats.last_forecast_at.isoformat()
            if stats.last_forecast_at
            else None,
        },
        "limits": [
            "no fill simulation; decisions recorded only",
            "no sequence-gap recovery on ws reconnect",
            "no state checkpoints",
            "risk gate uses point-in-time context cash/exposure/open-order snapshots",
        ],
    }
    manifest_path = run_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest_path


def _git_head_sha() -> str | None:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True,
        check=False,
        text=True,
    )
    if completed.returncode != 0:
        return None
    sha = completed.stdout.strip()
    return sha or None
