"""Phase-1 data-integrity gate: does GHCND TMAX reproduce Kalshi's KXHIGH settlement?

Kalshi settles KXHIGH on the NWS Climatological Report (Daily), an integer-°F high.
Our calibration is FIT on, and the paper ledger is SETTLED on, NOAA GHCND TMAX
(stored tenths-°C, requested units=standard → °F). They are the same underlying
observation, but the tenths-°C → °F round-trip can differ by ±1 °F, and with 1°-wide
brackets settling on strict integer comparisons a ±1° gap flips outcomes. If the
label is wrong, every model/edge result downstream is wrong — so verify it first.

Method (Kalshi's own settled brackets ARE the CLI ground truth):
  * For each settled KXHIGH bracket, recompute the YES/NO that GHCND TMAX would give
    via the exact settlement logic (greater→hi≥F+1, less→hi≤C−1, between→F≤hi≤C) and
    compare to Kalshi's actual `result`. Any disagreement = GHCND ≠ CLI for that day.
  * Where the bracket ladder pins the settled high to one integer, also compare that
    integer to round(GHCND TMAX) head-to-head.

Read-only (auth GET to Kalshi + NOAA CDO). Prints a per-station report; nonzero exit
if any bracket disagreement is found (so it can gate deployment / a kill-switch).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[2]
for _line in (ROOT / ".env").read_text().splitlines():
    _line = _line.strip()
    if _line and not _line.startswith("#") and "=" in _line:
        _k, _v = _line.split("=", 1)
        os.environ.setdefault(_k, _v.strip().strip('"').strip("'"))
sys.path.insert(0, str(ROOT / "python" / "src"))

from eventcontracts.adapters.venues.kalshi.client import KalshiPublicClient  # noqa: E402
from eventcontracts.weather.kxhigh import KXHIGH_STATIONS  # noqa: E402


def ghcnd_high(station_id: str, day: date, token: str) -> float | None:
    """NOAA GHCND TMAX for one station/day in °F (units=standard). Same call the
    calibration dataset builder and the paper-ledger settler use."""
    url = "https://www.ncdc.noaa.gov/cdo-web/api/v2/data?" + urllib.parse.urlencode({
        "datasetid": "GHCND", "stationid": f"GHCND:{station_id}", "datatypeid": "TMAX",
        "startdate": day.isoformat(), "enddate": day.isoformat(), "units": "standard", "limit": 5,
    })
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "token": token})
    with urllib.request.urlopen(req, timeout=40) as r:
        d = json.loads(r.read())
    for row in d.get("results", []):
        return float(row.get("value"))
    return None


def _implied_result(strike_type: str, floor: float | None, cap: float | None, hi: int) -> str | None:
    """YES/NO this bracket would settle at integer high `hi`, via the exact rules."""
    if strike_type == "greater" and floor is not None:
        return "yes" if hi >= int(floor) + 1 else "no"
    if strike_type == "less" and cap is not None:
        return "yes" if hi <= int(cap) - 1 else "no"
    if strike_type == "between" and floor is not None and cap is not None:
        return "yes" if int(floor) <= hi <= int(cap) else "no"
    return None


def _kalshi_high_bounds(brackets: list[dict]) -> tuple[int, int]:
    """Tightest [lo, hi] integer bound on the settled high implied by all results."""
    lo, hi = -999, 999
    for b in brackets:
        st, f, cap, res = b["strike_type"], b["floor"], b["cap"], b["result"]
        yes = res == "yes"
        if st == "greater" and f is not None:
            lo = max(lo, int(f) + 1) if yes else lo
            hi = hi if yes else min(hi, int(f))
        elif st == "less" and cap is not None:
            hi = min(hi, int(cap) - 1) if yes else hi
            lo = lo if yes else max(lo, int(cap))
        elif st == "between" and f is not None and cap is not None and yes:
            lo, hi = max(lo, int(f)), min(hi, int(cap))
    return lo, hi


async def _settled_markets(c: KalshiPublicClient, series: str) -> list[dict]:
    out: list[dict] = []
    cursor = None
    while True:
        r = await c.get_markets_payload(series_ticker=series, status="settled", limit=1000, cursor=cursor)
        ms = r.get("markets", [])
        out.extend(ms)
        cursor = r.get("cursor")
        if not cursor or not ms:
            break
    return out


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--max-days", type=int, default=45, help="most recent GHCND-available days per station")
    ap.add_argument("--lag-days", type=int, default=4, help="skip the last N days (GHCND publication lag)")
    args = ap.parse_args()

    token = os.getenv("NOAA_TOKEN", "")
    if not token:
        print("NOAA_TOKEN not in .env; cannot reconcile")
        return 2
    c = KalshiPublicClient.from_env()
    cutoff = date.today().toordinal() - args.lag_days

    total_brackets = total_disagree = total_pinned = total_pin_mismatch = 0
    for series, (_code, loc) in KXHIGH_STATIONS.items():
        markets = await _settled_markets(c, series)
        bydate: dict[str, list[dict]] = defaultdict(list)
        for m in markets:
            parts = m.get("ticker", "").split("-")
            if len(parts) < 3 or m.get("result") not in ("yes", "no"):
                continue
            try:
                d = datetime.strptime(parts[1], "%y%b%d").date()
            except ValueError:
                continue
            bydate[d.isoformat()].append({
                "ticker": m["ticker"], "strike_type": m.get("strike_type"),
                "floor": m.get("floor_strike"), "cap": m.get("cap_strike"), "result": m["result"],
            })
        days = [d for d in sorted(bydate) if date.fromisoformat(d).toordinal() <= cutoff][-args.max_days:]
        print(f"\n=== {series} ({loc.name}, GHCND {loc.station_id}) — {len(days)} settled days checked ===")
        s_brackets = s_disagree = s_pinned = s_pinmis = 0
        mism_days: list[str] = []
        for d in days:
            try:
                raw = ghcnd_high(loc.station_id, date.fromisoformat(d), token)
            except Exception:  # noqa: BLE001
                raw = None
            time.sleep(0.2)  # NOAA politeness
            if raw is None:
                continue
            ghi = round(raw)
            brackets = bydate[d]
            day_dis = 0
            for b in brackets:
                imp = _implied_result(b["strike_type"], b["floor"], b["cap"], ghi)
                if imp is None:
                    continue
                s_brackets += 1
                if imp != b["result"]:
                    s_disagree += 1
                    day_dis += 1
            lo, hi = _kalshi_high_bounds(brackets)
            pinned = lo == hi and lo > -900
            if pinned:
                s_pinned += 1
                if lo != ghi:
                    s_pinmis += 1
                    mism_days.append(f"{d}: Kalshi_high={lo} GHCND={ghi} (raw {raw})")
            if day_dis:
                mism_days.append(f"{d}: {day_dis} bracket disagreement(s), GHCND_hi={ghi} (raw {raw})")
        print(f"  brackets checked={s_brackets}  outcome disagreements={s_disagree}  "
              f"({0 if not s_brackets else s_disagree / s_brackets:.1%})")
        print(f"  exact-pinned days={s_pinned}  integer mismatches={s_pinmis}")
        for line in mism_days[:20]:
            print(f"    ! {line}")
        total_brackets += s_brackets
        total_disagree += s_disagree
        total_pinned += s_pinned
        total_pin_mismatch += s_pinmis

    print("\n=== OVERALL ===")
    print(f"  brackets checked={total_brackets}  outcome disagreements={total_disagree} "
          f"({0 if not total_brackets else total_disagree / total_brackets:.2%})")
    print(f"  exact-pinned days={total_pinned}  integer mismatches={total_pin_mismatch}")
    if total_disagree == 0 and total_pin_mismatch == 0:
        print("  VERDICT: GHCND TMAX reproduces Kalshi/CLI settlement exactly — label is correct.")
        return 0
    print("  VERDICT: GHCND ≠ CLI on some days — settle/fit off the CLI value, not GHCND. **FIX NEEDED**")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
