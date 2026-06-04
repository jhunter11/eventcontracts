"""Paper step: price the LIVE KXHIGH books with the proven station calibration.

Read-only, no orders. For each KXHIGH{NY,CHI,MIA} open bracket it:
  1. pulls the live Open-Meteo forecast for the settlement station (local tz) and
     computes the daily-high the SAME way the calibration was fit (local-calendar
     -day max of hourly temperature_2m), then
  2. prices the bracket's YES probability via the fitted StationCalibration
     (the exact distribution the walk-forward gate validated), and
  3. compares that calibrated fair value to the live Kalshi mid, netting the
     Kalshi trading fee (~0.07*p*(1-p)/contract), and flags brackets that are
     genuinely two-sided + tradeable.

This is the necessary pre-capital check the calibration gate cannot give: the
gate proves good probabilities vs ground truth; THIS measures whether those
probabilities disagree with the market enough to trade after fees. One run is
not proof of realized edge; the `--record` / `--settle` loop is the durable
CLV + fee-net PnL evidence collector.

Honest caveat printed inline: the calibration sigma was fit on the historical-
forecast archive (≈nowcast lead). Live next-day brackets carry more forecast
error than that sigma, so same-day (lead=0) edges are the trustworthy ones.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

_reconfigure_stdout = getattr(sys.stdout, "reconfigure", None)
if _reconfigure_stdout is not None:
    _reconfigure_stdout(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "python" / "src"))

from eventcontracts.weather.calibration import StationCalibration, load_calibrations  # noqa: E402
from eventcontracts.weather.distribution import (  # noqa: E402
    DailyHighDistribution,
    StationObservationSnapshot,
    build_daily_high_distribution,
    probability_for_contract,
)
from eventcontracts.weather.kxhigh import KXHIGH_STATIONS, parse_kxhigh_market  # noqa: E402
from eventcontracts.weather.temperature import WeatherLocation, snapshot_from_open_meteo_payload  # noqa: E402

KALSHI = "https://external-api.kalshi.com/trade-api/v2"
OPEN_METEO = "https://api.open-meteo.com/v1/forecast"
CALIB_PATH = ROOT / "configs" / "weather" / "station_calibrations.json"


@dataclass(frozen=True)
class ForecastDailyState:
    payload: dict[str, Any]
    highs: dict[str, float]
    high_so_far: dict[str, float]
    as_of: datetime


def _get(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.loads(r.read())


def _f(x: object) -> float:
    try:
        return float(x)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def _opt_f(x: object) -> float | None:
    try:
        return float(x)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _market_time(value: object) -> datetime | None:
    if isinstance(value, int | float):
        return datetime.fromtimestamp(value, tz=UTC)
    if not isinstance(value, str) or not value:
        return None
    if value.isdigit():
        return datetime.fromtimestamp(int(value), tz=UTC)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def forecast_daily_highs(lat: float, lon: float, tz: str) -> dict[str, float]:
    """Local-calendar-day max of hourly temperature_2m (F), keyed YYYY-MM-DD.

    Mirrors weather_build_calibration_dataset.py so the forecast input matches
    what the calibration bias/sigma were fit against."""
    return forecast_daily_state(lat, lon, tz).highs


def forecast_daily_state(lat: float, lon: float, tz: str, *, as_of: datetime | None = None) -> ForecastDailyState:
    """Fetch Open-Meteo daily highs plus a same-day high-so-far proxy."""

    params = urllib.parse.urlencode(
        {
            "latitude": lat,
            "longitude": lon,
            "hourly": "temperature_2m",
            "temperature_unit": "fahrenheit",
            "timezone": tz,
            "forecast_days": 3,
            "past_days": 1,
        }
    )
    d = _get(f"{OPEN_METEO}?{params}")
    now = as_of or datetime.now(UTC)
    h = d.get("hourly", {})
    daymax: dict[str, float] = defaultdict(lambda: -999.0)
    high_so_far: dict[str, float] = defaultdict(lambda: -999.0)
    local_now = _payload_local_now(d, now)
    for t, v in zip(h.get("time", []), h.get("temperature_2m", []), strict=False):
        temperature = _opt_f(v)
        if temperature is None:
            continue
        day = str(t)[:10]
        if temperature > daymax[day]:
            daymax[day] = temperature
        if _open_meteo_local_time_is_observed(str(t), local_now) and temperature > high_so_far[day]:
            high_so_far[day] = temperature
    return ForecastDailyState(
        payload=d,
        highs={k: round(v, 2) for k, v in daymax.items() if v > -900},
        high_so_far={k: round(v, 2) for k, v in high_so_far.items() if v > -900},
        as_of=now,
    )


def _payload_local_now(payload: dict[str, Any], as_of: datetime) -> datetime | None:
    offset = payload.get("utc_offset_seconds")
    if offset is None:
        return None
    try:
        return (as_of + timedelta(seconds=int(offset))).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


def _open_meteo_local_time_is_observed(raw_time: str, local_now: datetime | None) -> bool:
    if local_now is None:
        return False
    try:
        parsed = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
    except ValueError:
        return False
    if parsed.tzinfo is not None:
        parsed = parsed.replace(tzinfo=None)
    return parsed <= local_now


def open_markets(series: str) -> list[dict]:
    out: list[dict] = []
    cursor: str | None = None
    for _ in range(5):
        url = f"{KALSHI}/markets?" + urllib.parse.urlencode(
            {"series_ticker": series, "status": "open", "limit": 200, **({"cursor": cursor} if cursor else {})}
        )
        d = _get(url)
        out.extend(d.get("markets", []) or [])
        cursor = d.get("cursor")
        if not cursor:
            break
    return out


def market_payload(ticker: str) -> dict[str, Any]:
    d = _get(f"{KALSHI}/markets/{urllib.parse.quote(ticker)}")
    market = d.get("market")
    return market if isinstance(market, dict) else d


def historical_candlesticks(
    ticker: str,
    *,
    start_ts: int,
    end_ts: int,
    period_interval: int = 1,
) -> list[dict[str, Any]]:
    params = urllib.parse.urlencode(
        {
            "start_ts": start_ts,
            "end_ts": end_ts,
            "period_interval": period_interval,
        }
    )
    d = _get(f"{KALSHI}/historical/markets/{urllib.parse.quote(ticker)}/candlesticks?{params}")
    candles = d.get("candlesticks")
    return [c for c in candles if isinstance(c, dict)] if isinstance(candles, list) else []


def _market_close_time(entry: dict[str, Any]) -> datetime | None:
    close_time = _market_time(entry.get("close_time") or entry.get("close_ts"))
    if close_time is not None:
        return close_time
    try:
        market = market_payload(str(entry["ticker"]))
    except Exception:  # noqa: BLE001
        return None
    return _market_time(market.get("close_time") or market.get("close_ts"))


def _candle_time(candle: dict[str, Any]) -> int | None:
    raw = candle.get("end_period_ts") or candle.get("end_ts") or candle.get("ts")
    try:
        return int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _candle_close(candle: dict[str, Any], key: str) -> float | None:
    raw = candle.get(key)
    if not isinstance(raw, dict):
        return None
    for field in ("close_dollars", "close", "mean_dollars", "mean"):
        value = _opt_f(raw.get(field))
        if value is not None:
            return value
    return None


def closing_yes_mid(ticker: str, close_time: datetime, *, window_minutes: int = 90) -> float | None:
    """Last two-sided YES midpoint before close, from Kalshi minute candles."""
    close_ts = int(close_time.timestamp())
    start_ts = int((close_time - timedelta(minutes=window_minutes)).timestamp())
    candles = historical_candlesticks(ticker, start_ts=start_ts, end_ts=close_ts + 120, period_interval=1)
    best: tuple[int, float] | None = None
    for candle in candles:
        ts = _candle_time(candle)
        bid = _candle_close(candle, "yes_bid")
        ask = _candle_close(candle, "yes_ask")
        if ts is None or bid is None or ask is None:
            continue
        if ts > close_ts or not (0.0 < bid <= ask < 1.0):
            continue
        mid = (bid + ask) / 2.0
        if best is None or ts > best[0]:
            best = (ts, mid)
    return best[1] if best is not None else None


def _bracket_center(c) -> float | None:
    """Representative temperature for a bracket, for market-center/spread estimates.
    between -> midpoint; less cap=C (YES high<=C-1) -> C-2; greater floor=F
    (YES high>=F+1) -> F+2 (one bracket-width into the open-ended tail)."""
    if c.strike_type == "between" and c.floor_strike is not None and c.cap_strike is not None:
        return (c.floor_strike + c.cap_strike) / 2.0
    if c.strike_type == "less" and c.cap_strike is not None:
        return c.cap_strike - 2.0
    if c.strike_type == "greater" and c.floor_strike is not None:
        return c.floor_strike + 2.0
    return None


def _kxhigh_distribution(
    *,
    state: ForecastDailyState,
    location: WeatherLocation,
    target_day: date,
    station_code: str,
    calibration: StationCalibration,
    high_so_far_f: float | None,
) -> DailyHighDistribution:
    snapshot = snapshot_from_open_meteo_payload(state.payload, location=location, as_of=state.as_of)
    observation = (
        StationObservationSnapshot(
            station_code=station_code,
            target_day=target_day,
            observed_high_f=high_so_far_f,
            as_of=state.as_of,
            source="open_meteo_hourly_proxy",
        )
        if high_so_far_f is not None
        else None
    )
    return build_daily_high_distribution(snapshot, target_day, calibration, observation=observation)


def kalshi_fee(price: float, contracts: int = 1) -> float:
    """Kalshi general trading fee ~ ceil(0.07 * C * p * (1-p)) cents; per-contract
    dollars here (no rounding) for a marginal-edge comparison."""
    return 0.07 * price * (1.0 - price) * contracts


def _side_price_from_yes(yes_price: float, side: str) -> float:
    return yes_price if side == "YES" else 1.0 - yes_price


def _entry_fill_price(entry: dict[str, Any]) -> float:
    return float(entry.get("fill_price", entry.get("entry_price", 0.0)))


def _entry_size(entry: dict[str, Any]) -> int:
    return int(entry.get("size", 1))


def _entry_fee(entry: dict[str, Any]) -> float:
    existing = _opt_f(entry.get("fee"))
    if existing is not None:
        return existing
    return kalshi_fee(_entry_fill_price(entry), _entry_size(entry))


def _attach_clv(entry: dict[str, Any], closing_yes_mid_value: float) -> None:
    side_mid = _side_price_from_yes(closing_yes_mid_value, str(entry["side"]))
    fill = _entry_fill_price(entry)
    clv_per_contract = side_mid - fill
    entry["market_yes_mid_near_close"] = round(closing_yes_mid_value, 4)
    entry["market_mid_near_close"] = round(side_mid, 4)
    entry["clv_per_contract"] = round(clv_per_contract, 4)
    entry["clv"] = round(clv_per_contract * _entry_size(entry), 4)


def _entry_yes_result(entry: dict[str, Any], high_f: float) -> bool:
    hi = round(high_f)
    floor_s, cap_s = entry.get("floor_strike"), entry.get("cap_strike")
    st = entry["strike_type"]
    if st == "greater":
        if floor_s is None:
            raise ValueError("greater entry missing floor_strike")
        return hi >= int(float(floor_s)) + 1
    if st == "less":
        if cap_s is None:
            raise ValueError("less entry missing cap_strike")
        return hi <= int(float(cap_s)) - 1
    if floor_s is None or cap_s is None:
        raise ValueError("between entry missing floor/cap strike")
    return int(float(floor_s)) <= hi <= int(float(cap_s))


def _entry_realized_pnl(entry: dict[str, Any], *, won: bool) -> tuple[float, float, float]:
    fill = _entry_fill_price(entry)
    size = _entry_size(entry)
    fee = _entry_fee(entry)
    gross = ((1.0 - fill) if won else -fill) * size
    return gross - fee, gross, fee


def taker_decision(fair: float, yes_bid: float, yes_ask: float) -> tuple[str, float, float] | None:
    """If a same-day fair value crosses the executable touch with positive
    post-fee edge, return (side, entry_price, edge_after_fee); else None.
      * fair > yes_ask  -> BUY YES at the ask
      * fair < yes_bid  -> BUY NO  at (1 - yes_bid)
    This is the honest taker assumption (cross the spread, pay the fee), stricter
    than a mid-based edge."""
    if not (0.0 < yes_bid < 1.0 and 0.0 < yes_ask < 1.0):
        return None
    if fair > yes_ask:
        edge = fair - yes_ask - kalshi_fee(yes_ask)
        if edge > 0:
            return ("YES", round(yes_ask, 2), edge)
    if fair < yes_bid:
        no_entry = round(1.0 - yes_bid, 2)
        edge = (yes_bid - fair) - kalshi_fee(no_entry)
        if edge > 0:
            return ("NO", no_entry, edge)
    return None


def _noaa_token() -> str:
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith("NOAA_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"').strip()
    return ""


def ghcnd_high(station_id: str, day: date, token: str) -> float | None:
    url = "https://www.ncdc.noaa.gov/cdo-web/api/v2/data?" + urllib.parse.urlencode(
        {
            "datasetid": "GHCND",
            "stationid": f"GHCND:{station_id}",
            "datatypeid": "TMAX",
            "startdate": day.isoformat(),
            "enddate": day.isoformat(),
            "units": "standard",
            "limit": 5,
        }
    )
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "token": token})
    with urllib.request.urlopen(req, timeout=40) as r:
        d = json.loads(r.read())
    for row in d.get("results", []):
        return float(row.get("value"))
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="KXHIGH calibrated paper pricing / recorder")
    ap.add_argument("--record", type=Path, default=None,
                    help="append same-day taker paper entries to this JSONL ledger")
    ap.add_argument("--settle", type=Path, default=None,
                    help="mark realized PnL on a ledger using GHCND actuals, then exit")
    ap.add_argument("--settle-out", type=Path, default=None,
                    help="write enriched settlement/CLV JSONL to this path")
    ap.add_argument("--write-settled", action="store_true",
                    help="rewrite --settle ledger in place with settlement/CLV fields")
    ap.add_argument("--clv-window-minutes", type=int, default=90,
                    help="closing-mid candle lookback window before market close")
    ap.add_argument("--size", type=int, default=10, help="paper contracts per entry")
    args = ap.parse_args()

    if args.settle is not None:
        return _settle(
            args.settle,
            settle_out=args.settle_out,
            write_settled=args.write_settled,
            clv_window_minutes=args.clv_window_minutes,
        )

    if not CALIB_PATH.exists():
        print(f"missing {CALIB_PATH}; run weather_calibration_report.py first")
        return 2
    calibs = load_calibrations(CALIB_PATH)
    today = datetime.now().date()
    now_iso = datetime.now(UTC).isoformat()

    print("=== KXHIGH PAPER PRICING (calibrated fair vs live mid, read-only) ===")
    print(f"today={today}  calibration={ {k: round(c.bias_f,2) for k,c in calibs.items()} }\n")

    tradeable: list[tuple] = []
    records: list[dict] = []
    for series, (code, loc) in KXHIGH_STATIONS.items():
        cal: StationCalibration | None = calibs.get(code)
        if cal is None:
            print(f"[{series}] no calibration for {code}; skip")
            continue
        try:
            forecast_state = forecast_daily_state(loc.latitude, loc.longitude, loc.timezone)
            highs = forecast_state.highs
            markets = open_markets(series)
        except Exception as exc:  # noqa: BLE001
            print(f"[{series}] fetch failed: {type(exc).__name__}: {str(exc)[:80]}")
            continue

        print(f"[{series}] {loc.name}  station={code} bias={cal.bias_f:+.2f}F sigma={cal.sigma_f:.2f}F")
        rows = []
        day_distribution: dict[str, DailyHighDistribution] = {}
        # market center/spread per day = prob-weighted bracket temperature using
        # ALL two-sided brackets (incl. open-ended less/greater via a proxy
        # center), to detect model-vs-market disagreement.
        mkt_num: dict[str, float] = defaultdict(float)
        mkt_den: dict[str, float] = defaultdict(float)
        for m in markets:
            c = parse_kxhigh_market(m)
            if c is None:
                continue
            fc = highs.get(c.target_day.isoformat())
            if fc is None:
                continue  # no forecast for that day (too far out / past)
            day_key = c.target_day.isoformat()
            lead = (c.target_day - today).days
            high_so_far_f = forecast_state.high_so_far.get(day_key)
            distribution = day_distribution.get(day_key)
            if distribution is None:
                distribution = _kxhigh_distribution(
                    state=forecast_state,
                    location=loc,
                    target_day=c.target_day,
                    station_code=code,
                    calibration=cal,
                    high_so_far_f=high_so_far_f,
                )
                day_distribution[day_key] = distribution
            fair = probability_for_contract(c, distribution)
            yb, ya = _f(m.get("yes_bid_dollars")), _f(m.get("yes_ask_dollars"))
            two_sided = yb > 0.0 and ya < 1.0
            mid = (yb + ya) / 2.0 if two_sided else None
            edge = (fair - mid) if mid is not None else None
            net = (abs(edge) - kalshi_fee(mid)) if (edge is not None and mid is not None) else None
            center = _bracket_center(c)
            if mid is not None and center is not None:
                mkt_num[c.target_day.isoformat()] += mid * center
                mkt_den[c.target_day.isoformat()] += mid
            rows.append((c, fc, high_so_far_f, distribution, lead, fair, yb, ya, mid, edge, net, two_sided))

        # second moment: prob-weighted spread across all two-sided brackets, to
        # compare the model's sigma against the market's implied sigma (a too-tight
        # model manufactures wing "edges").
        mkt_var_num: dict[str, float] = defaultdict(float)
        for m in markets:
            c = parse_kxhigh_market(m)
            if c is None:
                continue
            key = c.target_day.isoformat()
            center = _bracket_center(c)
            if mkt_den[key] <= 0 or center is None:
                continue
            yb, ya = _f(m.get("yes_bid_dollars")), _f(m.get("yes_ask_dollars"))
            if not (yb > 0.0 and ya < 1.0):
                continue
            mc = mkt_num[key] / mkt_den[key]
            mkt_var_num[key] += ((yb + ya) / 2.0) * (center - mc) ** 2

        # day-level model vs market: a large center gap OR a model sigma much
        # tighter than the market's = misplaced distribution (miscalibration).
        day_gap: dict[str, float] = {}
        day_bad: dict[str, bool] = {}
        for key in sorted(mkt_den):
            if mkt_den[key] <= 0:
                continue
            # any contract for this day to recover fc + month
            fc = highs.get(key)
            if fc is None:
                continue
            month = int(key[5:7])
            distribution = day_distribution.get(key)
            model_center = distribution.mean_f if distribution is not None else cal.corrected_high(fc, month=month)
            market_center = mkt_num[key] / mkt_den[key]
            gap = model_center - market_center
            mkt_sigma = (mkt_var_num[key] / mkt_den[key]) ** 0.5 if mkt_den[key] > 0 else 0.0
            too_tight = mkt_sigma > 0.0 and cal.sigma_f < 0.75 * mkt_sigma
            bad = abs(gap) > cal.sigma_f or too_tight
            day_gap[key] = gap
            day_bad[key] = bad
            why = []
            if abs(gap) > cal.sigma_f:
                why.append(f"center off {gap:+.2f}F")
            if too_tight:
                why.append(f"model σ {cal.sigma_f:.2f} ≪ market σ {mkt_sigma:.2f}")
            tag = "OK (model≈market)" if not bad else f"** MISCALIBRATED ({'; '.join(why)}) → model error, not alpha **"
            print(f"    day {key}: model_center−market_center={gap:+.2f}F  market_σ≈{mkt_sigma:.2f}  {tag}")

        rows.sort(key=lambda r: (r[4], r[0].ticker))
        hdr = (
            f"  {'bracket':22s} {'lead':>4s} {'fcHi':>5s} {'soFar':>5s} {'fair':>5s} "
            f"{'bid':>5s} {'ask':>5s} {'mid':>5s} {'edge':>6s} {'net':>6s}  flag"
        )
        print(hdr)
        for (c, fc, high_so_far_f, distribution, lead, fair, yb, ya, mid, edge, net, two) in rows:
            strike = c.strike_type[0].upper()
            hsf = f"{high_so_far_f:5.1f}" if high_so_far_f is not None else "  -  "
            mids = f"{mid:.2f}" if mid is not None else "  - "
            edges = f"{edge:+.3f}" if edge is not None else "   -  "
            nets = f"{net:+.3f}" if net is not None else "   -  "
            disagree = day_bad.get(c.target_day.isoformat(), False)
            flag = ""
            if two and net is not None and net > 0 and lead == 0 and not disagree:
                assert mid is not None
                decision = taker_decision(fair, yb, ya)
                if decision is not None:
                    side, entry, edge_af = decision
                    flag = f"** RECORD {side}@{entry:.2f} edge_af={edge_af:+.3f} **"
                    tradeable.append((c.ticker, fair, mid, edge, net))
                    model_price = _side_price_from_yes(fair, side)
                    market_mid_at_entry = _side_price_from_yes(mid, side)
                    records.append({
                        "entry_id": f"{c.ticker}:{side}:{now_iso}",
                        "ts": now_iso,
                        "entry_ts": now_iso,
                        "ticker": c.ticker,
                        "station": code,
                        "target_day": c.target_day.isoformat(),
                        "close_time": m.get("close_time"),
                        "strike_type": c.strike_type,
                        "floor_strike": c.floor_strike,
                        "cap_strike": c.cap_strike,
                        "side": side,
                        "execution_mode": "taker",
                        "entry_price": entry,
                        "fill_price": entry,
                        "size": args.size,
                        "fair": round(fair, 4),
                        "model_yes_price": round(fair, 4),
                        "model_price": round(model_price, 4),
                        "yes_bid": yb,
                        "yes_ask": ya,
                        "yes_mid_at_entry": round(mid, 4),
                        "market_mid_at_entry": round(market_mid_at_entry, 4),
                        "fee_per_contract": round(kalshi_fee(entry), 4),
                        "fee": round(kalshi_fee(entry, args.size), 4),
                        "edge_after_fee": round(edge_af, 4),
                        "predicted_edge_after_fee": round(edge_af, 4),
                        "distribution_method": distribution.method,
                        "distribution_feature_hash": distribution.feature_hash,
                        "expected_high_f": round(distribution.mean_f, 4),
                        "latent_expected_high_f": (
                            None if distribution.latent_mean_f is None else round(distribution.latent_mean_f, 4)
                        ),
                        "high_so_far_f": high_so_far_f,
                        "high_so_far_source": (
                            "open_meteo_hourly_proxy" if high_so_far_f is not None else None
                        ),
                    })
                else:
                    flag = "** candidate edge (model≈market) but spread eats it **"
            elif two and net is not None and net > 0 and disagree:
                flag = "(model/market disagree → miscalibration, skip)"
            elif two and net is not None and net > 0:
                flag = f"(edge but lead={lead}: sigma understated)"
            elif two:
                flag = "(2-sided, no net edge)"
            else:
                flag = "(thin/one-sided)"
            print(
                f"  {c.ticker:22s} {strike}{lead:>3d} {fc:5.1f} {hsf} {fair:5.2f} "
                f"{yb:5.2f} {ya:5.2f} {mids:>5s} {edges:>6s} {nets:>6s}  {flag}"
            )
        print()

    print("=== SUMMARY ===")
    if tradeable:
        print(f"  {len(tradeable)} same-day brackets where model≈market AND net edge>0 (worth recording):")
        for tk, fair, mid, edge, net in sorted(tradeable, key=lambda x: -x[4]):
            print(f"    {tk:24s} fair={fair:.2f} mid={mid:.2f} edge={edge:+.3f} net={net:+.3f}")
    else:
        print("  no candidate edge: on every day the model center disagrees with the")
        print("  liquid market by more than sigma, so the per-bracket 'edges' are a")
        print("  misplaced distribution (model error), not alpha. NOT deployable.")
    print("\n  NOTE: a candidate edge here is necessary but NOT proof of PnL; that needs")
    print("  recorded fills over many settlements. A large model−market center gap is")
    print("  the miscalibration tell — trust the liquid market over a drifting fit.")

    if args.record is not None:
        args.record.parent.mkdir(parents=True, exist_ok=True)
        # idempotent per (ticker, target_day): safe to run on a schedule.
        existing: set[tuple[str, str]] = set()
        if args.record.exists():
            for line in args.record.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    e = json.loads(line)
                    existing.add((e["ticker"], e["target_day"]))
        fresh = [r for r in records if (r["ticker"], r["target_day"]) not in existing]
        with args.record.open("a", encoding="utf-8") as fh:
            for rec in fresh:
                fh.write(json.dumps(rec) + "\n")
        skipped = len(records) - len(fresh)
        print(f"\n  recorded {len(fresh)} new paper entr{'y' if len(fresh)==1 else 'ies'}"
              f" ({skipped} already logged today) -> {args.record}")
    return 0


def _settle(
    ledger: Path,
    *,
    settle_out: Path | None = None,
    write_settled: bool = False,
    clv_window_minutes: int = 90,
) -> int:
    """Mark realized PnL and CLV on recorded entries.

    GHCND actuals lag by roughly 3-4 days, while CLV is available once Kalshi
    closing candles are available. Entries missing either value remain pending
    for that specific metric and can be re-run idempotently.
    """
    if not ledger.exists():
        print(f"no ledger at {ledger}")
        return 2
    token = _noaa_token()
    if not token:
        print("NOAA_TOKEN not in .env; cannot settle")
        return 2
    sid_by_code = {code: loc.station_id for _, (code, loc) in KXHIGH_STATIONS.items()}
    entries = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines() if line.strip()]
    actual_cache: dict[tuple[str, str], float | None] = {}
    close_cache: dict[str, datetime | None] = {}
    closing_mid_cache: dict[str, float | None] = {}
    updated_entries: list[dict[str, Any]] = []
    settled = pending = 0
    pnl_total = 0.0
    gross_total = 0.0
    fee_total = 0.0
    clv_total = 0.0
    clv_count = 0
    wins = 0
    print(f"=== SETTLE {ledger.name}: {len(entries)} recorded entries ===")
    now = datetime.now(UTC)
    for raw_entry in entries:
        e = dict(raw_entry)
        code, day_s = e["station"], e["target_day"]
        sid = sid_by_code.get(code)
        key = (code, day_s)
        if key not in actual_cache:
            try:
                actual_cache[key] = ghcnd_high(sid, date.fromisoformat(day_s), token) if sid else None
            except Exception:  # noqa: BLE001
                actual_cache[key] = None

        ticker = str(e["ticker"])
        if ticker not in close_cache:
            close_cache[ticker] = _market_close_time(e)
        close_time = close_cache[ticker]
        if close_time is not None:
            e["close_time"] = close_time.isoformat()
        if close_time is not None and close_time <= now:
            if ticker not in closing_mid_cache:
                try:
                    closing_mid_cache[ticker] = closing_yes_mid(
                        ticker,
                        close_time,
                        window_minutes=clv_window_minutes,
                    )
                except Exception:  # noqa: BLE001
                    closing_mid_cache[ticker] = None
            closing_mid = closing_mid_cache[ticker]
            if closing_mid is not None:
                _attach_clv(e, closing_mid)
                clv_total += float(e["clv"])
                clv_count += 1
            else:
                e["clv_status"] = "pending:no_closing_mid"
        else:
            e["clv_status"] = "pending:market_not_closed"

        actual = actual_cache[key]
        if actual is None:
            e["settlement_status"] = "pending:no_ghcnd_actual"
            updated_entries.append(e)
            pending += 1
            continue
        hi = round(actual)
        yes = _entry_yes_result(e, actual)
        won = yes if e["side"] == "YES" else (not yes)
        pnl, gross, fee = _entry_realized_pnl(e, won=won)
        pnl_total += pnl
        gross_total += gross
        fee_total += fee
        wins += 1 if won else 0
        settled += 1
        e["settlement_status"] = "settled"
        e["actual_high_f"] = round(actual, 2)
        e["actual_high_int"] = hi
        e["settled_yes"] = yes
        e["won"] = won
        e["gross_pnl"] = round(gross, 4)
        e["fee"] = round(fee, 4)
        e["realized_pnl"] = round(pnl, 4)
        clv_text = f" clv={float(e['clv']):+.2f}" if "clv" in e else ""
        print(f"  {ticker:24s} {e['side']:3s}@{_entry_fill_price(e):.2f}x{_entry_size(e):>3d} "
              f"actual_hi={hi} -> {'WON ' if won else 'LOST'} pnl={pnl:+.2f}{clv_text}")
        updated_entries.append(e)
    print(f"\n  settled={settled} pending={pending} (GHCND not yet published)")
    if settled:
        print(f"  realized PnL = ${pnl_total:+.2f}  gross=${gross_total:+.2f}  fees=${fee_total:.2f}")
        print(f"  win_rate={wins/settled:.0%}  over {settled} fills")
    if clv_count:
        print(f"  CLV = ${clv_total:+.2f}  avg_per_fill=${clv_total/clv_count:+.2f}  over {clv_count} fills")
    out_path = ledger if write_settled else settle_out
    if out_path is not None:
        _write_jsonl_atomic(out_path, updated_entries)
        print(f"  wrote enriched ledger -> {out_path}")
    else:
        print("  dry-run only; pass --write-settled or --settle-out to persist settlement/CLV fields")
    return 0


def _write_jsonl_atomic(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True) + "\n")
    tmp.replace(path)


if __name__ == "__main__":
    raise SystemExit(main())
