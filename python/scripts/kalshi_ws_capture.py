"""Read-only Kalshi WebSocket JSONL capture for selected series/tickers.

This script never submits, cancels, or replaces orders. It only discovers
market tickers through Kalshi REST and subscribes to market-data WebSocket
channels, then writes raw envelopes to JSONL for later normalization/replay.

Use ``--no-network`` for a deterministic file-format self-test.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import queue as _queue
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "python" / "src"))

from eventcontracts.adapters.venues.kalshi.client import (  # noqa: E402
    KALSHI_REST_PROD,
    KALSHI_WS_PROD,
    KalshiAuth,
    KalshiPublicClient,
    KalshiWebSocketClient,
)
from eventcontracts.domain.models import Venue  # noqa: E402
from eventcontracts.env import load_default_env  # noqa: E402
from eventcontracts.research.ledger import to_jsonable  # noqa: E402
from eventcontracts.storage.interfaces import EventEnvelope  # noqa: E402

DEFAULT_CHANNELS = ("ticker", "trade", "orderbook_delta", "market_lifecycle_v2")
DEFAULT_SERIES = ("KXBTC15M", "KXHIGHNY", "KXHIGHCHI", "KXHIGHMIA", "KXATPMATCH")


def _split_csv(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in value.split(",") if part.strip())


def _utc_run_id() -> str:
    return datetime.now(UTC).strftime("run-%Y%m%dT%H%M%S%fZ")


def envelope_to_row(envelope: EventEnvelope) -> dict[str, Any]:
    """Lossless JSON row for a raw market-data envelope."""

    return {
        "venue": envelope.venue.value if envelope.venue is not None else None,
        "source": envelope.source,
        "channel": envelope.channel,
        "received_at": envelope.received_at.isoformat(),
        "exchange_ts": envelope.exchange_ts.isoformat() if envelope.exchange_ts is not None else None,
        "schema_version": envelope.schema_version,
        "metadata": envelope.metadata,
        "payload": envelope.payload,
    }


def _ticker_from_payload(payload: Mapping[str, Any]) -> str | None:
    msg = payload.get("msg")
    candidates: list[object] = [payload.get("ticker"), payload.get("market_ticker")]
    if isinstance(msg, Mapping):
        candidates.extend([msg.get("ticker"), msg.get("market_ticker")])
    for candidate in candidates:
        if isinstance(candidate, str) and candidate:
            return candidate
    return None


async def discover_tickers(
    rest: KalshiPublicClient,
    *,
    series_tickers: Sequence[str],
    explicit_tickers: Sequence[str],
    max_pages: int,
) -> tuple[str, ...]:
    found = set(explicit_tickers)
    for series in series_tickers:
        cursor: str | None = None
        for _ in range(max_pages):
            payload = await rest.get_markets_payload(limit=1000, cursor=cursor, series_ticker=series)
            for raw_market in payload.get("markets", []) or []:
                if not isinstance(raw_market, Mapping) or not _is_live_or_upcoming(raw_market):
                    continue
                ticker = raw_market.get("ticker") or raw_market.get("market_ticker")
                if isinstance(ticker, str) and ticker:
                    found.add(ticker)
            cursor_value = payload.get("cursor")
            cursor = str(cursor_value) if cursor_value else None
            if cursor is None:
                break
    return tuple(sorted(found))


def _is_live_or_upcoming(market: Mapping[str, Any]) -> bool:
    status = market.get("status")
    return isinstance(status, str) and status.lower() in {"active", "initialized", "open"}


async def _write_stream(
    *,
    args: argparse.Namespace,
    run_dir: Path,
    raw_path: Path,
    manifest_path: Path,
) -> int:
    auth = KalshiAuth.from_env()
    rest = KalshiPublicClient(base_url=KALSHI_REST_PROD, auth=auth)
    ws = KalshiWebSocketClient(ws_url=KALSHI_WS_PROD, auth=auth)
    started_at = datetime.now(UTC)
    deadline = started_at + timedelta(seconds=float(args.duration_sec))
    channels = _split_csv(args.channels)
    series_tickers = _split_csv(args.series_tickers)
    explicit_tickers = _split_csv(args.tickers)

    stats: dict[str, Any] = {
        "started_at": started_at.isoformat(),
        "finished_at": None,
        "duration_sec": args.duration_sec,
        "channels": channels,
        "series_tickers": series_tickers,
        "explicit_tickers": explicit_tickers,
        "sessions": 0,
        "rows": 0,
        "by_channel": {},
        "by_ticker": {},
        "last_discovered_count": 0,
        "last_error": None,
        "run_dir": str(run_dir),
        "raw_jsonl": str(raw_path),
    }

    # Decouple disk IO from the recv loop. Two failure modes drove this design:
    #
    #   1. Original code did `with raw_path.open("a"): write` per message inside the
    #      `async for`, so the next `websocket.recv()` could not run until that blocking
    #      per-message open/write/close completed -> ~37s `received_at` lag
    #      ([[ws-capture-37s-stale]]).
    #   2. The first fix moved writes to an asyncio task, but `file.write()` still runs
    #      ON the event loop. On the slow FAT32 USB capture target, dirty-page write-back
    #      throttling makes write() block -> the loop (and recv) stalls -> ~13s lag
    #      reappears (sub-second only held on a fast local SSD /tmp test).
    #
    # Fix: run disk IO on a DEDICATED OS THREAD (not an asyncio task), so a slow disk can
    # never block recv. The recv loop only does a non-blocking `put_nowait`; received_at
    # stays fresh regardless of disk speed (average data rate << even slow-USB throughput,
    # so the queue drains and memory stays bounded; bursts are absorbed).
    #
    # Also shard the raw file under the FAT32 4 GiB per-file cap. The USB target is
    # `msdos`/FAT32; a single raw.jsonl froze at ~4 GiB mid-run and silently deadlocked
    # the writer. Roll to raw-0001.jsonl, raw-0002.jsonl, ... before the cap.
    # The recv loop must do the MINIMUM per message — anything else on the event loop
    # (JSON serialization, stats bookkeeping, and especially the synchronous
    # `_write_manifest` to the slow USB) steadily backs up the websocket buffer under real
    # orderbook load and reintroduces multi-second lag (~9.5s observed with only the raw
    # write moved off-loop). So the recv loop ONLY enqueues the raw envelope; the writer
    # THREAD owns serialization, counting, sharding, flushing, and manifest writes.
    SHARD_MAX_BYTES = 3_500_000_000  # safety margin under FAT32's 4 GiB per-file cap
    write_q: _queue.Queue[Any] = _queue.Queue(maxsize=1_000_000)
    _SENTINEL = object()
    writer_state: dict[str, int] = {"shard": 0, "dropped": 0, "write_errors": 0}
    stats["dropped"] = 0
    stats["write_errors"] = 0

    def _shard_path(idx: int) -> Path:
        if idx == 0:
            return raw_path
        return raw_path.with_name(f"{raw_path.stem}-{idx:04d}{raw_path.suffix}")

    def _writer_thread() -> None:
        idx = 0
        handle = _shard_path(idx).open("a", encoding="utf-8")
        written = _shard_path(idx).stat().st_size
        last_flush = time.monotonic()
        last_manifest = time.monotonic()
        try:
            while True:
                envelope = write_q.get()
                if envelope is _SENTINEL:
                    handle.flush()
                    _write_manifest(manifest_path, stats)
                    return
                # Serialize + count here, off the event loop.
                line = json.dumps(to_jsonable(envelope_to_row(envelope)), sort_keys=True) + "\n"
                stats["rows"] += 1
                by_channel = stats["by_channel"]
                by_channel[envelope.channel] = by_channel.get(envelope.channel, 0) + 1
                ticker = _ticker_from_payload(envelope.payload)
                if ticker is not None:
                    by_ticker = stats["by_ticker"]
                    by_ticker[ticker] = by_ticker.get(ticker, 0) + 1
                nbytes = len(line.encode("utf-8"))
                if written + nbytes > SHARD_MAX_BYTES:
                    handle.flush()
                    handle.close()
                    idx += 1
                    handle = _shard_path(idx).open("a", encoding="utf-8")
                    written = 0
                    writer_state["shard"] = idx
                try:
                    handle.write(line)
                    written += nbytes
                except OSError as exc:
                    writer_state["write_errors"] += 1
                    stats["write_errors"] = writer_state["write_errors"]
                    stats["last_error"] = f"write_error: {type(exc).__name__}: {exc}"
                    continue
                now = time.monotonic()
                if now - last_flush >= 1.0:
                    handle.flush()
                    last_flush = now
                if now - last_manifest >= 5.0:
                    _write_manifest(manifest_path, stats)
                    last_manifest = now
        finally:
            with contextlib.suppress(Exception):
                handle.flush()
                handle.close()

    writer_thread = threading.Thread(target=_writer_thread, name="ws-capture-writer", daemon=True)
    writer_thread.start()

    try:
        while datetime.now(UTC) < deadline:
            remaining = max((deadline - datetime.now(UTC)).total_seconds(), 0.0)
            tickers = await discover_tickers(
                rest,
                series_tickers=series_tickers,
                explicit_tickers=explicit_tickers,
                max_pages=args.discover_max_pages,
            )
            stats["last_discovered_count"] = len(tickers)
            _write_manifest(manifest_path, stats)
            if not tickers:
                await asyncio.sleep(min(args.idle_poll_sec, remaining))
                continue

            stats["sessions"] += 1
            session_end = datetime.now(UTC) + timedelta(seconds=min(args.rediscover_interval_sec, remaining))
            try:
                async for envelope in ws.stream(channels=channels, market_tickers=tickers):
                    # Minimal work on the event loop: just hand the raw envelope to the
                    # writer thread. Non-blocking so a slow disk can never stall recv.
                    try:
                        write_q.put_nowait(envelope)
                    except _queue.Full:
                        # Disk fell catastrophically behind; drop rather than stall recv
                        # (keeps received_at honest). Counted so it surfaces in the manifest.
                        writer_state["dropped"] += 1
                        stats["dropped"] = writer_state["dropped"]
                    if datetime.now(UTC) >= session_end:
                        break
            except Exception as exc:  # noqa: BLE001 - long-running capture records and retries
                stats["last_error"] = f"{type(exc).__name__}: {exc}"
                _write_manifest(manifest_path, stats)
                await asyncio.sleep(min(10.0, max((deadline - datetime.now(UTC)).total_seconds(), 0.0)))
    finally:
        write_q.put(_SENTINEL)
        writer_thread.join(timeout=30)
        stats["dropped"] = writer_state["dropped"]
        stats["write_errors"] = writer_state["write_errors"]
        stats["shards"] = writer_state["shard"] + 1

    stats["finished_at"] = datetime.now(UTC).isoformat()
    _write_manifest(manifest_path, stats)
    print(json.dumps(to_jsonable(stats), sort_keys=True))
    return 0


def _write_manifest(path: Path, stats: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(to_jsonable(stats), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _run_no_network(args: argparse.Namespace) -> int:
    run_dir = args.out / _utc_run_id()
    run_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC)
    envelope = EventEnvelope(
        venue=Venue.KALSHI,
        source="kalshi-ws",
        channel="ticker",
        received_at=now,
        exchange_ts=now,
        payload={
            "type": "ticker",
            "msg": {
                "ticker": "KXBTC15M-SELFTEST",
                "yes_bid": 49,
                "yes_ask": 51,
            },
        },
        schema_version="kalshi-ws-v1",
        metadata={"fixture": True},
    )
    raw_path = run_dir / "raw.jsonl"
    manifest_path = run_dir / "manifest.json"
    raw_path.write_text(json.dumps(to_jsonable(envelope_to_row(envelope)), sort_keys=True) + "\n", encoding="utf-8")
    _write_manifest(
        manifest_path,
        {
            "fixture": True,
            "rows": 1,
            "run_dir": str(run_dir),
            "raw_jsonl": str(raw_path),
            "series_tickers": _split_csv(args.series_tickers),
        },
    )
    print(str(run_dir))
    return 0


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-network", action="store_true", help="write a deterministic fixture row and exit")
    parser.add_argument("--out", type=Path, default=ROOT / "data" / "ws-capture" / "top3")
    parser.add_argument("--series-tickers", default=",".join(DEFAULT_SERIES))
    parser.add_argument("--tickers", default="", help="comma-separated exact market tickers to include")
    parser.add_argument("--channels", default=",".join(DEFAULT_CHANNELS))
    parser.add_argument("--duration-sec", type=float, default=43_200.0)
    parser.add_argument("--rediscover-interval-sec", type=float, default=300.0)
    parser.add_argument("--idle-poll-sec", type=float, default=60.0)
    parser.add_argument("--discover-max-pages", type=int, default=5)
    parser.add_argument("--manifest-every", type=int, default=250)
    return parser.parse_args(list(argv))


def main(argv: Sequence[str] | None = None) -> int:
    load_default_env(start=ROOT)
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    if args.no_network:
        return _run_no_network(args)
    run_dir = args.out / _utc_run_id()
    run_dir.mkdir(parents=True, exist_ok=True)
    return asyncio.run(
        _write_stream(
            args=args,
            run_dir=run_dir,
            raw_path=run_dir / "raw.jsonl",
            manifest_path=run_dir / "manifest.json",
        )
    )


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        raise SystemExit(main())
