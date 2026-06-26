"""S1 high-so-far sniper poller for KXHIGH (read-only, no orders).

For each KXHIGH series it pulls the official **NWS station observations**, computes
the observed daily-high-so-far (ground truth, F), then walks the live Kalshi
KXHIGH book and flags every bracket the running max has ALREADY locked — buying
the guaranteed-winning side only when the price still nets edge after the Kalshi
fee. This is a deterministic free-roll scan, distinct from
``weather_kxhigh_paper.py``'s calibrated *forecast* pricing: it asserts certainty
only from real observations (a P=1.0 you can size into), never a model proxy.

Read-only: prints a table, and with ``--record`` appends paper entries to a JSONL
ledger. It never places an order. All decision logic lives in
``eventcontracts.weather.snipe`` (unit-tested); this is a thin IO wrapper.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "python" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from eventcontracts.weather.kxhigh import KXHIGH_STATIONS, parse_kxhigh_market  # noqa: E402
from eventcontracts.weather.snipe import (  # noqa: E402
    NWS_STATIONS,
    observed_daily_high_f,
    s1_signal,
)
from weather_kxhigh_paper import _opt_f, open_markets  # noqa: E402

NWS = "https://api.weather.gov"
UA = "eventcontracts/0.1 (s1 snipe; research)"


def nws_observations(station_id: str, *, limit: int = 24) -> dict:
    """Latest NWS observations GeoJSON for a station (keyless, UA required)."""
    url = f"{NWS}/stations/{station_id}/observations?" + urllib.parse.urlencode({"limit": limit})
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/geo+json"})
    with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 (fixed https host)
        return json.loads(r.read())


def main() -> int:
    ap = argparse.ArgumentParser(description="KXHIGH S1 high-so-far deterministic sniper (read-only)")
    ap.add_argument("--record", type=Path, default=None,
                    help="append locked-snipe paper entries to this JSONL ledger")
    ap.add_argument("--min-edge", type=float, default=0.01,
                    help="min net edge/contract to flag, dollars (default 0.01 = 1c)")
    args = ap.parse_args()

    today = datetime.now().date()
    now = datetime.now(UTC)
    print("=== KXHIGH S1 HIGH-SO-FAR SNIPER (NWS ground truth, read-only) ===")
    print(f"now={now.isoformat()}  today={today}  min_edge=${args.min_edge:.3f}\n")

    records: list[dict] = []
    for series, station_id in NWS_STATIONS.items():
        code, loc = KXHIGH_STATIONS[series]
        try:
            obs = nws_observations(station_id)
            markets = open_markets(series)
        except Exception as exc:  # noqa: BLE001
            print(f"[{series}] fetch failed: {type(exc).__name__}: {str(exc)[:80]}")
            continue
        high = observed_daily_high_f(obs, target_day=today, timezone=loc.timezone)
        if high is None:
            print(f"[{series}] {station_id}: no observation for {today} yet")
            continue
        print(f"[{series}] {loc.name} station={station_id} observed_high_so_far={high:.1f}F")
        locks = 0
        for m in markets:
            c = parse_kxhigh_market(m)
            if c is None or c.target_day != today:
                continue  # only today's book can be locked by today's running max
            sig = s1_signal(
                c, high,
                yes_bid=_opt_f(m.get("yes_bid_dollars")),
                yes_ask=_opt_f(m.get("yes_ask_dollars")),
                min_edge=args.min_edge,
            )
            if sig is None:
                continue
            locks += 1
            print(f"   LOCK {sig.side:3s} {sig.ticker}  fill={sig.fill_price:.2f} "
                  f"fee={sig.fee:.4f} edge={sig.edge:+.4f}")
            records.append(sig.as_record(as_of=now))
        if locks == 0:
            print("   no locked brackets at the current book")

    if args.record is not None and records:
        args.record.parent.mkdir(parents=True, exist_ok=True)
        with args.record.open("a", encoding="utf-8") as fh:
            for rec in records:
                fh.write(json.dumps(rec) + "\n")
        print(f"\nrecorded {len(records)} snipe(s) -> {args.record}")
    print(f"\n{len(records)} locked snipe(s) total")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
