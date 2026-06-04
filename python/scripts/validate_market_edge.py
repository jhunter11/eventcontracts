"""Read-only edge validation across the two liquid Kalshi series we found.

NO orders. Computes an independent fair value per market and compares to the live
Kalshi mid, to see WHERE the bigger, more consistent mispricing is.

WEATHER (KXHIGHNY/CHI/MIA): Open-Meteo hourly forecast -> repo
  TemperatureThresholdModel -> expected_high & uncertainty -> normal-CDF price of
  the exact strike (handles greater / less / between integer-degree brackets).
CRYPTO (KXBTCD): live BTC spot + hourly realized vol -> lognormal P(close>K) at
  the market's settlement time.

Writes JSON to .val.json (read deterministically). Skip reasons are counted so
nothing is silently dropped.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import sys
import time
import urllib.request
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "python" / "src"))

from eventcontracts.domain.models import InstrumentId, Venue  # noqa: E402
from eventcontracts.weather.temperature import (  # noqa: E402
    TemperatureThresholdMarket,
    TemperatureThresholdModel,
    WeatherLocation,
    snapshot_from_open_meteo_payload,
)

H = "https://api.elections.kalshi.com/trade-api/v2"
CITIES = {
    "KXHIGHNY": WeatherLocation(name="NYC", latitude=40.78, longitude=-73.97, timezone="UTC"),
    "KXHIGHCHI": WeatherLocation(name="CHI", latitude=41.79, longitude=-87.75, timezone="UTC"),
    "KXHIGHMIA": WeatherLocation(name="MIA", latitude=25.79, longitude=-80.29, timezone="UTC"),
}


def get(url, tries=5):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except Exception as e:  # noqa: BLE001
            if "429" in str(e) and i < tries - 1:
                time.sleep(2 + 2 * i)
                continue
            raise
    return {}


def fnum(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def active(series):
    out, cursor = [], None
    for _ in range(12):
        url = f"{H}/markets?limit=200&series_ticker={series}&status=open" + (f"&cursor={cursor}" if cursor else "")
        d = get(url)
        out += [m for m in d.get("markets", []) if m.get("status") == "active"]
        cursor = d.get("cursor")
        cursor = str(cursor) if cursor else None
        if not cursor:
            break
        time.sleep(0.3)
    return out


def mid_of(m):
    yb, ya = fnum(m.get("yes_bid_dollars")), fnum(m.get("yes_ask_dollars"))
    if yb <= 0 or ya <= 0 or ya >= 1 or ya < yb:
        return None, yb, ya
    return (yb + ya) / 2.0, yb, ya


def ncdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def close_dt(m):
    try:
        return dt.datetime.fromisoformat(str(m.get("close_time")).replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return None


def weather_edges():
    rows, skips = [], Counter()
    model = TemperatureThresholdModel()
    now = dt.datetime.now(dt.UTC)
    for series, loc in CITIES.items():
        try:
            payload = get(
                "https://api.open-meteo.com/v1/forecast?"
                f"latitude={loc.latitude}&longitude={loc.longitude}"
                "&hourly=temperature_2m,cloud_cover,precipitation_probability,wind_speed_10m,relative_humidity_2m"
                "&temperature_unit=fahrenheit&wind_speed_unit=mph&forecast_days=3&timezone=UTC"
            )
            snap = snapshot_from_open_meteo_payload(payload, location=loc, as_of=now)
        except Exception as e:  # noqa: BLE001
            skips[f"{series}:forecast_fetch"] += 1
            continue
        for m in active(series):
            mid, yb, ya = mid_of(m)
            if mid is None:
                skips["no_two_sided"] += 1
                continue
            ct = close_dt(m)
            if ct is None or (ct - now).total_seconds() > 3 * 86400 or ct < now:
                skips["outside_forecast_horizon"] += 1
                continue
            tday = ct.date()  # settlement day (close is ~04:00Z next day; high is that local day)
            # Kalshi high is reported for the local calendar day; close is 03:59Z next day.
            tday = (ct - dt.timedelta(hours=5)).date()
            # Single predict to get expected_high + uncertainty for the day.
            iid = InstrumentId(venue=Venue.KALSHI, market_id=str(m.get("ticker")), outcome_id="yes")
            probe = TemperatureThresholdMarket(instrument_id=iid, threshold_f=70.0, target_day=tday, direction="above")
            try:
                pred = model.predict(snap, probe)
            except Exception:  # noqa: BLE001
                skips["no_points_for_day"] += 1
                continue
            mu, sd = pred.expected_high_f, pred.uncertainty_f
            st = m.get("strike_type")
            floor, cap = m.get("floor_strike"), m.get("cap_strike")

            def p_above(thr):  # P(integer high >= thr)  ~ P(continuous > thr-0.5)
                return 1.0 - ncdf((thr - 0.5 - mu) / sd)

            if st == "greater" and floor is not None:
                fair = p_above(float(floor) + 1)        # "floor+1 or above"
            elif st == "less" and cap is not None:
                fair = 1.0 - p_above(float(cap) + 1)     # "cap or below"
            elif st == "between" and floor is not None and cap is not None:
                fair = p_above(float(floor)) - p_above(float(cap) + 1)  # high in {floor..cap}
            else:
                skips["unknown_strike"] += 1
                continue
            fair = max(0.0, min(1.0, fair))
            rows.append({
                "series": series, "ticker": m.get("ticker"), "strike_type": st,
                "fair": round(fair, 3), "mid": round(mid, 3), "yb": yb, "ya": ya,
                "edge_c": round((fair - mid) * 100, 1),
                "spread_c": round((ya - yb) * 100, 1),
                "v24": int(fnum(m.get("volume_24h_fp"))),
                "mu": round(mu, 1), "sd": round(sd, 2),
            })
    return rows, skips


def btc_spot_vol():
    # Kraken public OHLC (Coinbase 403 / Binance 451 geo-blocked here).
    d = get("https://api.kraken.com/0/public/OHLC?pair=XBTUSD&interval=60")
    result = d.get("result", {})
    key = next(k for k in result if k != "last")
    rows = result[key]  # [time, open, high, low, close, vwap, vol, count], oldest first
    closes = [float(r[4]) for r in rows][-240:]
    spot = closes[-1]
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    n = len(rets)
    mean = sum(rets) / n
    sd = math.sqrt(sum((r - mean) ** 2 for r in rets) / (n - 1))
    return spot, sd


def crypto_edges():
    rows, skips = [], Counter()
    spot, sig_h = btc_spot_vol()
    now = dt.datetime.now(dt.UTC)
    for m in active("KXBTCD"):
        mid, yb, ya = mid_of(m)
        if mid is None:
            skips["no_two_sided"] += 1
            continue
        ct = close_dt(m)
        if ct is None or ct < now or (ct - now).total_seconds() > 2 * 86400:
            skips["outside_horizon"] += 1
            continue
        tau_h = max((ct - now).total_seconds() / 3600.0, 0.05)
        sig_T = sig_h * math.sqrt(tau_h)
        st, floor, cap = m.get("strike_type"), m.get("floor_strike"), m.get("cap_strike")
        if st == "greater" and floor is not None:
            K = float(floor)
            fair = ncdf((math.log(spot / K) - 0.5 * sig_T ** 2) / sig_T)
        elif st == "less" and cap is not None:
            K = float(cap)
            fair = 1.0 - ncdf((math.log(spot / K) - 0.5 * sig_T ** 2) / sig_T)
        elif st == "between" and floor is not None and cap is not None:
            a = ncdf((math.log(spot / float(cap)) - 0.5 * sig_T ** 2) / sig_T)
            b = ncdf((math.log(spot / float(floor)) - 0.5 * sig_T ** 2) / sig_T)
            fair = max(0.0, b - a)
        else:
            skips["unknown_strike"] += 1
            continue
        fair = max(0.0, min(1.0, fair))
        rows.append({
            "series": "KXBTCD", "ticker": m.get("ticker"), "strike_type": st,
            "fair": round(fair, 3), "mid": round(mid, 3), "yb": yb, "ya": ya,
            "edge_c": round((fair - mid) * 100, 1),
            "spread_c": round((ya - yb) * 100, 1),
            "tau_h": round(tau_h, 1), "v24": int(fnum(m.get("volume_24h_fp"))),
        })
    return rows, skips, spot, sig_h


def main():
    result = {}
    w_rows, w_skips = weather_edges()
    result["weather"] = {"rows": w_rows, "skips": dict(w_skips)}
    c_rows, c_skips, spot, sig = crypto_edges()
    result["crypto"] = {"rows": c_rows, "skips": dict(c_skips), "spot": spot, "sigma_hourly": sig}
    Path(".val.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    print("WROTE .val.json")
    print("weather priced:", len(w_rows), "skips:", dict(w_skips))
    print("crypto  priced:", len(c_rows), "skips:", dict(c_skips))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
