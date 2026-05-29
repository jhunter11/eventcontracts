"""Capture raw venue data into the Parquet event lake."""

from __future__ import annotations

import argparse
import asyncio
import fnmatch
import json
import os
import subprocess
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from pprint import pprint
from typing import Any

from eventcontracts.adapters.venues.kalshi.client import (
    KalshiPublicClient,
    KalshiWebSocketClient,
    rest_envelope,
)
from eventcontracts.cli.normalize import normalize_event_lake
from eventcontracts.config import load_strategy_spec
from eventcontracts.ingestion.subscriptions import SubscriptionPlanner
from eventcontracts.storage import ParquetEventStore

DEFAULT_WS_CHANNELS = ("ticker", "trade", "orderbook_delta", "market_lifecycle_v2")


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("capture", help="Capture raw venue data to Parquet.")
    parser.add_argument("--venue", choices=("kalshi",), required=True)
    parser.add_argument("--patterns", default="", help="Comma-separated market ticker glob patterns.")
    parser.add_argument("--strategy", action="append", type=Path, default=[], help="Strategy TOML to include.")
    parser.add_argument("--transport", choices=("ws", "rest"), default="ws")
    parser.add_argument("--out", type=Path, required=True, help="Event lake root.")
    parser.add_argument("--status", default="open", help="Market status filter for REST discovery.")
    parser.add_argument("--channels", default="", help="Comma-separated Kalshi WS channels.")
    parser.add_argument("--max-messages", type=int, default=None, help="Stop after N WS messages.")
    parser.add_argument("--max-polls", type=int, default=1, help="Stop REST capture after N polling rounds.")
    parser.add_argument("--poll-interval-seconds", type=float, default=0.0, help="Delay between REST polling rounds.")
    parser.add_argument("--trades-limit", type=int, default=100, help="Recent trades fetched per REST market poll.")
    parser.add_argument("--normalize", action="store_true", help="Normalize captured raw envelopes after capture.")
    parser.add_argument(
        "--fixture-jsonl",
        type=Path,
        default=None,
        help="Replay recorded Kalshi WS JSONL instead of connecting to the network.",
    )
    parser.set_defaults(handler=_handle_capture)


def _handle_capture(args: argparse.Namespace) -> int:
    if args.venue != "kalshi":
        raise ValueError(f"unsupported venue: {args.venue}")
    started_at = datetime.now(UTC)
    if args.fixture_jsonl is not None:
        count = capture_kalshi_fixture(args.fixture_jsonl, args.out)
        normalization = (
            normalize_event_lake(args.out, source="kalshi-ws", normalizer_name="kalshi") if args.normalize else None
        )
        manifest = _write_capture_manifest(
            args.out,
            {
                "venue": "kalshi",
                "transport": "fixture",
                "source": "kalshi-ws",
                "fixture_jsonl": str(args.fixture_jsonl),
                "captured": count,
                "patterns": (),
                "channels": (),
                "strategy_configs": (),
                "normalization": normalization,
            },
            started_at=started_at,
        )
        summary: dict[str, Any] = {
            "captured": count,
            "out": str(args.out),
            "source": "kalshi-ws",
            "manifest": str(manifest),
        }
        if normalization is not None:
            summary["normalization"] = normalization
        pprint(summary)
        return 0

    patterns = _patterns_from_args(args.patterns)
    strategy_paths = tuple(Path(path) for path in args.strategy)
    if strategy_paths:
        plan = SubscriptionPlanner().plan(tuple(load_strategy_spec(path) for path in strategy_paths))
        patterns = tuple(sorted(set(patterns).union(plan.instrument_patterns)))
        if not args.channels:
            channels = _channels_for_event_kinds(plan.event_kinds)
        else:
            channels = _channels_from_args(args.channels)
    else:
        channels = _channels_from_args(args.channels) if args.channels else DEFAULT_WS_CHANNELS
    if not patterns:
        raise ValueError("capture requires --patterns or at least one --strategy")

    count = asyncio.run(
        _capture_kalshi_live(
            patterns=patterns,
            channels=channels,
            transport=args.transport,
            out=args.out,
            status=args.status,
            max_messages=args.max_messages,
            max_polls=args.max_polls,
            poll_interval_seconds=args.poll_interval_seconds,
            trades_limit=args.trades_limit,
        )
    )
    source = f"kalshi-{args.transport}"
    normalization = normalize_event_lake(args.out, source=source, normalizer_name="kalshi") if args.normalize else None
    manifest = _write_capture_manifest(
        args.out,
        {
            "venue": "kalshi",
            "transport": args.transport,
            "source": source,
            "patterns": tuple(patterns),
            "channels": tuple(channels),
            "status": args.status,
            "max_messages": args.max_messages,
            "max_polls": args.max_polls,
            "poll_interval_seconds": args.poll_interval_seconds,
            "trades_limit": args.trades_limit,
            "captured": count,
            "strategy_configs": tuple(str(path) for path in strategy_paths),
            "normalization": normalization,
        },
        started_at=started_at,
    )
    summary = {"captured": count, "out": str(args.out), "source": source, "manifest": str(manifest)}
    if normalization is not None:
        summary["normalization"] = normalization
    pprint(summary)
    return 0


