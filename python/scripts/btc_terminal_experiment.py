"""Experiment: price the live KXBTCD ladder with the HAR-RV + Student-t terminal
model and compare to the Kalshi market mid.

Read-only. Pulls public BTC spot history (Coinbase 6h candles) to fit HAR-RV,
the live KXBTCD strikes from Kalshi prod, and prints model-vs-market per strike
for the nearest-expiry event -- the iteration loop for tuning vol/dof.

    .venv/Scripts/python.exe python/scripts/btc_terminal_experiment.py
"""

from __future__ import annotations

import asyncio
import math
import statistics
from collections import defaultdict
from datetime import UTC, datetime, timedelta

import httpx

from eventcontracts.adapters.venues.kalshi.client import KALSHI_REST_PROD, KalshiPublicClient
from eventcontracts.research.btc_terminal import BtcTerminalModel, horizon_sigma_from_daily_vol
from eventcontracts.research.har_rv import HARFamily

COINBASE = "https://api.exchange.coinbase.com"


def _num(x: object) -> float | None:
    try:
        return float(x)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _parse_ts(value: object) -> float | None:
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str) and value:
        text = value.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(text).timestamp()
        except ValueError:
            return None
    return None


async def _coinbase_hourly_closes(cb: httpx.AsyncClient, *, days: int = 34) -> list[tuple[int, float]]:
    """Paginate Coinbase 1h candles back ~``days`` days (300 candles/call)."""

    end = datetime.now(UTC)
    seen: dict[int, float] = {}
    for _ in range(6):
        start = end - timedelta(hours=300)
        r = await cb.get(
            f"{COINBASE}/products/BTC-USD/candles",
            params={"granularity": 3600, "start": start.isoformat(), "end": end.isoformat()},
        )
        r.raise_for_status()
        for c in r.json():  # [time, low, high, open, close, volume]
            seen[int(c[0])] = float(c[4])
        end = start
        if len(seen) >= days * 24:
            break
    return [(t, seen[t]) for t in sorted(seen)]


async def _coinbase_spot(cb: httpx.AsyncClient) -> float:
    r = await cb.get(f"{COINBASE}/products/BTC-USD/ticker")
    r.raise_for_status()
    return float(r.json()["price"])


def _daily_returns(closes: list[tuple[int, float]]) -> tuple[list[list[float]], list[float]]:
    """Group intraday (hourly) log-returns by UTC day + the last close per day."""

    rets_by_day: dict[object, list[float]] = defaultdict(list)
    last_close_by_day: dict[object, float] = {}
    for (_t0, c0), (t1, c1) in zip(closes, closes[1:], strict=False):
        day = datetime.fromtimestamp(t1, UTC).date()
        if c0 > 0 and c1 > 0:
            rets_by_day[day].append(math.log(c1 / c0))
        last_close_by_day[day] = c1
    days = sorted(rets_by_day)
    return [rets_by_day[d] for d in days], [last_close_by_day[d] for d in days]


