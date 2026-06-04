"""Prove-before-expand recorder: Kalshi BTC model-vs-market gap series.

Read-only. Pulls REAL public data and prices each open Kalshi BTC market with the
settlement kernel (`eventcontracts.research.btc_settlement`), then logs the gap
between the model P(Yes) and the Kalshi market-implied price. Repeated runs append
to a JSONL ledger so a gap series accumulates over time — the measurement that
must clear before any Rust execution build (calibration != edge; prove it at the
tradable moment).

Data sources (all public, no auth):
  - Coinbase Exchange REST  -> BTC-USD spot + realized vol (the index proxy ``c``)
  - Kalshi REST (markets)   -> strike, close_time, yes_bid/ask, settlement

HONEST SCOPE. This measures the gap for the markets that EXIST today (monthly
``KX{BTC,ETH}{MAX,MIN}MON`` trimmed-mean contracts), pricing them as a diffusion of
the *level* to expiry (`before_window_forecast`) — which is exactly the dominant
uncertainty the 60s-average kernel does NOT remove. It does NOT yet measure the two
things the 15-minute thesis actually rides on:
  (1) the ms constituent-lead on ``c`` (needs Coinbase/Kraken/Bitstamp/LMAX/Gemini
      L2 feeds + the official CF Benchmarks RTI print to diff against), and
  (2) the final-60s second-level convergence (needs a 15-min market, none open now).
Those need real-time feeds this repo does not have; this recorder is the rung-1
machinery + a real calibration probe, reusable verbatim when a short-duration
market and constituent feeds exist.
"""

from __future__ import annotations

import argparse
import io
import json
import math
import sys
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "python" / "src"))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from eventcontracts.research.btc_settlement import (  # noqa: E402
    forecast_at,
    sigma_per_second_from_annual,
)

KALSHI = "https://api.elections.kalshi.com/trade-api/v2/markets"
COINBASE = "https://api.exchange.coinbase.com"
DEFAULT_LEDGER = ROOT / "live-test" / "btc_gap_ledger.jsonl"
KALSHI_FEE_COEF = 0.07  # Kalshi fee ~ 0.07 * p * (1-p) per contract


def _get(url: str, tries: int = 5) -> object:
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "ec-research/1.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except Exception as e:  # noqa: BLE001
            if i < tries - 1:
                time.sleep(1.0 + i)
                continue
            return {"_err": str(e)}
    return {}


def _fnum(v: object) -> float:
    try:
        return float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def coinbase_spot_and_vol(product: str = "BTC-USD") -> tuple[float, float, float]:
    """Return (spot, annualised_vol, spot_age_sec).

    ``spot`` is the **real-time ticker** price, not the 1-min candle close. The
    candle close can lag the live price by up to a full minute (observed ~$95 /
    5 min in testing); pricing ``c`` off a stale candle manufactures a phantom
    "distance to strike" that reads as a fake edge. Vol still comes from the
    candle series (a per-minute realized estimate); ``spot_age_sec`` is the
    ticker's own age so a stale feed is recorded, never silently trusted.
    """
    candles = _get(f"{COINBASE}/products/{product}/candles?granularity=60")
    if not isinstance(candles, list) or not candles:
        raise SystemExit(f"Coinbase candles unavailable: {candles}")
    # candle = [time, low, high, open, close, volume]; newest first.
    closes = [c[4] for c in candles if isinstance(c, list) and len(c) >= 5]
    closes = list(reversed(closes))[-180:]
    ticker = _get(f"{COINBASE}/products/{product}/ticker")
    spot = _fnum(ticker.get("price")) if isinstance(ticker, dict) else 0.0
    spot_age = 0.0
    if spot <= 0.0:  # ticker unavailable -> fall back to candle close, flagged stale
        spot = closes[-1]
        spot_age = float("nan")
    elif isinstance(ticker, dict) and ticker.get("time"):
        try:
            t_tick = datetime.fromisoformat(str(ticker["time"]).replace("Z", "+00:00"))
            spot_age = max((datetime.now(UTC) - t_tick).total_seconds(), 0.0)
        except ValueError:
            spot_age = 0.0
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes)) if closes[i - 1] > 0]
    if len(rets) < 5:
        return spot, 0.5, spot_age
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    vol_per_min = math.sqrt(var)
    annual_vol = vol_per_min * math.sqrt(525_600.0)  # minutes per year
    return spot, annual_vol, spot_age


