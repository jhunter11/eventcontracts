"""Tests for the S1 high-so-far sniper (deterministic KXHIGH locks).

Locks the running-max guarantee: once the observed daily high crosses a bracket
boundary, the bracket is decided independent of any forecast, and the sniper
should only ever buy the side the observation already guarantees — and only when
the live price still leaves net edge after the Kalshi fee.
"""

from __future__ import annotations

from datetime import date

import pytest

from eventcontracts.weather.kxhigh import KalshiHighContract, parse_kxhigh_market
from eventcontracts.weather.snipe import (
    NWS_STATIONS,
    deterministic_settlement,
    observed_daily_high_f,
    s1_signal,
)

_GREATER = {"ticker": "KXHIGHNY-26MAY31-T79", "strike_type": "greater", "floor_strike": 79}
_LESS = {"ticker": "KXHIGHNY-26MAY31-T72", "strike_type": "less", "cap_strike": 72}
_BETWEEN = {
    "ticker": "KXHIGHNY-26MAY31-B74.5",
    "strike_type": "between",
    "floor_strike": 74,
    "cap_strike": 75,
}


def _contract(payload: dict[str, object]) -> KalshiHighContract:
    parsed = parse_kxhigh_market(payload)
    assert parsed is not None
    return parsed


def _obs(*celsius_on_day: float, off_day_c: float | None = None) -> dict:
    features = [
        {
            "properties": {
                "timestamp": f"2026-05-31T{h:02d}:51:00+00:00",
                "temperature": {"unitCode": "wmoUnit:degC", "value": c},
            }
        }
        for h, c in enumerate(celsius_on_day)
    ]
    if off_day_c is not None:
        features.append(
            {
                "properties": {
                    "timestamp": "2026-06-01T18:51:00+00:00",
                    "temperature": {"unitCode": "wmoUnit:degC", "value": off_day_c},
                }
            }
        )
    return {"features": features}


# --- observed_daily_high_f -------------------------------------------------

def test_observed_high_takes_local_day_max_in_fahrenheit() -> None:
    # 20C, 31C, 26C on the day; 40C the next day must be excluded.
    payload = _obs(20.0, 31.0, 26.0, off_day_c=40.0)
    high = observed_daily_high_f(payload, target_day=date(2026, 5, 31), timezone="UTC")
    assert high == pytest.approx(31.0 * 9 / 5 + 32)  # 87.8F


def test_observed_high_local_day_boundary_respects_timezone() -> None:
    # 16:51Z / 19:51Z on May 31 are 12:51 / 15:51 EDT (afternoon May 31 in NY);
    # the 40C reading at 03:51Z June 1 is 23:51 EDT May 31 — still May 31 local,
    # so it MUST be included, while a 02:00Z May 31 reading (22:00 EDT May 30) must not.
    payload = {
        "features": [
            {"properties": {"timestamp": "2026-05-31T02:00:00+00:00", "temperature": {"value": 99.0}}},
            {"properties": {"timestamp": "2026-05-31T16:51:00+00:00", "temperature": {"value": 20.0}}},
            {"properties": {"timestamp": "2026-05-31T19:51:00+00:00", "temperature": {"value": 31.0}}},
            {"properties": {"timestamp": "2026-06-01T03:51:00+00:00", "temperature": {"value": 28.0}}},
        ]
    }
    high = observed_daily_high_f(payload, target_day=date(2026, 5, 31), timezone="America/New_York")
    # max of the three May-31-NY readings (20, 31, 28 C); the 99C 22:00-EDT-May-30 excluded.
    assert high == pytest.approx(31.0 * 9 / 5 + 32)


def test_observed_high_none_when_no_readings_for_day() -> None:
    payload = {"features": []}
    assert observed_daily_high_f(payload, target_day=date(2026, 5, 31), timezone="UTC") is None


def test_observed_high_skips_null_temperatures() -> None:
    payload = {
        "features": [
            {"properties": {"timestamp": "2026-05-31T10:51:00+00:00", "temperature": {"value": None}}},
            {"properties": {"timestamp": "2026-05-31T11:51:00+00:00", "temperature": {"value": 30.0}}},
        ]
    }
    high = observed_daily_high_f(payload, target_day=date(2026, 5, 31), timezone="UTC")
    assert high == pytest.approx(86.0)


