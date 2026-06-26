"""Read-only BTC constituent-lead recorder for the KXBTC15M thesis.

No credentials, no orders, no authenticated writes. Live mode subscribes to
public Coinbase and Kraken WebSocket ticker feeds and logs a weighted synthetic
BTC index. If an official CF Benchmarks RTI print is not supplied, lead is
recorded as unavailable rather than fabricated.

Use ``--no-network`` for a deterministic self-test that writes a fixture ledger
row and exercises the latency gate end-to-end.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

stdout_reconfigure = getattr(sys.stdout, "reconfigure", None)
if callable(stdout_reconfigure):
    stdout_reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "python" / "src"))

from eventcontracts.research.btc_lead import (  # noqa: E402
    CryptoTick,
    RollingRealizedVol,
    SyntheticIndexState,
    evaluate_btc15m_timing_candidate,
    measure_reference_lead,
    parse_cfb_rti_stats,
    parse_coinbase_ticker,
    parse_kraken_ticker,
)
from eventcontracts.research.ledger import append_jsonl, stable_hash, to_jsonable  # noqa: E402

DEFAULT_LEDGER = ROOT / "live-test" / "btc_clead_ledger.jsonl"
COINBASE_WS = "wss://ws-feed.exchange.coinbase.com"
KRAKEN_WS = "wss://ws.kraken.com"
CFB_WS = "wss://www.cfbenchmarks.com/ws/v4"


QueueItem = tuple[str, CryptoTick]


async def _coinbase_reader(queue: asyncio.Queue[QueueItem], stop_at: datetime) -> None:
    # RESILIENCE-TODO: no reconnect loop — a single WS disconnect ends this reader task.
    # The outer _record_live will keep running but silently lose Coinbase ticks until restart.
    # Fix: wrap the websockets.connect call in a while-loop with exponential backoff.
    import websockets

    async with websockets.connect(COINBASE_WS, ping_interval=20, ping_timeout=20) as ws:
        await ws.send(json.dumps({"type": "subscribe", "product_ids": ["BTC-USD"], "channels": ["ticker"]}))
        async for raw in ws:
            if datetime.now(UTC) >= stop_at:
                break
            payload = json.loads(raw)
            if isinstance(payload, dict) and payload.get("type") == "ticker" and payload.get("price") is not None:
                await queue.put(("component", parse_coinbase_ticker(payload, received_at=datetime.now(UTC))))


async def _kraken_reader(queue: asyncio.Queue[QueueItem], stop_at: datetime) -> None:
    # RESILIENCE-TODO: same as _coinbase_reader — no reconnect loop on WS disconnect.
    import websockets

    async with websockets.connect(KRAKEN_WS, ping_interval=20, ping_timeout=20) as ws:
        await ws.send(json.dumps({"event": "subscribe", "pair": ["XBT/USD"], "subscription": {"name": "ticker"}}))
        async for raw in ws:
            if datetime.now(UTC) >= stop_at:
                break
            payload = json.loads(raw)
            if not isinstance(payload, list) or len(payload) < 4 or payload[-2] != "ticker":
                continue
            ticker_payload = {"result": {"XXBTZUSD": payload[1]}}
            await queue.put(("component", parse_kraken_ticker(ticker_payload, received_at=datetime.now(UTC))))


async def _cfb_rti_reader(
    queue: asyncio.Queue[QueueItem],
    stop_at: datetime,
    *,
    url: str,
    username: str,
    password: str,
    stream_id: str,
) -> None:
    import websockets

    credentials = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
    async with websockets.connect(
        url,
        additional_headers={"Authorization": f"Basic {credentials}"},
        ping_interval=20,
        ping_timeout=20,
    ) as ws:
        await ws.send(json.dumps({"type": "subscribe", "id": stream_id, "stream": "rti_stats"}))
        async for raw in ws:
            if datetime.now(UTC) >= stop_at:
                break
            payload = json.loads(raw)
            if not isinstance(payload, dict) or payload.get("type") != "rti_stats":
                continue
            if str(payload.get("id") or "") != stream_id:
                continue
            await queue.put(("reference", parse_cfb_rti_stats(payload, received_at=datetime.now(UTC))))


async def _record_live(args: argparse.Namespace) -> int:
    queue: asyncio.Queue[QueueItem] = asyncio.Queue(maxsize=10_000)
    state = SyntheticIndexState({"coinbase": args.coinbase_weight, "kraken": args.kraken_weight})
    vol = RollingRealizedVol(max_points=args.vol_points)
    latest_reference: CryptoTick | None = None
    latest_snapshot: Any | None = None
    stop_at = datetime.now(UTC) + timedelta(seconds=args.duration_sec)
    tasks = [
        asyncio.create_task(_coinbase_reader(queue, stop_at)),
        asyncio.create_task(_kraken_reader(queue, stop_at)),
    ]
    cfb_configured = (
        not args.disable_cfb_rti
        and args.cfb_ws_username is not None
        and args.cfb_ws_password is not None
    )
    if cfb_configured:
        tasks.append(
            asyncio.create_task(
                _cfb_rti_reader(
                    queue,
                    stop_at,
                    url=args.cfb_ws_url,
                    username=args.cfb_ws_username,
                    password=args.cfb_ws_password,
                    stream_id=args.cfb_stream_id,
                )
            )
        )
    written = 0
    try:
        while datetime.now(UTC) < stop_at:
            try:
                kind, tick = await asyncio.wait_for(queue.get(), timeout=1.0)
            except TimeoutError:
                continue
            if kind == "reference":
                latest_reference = tick
                if latest_snapshot is not None:
                    lead = measure_reference_lead(latest_snapshot, tick)
                    append_jsonl(
                        args.ledger,
                        {
                            "row_type": "official_rti_lead",
                            "row_id": stable_hash({"snapshot": latest_snapshot, "reference": tick}),
                            "snapshot": latest_snapshot,
                            "reference": tick,
                            "lead": lead,
                            "lead_label": "official_rti",
                            "lead_reason": lead.reason,
                            "sigma_per_second": vol.sigma_per_second(),
                            "decision": _decision_from_args(
                                args,
                                latest_snapshot,
                                vol.sigma_per_second(),
                                measured_lead_ms=lead.lead_ms,
                            ),
                        },
                    )
                    written += 1
                continue
            state.update(tick)
            snapshot = state.snapshot(
                datetime.now(UTC),
                max_component_age_ms=args.max_component_age_ms,
                min_venues=args.min_venues,
            )
            if snapshot is None:
                continue
            latest_snapshot = snapshot
            vol.update(snapshot)
            row_lead = measure_reference_lead(snapshot, latest_reference) if latest_reference is not None else None
            if row_lead is not None:
                lead_label = "official_rti"
                lead_reason = row_lead.reason
                measured_lead_ms = row_lead.lead_ms
            elif cfb_configured:
                lead_label = "official_rti_configured_waiting"
                lead_reason = "official_cfb_rti_configured_no_print_yet"
                measured_lead_ms = None
            else:
                lead_label = "proxy_only_no_official_rti"
                lead_reason = "official_cfb_rti_not_configured"
                measured_lead_ms = None
            sigma_per_second = vol.sigma_per_second()
            append_jsonl(
                args.ledger,
                {
                    "row_type": "synthetic_index",
                    "row_id": stable_hash({"as_of": snapshot.as_of, "price": snapshot.price}),
                    "snapshot": snapshot,
                    "sigma_per_second": sigma_per_second,
                    "lead": row_lead,
                    "lead_label": lead_label,
                    "lead_reason": lead_reason,
                    "decision": _decision_from_args(
                        args,
                        snapshot,
                        sigma_per_second,
                        measured_lead_ms=measured_lead_ms,
                    ),
                },
            )
            written += 1
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    print(f"wrote {written} read-only synthetic-index rows to {args.ledger}")
    if cfb_configured:
        print(f"official CFB RTI stream enabled for {args.cfb_stream_id}; inspect lead_label=official_rti rows")
    else:
        print(
            "official CFB RTI lead was not measured; "
            "set CFB_WS_USERNAME/CFB_WS_PASSWORD before claiming a c-lead edge"
        )
    return 0


def _decision_from_args(
    args: argparse.Namespace,
    snapshot: Any,
    sigma_per_second: float | None,
    *,
    measured_lead_ms: float | None = None,
) -> dict[str, Any] | None:
    if args.strike is None or args.yes_bid is None or args.yes_ask is None:
        return None
    if sigma_per_second is None:
        return {"candidate": False, "reason": "insufficient_vol_points"}
    decision = evaluate_btc15m_timing_candidate(
        market_id=args.market_id,
        synthetic=snapshot,
        strike=args.strike,
        seconds_to_expiry=args.seconds_to_expiry,
        sigma_per_sec=sigma_per_second,
        yes_bid=Decimal(str(args.yes_bid)),
        yes_ask=Decimal(str(args.yes_ask)),
        measured_lead_ms=measured_lead_ms,
        residual_latency_ms=args.residual_latency_ms,
        min_net_edge=args.min_net_edge,
    )
    return to_jsonable(decision)


def _run_no_network(args: argparse.Namespace) -> int:
    now = datetime(2026, 6, 2, 12, 0, tzinfo=UTC)
    state = SyntheticIndexState({"coinbase": args.coinbase_weight, "kraken": args.kraken_weight})
    state.update(
        CryptoTick(
            venue="coinbase",
            symbol="BTC-USD",
            price=100_150.0,
            exchange_ts=now - timedelta(milliseconds=4),
            received_at=now,
        )
    )
    state.update(
        CryptoTick(
            venue="kraken",
            symbol="XBT/USD",
            price=100_152.0,
            received_at=now,
        )
    )
    snapshot = state.snapshot(now, max_component_age_ms=args.max_component_age_ms, min_venues=1)
    if snapshot is None:
        raise SystemExit("fixture did not produce a synthetic snapshot")
    reference = CryptoTick(
        venue="cfb-rti-fixture",
        symbol="BTC-USD",
        price=100_151.0,
        exchange_ts=now + timedelta(milliseconds=args.fixture_lead_ms),
        received_at=now + timedelta(milliseconds=args.fixture_lead_ms + 1.0),
    )
    lead = measure_reference_lead(snapshot, reference)
    decision = evaluate_btc15m_timing_candidate(
        market_id=args.market_id,
        synthetic=snapshot,
        strike=args.strike if args.strike is not None else 100_000.0,
        seconds_to_expiry=args.seconds_to_expiry,
        sigma_per_sec=args.fixture_sigma_per_second,
        yes_bid=Decimal(str(args.yes_bid if args.yes_bid is not None else 0.52)),
        yes_ask=Decimal(str(args.yes_ask if args.yes_ask is not None else 0.55)),
        measured_lead_ms=lead.lead_ms,
        residual_latency_ms=args.residual_latency_ms,
        min_net_edge=args.min_net_edge,
    )
    row = {
        "row_type": "no_network_self_test",
        "row_id": stable_hash({"snapshot": snapshot, "lead": lead, "decision": decision}),
        "snapshot": snapshot,
        "lead": lead,
        "lead_label": "official_rti_fixture",
        "lead_reason": lead.reason,
        "decision": decision,
        "fixture": True,
    }
    append_jsonl(args.ledger, row)
    print(json.dumps(to_jsonable(row), sort_keys=True))
    return 0


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-network", action="store_true", help="write a deterministic fixture row and exit")
    parser.add_argument("--duration-sec", type=float, default=30.0)
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument("--coinbase-weight", type=float, default=0.5)
    parser.add_argument("--kraken-weight", type=float, default=0.5)
    parser.add_argument("--cfb-ws-url", default=CFB_WS)
    parser.add_argument("--cfb-stream-id", default="BRTI")
    parser.add_argument("--cfb-ws-username", default=os.getenv("CFB_WS_USERNAME"))
    parser.add_argument("--cfb-ws-password", default=os.getenv("CFB_WS_PASSWORD"))
    parser.add_argument("--disable-cfb-rti", action="store_true")
    parser.add_argument("--max-component-age-ms", type=float, default=1_000.0)
    parser.add_argument("--min-venues", type=int, default=1)
    parser.add_argument("--vol-points", type=int, default=300)
    parser.add_argument("--market-id", default="KXBTC15M-PAPER")
    parser.add_argument("--strike", type=float, default=None)
    parser.add_argument("--seconds-to-expiry", type=float, default=30.0)
    parser.add_argument("--yes-bid", type=float, default=None)
    parser.add_argument("--yes-ask", type=float, default=None)
    parser.add_argument("--residual-latency-ms", type=float, default=2.0)
    parser.add_argument("--min-net-edge", type=float, default=0.02)
    parser.add_argument("--fixture-lead-ms", type=float, default=4.0)
    parser.add_argument("--fixture-sigma-per-second", type=float, default=5.0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    if args.no_network:
        return _run_no_network(args)
    return asyncio.run(_record_live(args))


if __name__ == "__main__":
    raise SystemExit(main())