def open_btc_markets(series_filter: str | None) -> list[dict]:
    """Open crypto markets.

    When a ``series_filter`` is given we hit the ``series_ticker=`` endpoint
    directly — the reliable path. The alternative (scan every open market and
    filter client-side) silently misses thin short-duration series like
    ``KXBTC15M`` that have a single live market buried in 60k+ open contracts,
    which is exactly the false "0 open markets" the broad scan produced.
    """
    out: list[dict] = []
    cursor = None
    for _ in range(60):
        if series_filter:
            url = f"{KALSHI}?series_ticker={series_filter}&limit=1000&status=open"
        else:
            url = f"{KALSHI}?limit=1000&status=open"
        if cursor:
            url += f"&cursor={cursor}"
        d = _get(url)
        ms = d.get("markets", []) if isinstance(d, dict) else []
        for m in ms:
            t = str(m.get("ticker") or "")
            if series_filter:
                if t.startswith(series_filter):
                    out.append(m)
                continue
            blob = (t + " " + str(m.get("title") or "")).upper()
            if "BTC" in blob or "BITCOIN" in blob or "ETH" in blob:
                out.append(m)
        cursor = d.get("cursor") if isinstance(d, dict) else None
        cursor = str(cursor) if cursor else None
        if not cursor or not ms:
            break
        time.sleep(0.3)
    return out