async def main() -> None:
    async with httpx.AsyncClient(timeout=20.0) as cb:
        closes = await _coinbase_hourly_closes(cb, days=60)
        spot = await _coinbase_spot(cb)
    daily_rets, daily_closes = _daily_returns(closes)
    print(f"BTC vol (Coinbase 1h, {len(daily_rets)} days, {len(closes)} candles, ridge=5):")
    variants: dict[str, tuple[object, float]] = {}
    for v in ("har", "har_rs", "har_cj"):
        fit = HARFamily(variant=v, ridge=5.0).fit(daily_rets)
        dv = math.sqrt(fit.forecast(daily_rets))
        variants[v] = (fit, dv)
        print(f"  {v:7} daily_vol={dv*100:.2f}% (~{dv*math.sqrt(365)*100:.0f}% ann)  in-sample R2={fit.r_squared:.2f}")
    dret = [math.log(b / a) for a, b in zip(daily_closes[-21:], daily_closes[-20:], strict=False) if a > 0 and b > 0]
    simple_vol = statistics.pstdev(dret) if len(dret) > 2 else float("nan")
    print(f"  simple-20d realized={simple_vol*100:.2f}% (cross-check)   spot=${spot:,.0f}")
    daily_vol = variants["har_rs"][1]  # price with the research-preferred semivariance variant

    kc = KalshiPublicClient(base_url=KALSHI_REST_PROD)
    markets = (await kc.get_markets_payload(series_ticker="KXBTCD", status="open", limit=1000)).get("markets", [])
    now = datetime.now(UTC).timestamp()
    events: dict[str, list[dict[str, object]]] = defaultdict(list)
    for m in markets:
        ct = _parse_ts(m.get("close_time"))
        yb, ya = _num(m.get("yes_bid_dollars")), _num(m.get("yes_ask_dollars"))
        if ct is None or ct <= now or yb is None or ya is None or yb <= 0 or ya <= 0:
            continue
        events[str(m.get("event_ticker"))].append(m)
    if not events:
        print("no quoted future KXBTCD strikes right now")
        return
    # Near-expiry daily strikes are pinned (no dispersion); target a horizon with
    # real spread so the model-vs-market comparison is informative.
    def event_close(ms: list[dict[str, object]]) -> float:
        return min(_parse_ts(m.get("close_time")) or 1e18 for m in ms)

    # Need real dispersion: pick the event >= 2h out with the most quoted strikes.
    midrange = {e: ms for e, ms in events.items() if (event_close(ms) - now) >= 2 * 3600}
    pool = midrange or events
    ev, ms = max(pool.items(), key=lambda kv: len(kv[1]))
    horizon = event_close(ms) - now
    print(
        f"\nevent={ev}  strikes={len(ms)}  horizon={horizon/3600:.2f}h  "
        f"settle={datetime.fromtimestamp(event_close(ms), UTC):%Y-%m-%d %H:%MZ}"
    )
    hsig = horizon_sigma_from_daily_vol(daily_vol, horizon)
    print(f"horizon_sigma={hsig*100:.2f}% of spot (~${spot*hsig:,.0f})")
    t_model = BtcTerminalModel(spot=spot, horizon_sigma=hsig, dof=4.0)
    n_model = BtcTerminalModel(spot=spot, horizon_sigma=hsig, dof=math.inf)
    rows = []
    for m in ms:
        strike = _num(str(m.get("ticker")).split("-T")[-1])
        if strike is None:
            continue
        yb, ya = _num(m.get("yes_bid_dollars")) or 0.0, _num(m.get("yes_ask_dollars")) or 0.0
        rows.append((strike, (yb + ya) / 2, n_model.prob_above(strike), t_model.prob_above(strike)))
    rows.sort()
    active = [r for r in rows if 0.02 < r[3] < 0.98]  # the non-degenerate region near spot
    shown = active or rows
    print(f"\nspot=${spot:,.0f}; showing {len(shown)} active strikes ({len(rows)-len(shown)} pinned ~0/1 omitted)")
    print(f"{'strike':>10}{'mkt_mid':>9}{'model_N':>9}{'model_t4':>10}{'edge_t(c)':>11}")
    for strike, mid, pn, pt in shown:
        print(f"{strike:>10,.0f}{mid:>9.3f}{pn:>9.3f}{pt:>10.3f}{(pt-mid)*100:>+11.1f}")
    best = max(shown, key=lambda r: abs(r[3] - r[1]))
    print(
        f"\nlargest |edge| (active): strike {best[0]:,.0f}  "
        f"mkt {best[1]:.3f} vs model_t4 {best[3]:.3f} = {(best[3]-best[1])*100:+.1f}c"
    )
    print("(model-vs-mid only -- NOT fee/spread/CLV-adjusted; next gate per edge-validation philosophy)")


if __name__ == "__main__":
    asyncio.run(main())
