"""Read-only: rank open Kalshi markets by REAL liquidity (correct field names).

Kalshi renamed fields: volume_fp / volume_24h_fp / open_interest_fp (fixed-point
ints) and yes_bid_dollars / yes_ask_dollars / liquidity_dollars (decimal strings).
The old volume/yes_bid keys now return null. This reads the live ones, skips the
auto-generated parlay megaseries (KXMVE*), and ranks by 24h volume. No orders.
"""

from __future__ import annotations

import io
import json
import sys
import time
import urllib.request
from collections import defaultdict

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
BASE = "https://api.elections.kalshi.com/trade-api/v2/markets"
SKIP_PREFIXES = ("KXMVE", "KXNBASERIES")


def get(url: str, tries: int = 6) -> dict:
    for i in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                return json.loads(r.read())
        except Exception as e:  # noqa: BLE001
            if "429" in str(e) and i < tries - 1:
                time.sleep(2 + 2 * i)
                continue
            raise
    return {}


def fnum(v) -> float:
    if v is None:
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def main() -> int:
    cursor = None
    pages = 0
    total = 0
    markets = []
    s_v24: dict[str, float] = defaultdict(float)
    s_cnt: dict[str, int] = defaultdict(int)
    s_oi: dict[str, float] = defaultdict(float)

    while pages < 60:
        url = f"{BASE}?limit=1000&status=open" + (f"&cursor={cursor}" if cursor else "")
        d = get(url)
        ms = d.get("markets", [])
        total += len(ms)
        for m in ms:
            t = str(m.get("ticker") or "")
            series = t.split("-")[0]
            if any(series.startswith(p) for p in SKIP_PREFIXES):
                continue
            # fixed-point ints are in cents/contracts; relative ranking only.
            v24 = fnum(m.get("volume_24h_fp"))
            vol = fnum(m.get("volume_fp"))
            oi = fnum(m.get("open_interest_fp"))
            yb = m.get("yes_bid_dollars")
            ya = m.get("yes_ask_dollars")
            s_v24[series] += v24
            s_cnt[series] += 1
            s_oi[series] += oi
            if v24 or vol or yb or ya:
                markets.append(
                    {
                        "t": t, "v24": v24, "vol": vol, "oi": oi,
                        "yb": yb, "ya": ya,
                        "liq": m.get("liquidity_dollars"),
                        "title": (m.get("title") or m.get("yes_sub_title") or "")[:60],
                    }
                )
        cursor = d.get("cursor")
        cursor = str(cursor) if cursor else None
        pages += 1
        if not cursor or not ms:
            break
        time.sleep(0.35)

    print(f"scanned {total} open markets over {pages} pages (skipping parlay series)\n")

    print("=== TOP 25 SERIES BY 24h VOLUME ===")
    for s, v in sorted(s_v24.items(), key=lambda kv: kv[1], reverse=True)[:25]:
        if v <= 0:
            continue
        print(f"  {s:34s} v24={int(v):>12,} oi={int(s_oi[s]):>12,} markets={s_cnt[s]}")

    print("\n=== TOP 30 SINGLE MARKETS BY 24h VOLUME ===")
    markets.sort(key=lambda m: m["v24"], reverse=True)
    for m in markets[:30]:
        print(
            f"  {m['t']:44s} v24={int(m['v24']):>11,} oi={int(m['oi']):>10,} "
            f"yb={m['yb']} ya={m['ya']} liq={m['liq']} :: {m['title']}"
        )
    print(f"\nmarkets_with_activity={len(markets)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