def _strike_and_side(m: dict) -> tuple[float | None, str]:
    """(strike_usd, 'above'|'below') from the market cap/floor or title."""
    series = str(m.get("ticker") or "").split("-")[0]
    side = "below" if "MIN" in series else "above"
    cap = m.get("cap_strike") or m.get("floor_strike")
    if cap is not None:
        return _fnum(cap), side
    # Fall back to the last numeric token of the ticker (cents*100 in these series).
    tail = str(m.get("ticker") or "").split("-")[-1]
    if tail.isdigit():
        return float(tail) / 100.0, side  # e.g. 9250000 -> 92500.00
    return None, side


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--series", default=None, help="Kalshi series prefix, e.g. KXBTCMAXMON")
    ap.add_argument("--annual-vol", type=float, default=None, help="Override realized vol")
    ap.add_argument("--edge-threshold", type=float, default=0.05, help="Flag |gap| over this")
    ap.add_argument("--max-spot-age-sec", type=float, default=15.0,
                    help="Treat spot as stale (no actionable flags) above this age")
    ap.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    args = ap.parse_args()

    now = datetime.now(UTC)
    spot, realized_vol, spot_age = coinbase_spot_and_vol()
    annual_vol = args.annual_vol if args.annual_vol is not None else realized_vol
    sigma_sec = sigma_per_second_from_annual(spot, annual_vol)
    stale = math.isnan(spot_age) or spot_age > args.max_spot_age_sec
    print(f"[{now.isoformat(timespec='seconds')}] BTC spot=${spot:,.0f}  "
          f"annual_vol={annual_vol:.1%}  sigma/sec=${sigma_sec:,.2f}  "
          f"spot_age={spot_age:.0f}s{'  *** STALE ***' if stale else ''}")
    if stale:
        # A stale `c` manufactures phantom distance-to-strike that reads as a
        # fake edge (the exact bug that flagged a $101 'edge' off a 5-min-old
        # candle). Refuse to emit actionable flags rather than mislead.
        print(f"WARNING: spot is stale (age {spot_age:.0f}s > {args.max_spot_age_sec:.0f}s); "
              "gaps are NOT actionable this run — `c` is unreliable.")

    markets = open_btc_markets(args.series)
    print(f"open crypto markets: {len(markets)}\n")
    args.ledger.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for m in markets:
        ticker = str(m.get("ticker") or "")
        # We fetched BTC spot only; price BTC markets unless a series is pinned
        # (ETH/others would need their own spot — pricing them with BTC spot is
        # the spurious-edge bug this guard prevents).
        if args.series is None and not ticker.startswith("KXBTC"):
            continue
        strike, side = _strike_and_side(m)
        close = m.get("close_time")
        yb, ya = m.get("yes_bid_dollars"), m.get("yes_ask_dollars")
        if strike is None or close is None or yb is None or ya is None:
            continue
        try:
            t_expiry = datetime.fromisoformat(str(close).replace("Z", "+00:00"))
        except ValueError:
            continue
        t_sec = max((t_expiry - now).total_seconds(), 0.0)
        # Unified time-to-settlement model: forecast_at dispatches to the
        # before-window level diffusion (t_sec >= 60) and the in-window Asian
        # average (t_sec < 60) at the right seam, so a single call spans the
        # whole 15-minute contract life including the final-60s lock.
        fc = forecast_at(t_sec, spot, sigma_sec)
        model_p = fc.prob_above(strike) if side == "above" else fc.prob_below(strike)
        mkt_mid = (_fnum(yb) + _fnum(ya)) / 2.0
        gap = model_p - mkt_mid
        fee = KALSHI_FEE_COEF * mkt_mid * (1.0 - mkt_mid)
        spread = _fnum(ya) - _fnum(yb)
        # Net edge must clear half-spread (cross to the touch) + fee.
        net_edge = abs(gap) - (spread / 2.0) - fee
        row = {
            "ts": now.isoformat(timespec="seconds"), "ticker": ticker, "side": side,
            "strike": strike, "spot": round(spot, 2), "annual_vol": round(annual_vol, 4),
            "t_to_expiry_sec": round(t_sec, 1), "model_p": round(model_p, 4),
            "mkt_bid": yb, "mkt_ask": ya, "mkt_mid": round(mkt_mid, 4),
            "gap": round(gap, 4), "spread": round(spread, 4), "fee": round(fee, 4),
            "net_edge_after_costs": round(net_edge, 4),
            # Provenance caveats so a gap is never mistaken for proven edge:
            # `c` is a single Coinbase constituent, not the CFB RTI the strike is
            # set from (a real basis), and spot may be stale.
            "spot_age_sec": None if math.isnan(spot_age) else round(spot_age, 1),
            "spot_stale": stale,
            "index_basis": "coinbase_spot_vs_cfb_rti_unmeasured",
        }
        rows.append(row)
        with args.ledger.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")

    rows.sort(key=lambda r: r["net_edge_after_costs"], reverse=True)
    # The diffusion/60s-kernel model is the RIGHT settlement model only for
    # short-duration products (hourly/daily/15-min settle on the 60s RTI average
    # at expiry). Monthly "trimmed mean ... by month-end" is a touch/period
    # contract, so a gap there is settlement-model mismatch, NOT edge.
    SHORT_DURATION_SEC = 3600.0
    print(f"{'ticker':32s} {'K':>10s} {'model':>6s} {'mkt':>6s} {'gap':>7s} "
          f"{'spread':>6s} {'net_edge':>8s}  days_left")
    actionable = 0
    for r in rows[:20]:
        short = r["t_to_expiry_sec"] <= SHORT_DURATION_SEC
        if short and not stale and r["net_edge_after_costs"] > args.edge_threshold:
            # Even when flagged, this is a CANDIDATE, not proven edge: the
            # Coinbase-vs-CFB-RTI basis is still unmeasured. Real validation
            # needs `c` = the constituent-built RTI, diffed vs the official print.
            flag = "  <== candidate (UNCONFIRMED: coinbase!=CFB basis unmeasured)"
            actionable += 1
        elif short and stale:
            flag = "  (spot stale -> not actionable)"
        elif not short:
            flag = "  (monthly: settlement != model -> gap NOT actionable)"
        else:
            flag = ""
        print(f"{r['ticker']:32s} {r['strike']:>10,.0f} {r['model_p']:>6.3f} "
              f"{r['mkt_mid']:>6.3f} {r['gap']:>+7.3f} {r['spread']:>6.3f} "
              f"{r['net_edge_after_costs']:>+8.3f}  {r['t_to_expiry_sec']/86400:>5.1f}{flag}")
    print(f"\nappended {len(rows)} BTC rows -> {args.ledger}")
    print(f"actionable short-duration edges: {actionable}")
    if all(r["t_to_expiry_sec"] > SHORT_DURATION_SEC for r in rows):
        print("FINDING: every open BTC market is monthly trimmed-mean (settlement != "
              "model) -> nothing actionable. The 60s-average thesis needs a short-duration "
              "product (none open) + constituent feeds (not wired) to measure for real.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