async def _capture_kalshi_live(
    *,
    patterns: Sequence[str],
    channels: Sequence[str],
    transport: str,
    out: Path,
    status: str,
    max_messages: int | None,
    max_polls: int,
    poll_interval_seconds: float,
    trades_limit: int,
) -> int:
    rest = KalshiPublicClient.from_env()
    tickers = await resolve_kalshi_tickers(rest, patterns=patterns, status=status)
    if not tickers:
        raise ValueError(f"no Kalshi markets matched patterns: {patterns}")
    store = ParquetEventStore(out)
    if transport == "rest":
        count = await capture_kalshi_rest_polls(
            rest,
            store=store,
            tickers=tickers,
            max_polls=max_polls,
            poll_interval_seconds=poll_interval_seconds,
            trades_limit=trades_limit,
        )
    else:
        ws = KalshiWebSocketClient.from_env()
        count = 0
        async for envelope in ws.stream(channels=channels, market_tickers=tickers, max_messages=max_messages):
            store.append(envelope)
            count += 1
    store.flush()
    return count


async def resolve_kalshi_tickers(
    client: KalshiPublicClient,
    *,
    patterns: Sequence[str],
    status: str | None = "open",
) -> tuple[str, ...]:
    exact = tuple(pattern for pattern in patterns if not _has_glob(pattern))
    glob_patterns = tuple(pattern for pattern in patterns if _has_glob(pattern))
    payloads = await client.list_market_payloads(status=status)
    matched: set[str] = set(exact)
    for payload in payloads:
        ticker = payload.get("ticker") or payload.get("market_ticker")
        if not isinstance(ticker, str):
            continue
        if any(fnmatch.fnmatchcase(ticker, pattern) for pattern in glob_patterns):
            matched.add(ticker)
    return tuple(sorted(matched))


async def capture_kalshi_rest_snapshot(
    client: KalshiPublicClient,
    *,
    store: ParquetEventStore,
    tickers: Sequence[str],
) -> int:
    return await capture_kalshi_rest_polls(
        client,
        store=store,
        tickers=tickers,
        max_polls=1,
        poll_interval_seconds=0.0,
        trades_limit=100,
    )


