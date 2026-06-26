#!/usr/bin/env python3
"""Emit the comma-separated list of LIQUID (tradeable) Kalshi market tickers
across the legit (non-parlay) series — i.e. markets with a real quote or volume.

Subscribing the WS depth feed to *these* gives clean order-book deltas for
everything we'd actually be able to trade, without drowning the connection in
the ~30k illiquid prop/strike/parlay markets that have no book.

Prints tickers to stdout (one comma-sep line); a short summary to stderr.
"""
import collections
import json
import sys
import urllib.request

BASE = "https://api.elections.kalshi.com/trade-api/v2"

# Legit (non-parlay) series — the tradeable universe. Illiquid members are
# filtered out by the liquidity test below, so over-inclusion here is harmless.
SERIES = [
    # liquid AND fast (ms-relevant). Golf/macro/rain dropped: tradeable but
    # slow-resolving -> minute bars suffice, broadcast channels already cover them.
    "KXHIGHNY", "KXHIGHCHI", "KXHIGHMIA", "KXHIGHAUS",
    "KXBTC", "KXBTCD", "KXBTC15M", "KXETH", "KXETHD", "KXETH15M",
    "KXSOLD", "KXSOLE", "KXSOL15M", "KXXRP", "KXXRPD", "KXXRP15M",
    "KXDOGE", "KXDOGED", "KXDOGE15M", "KXBNB", "KXBNBD", "KXBNB15M",
    "KXHYPE", "KXHYPED", "KXHYPE15M",
    "KXATPMATCH", "KXATPSETWINNER", "KXATPEXACTMATCH", "KXATPGTOTAL",
    "KXATPGSPREAD", "KXATPCHALLENGERMATCH", "KXWTAMATCH", "KXWTASETWINNER",
    "KXWTACHALLENGERMATCH", "KXITFMATCH", "KXITFWMATCH", "KXT20MATCH",
    "KXMLBGAME", "KXMLBSPREAD", "KXMLBTOTAL", "KXMLBKS", "KXMLBHIT", "KXMLBHRR",
    "KXMLBTB", "KXNPBGAME",
    "KXPGATOUR",
    "KXSOCCER", "KXFIFA", "KXWORLDCUP",
    "KXWNBA3PT", "KXWNBAAST", "KXWNBAPTS", "KXWNBAREB",
    "KXCS2MAP", "KXCS2GAME", "KXCS2TOTALMAPS", "KXLOLGAME", "KXLOLMAP",
    "KXLOLTOTALMAPS",
]


def _get(path):
    req = urllib.request.Request(BASE + path, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=12) as r:
        return json.loads(r.read())


def liquid(m):
    yb = m.get("yes_bid_dollars"); ya = m.get("yes_ask_dollars")
    vol = m.get("volume_fp")
    has_quote = (yb not in (None, "0.0000")) or (ya not in (None, "0.0000", "1.0000"))
    has_vol = vol not in (None, "0.00", "0")
    return has_quote or has_vol


def main():
    tickers = []
    per = collections.Counter()
    for s in SERIES:
        try:
            d = _get(f"/markets?series_ticker={s}&status=open&limit=1000")
        except Exception:
            continue
        for m in d.get("markets", []):
            if liquid(m):
                tickers.append(m["ticker"]); per[s] += 1
    sys.stderr.write(
        f"liquid tickers: {len(tickers)} across {len(per)} series; top {per.most_common(8)}\n"
    )
    print(",".join(tickers))


if __name__ == "__main__":
    main()
