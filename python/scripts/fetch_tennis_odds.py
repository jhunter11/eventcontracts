"""Fetch live pre-match ATP odds from The Odds API -> odds.csv.

Produces the `player,decimal_odds` CSV that build_upcoming_snapshot.py consumes
(--odds-csv), so the live tennis sleeve's require_odds_present=true is satisfied
from a real bookmaker line instead of hand-typed numbers.

Uses The Odds API v4 (https://the-odds-api.com), stdlib-only (urllib). Set the key
via THE_ODDS_API_KEY or --api-key. Tennis is exposed as per-tournament sport keys
(e.g. tennis_atp_french_open) that are only `active` during the event.

Usage:
    # see which ATP tournaments are live + your remaining quota
    python python/scripts/fetch_tennis_odds.py --list-sports

    # write odds.csv for one match (prefers Pinnacle, the sharp book)
    python python/scripts/fetch_tennis_odds.py --sport tennis_atp_french_open \
        --p1 "Carlos Alcaraz" --p2 "Alexander Zverev" --out odds.csv

    # verify the parser offline (no network / key needed)
    python python/scripts/fetch_tennis_odds.py --selftest
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

API = "https://api.the-odds-api.com/v4"


def _get(url: str) -> tuple[object, dict]:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 (trusted host)
        headers = {
            "remaining": resp.headers.get("x-requests-remaining"),
            "used": resp.headers.get("x-requests-used"),
        }
        return json.loads(resp.read().decode("utf-8")), headers


def _norm(name: str) -> str:
    return " ".join(str(name).strip().lower().split())


def extract_pair_odds(
    event: dict, p1: str | None, p2: str | None, prefer_book: str = "pinnacle"
) -> dict[str, float]:
    """Return {player_name: decimal_odds} for an h2h event.

    Prefers ``prefer_book`` (default Pinnacle, the sharp line); falls back to the
    mean decimal price across all books carrying h2h. If p1/p2 are given, returns
    only matching players (normalized), else every outcome in the event.
    """
    wanted = {_norm(p1), _norm(p2)} - {""} if (p1 or p2) else None
    # Collect prices per player across books.
    prices: dict[str, list[float]] = {}
    preferred: dict[str, float] = {}
    for book in event.get("bookmakers", []):
        is_pref = book.get("key") == prefer_book
        for market in book.get("markets", []):
            if market.get("key") != "h2h":
                continue
            for outcome in market.get("outcomes", []):
                name = outcome.get("name")
                price = outcome.get("price")
                if name is None or price is None:
                    continue
                prices.setdefault(name, []).append(float(price))
                if is_pref:
                    preferred[name] = float(price)
    out: dict[str, float] = {}
    for name, plist in prices.items():
        if wanted is not None and _norm(name) not in wanted:
            continue
        out[name] = preferred.get(name) or (sum(plist) / len(plist))
    return out


_SAMPLE_EVENT = {
    "id": "abc",
    "home_team": "Carlos Alcaraz",
    "away_team": "Alexander Zverev",
    "bookmakers": [
        {
            "key": "pinnacle",
            "markets": [
                {
                    "key": "h2h",
                    "outcomes": [
                        {"name": "Carlos Alcaraz", "price": 1.40},
                        {"name": "Alexander Zverev", "price": 3.05},
                    ],
                }
            ],
        },
        {
            "key": "betfair",
            "markets": [
                {
                    "key": "h2h",
                    "outcomes": [
                        {"name": "Carlos Alcaraz", "price": 1.44},
                        {"name": "Alexander Zverev", "price": 2.95},
                    ],
                }
            ],
        },
    ],
}


def _selftest() -> int:
    got = extract_pair_odds(_SAMPLE_EVENT, "Carlos Alcaraz", "Alexander Zverev")
    assert abs(got["Carlos Alcaraz"] - 1.40) < 1e-9, got  # prefers Pinnacle
    assert abs(got["Alexander Zverev"] - 3.05) < 1e-9, got
    no_pref = extract_pair_odds(_SAMPLE_EVENT, None, None, prefer_book="nope")
    assert abs(no_pref["Carlos Alcaraz"] - (1.40 + 1.44) / 2) < 1e-9, no_pref  # mean fallback
    assert set(extract_pair_odds(_SAMPLE_EVENT, "Carlos Alcaraz", "Nobody")) == {"Carlos Alcaraz"}
    print("SELFTEST OK: Pinnacle preference, mean fallback, and pair filtering all pass.")
    return 0


def _write_csv(path: str, odds: dict[str, float]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["player", "decimal_odds"])
        for name, price in odds.items():
            w.writerow([name, f"{price:.4f}"])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--api-key", default=os.environ.get("THE_ODDS_API_KEY"))
    ap.add_argument("--list-sports", action="store_true", help="List active tennis sport keys + quota.")
    ap.add_argument("--sport", help="Sport key, e.g. tennis_atp_french_open.")
    ap.add_argument("--regions", default="eu,uk,us", help="Comma regions (Pinnacle is usually 'eu').")
    ap.add_argument("--book", default="pinnacle", help="Preferred (sharp) bookmaker key.")
    ap.add_argument("--p1", help="Player 1 name filter (matches the event outcome name).")
    ap.add_argument("--p2", help="Player 2 name filter.")
    ap.add_argument("--out", help="Write player,decimal_odds CSV here.")
    ap.add_argument("--selftest", action="store_true", help="Run the offline parser test and exit.")
    args = ap.parse_args()

    if args.selftest:
        return _selftest()
    if not args.api_key:
        raise SystemExit("set THE_ODDS_API_KEY or pass --api-key (or use --selftest offline)")

    if args.list_sports:
        data, hdr = _get(f"{API}/sports/?apiKey={args.api_key}")
        tennis = [s for s in data if "tennis" in str(s.get("key", "")).lower()]
        print(f"quota: used={hdr['used']} remaining={hdr['remaining']}")
        for s in tennis:
            flag = "ACTIVE" if s.get("active") else "      "
            print(f"  [{flag}] {s.get('key'):32s} {s.get('title')}")
        if not tennis:
            print("  (no tennis sport keys returned — likely no tournament in progress)")
        return 0

    if not args.sport:
        raise SystemExit("provide --sport (see --list-sports) or --list-sports")

    q = urllib.parse.urlencode(
        {"apiKey": args.api_key, "regions": args.regions, "markets": "h2h", "oddsFormat": "decimal"}
    )
    events, hdr = _get(f"{API}/sports/{args.sport}/odds/?{q}")
    print(f"quota: used={hdr['used']} remaining={hdr['remaining']} ; {len(events)} events")

    if args.p1 or args.p2:
        wanted = {_norm(args.p1), _norm(args.p2)} - {""}
        match = None
        for ev in events:
            names = {_norm(ev.get("home_team")), _norm(ev.get("away_team"))}
            if wanted & names:
                match = ev
                break
        if match is None:
            print("no event matched; available matchups:")
            for ev in events:
                print(f"  {ev.get('home_team')} vs {ev.get('away_team')}")
            return 2
        odds = extract_pair_odds(match, args.p1, args.p2, prefer_book=args.book)
        print(f"matched: {match.get('home_team')} vs {match.get('away_team')} (book pref={args.book})")
        for name, price in odds.items():
            print(f"  {name}: {price:.4f}")
        if args.out:
            _write_csv(args.out, odds)
            print(f"wrote {args.out}")
        return 0

    # No filter: dump every matchup with preferred-book odds.
    for ev in events:
        odds = extract_pair_odds(ev, None, None, prefer_book=args.book)
        pretty = " | ".join(f"{n} {p:.2f}" for n, p in odds.items())
        print(f"  {ev.get('home_team')} vs {ev.get('away_team')}: {pretty}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