async def capture_kalshi_rest_polls(
    client: KalshiPublicClient,
    *,
    store: ParquetEventStore,
    tickers: Sequence[str],
    max_polls: int,
    poll_interval_seconds: float,
    trades_limit: int,
    monotonic: Callable[[], float] | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> int:
    """Capture repeated REST book snapshots and deduped recent trades.

    This is the capture mode for predictive, non-latency-sensitive research:
    every poll records the latest book, while trades are only appended once so
    repeated `/markets/trades` calls do not inflate passive fill volume.
    """

    if max_polls <= 0:
        raise ValueError("max_polls must be positive")
    if poll_interval_seconds < 0:
        raise ValueError("poll_interval_seconds must be non-negative")
    if trades_limit <= 0:
        raise ValueError("trades_limit must be positive")

    count = 0
    seen_trades: set[str] = set()
    now_monotonic = monotonic or asyncio.get_running_loop().time
    next_deadline = now_monotonic()
    for poll_index in range(max_polls):
        for ticker in tickers:
            orderbook = await client.get_order_book_payload(ticker)
            store.append(
                rest_envelope(
                    channel="book",
                    payload={"market_ticker": ticker, **orderbook},
                    metadata={"poll_index": poll_index},
                )
            )
            count += 1
            trades = await client.get_trades_payload(ticker=ticker, limit=trades_limit)
            for item in trades.get("trades", []):
                if not isinstance(item, dict):
                    continue
                trade_key = _rest_trade_key(ticker, item)
                if trade_key in seen_trades:
                    continue
                seen_trades.add(trade_key)
                store.append(
                    rest_envelope(
                        channel="trade",
                        payload={"market_ticker": ticker, **item},
                        metadata={"poll_index": poll_index, "trade_key": trade_key},
                    )
                )
                count += 1
        if poll_index + 1 < max_polls and poll_interval_seconds > 0:
            next_deadline += poll_interval_seconds
            await sleep(max(0.0, next_deadline - now_monotonic()))
    store.flush()
    return count


def capture_kalshi_fixture(path: Path, out: Path) -> int:
    store = ParquetEventStore(out)
    client = KalshiWebSocketClient()
    count = 0
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        payload = _json_object(line)
        envelope = client.message_to_envelope(payload)
        if envelope is not None:
            store.append(envelope)
            count += 1
    store.flush()
    return count


def _json_object(line: str) -> dict[str, Any]:
    decoded = json.loads(line)
    if not isinstance(decoded, dict):
        raise ValueError("fixture JSONL entries must be objects")
    return decoded


def _rest_trade_key(ticker: str, trade: dict[str, Any]) -> str:
    explicit = trade.get("trade_id")
    if isinstance(explicit, str) and explicit:
        return f"{ticker}:trade_id:{explicit}"
    fields = (
        ticker,
        str(trade.get("ts_ms") or trade.get("ts") or ""),
        str(trade.get("yes_price_dollars") or trade.get("yes_price") or trade.get("price") or ""),
        str(trade.get("no_price_dollars") or trade.get("no_price") or ""),
        str(trade.get("count_fp") or trade.get("count") or trade.get("quantity") or ""),
        str(trade.get("taker_side") or trade.get("side") or ""),
    )
    return ":".join(fields)


def _patterns_from_args(raw: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _channels_from_args(raw: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _channels_for_event_kinds(event_kinds: Sequence[str]) -> tuple[str, ...]:
    channels: set[str] = set()
    for kind in event_kinds:
        if kind == "quote":
            channels.add("ticker")
        elif kind == "trade":
            channels.add("trade")
        elif kind == "book":
            channels.add("orderbook_delta")
        elif kind in {"lifecycle", "settlement"}:
            channels.add("market_lifecycle_v2")
    return tuple(sorted(channels)) if channels else DEFAULT_WS_CHANNELS


def _has_glob(pattern: str) -> bool:
    return any(char in pattern for char in "*?[]")


def _write_capture_manifest(out: Path, payload: dict[str, Any], *, started_at: datetime) -> Path:
    ended_at = datetime.now(UTC)
    manifest = {
        "started_at": started_at.isoformat(),
        "ended_at": ended_at.isoformat(),
        "env": os.environ.get("EVENTCONTRACTS_ENV", "unknown"),
        "code_version": os.environ.get("EVENTCONTRACTS_CODE_VERSION") or _git_head_sha() or "unknown",
        "output_root": str(out),
        **payload,
    }
    manifest_dir = out / "manifests"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    path = manifest_dir / f"capture-{ended_at.strftime('%Y%m%dT%H%M%S%fZ')}.json"
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