# --- deterministic_settlement ---------------------------------------------

def test_greater_locks_yes_once_threshold_reached() -> None:
    c = _contract(_GREATER)  # YES region high >= 80
    assert deterministic_settlement(c, 80.0) == 1.0
    assert deterministic_settlement(c, 79.6) == 1.0  # rounds to 80
    assert deterministic_settlement(c, 79.0) is None  # still below, can rise


def test_less_locks_no_once_high_reaches_cap() -> None:
    c = _contract(_LESS)  # YES region high <= 71
    assert deterministic_settlement(c, 72.0) == 0.0
    assert deterministic_settlement(c, 71.0) is None  # 71 still satisfies YES, can rise
    assert deterministic_settlement(c, 60.0) is None


def test_between_locks_no_once_past_cap() -> None:
    c = _contract(_BETWEEN)  # YES region 74..75
    assert deterministic_settlement(c, 76.0) == 0.0  # rounds to 76 > 75
    assert deterministic_settlement(c, 75.0) is None  # within range, can still rise
    assert deterministic_settlement(c, 73.0) is None  # below, can rise into range


def test_settlement_ignores_non_finite() -> None:
    c = _contract(_GREATER)
    assert deterministic_settlement(c, float("nan")) is None


# --- s1_signal -------------------------------------------------------------

def test_yes_lock_emits_buy_yes_with_net_edge() -> None:
    c = _contract(_GREATER)
    sig = s1_signal(c, 81.0, yes_bid=0.90, yes_ask=0.94)
    assert sig is not None
    assert sig.side == "YES"
    assert sig.fill_price == pytest.approx(0.94)
    # edge = 1 - 0.94 - fee(0.94); fee = 0.07*0.94*0.06 ~ 0.00395
    assert sig.edge == pytest.approx(1 - 0.94 - 0.07 * 0.94 * 0.06)
    assert sig.edge > 0


def test_no_lock_emits_buy_no_with_net_edge() -> None:
    c = _contract(_LESS)  # high>=72 -> YES impossible
    sig = s1_signal(c, 73.0, yes_bid=0.08, yes_ask=0.12)
    assert sig is not None
    assert sig.side == "NO"
    # buy NO at 1 - yes_bid = 0.92; edge = 1 - 0.92 - fee(0.92)
    assert sig.fill_price == pytest.approx(0.92)
    assert sig.edge == pytest.approx(0.08 - 0.07 * 0.92 * 0.08)


def test_no_signal_at_degenerate_price() -> None:
    c = _contract(_GREATER)
    # ask pinned at 1.0 (or absent) is not a tradeable lock.
    assert s1_signal(c, 81.0, yes_bid=0.99, yes_ask=1.0) is None
    assert s1_signal(c, 81.0, yes_bid=0.99, yes_ask=None) is None


def test_min_edge_filter_drops_subtick_locks() -> None:
    c = _contract(_GREATER)
    # YES locked at 0.99 nets ~0.9c after fee — real, but below a 2c floor.
    assert s1_signal(c, 81.0, yes_bid=0.98, yes_ask=0.99, min_edge=0.02) is None
    assert s1_signal(c, 81.0, yes_bid=0.98, yes_ask=0.99, min_edge=0.0) is not None


def test_no_signal_when_not_locked() -> None:
    c = _contract(_GREATER)  # needs >=80, only 78 observed
    assert s1_signal(c, 78.0, yes_bid=0.40, yes_ask=0.44) is None


def test_record_shape_round_trips_key_fields() -> None:
    from datetime import UTC, datetime

    c = _contract(_GREATER)
    sig = s1_signal(c, 81.0, yes_bid=0.90, yes_ask=0.94)
    assert sig is not None
    rec = sig.as_record(as_of=datetime(2026, 5, 31, 18, 0, tzinfo=UTC))
    assert rec["strategy"] == "weather_kxhigh_s1_snipe"
    assert rec["side"] == "YES"
    assert rec["high_so_far_source"] == "nws_observation"
    assert rec["ticker"] == "KXHIGHNY-26MAY31-T79"


def test_nws_station_registry_covers_kxhigh_series() -> None:
    assert set(NWS_STATIONS) == {"KXHIGHNY", "KXHIGHCHI", "KXHIGHMIA"}
