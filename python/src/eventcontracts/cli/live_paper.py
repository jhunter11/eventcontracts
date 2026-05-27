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
  InMemoryContext, an allow-all risk gate) into a single async loop,
- discovers Kalshi weather contracts periodically,
- polls forecasts per unique location on a schedule,
- writes decisions / risk verdicts / forecast snapshots to a run directory,
- prints stderr snapshots every N seconds,
- shuts down cleanly on Ctrl-C / SIGTERM.

Out of scope for this MVP (called out in the manifest under `limits`):
fill simulation, sequence-gap recovery on WS reconnect, persistent state
checkpoints, multi-strategy / multi-sleeve, real risk gate driven by the
sleeve's `risk` block.
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
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

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
    decision_priority,
)
from eventcontracts.domain.events import NormalizedEvent
from eventcontracts.domain.ids import CorrelationId, SleeveId, StrategyId
from eventcontracts.normalization.kalshi import KalshiNormalizer
from eventcontracts.runner.ports import RiskDecision
from eventcontracts.strategy.registry import create_from_spec
from eventcontracts.testing.doubles import AllowAllRiskGate, InMemoryContext
from eventcontracts.weather.clients import OpenMeteoClient
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
    decisions: int = 0
    decisions_by_kind: dict[str, int] = field(default_factory=dict)
    intents_dispatched: int = 0
    intents_rejected: int = 0
    risk_reject_reasons: dict[str, int] = field(default_factory=dict)
    discoveries: int = 0
    last_discovered_markets: int = 0
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
    risk_gate = AllowAllRiskGate()
    threshold_model = TemperatureThresholdModel()

    try:
        asyncio.run(
            _run(
                strategy=strategy,
                strategy_spec=strategy_spec,
                sleeve_spec=sleeve_spec,
                risk_gate=risk_gate,
                threshold_model=threshold_model,
                patterns=patterns,
                series_tickers=series_tickers,
                channels=channels,
                files=files,
                stats=stats,
                args=args,
                shutdown=shutdown,
            )
        )
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
    return 0


# ---------- async orchestration ----------


