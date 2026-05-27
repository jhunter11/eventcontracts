"""Long-running Kalshi weather-market capture for overnight data collection.

Wraps the existing `capture` machinery with three additions needed for an
unattended overnight run:

1. **Periodic market re-discovery.** Weather markets open through the morning;
   the runner re-discovers KXHIGH*/KXTEMP*/KXWX* markets every N seconds and
   reconnects with the updated subscription set when it changes.
2. **Graceful shutdown.** SIGINT/SIGTERM flushes the Parquet buffer and writes
   a final manifest before exit.
3. **Bounded duration + periodic stdout snapshots** so an unattended run can't
   leak indefinitely and so an operator can tail logs and confirm flow.

The output layout matches `capture`: `<out>/raw/venue=kalshi/source=kalshi-ws/...`
plus a `manifests/` subdirectory. Re-discovery means multiple sessions per
night accumulate into one event lake — backtests just point at `<out>` and
replay everything in event-time order.
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

from eventcontracts.adapters.venues.kalshi.client import (
    KalshiPublicClient,
    KalshiWebSocketClient,
)
from eventcontracts.storage import ParquetEventStore

DEFAULT_PATTERNS: tuple[str, ...] = (
    "KXHIGH*",
    "KXTEMP*",
    "KXWX*",
    "KXLOW*",
)
DEFAULT_CHANNELS: tuple[str, ...] = (
    "ticker",
    "trade",
    "orderbook_delta",
    "market_lifecycle_v2",
)


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "capture-weather",
        help="Long-running Kalshi weather-market capture with periodic re-discovery.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        required=True,
        help="Event-lake root. Re-used across sessions tonight so backtests see one lake.",
    )
    parser.add_argument(
        "--patterns",
        default=",".join(DEFAULT_PATTERNS),
        help=f"Comma-separated market-ticker glob patterns. Default: {','.join(DEFAULT_PATTERNS)}",
    )
    parser.add_argument(
        "--channels",
        default=",".join(DEFAULT_CHANNELS),
        help=f"Kalshi WS channels. Default: {','.join(DEFAULT_CHANNELS)}",
    )
    parser.add_argument(
        "--rediscover-interval-seconds",
        type=int,
        default=600,
        help="Re-list open markets every N seconds. Default 600 (10 min).",
    )
    parser.add_argument(
        "--max-duration-seconds",
        type=int,
        default=43200,
        help="Hard cap on total runtime. Default 43200 (12 hours).",
    )
    parser.add_argument(
        "--snapshot-interval-seconds",
        type=int,
        default=60,
        help="Print a stdout progress snapshot every N seconds. Default 60.",
    )
    parser.add_argument(
        "--idle-poll-seconds",
        type=int,
        default=60,
        help="If discovery returns zero markets, wait this long before re-polling. Default 60.",
    )
    parser.add_argument(
        "--discover-timeout-seconds",
        type=int,
        default=20,
        help="Per-discovery wall-clock cap. Default 20.",
    )
    parser.add_argument(
        "--discover-max-pages",
        type=int,
        default=5,
        help="Max REST pages to scan per discovery (page size 1000). Default 5 = 5000 markets.",
    )
    parser.set_defaults(handler=_handle)


# ---------- runtime state ----------


@dataclass
class CaptureStats:
    started_at: datetime
    sessions: int = 0
    total_envelopes: int = 0
    by_channel: dict[str, int] = field(default_factory=dict)
    by_ticker: dict[str, int] = field(default_factory=dict)
    last_envelope_at: datetime | None = None
    last_discovery_at: datetime | None = None
    last_discovered_count: int = 0


# ---------- handler ----------


def _handle(args: argparse.Namespace) -> int:
    patterns = tuple(p.strip() for p in args.patterns.split(",") if p.strip())
    channels = tuple(c.strip() for c in args.channels.split(",") if c.strip())
    if not patterns:
        print("error: --patterns must include at least one entry", file=sys.stderr)
        return 2
    args.out.mkdir(parents=True, exist_ok=True)
    store = ParquetEventStore(args.out)
    stats = CaptureStats(started_at=datetime.now(UTC))

    shutdown = asyncio.Event()

    def _signal_handler(signum: int, _frame: object) -> None:
        signame = signal.Signals(signum).name
        print(
            f"[capture-weather] received {signame}, finishing current session...",
            file=sys.stderr,
            flush=True,
        )
        try:
            asyncio.get_running_loop().call_soon_threadsafe(shutdown.set)
        except RuntimeError:
            shutdown.set()

    # Windows doesn't support add_signal_handler for SIGINT — fall back to
    # signal.signal which still works for our needs.
    signal.signal(signal.SIGINT, _signal_handler)
    # SIGTERM is not exposed on Windows; signal.signal raises AttributeError
    # or ValueError there.
    with contextlib.suppress(AttributeError, ValueError):
        signal.signal(signal.SIGTERM, _signal_handler)

    print(
        f"[capture-weather] starting; out={args.out} patterns={patterns} "
        f"channels={channels} rediscover={args.rediscover_interval_seconds}s "
        f"max={args.max_duration_seconds}s",
        file=sys.stderr,
        flush=True,
    )

    try:
        asyncio.run(
            _run_loop(
                store=store,
                stats=stats,
                patterns=patterns,
                channels=channels,
                rediscover_interval=args.rediscover_interval_seconds,
                max_duration=args.max_duration_seconds,
                snapshot_interval=args.snapshot_interval_seconds,
                idle_poll=args.idle_poll_seconds,
                discover_timeout=args.discover_timeout_seconds,
                discover_max_pages=args.discover_max_pages,
                shutdown=shutdown,
            )
        )
    finally:
        store.flush()
        manifest = _write_manifest(args.out, stats, patterns, channels, args)
        print(
            f"[capture-weather] done; envelopes={stats.total_envelopes} "
            f"sessions={stats.sessions} manifest={manifest}",
            file=sys.stderr,
            flush=True,
        )
    return 0


# ---------- main loop ----------


async def _run_loop(
    *,
    store: ParquetEventStore,
    stats: CaptureStats,
    patterns: Sequence[str],
    channels: Sequence[str],
    rediscover_interval: int,
    max_duration: int,
    snapshot_interval: int,
    idle_poll: int,
    discover_timeout: int,
    discover_max_pages: int,
    shutdown: asyncio.Event,
) -> None:
    deadline = stats.started_at + timedelta(seconds=max_duration)
    rest = KalshiPublicClient.from_env()

    snapshot_task = asyncio.create_task(
        _snapshot_loop(stats, interval=snapshot_interval, shutdown=shutdown)
    )

    try:
        while not shutdown.is_set() and datetime.now(UTC) < deadline:
            try:
                tickers = await asyncio.wait_for(
                    _discover(rest, patterns, max_pages=discover_max_pages),
                    timeout=discover_timeout,
                )
            except TimeoutError:
                print(
                    f"[capture-weather] discovery exceeded {discover_timeout}s; "
                    f"treating as empty and re-polling",
                    file=sys.stderr,
                    flush=True,
                )
                tickers = tuple()
            stats.last_discovery_at = datetime.now(UTC)
            stats.last_discovered_count = len(tickers)

            if not tickers:
                print(
                    f"[capture-weather] no markets match {list(patterns)} right now; "
                    f"sleeping {idle_poll}s before re-poll",
                    file=sys.stderr,
                    flush=True,
                )
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(shutdown.wait(), timeout=idle_poll)
                continue

            stats.sessions += 1
            print(
                f"[capture-weather] session {stats.sessions} starting with "
                f"{len(tickers)} markets",
                file=sys.stderr,
                flush=True,
            )
            await _stream_session(
                store=store,
                stats=stats,
                tickers=tickers,
                channels=channels,
                session_duration=rediscover_interval,
                shutdown=shutdown,
                deadline=deadline,
            )
            store.flush()
    finally:
        snapshot_task.cancel()
        with contextlib.suppress(BaseException):
            await snapshot_task


async def _discover(
    rest: KalshiPublicClient, patterns: Sequence[str], *, max_pages: int
) -> tuple[str, ...]:
    """Discover Kalshi markets matching weather patterns.

    Paginates manually instead of using `list_market_payloads` so we can cap
    page count and surface progress. Prod has thousands of open markets;
    discovery would otherwise dominate the loop cadence.
    """
    exact = {p for p in patterns if not any(c in p for c in "*?[]")}
    globs = [p for p in patterns if any(c in p for c in "*?[]")]
    matched: set[str] = set(exact)
    cursor: str | None = None
    for page_index in range(max_pages):
        payload = await rest.get_markets_payload(
            limit=1000, cursor=cursor, status="open"
        )
        markets = payload.get("markets", []) or []
        for market in markets:
            if not isinstance(market, dict):
                continue
            ticker = market.get("ticker") or market.get("market_ticker")
            if not isinstance(ticker, str):
                continue
            if any(fnmatch.fnmatchcase(ticker, glob) for glob in globs):
                matched.add(ticker)
        cursor_value = payload.get("cursor")
        if not cursor_value:
            break
        cursor = str(cursor_value)
        _ = page_index  # silence ruff if unused
    return tuple(sorted(matched))


async def _stream_session(
    *,
    store: ParquetEventStore,
    stats: CaptureStats,
    tickers: Sequence[str],
    channels: Sequence[str],
    session_duration: int,
    shutdown: asyncio.Event,
    deadline: datetime,
) -> None:
    ws = KalshiWebSocketClient.from_env()
    session_end = datetime.now(UTC) + timedelta(seconds=session_duration)
    stream_task = asyncio.create_task(
        _stream_into_store(ws, channels, tickers, store, stats)
    )
    # Wait for whichever fires first: session expiry, hard deadline, or shutdown.
    stop_waiter = asyncio.create_task(_wait_until_stop(shutdown, session_end, deadline))
    done, pending = await asyncio.wait(
        {stream_task, stop_waiter}, return_when=asyncio.FIRST_COMPLETED
    )
    for task in pending:
        task.cancel()
    for task in pending:
        with contextlib.suppress(BaseException):
            await task
    # Surface any non-cancellation exceptions from the stream itself.
    for task in done:
        if task is stream_task:
            exc = task.exception()
            if exc is not None and not isinstance(exc, asyncio.CancelledError):
                print(
                    f"[capture-weather] stream error: {exc!r}",
                    file=sys.stderr,
                    flush=True,
                )


async def _stream_into_store(
    ws: KalshiWebSocketClient,
    channels: Sequence[str],
    tickers: Sequence[str],
    store: ParquetEventStore,
    stats: CaptureStats,
) -> None:
    async for envelope in ws.stream(channels=channels, market_tickers=tickers):
        store.append(envelope)
        stats.total_envelopes += 1
        stats.last_envelope_at = datetime.now(UTC)
        channel = envelope.channel
        stats.by_channel[channel] = stats.by_channel.get(channel, 0) + 1
        ticker = _ticker_from_envelope(envelope)
        if ticker is not None:
            stats.by_ticker[ticker] = stats.by_ticker.get(ticker, 0) + 1


async def _wait_until_stop(
    shutdown: asyncio.Event, session_end: datetime, deadline: datetime
) -> None:
    while not shutdown.is_set():
        now = datetime.now(UTC)
        next_stop = min(session_end, deadline)
        remaining = (next_stop - now).total_seconds()
        if remaining <= 0:
            return
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=min(remaining, 5.0))
            return
        except TimeoutError:
            continue


# ---------- progress + manifest ----------


async def _snapshot_loop(
    stats: CaptureStats, *, interval: int, shutdown: asyncio.Event
) -> None:
    while not shutdown.is_set():
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=interval)
            return
        except TimeoutError:
            _print_snapshot(stats)


def _print_snapshot(stats: CaptureStats) -> None:
    now = datetime.now(UTC)
    elapsed = (now - stats.started_at).total_seconds()
    rate = stats.total_envelopes / elapsed if elapsed > 0 else 0.0
    top = sorted(stats.by_ticker.items(), key=lambda kv: kv[1], reverse=True)[:5]
    top_str = ", ".join(f"{t}={c}" for t, c in top) if top else "-"
    last = stats.last_envelope_at.isoformat() if stats.last_envelope_at else "-"
    last_disc = (
        stats.last_discovery_at.isoformat() if stats.last_discovery_at else "-"
    )
    print(
        f"[capture-weather] elapsed={int(elapsed)}s sessions={stats.sessions} "
        f"envelopes={stats.total_envelopes} ({rate:.1f}/s) "
        f"discovered={stats.last_discovered_count} last_disc={last_disc} "
        f"last_env={last} top={top_str}",
        file=sys.stderr,
        flush=True,
    )


def _ticker_from_envelope(envelope: object) -> str | None:
    # EventEnvelope.payload is a dict-like with the original WS msg.
    payload = getattr(envelope, "payload", None)
    if not isinstance(payload, dict):
        return None
    msg = payload.get("msg")
    if isinstance(msg, dict):
        for key in ("market_ticker", "ticker"):
            value = msg.get(key)
            if isinstance(value, str):
                return value
    return None


def _write_manifest(
    out: Path,
    stats: CaptureStats,
    patterns: Sequence[str],
    channels: Sequence[str],
    args: argparse.Namespace,
) -> Path:
    ended_at = datetime.now(UTC)
    manifest = {
        "kind": "capture-weather",
        "started_at": stats.started_at.isoformat(),
        "ended_at": ended_at.isoformat(),
        "elapsed_seconds": int((ended_at - stats.started_at).total_seconds()),
        "env": os.environ.get("EVENTCONTRACTS_ENV", "unknown"),
        "code_version": os.environ.get("EVENTCONTRACTS_CODE_VERSION")
        or _git_head_sha()
        or "unknown",
        "kalshi_env": os.environ.get("KALSHI_ENV", "unknown"),
        "output_root": str(out),
        "patterns": list(patterns),
        "channels": list(channels),
        "rediscover_interval_seconds": args.rediscover_interval_seconds,
        "max_duration_seconds": args.max_duration_seconds,
        "snapshot_interval_seconds": args.snapshot_interval_seconds,
        "idle_poll_seconds": args.idle_poll_seconds,
        "sessions": stats.sessions,
        "total_envelopes": stats.total_envelopes,
        "by_channel": dict(stats.by_channel),
        "by_ticker": dict(stats.by_ticker),
        "last_envelope_at": stats.last_envelope_at.isoformat()
        if stats.last_envelope_at
        else None,
        "last_discovered_count": stats.last_discovered_count,
    }
    manifest_dir = out / "manifests"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    path = (
        manifest_dir
        / f"capture-weather-{ended_at.strftime('%Y%m%dT%H%M%S%fZ')}.json"
    )
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return path


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
