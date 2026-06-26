"""S1 high-so-far sniper: deterministic KXHIGH YES/NO locks from NWS observations.

KXHIGH brackets settle on the integer NWS daily-high. The terminal daily high is
a running max, so the *observed* high-so-far ``H`` is a hard lower bound on it:
``final_high >= H`` always. That makes some brackets already decided mid-session,
independent of any forecast:

  * ``greater`` floor=F  (YES region ``high >= F+1``): once ``round(H) >= F+1`` the
    YES side is **locked** (P=1.0).
  * ``less`` cap=C        (YES region ``high <= C-1``): once ``round(H) >= C`` the
    final high already exceeds ``C-1``, so YES is **impossible** (P=0.0 -> NO lock).
  * ``between`` [F, C]     (YES region ``F <= high <= C``): once ``round(H) >= C+1``
    the high has blown past the cap, so YES is **impossible** (P=0.0 -> NO lock).

The lower edge of a bracket can never *lock* a side, because the high can always
rise further before close. So this module only ever asserts certainty in the
direction the running max guarantees.

Crucially this uses **real NWS observations** (the ground truth KXHIGH settles
on), not the Open-Meteo hourly proxy the calibrated paper path feeds in: a
"certain" P=1.0 is only safe to size into if it is backed by the official
station reading. ``round(H)`` uses the same half-up integer rounding as
:func:`eventcontracts.weather.distribution._rounded_high` so the lock threshold
matches settlement.

This module is pure (no IO). The poller script wires it to ``NwsClient`` +
the live Kalshi book; everything here is unit-testable from payloads.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from eventcontracts.weather.kxhigh import KalshiHighContract

# Kalshi KXHIGH series -> the NWS station the market settles on (METAR/CLI id).
# Central Park (KNYC), Chicago Midway (KMDW), Miami Intl (KMIA) mirror the
# settlement points in KXHIGH_STATIONS.
NWS_STATIONS: dict[str, str] = {
    "KXHIGHNY": "KNYC",
    "KXHIGHCHI": "KMDW",
    "KXHIGHMIA": "KMIA",
}

# Kalshi general trading fee ~ 0.07 * p * (1-p) per contract (dollars, unrounded);
# matches weather_kxhigh_paper.kalshi_fee so snipe edges net the same way.
def kalshi_fee(price: float) -> float:
    return 0.07 * price * (1.0 - price)


def _rounded_high(value_f: float) -> int:
    """Half-up integer high — identical to distribution._rounded_high so the
    lock threshold lines up exactly with integer settlement."""
    return math.floor(value_f + 0.5)


def _c_to_f(celsius: float) -> float:
    return celsius * 9.0 / 5.0 + 32.0


def observed_daily_high_f(
    observations_payload: dict[str, Any],
    *,
    target_day: date,
    timezone: str,
) -> float | None:
    """Max observed temperature (F) on ``target_day`` (local tz) from an NWS
    ``/stations/{id}/observations`` GeoJSON payload.

    NWS reports temperature in degrees C under ``properties.temperature.value``;
    we convert to F and keep only readings whose ``timestamp`` falls on the
    settlement station's local calendar day. Returns ``None`` when the payload
    carries no usable reading for that day (missing values are common at the
    top of the hour)."""
    tz = ZoneInfo(timezone)
    features = observations_payload.get("features")
    if not isinstance(features, list):
        return None
    best: float | None = None
    for feature in features:
        if not isinstance(feature, dict):
            continue
        props = feature.get("properties")
        if not isinstance(props, dict):
            continue
        temp = props.get("temperature")
        value = temp.get("value") if isinstance(temp, dict) else None
        if not isinstance(value, int | float) or isinstance(value, bool):
            continue
        when = _parse_obs_time(props.get("timestamp"))
        if when is None or when.astimezone(tz).date() != target_day:
            continue
        fahrenheit = _c_to_f(float(value))
        if best is None or fahrenheit > best:
            best = fahrenheit
    return best


def deterministic_settlement(
    contract: KalshiHighContract,
    observed_high_f: float,
) -> float | None:
    """Locked YES probability (1.0 or 0.0) given a real observed high-so-far, or
    ``None`` when the bracket is not yet decided by the running max.

    Mirrors the integer YES regions in
    :meth:`eventcontracts.weather.kxhigh.KalshiHighContract.yes_probability`."""
    if not math.isfinite(observed_high_f):
        return None
    high = _rounded_high(observed_high_f)
    if contract.strike_type == "greater":
        if contract.floor_strike is None:
            return None
        # YES region high >= floor+1; once reached, YES is certain.
        if high >= contract.floor_strike + 1:
            return 1.0
        return None
    if contract.strike_type == "less":
        if contract.cap_strike is None:
            return None
        # YES region high <= cap-1; once high >= cap, YES is impossible.
        if high >= contract.cap_strike:
            return 0.0
        return None
    if contract.strike_type == "between":
        if contract.cap_strike is None:
            return None
        # YES region floor <= high <= cap; once high > cap, YES is impossible.
        if high >= contract.cap_strike + 1:
            return 0.0
        return None
    return None


@dataclass(frozen=True)
class SnipeSignal:
    """A deterministic lock the sniper would take at the current book."""

    ticker: str
    series_ticker: str
    station_code: str
    target_day: date
    side: str  # "YES" | "NO" — the locked-winning side to buy
    settle_prob: float  # 1.0 (YES locked) | 0.0 (YES impossible)
    observed_high_f: float
    fill_price: float  # price paid for one contract of the winning side
    fee: float
    edge: float  # net profit per contract after fee = 1 - fill_price - fee

    def as_record(self, *, as_of: datetime, source: str = "nws_observation") -> dict[str, Any]:
        return {
            "strategy": "weather_kxhigh_s1_snipe",
            "ticker": self.ticker,
            "series_ticker": self.series_ticker,
            "station_code": self.station_code,
            "target_day": self.target_day.isoformat(),
            "side": self.side,
            "settle_prob": self.settle_prob,
            "observed_high_f": round(self.observed_high_f, 2),
            "high_so_far_source": source,
            "fill_price": round(self.fill_price, 4),
            "size": 1,
            "fee": round(self.fee, 6),
            "edge": round(self.edge, 4),
            "as_of": as_of.isoformat(),
        }


def s1_signal(
    contract: KalshiHighContract,
    observed_high_f: float,
    *,
    yes_bid: float | None,
    yes_ask: float | None,
    min_edge: float = 0.0,
) -> SnipeSignal | None:
    """Return a positive-edge deterministic snipe at the current book, or None.

    ``yes_bid``/``yes_ask`` are the live YES prices in dollars (0..1). To take the
    locked-winning side as a taker:
      * YES lock  -> buy YES at ``yes_ask``  (edge ``1 - yes_ask - fee``)
      * NO  lock  -> buy NO  at ``1 - yes_bid`` (edge ``yes_bid - fee``)
    Only emits when the lock is real *and* the net edge after the Kalshi fee
    clears ``min_edge`` (dollars/contract) — a genuine free roll, not a marginal
    forecast edge. ``min_edge`` lets the poller drop sub-tick locks that aren't
    worth the slippage."""
    prob = deterministic_settlement(contract, observed_high_f)
    if prob is None:
        return None
    if prob == 1.0:
        if yes_ask is None or not 0.0 < yes_ask < 1.0:
            return None
        side, fill = "YES", yes_ask
    else:  # prob == 0.0 -> buy NO
        if yes_bid is None or not 0.0 < yes_bid < 1.0:
            return None
        side, fill = "NO", 1.0 - yes_bid
    fee = kalshi_fee(fill)
    edge = 1.0 - fill - fee
    if edge <= 0.0 or edge < min_edge:
        return None
    return SnipeSignal(
        ticker=contract.ticker,
        series_ticker=contract.series_ticker,
        station_code=contract.station_code,
        target_day=contract.target_day,
        side=side,
        settle_prob=prob,
        observed_high_f=observed_high_f,
        fill_price=fill,
        fee=fee,
        edge=edge,
    )


def _parse_obs_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None
