"""Phase 0b: build the (forecast -> actual settlement high) dataset.

For each Kalshi weather station, pull:
  * Open-Meteo HISTORICAL-FORECAST archive (what the model would have seen) ->
    daily max of hourly temperature_2m, in LOCAL time (Kalshi/NWS settle on the
    local calendar day high).
  * Open-Meteo ENSEMBLE archive is not historical; for spread we use the live
    ensemble only at predict time. Here we also pull the simple model daily high.
  * NOAA GHCND TMAX (units=standard, whole degrees F) -> the ACTUAL value the
    contract settles on.

Writes one CSV per station to data/weather-calib/<station>.csv with columns:
  date, station, om_archive_high_f, ghcnd_tmax_f, bias_f (actual - forecast)

This is the ground-truth dataset the calibration (Phase 1) fits on. Read-only
external calls; no trading. NOAA token from .env (NOAA_TOKEN).
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "data" / "weather-calib"
OUT.mkdir(parents=True, exist_ok=True)

# Kalshi settlement stations: (GHCND id, lat, lon, IANA tz, label)
STATIONS = {
    "NY": ("USW00094728", 40.7790, -73.9693, "America/New_York", "NYC Central Park"),
    "CHI": ("USW00014819", 41.7860, -87.7524, "America/Chicago", "Chicago Midway"),
    "MIA": ("USW00012839", 25.7906, -80.3164, "America/New_York", "Miami Intl"),
}
OM_ARCHIVE = "https://historical-forecast-api.open-meteo.com/v1/forecast"
NOAA = "https://www.ncdc.noaa.gov/cdo-web/api/v2/data"


def _token() -> str:
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith("NOAA_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"').strip()
    return os.getenv("NOAA_TOKEN", "")


def get(url: str, headers: dict | None = None, tries: int = 5):
    h = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    if headers:
        h.update(headers)
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=45) as r:
                return json.loads(r.read())
        except Exception as e:  # noqa: BLE001
            last = e
            code = getattr(e, "code", None)
            if code in (429, 503, 502, 500) and i < tries - 1:
                time.sleep(3 + 3 * i)
                continue
            if i < tries - 1:
                time.sleep(2)
                continue
            raise
    raise last  # type: ignore[misc]


def om_archive_daily_high(lat, lon, tz, start, end) -> dict[str, float]:
    params = urllib.parse.urlencode(
        {
            "latitude": lat,
            "longitude": lon,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "hourly": "temperature_2m",
            "temperature_unit": "fahrenheit",
            "timezone": tz,
        }
    )
    d = get(f"{OM_ARCHIVE}?{params}")
    h = d.get("hourly", {})
    times, temps = h.get("time", []), h.get("temperature_2m", [])
    daymax: dict[str, float] = defaultdict(lambda: -999.0)
    for t, v in zip(times, temps):
        if v is None:
            continue
        day = t[:10]
        if v > daymax[day]:
            daymax[day] = v
    return {k: round(v, 2) for k, v in daymax.items() if v > -900}


def ghcnd_tmax(station_id, token, start, end) -> dict[str, float]:
    out: dict[str, float] = {}
    # NOAA caps ~1yr/1000 rows per call; chunk by year.
    cur = start
    while cur <= end:
        chunk_end = min(dt.date(cur.year, 12, 31), end)
        params = urllib.parse.urlencode(
            {
                "datasetid": "GHCND",
                "stationid": f"GHCND:{station_id}",
                "datatypeid": "TMAX",
                "startdate": cur.isoformat(),
                "enddate": chunk_end.isoformat(),
                "units": "standard",
                "limit": 1000,
            }
        )
        d = get(f"{NOAA}?{params}", headers={"token": token})
        for r in d.get("results", []):
            out[str(r.get("date"))[:10]] = float(r.get("value"))
        time.sleep(0.5)
        cur = dt.date(chunk_end.year + 1, 1, 1)
    return out


def main() -> int:
    token = _token()
    if not token:
        print("ERROR: NOAA_TOKEN not found in .env")
        return 2
    years = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    end = dt.date(2026, 5, 27)  # GHCND max date observed
    start = dt.date(end.year - years, end.month, end.day)
    print(f"building calibration dataset {start} .. {end} ({years}y) for {list(STATIONS)}")

    for code, (sid, lat, lon, tz, label) in STATIONS.items():
        print(f"\n[{code}] {label} ({sid}) ...")
        try:
            fc = om_archive_daily_high(lat, lon, tz, start, end)
            print(f"  open-meteo archive days: {len(fc)}")
            act = ghcnd_tmax(sid, token, start, end)
            print(f"  ghcnd actual days:       {len(act)}")
        except Exception as e:  # noqa: BLE001
            print(f"  FAILED: {type(e).__name__}: {str(e)[:120]}")
            continue
        rows = []
        for day in sorted(set(fc) & set(act)):
            f, a = fc[day], act[day]
            rows.append((day, code, f, a, round(a - f, 2)))
        path = OUT / f"{code}.csv"
        with path.open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["date", "station", "om_archive_high_f", "ghcnd_tmax_f", "bias_f"])
            w.writerows(rows)
        biases = [r[4] for r in rows]
        if biases:
            mean_b = sum(biases) / len(biases)
            sd_b = (sum((b - mean_b) ** 2 for b in biases) / max(1, len(biases) - 1)) ** 0.5
            print(f"  paired days={len(rows)}  mean_bias={mean_b:+.2f}F  sd={sd_b:.2f}F  -> {path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
