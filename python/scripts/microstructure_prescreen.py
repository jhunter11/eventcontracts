"""Lane-2 feasibility pre-screen: is Kalshi BTC liquidity fleeting, and how fast
does it reprice a BTC move? Read-only, no creds, runnable from the current host.

Polls a near-the-money KXBTCD strike's REST order book + recent trades and the
Coinbase spot reference for ~40 s, then runs the microstructure analyzer. REST
polling is coarse (~1.5 s) -- it gives a real first read on quote lifetime,
depth, reprice lag, and trade-vs-cancel mix; ms-scale numbers need a WS capture
from the trade host.

    .venv/Scripts/python.exe python/scripts/microstructure_prescreen.py
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime

import httpx

from eventcontracts.adapters.venues.kalshi.client import KALSHI_REST_PROD, KalshiPublicClient
from eventcontracts.domain.models import InstrumentId, Venue
from eventcontracts.research.microstructure import (
    BookTop,
    TradePrint,
    depth_summary,
    quote_lifetimes_ms,
    reaction_lag,
    repricing_decomposition,
    summarize,
)

COINBASE = "https://api.exchange.coinbase.com"
DURATION_S = 40.0
CADENCE_S = 1.5


def _num(x: object) -> float | None:
    try:
        return float(x)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _parse_ts(value: object) -> float | None:
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


async def _coinbase_spot(cb: httpx.AsyncClient) -> float:
    r = await cb.get(f"{COINBASE}/products/BTC-USD/ticker")
    r.raise_for_status()
    return float(r.json()["price"])


async def _pick_market(kc: KalshiPublicClient, spot: float) -> tuple[str, float] | None:
    markets = (await kc.get_markets_payload(series_ticker="KXBTCD", status="open", limit=1000)).get("markets", [])
    now = datetime.now(UTC).timestamp()
    cands: list[tuple[float, str, float]] = []
    for m in markets:
        yb, ya = _num(m.get("yes_bid_dollars")), _num(m.get("yes_ask_dollars"))
        ct = _parse_ts(m.get("close_time"))
        strike = _num(str(m.get("ticker")).split("-T")[-1])
        if yb and ya and strike and ct and ct - now > 3600:
            cands.append((abs(strike - spot), str(m.get("ticker")), strike))
    if not cands:
        return None
    cands.sort()
    return cands[0][1], cands[0][2]


async def main() -> None:
    async with httpx.AsyncClient(timeout=15.0) as cb:
        spot0 = await _coinbase_spot(cb)
        kc = KalshiPublicClient(base_url=KALSHI_REST_PROD)
        picked = await _pick_market(kc, spot0)
        if picked is None:
            print("no quoted near-expiry KXBTCD strike to probe")
            return
        ticker, strike = picked
        print(
            f"probing {ticker} (strike ${strike:,.0f}, spot ${spot0:,.0f}) "
            f"for {DURATION_S:.0f}s @ {CADENCE_S}s REST polls"
        )
        inst = InstrumentId(venue=Venue.KALSHI, market_id=ticker)
        tops: list[BookTop] = []
        ref_t: list[datetime] = []
        ref_p: list[float] = []
        trades: dict[str, TradePrint] = {}
        end = time.time() + DURATION_S
        polls = 0
        while time.time() < end:
            now = datetime.now(UTC)
            try:
                spot = await _coinbase_spot(cb)
                ob = await kc.get_order_book(inst)
            except Exception as exc:  # noqa: BLE001 - pre-screen tolerates a dropped poll
                print(f"  poll error: {type(exc).__name__}")
                await asyncio.sleep(CADENCE_S)
                continue
            bb = ob.yes_bids[0] if ob.yes_bids else None
            ba = ob.yes_asks[0] if ob.yes_asks else None
            tops.append(
                BookTop(
                    ts=now,
                    bid=float(bb.price) if bb else None,
                    bid_size=float(bb.quantity) if bb else 0.0,
                    ask=float(ba.price) if ba else None,
                    ask_size=float(ba.quantity) if ba else 0.0,
                )
            )
            ref_t.append(now)
            ref_p.append(spot)
            for tr in await kc.get_recent_trades(ticker, limit=50):
                if tr.trade_id and tr.trade_id not in trades:
                    trades[tr.trade_id] = TradePrint(
                        ts=tr.exchange_ts or tr.received_at, price=float(tr.price), size=float(tr.quantity)
                    )
            polls += 1
            await asyncio.sleep(CADENCE_S)

    moved = ref_p[-1] - ref_p[0]
    print(f"\ncaptured {polls} polls, {len(trades)} unique trades, spot ${ref_p[-1]:,.0f} (moved ${moved:+,.0f})")
    lifetimes = quote_lifetimes_ms(tops, side="ask") + quote_lifetimes_ms(tops, side="bid")
    ql = summarize(lifetimes)
    depth = depth_summary(tops)
    print(f"  quote lifetime (ms): median={ql.median:.0f} p10={ql.p10:.0f} p90={ql.p90:.0f} n={ql.n}")
    print(
        f"  spread: median={depth['spread'].median:.3f}   "
        f"top size: bid={depth['bid_size'].median:.0f} ask={depth['ask_size'].median:.0f}"
    )
    mids = [t.mid for t in tops if t.mid is not None]
    midt = [t.ts for t in tops if t.mid is not None]
    rl = reaction_lag(ref_t, ref_p, midt, mids, max_lag_s=9.0, step_s=CADENCE_S)
    if rl is not None:
        print(f"  reaction lag: {rl.best_lag_s:+.1f}s (corr {rl.correlation:+.2f}) -- +ve = Kalshi reprices after BTC")
    else:
        print("  reaction lag: insufficient signal (BTC barely moved in window)")
    mix = repricing_decomposition(tops, list(trades.values()), side="ask", window_ms=CADENCE_S * 1000)
    if mix.n_changes:
        print(
            f"  repricing: {mix.trade_driven*100:.0f}% trade-driven (catchable) / "
            f"{mix.cancel_driven*100:.0f}% bare-cancel (pulled), n={mix.n_changes}"
        )
    print("\nNOTE: REST ~1.5s resolution -- indicative only. ms-scale needs a WS capture from the trade host.")


if __name__ == "__main__":
    asyncio.run(main())