async def _run(
    *,
    strategy: Any,
    strategy_spec: Any,
    sleeve_spec: Any,
    risk_gate: AllowAllRiskGate,
    threshold_model: TemperatureThresholdModel,
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

    # Initial strategy lifecycle.
    ctx_provider = _ContextProvider(strategy_spec.strategy_id, sleeve_spec.sleeve_id)
    strategy.on_init(ctx_provider.context())

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
                event_queue=event_queue,
                files=files,
                stats=stats,
                shutdown=shutdown,
                deadline=deadline,
                interval=args.forecast_interval_seconds,
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
                strategy=strategy,
                strategy_spec=strategy_spec,
                sleeve_spec=sleeve_spec,
                risk_gate=risk_gate,
                ctx_provider=ctx_provider,
                event_queue=event_queue,
                files=files,
                stats=stats,
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
            strategy.on_shutdown(ctx_provider.context())


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
    last_contracts: tuple[KalshiTemperatureContract, ...] = ()
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
        stats.normalized_events += 1
        stats.last_event_at = datetime.now(UTC)
        await event_queue.put(normalized)


async def _discover_weather_markets(
    rest: KalshiPublicClient,
    patterns: Sequence[str],
    max_pages: int,
    *,
    series_tickers: Sequence[str] = (),
) -> tuple[tuple[str, ...], tuple[KalshiTemperatureContract, ...]]:
    globs = [p for p in patterns if any(c in p for c in "*?[]")]
    exact = {p for p in patterns if not any(c in p for c in "*?[]")}
    matched_tickers: set[str] = set(exact)
    contracts: list[KalshiTemperatureContract] = []

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
                        contract = parse_kalshi_temperature_contract(market)
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
                contract = parse_kalshi_temperature_contract(market)
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
                market = TemperatureThresholdMarket(
                    instrument_id=_kalshi_instrument(contract.ticker),
                    threshold_f=contract.threshold_f,
                    target_day=contract.target_time.date(),
                    direction=contract.direction,
                    target_time=contract.target_time,
                )
                try:
                    prediction = threshold_model.predict(snapshot, market)
                except Exception as exc:  # noqa: BLE001
                    print(
                        f"[live-paper] predict error for {contract.ticker}: {exc}",
                        file=sys.stderr,
                        flush=True,
                    )
                    continue
                signal_event = prediction.to_external_signal()
                stats.external_signals_emitted += 1
                files.append(
                    files.signals,
                    {
                        "as_of": as_of.isoformat(),
                        "instrument": contract.ticker,
                        "implied_prob": prediction.implied_probability,
                        "expected_temperature_f": prediction.expected_high_f,
                        "uncertainty_f": prediction.uncertainty_f,
                    },
                )
                await event_queue.put(signal_event)
        await _wait_or_shutdown(shutdown, interval)


# ---------- strategy loop ----------


async def _strategy_loop(
    *,
    strategy: Any,
    strategy_spec: Any,
    sleeve_spec: Any,
    risk_gate: AllowAllRiskGate,
    ctx_provider: _ContextProvider,
    event_queue: asyncio.Queue[NormalizedEvent],
    files: RunFiles,
    stats: LiveStats,
    shutdown: asyncio.Event,
) -> None:
    while not shutdown.is_set():
        try:
            event = await asyncio.wait_for(event_queue.get(), timeout=2.0)
        except TimeoutError:
            continue
        ctx = ctx_provider.context()
        try:
            decisions = strategy.on_event(event, ctx)
        except Exception as exc:  # noqa: BLE001
            print(f"[live-paper] strategy error: {exc!r}", file=sys.stderr, flush=True)
            continue
        for decision in decisions:
            kind = decision_kind(decision)
            stats.decisions += 1
            stats.decisions_by_kind[kind] = stats.decisions_by_kind.get(kind, 0) + 1
            envelope = IntentEnvelope(
                decision=decision,
                strategy_id=strategy_spec.strategy_id,
                sleeve_id=sleeve_spec.sleeve_id,
                correlation_id=CorrelationId(uuid4().hex),
                emitted_at=datetime.now(UTC),
                priority=decision_priority(
                    decision, strategy_spec.default_execution_priority
                ),
                triggered_by_event_id=getattr(event, "event_id", None),
                metadata={"decision_kind": kind},
            )
            verdict: RiskDecision = risk_gate.evaluate(envelope, ctx)
            files.append(
                files.risk,
                {
                    "correlation_id": str(envelope.correlation_id),
                    "decision_kind": kind,
                    "allowed": verdict.allowed,
                    "reasons": list(verdict.reasons),
                    "emitted_at": envelope.emitted_at.isoformat(),
                },
            )
            if verdict.allowed:
                stats.intents_dispatched += 1
                files.append(
                    files.decisions,
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
            else:
                stats.intents_rejected += 1
                for reason in verdict.reasons or ("unspecified",):
                    stats.risk_reject_reasons[reason] = (
                        stats.risk_reject_reasons.get(reason, 0) + 1
                    )


# ---------- helpers ----------


@dataclass
class WeatherMarketCatalog:
    """Tracks open weather contracts and the unique locations to poll."""

    _by_ticker: dict[str, KalshiTemperatureContract] = field(default_factory=dict)

    def update(self, contracts: Sequence[KalshiTemperatureContract]) -> None:
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
    ) -> tuple[KalshiTemperatureContract, ...]:
        return tuple(
            contract
            for contract in self._by_ticker.values()
            if contract.location.latitude == location.latitude
            and contract.location.longitude == location.longitude
        )


@dataclass
class _ContextProvider:
    strategy_id: StrategyId
    sleeve_id: SleeveId

    def context(self) -> InMemoryContext:
        return InMemoryContext(
            strategy_id_value=self.strategy_id,
            sleeve_id_value=self.sleeve_id,
            clock_now=datetime.now(UTC),
        )


def _kalshi_instrument(ticker: str) -> Any:
    from eventcontracts.domain.models import InstrumentId, Venue

    return InstrumentId(venue=Venue.KALSHI, market_id=ticker)


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
    print(
        f"[live-paper] elapsed={int(elapsed)}s "
        f"discoveries={stats.discoveries} markets={stats.last_discovered_markets} "
        f"raw_ws={stats.raw_ws_events} normalized={stats.normalized_events} "
        f"({rate:.1f}/s) signals={stats.external_signals_emitted} "
        f"forecasts={stats.forecast_snapshots} decisions={stats.decisions} "
        f"dispatched={stats.intents_dispatched} rejected={stats.intents_rejected} "
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
        },
        "stats": {
            "discoveries": stats.discoveries,
            "last_discovered_markets": stats.last_discovered_markets,
            "raw_ws_events": stats.raw_ws_events,
            "normalized_events": stats.normalized_events,
            "normalize_skipped": stats.normalize_skipped,
            "by_channel": dict(stats.by_channel),
            "external_signals_emitted": stats.external_signals_emitted,
            "forecast_snapshots": stats.forecast_snapshots,
            "decisions": stats.decisions,
            "decisions_by_kind": dict(stats.decisions_by_kind),
            "intents_dispatched": stats.intents_dispatched,
            "intents_rejected": stats.intents_rejected,
            "risk_reject_reasons": dict(stats.risk_reject_reasons),
            "last_event_at": stats.last_event_at.isoformat() if stats.last_event_at else None,
            "last_forecast_at": stats.last_forecast_at.isoformat()
            if stats.last_forecast_at
            else None,
        },
        "limits": [
            "no fill simulation; decisions recorded only",
            "no sequence-gap recovery on ws reconnect",
            "no state checkpoints",
            "allow-all risk gate (sleeve.risk limits not enforced yet)",
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
