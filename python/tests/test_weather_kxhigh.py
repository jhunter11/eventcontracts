"""Tests for KXHIGH daily-high parsing + calibrated pricing.

These lock in the seam that makes the proven station calibration actually price
the liquid, NWS-settled KXHIGH markets (see test_weather_calibration.py for the
calibration itself).
"""

from __future__ import annotations

import sys
from datetime import UTC, date, datetime

import pytest

from eventcontracts.cli.live_paper import (
    _calibration_staleness_warning,
    _kxhigh_external_signal,
    _parse_live_weather_contract,
    _payload_high_so_far_f,
)
from eventcontracts.weather.calibration import StationCalibration, load_calibrations
from eventcontracts.weather.kxhigh import (
    KXHIGH_STATIONS,
    KalshiHighContract,
    parse_kxhigh_market,
)
from eventcontracts.weather.temperature import HourlyWeatherPoint, TemperatureForecastSnapshot
from tests.conftest import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT / "python" / "scripts"))
from weather_kxhigh_paper import (  # noqa: E402
    _attach_clv,
    _entry_realized_pnl,
    _entry_yes_result,
    _round_high_to_int,
)

# Real-shaped Kalshi /markets payloads (fields trimmed to what the parser reads).
_GREATER = {"ticker": "KXHIGHNY-26MAY31-T79", "strike_type": "greater", "floor_strike": 79}
_LESS = {"ticker": "KXHIGHNY-26MAY31-T72", "strike_type": "less", "cap_strike": 72}
_BETWEEN = {
    "ticker": "KXHIGHNY-26MAY31-B74.5",
    "strike_type": "between",
    "floor_strike": 74,
    "cap_strike": 75,
}


def _live_kxhigh_contract(payload: dict[str, object]) -> KalshiHighContract:
    parsed = _parse_live_weather_contract(payload)
    assert isinstance(parsed, KalshiHighContract)
    return parsed


def test_parse_greater_bracket() -> None:
    c = parse_kxhigh_market(_GREATER)
    assert c is not None
    assert c.series_ticker == "KXHIGHNY"
    assert c.station_code == "NY"
    assert c.target_day == date(2026, 5, 31)
    assert c.strike_type == "greater"
    assert c.floor_strike == 79.0
    assert c.cap_strike is None
    assert c.location.timezone == "America/New_York"


def test_parse_less_and_between_brackets() -> None:
    less = parse_kxhigh_market(_LESS)
    assert less is not None and less.strike_type == "less" and less.cap_strike == 72.0
    btw = parse_kxhigh_market(_BETWEEN)
    assert btw is not None and btw.strike_type == "between"
    assert (btw.floor_strike, btw.cap_strike) == (74.0, 75.0)


def test_parse_rejects_non_kxhigh_and_unknown_station() -> None:
    assert parse_kxhigh_market({"ticker": "KXTEMPNYCH-26MAY3100-T62.99", "strike_type": "greater"}) is None
    assert parse_kxhigh_market({"ticker": "KXHIGHXYZ-26MAY31-T79", "strike_type": "greater"}) is None
    assert parse_kxhigh_market({"ticker": 123}) is None
    assert parse_kxhigh_market({"ticker": "KXHIGHNY-26MAY31-T79", "strike_type": "weird"}) is None


def test_calibration_staleness_warning() -> None:
    now = datetime(2026, 6, 3, 12, tzinfo=UTC)
    # Fresh fit (2 days old, < 7d default) -> no warning.
    assert _calibration_staleness_warning({"generated_at": "2026-06-01T00:00:00Z"}, now=now) is None
    # Stale fit -> warns with the age.
    stale = _calibration_staleness_warning({"generated_at": "2026-05-01T00:00:00Z"}, now=now)
    assert stale is not None and "days old" in stale
    # Missing/null provenance (e.g. backfilled artifact) -> warns to regenerate.
    assert _calibration_staleness_warning({}, now=now) is not None
    assert _calibration_staleness_warning({"generated_at": None}, now=now) is not None
    # Unparsable timestamp -> warns rather than crashing the live runner.
    assert _calibration_staleness_warning({"generated_at": "not-a-date"}, now=now) is not None


def test_parse_kxhigh_market_reads_close_time() -> None:
    iso = parse_kxhigh_market({**_BETWEEN, "close_time": "2026-05-31T23:00:00Z"})
    assert iso is not None
    assert iso.close_time == datetime(2026, 5, 31, 23, tzinfo=UTC)
    unix = parse_kxhigh_market({**_BETWEEN, "close_time": 1769900400})
    assert unix is not None and unix.close_time is not None
    # absent close_time -> None (trimmed fixtures, the standalone recorder, etc.)
    assert parse_kxhigh_market(_BETWEEN).close_time is None  # type: ignore[union-attr]


def test_yes_probability_matches_calibration_primitives() -> None:
    cal = StationCalibration(station="NY", bias_f=0.5, sigma_f=2.0, n=100)
    fc, month = 76.0, 5
    greater = parse_kxhigh_market(_GREATER)
    less = parse_kxhigh_market(_LESS)
    btw = parse_kxhigh_market(_BETWEEN)
    assert greater is not None and less is not None and btw is not None
    # greater floor=79 -> high >= 80
    assert greater.yes_probability(fc, cal) == cal.p_high_at_least(80.0, fc, month)
    # less cap=72 -> high <= 71
    assert less.yes_probability(fc, cal) == pytest.approx(1.0 - cal.p_high_at_least(72.0, fc, month))
    # between [74,75]
    assert btw.yes_probability(fc, cal) == cal.p_between(74.0, 75.0, fc, month)


def test_yes_probability_monotone_in_forecast() -> None:
    cal = StationCalibration(station="NY", bias_f=0.0, sigma_f=2.0, n=100)
    greater = parse_kxhigh_market(_GREATER)
    less = parse_kxhigh_market(_LESS)
    assert greater is not None and less is not None
    # Hotter forecast -> more likely "80+", less likely "71 or below".
    assert greater.yes_probability(72.0, cal) < greater.yes_probability(82.0, cal)
    assert less.yes_probability(72.0, cal) > less.yes_probability(82.0, cal)


def test_bracket_partition_sums_to_one() -> None:
    """The full ladder for a day (<=71, [72-73], [74-75], [76-77], [78-79], >=80)
    tiles every integer high exactly once, so YES probabilities must sum to 1.
    This catches any off-by-one in the strike-type -> YES-region mapping."""
    cal = StationCalibration(station="NY", bias_f=0.3, sigma_f=1.8, n=100)
    fc = 75.0
    ladder = [
        {"ticker": "KXHIGHNY-26MAY31-T72", "strike_type": "less", "cap_strike": 72},
        {"ticker": "KXHIGHNY-26MAY31-B72.5", "strike_type": "between", "floor_strike": 72, "cap_strike": 73},
        {"ticker": "KXHIGHNY-26MAY31-B74.5", "strike_type": "between", "floor_strike": 74, "cap_strike": 75},
        {"ticker": "KXHIGHNY-26MAY31-B76.5", "strike_type": "between", "floor_strike": 76, "cap_strike": 77},
        {"ticker": "KXHIGHNY-26MAY31-B78.5", "strike_type": "between", "floor_strike": 78, "cap_strike": 79},
        {"ticker": "KXHIGHNY-26MAY31-T79", "strike_type": "greater", "floor_strike": 79},
    ]
    total = 0.0
    for payload in ladder:
        c = parse_kxhigh_market(payload)
        assert c is not None
        total += c.yes_probability(fc, cal)
    assert total == pytest.approx(1.0, abs=1e-9)


def test_station_registry_covers_three_cities() -> None:
    assert set(KXHIGH_STATIONS) == {"KXHIGHNY", "KXHIGHCHI", "KXHIGHMIA"}
    codes = {v[0] for v in KXHIGH_STATIONS.values()}
    assert codes == {"NY", "CHI", "MIA"}
    # tz must be local (not UTC) so the daily-high is the local-calendar-day max.
    assert all(v[1].timezone != "UTC" for v in KXHIGH_STATIONS.values())


def test_prices_against_persisted_calibration() -> None:
    """Integration: load the real fitted calibration and price a live-shaped
    bracket; the result must be a sane probability."""
    path = REPO_ROOT / "configs" / "weather" / "station_calibrations.json"
    if not path.exists():
        pytest.skip("station_calibrations.json not built")
    calibs = load_calibrations(path)
    assert {"NY", "CHI", "MIA"} <= set(calibs)
    c = parse_kxhigh_market(_BETWEEN)
    assert c is not None
    # NY forecast high ~75 -> the 74-75 bracket should be plausible (>1%).
    p = c.yes_probability(74.6, calibs[c.station_code])
    assert 0.0 < p < 1.0


def test_live_paper_emits_kxhigh_forecast_signal() -> None:
    c = _live_kxhigh_contract(_BETWEEN)
    cal = StationCalibration(station="NY", bias_f=0.5, sigma_f=2.0, n=100)
    as_of = datetime(2026, 5, 31, 12, tzinfo=UTC)
    snapshot = TemperatureForecastSnapshot(
        location=c.location,
        as_of=as_of,
        hourly=(
            HourlyWeatherPoint(timestamp=datetime(2026, 5, 31, 14, tzinfo=UTC), temperature_f=72.0),
            HourlyWeatherPoint(timestamp=datetime(2026, 5, 31, 18, tzinfo=UTC), temperature_f=75.0),
            HourlyWeatherPoint(timestamp=datetime(2026, 5, 31, 21, tzinfo=UTC), temperature_f=74.2),
        ),
        source="open-meteo",
    )

    result = _kxhigh_external_signal(c, snapshot=snapshot, as_of=as_of, calibrations={"NY": cal})
    assert result is not None  # snapshot's only day == target day -> lead 0 -> emit
    signal, row = result

    assert signal.source == "open-meteo"
    assert signal.payload["market_id"] == "KXHIGHNY-26MAY31-B74.5"
    assert signal.payload["instrument_id"]["market_id"] == "KXHIGHNY-26MAY31-B74.5"
    assert signal.payload["implied_prob"] == pytest.approx(c.yes_probability(75.0, cal))
    assert signal.payload["lead_days"] == 0
    assert row["instrument"] == "KXHIGHNY-26MAY31-B74.5"
    assert row["lead_days"] == 0


def test_live_paper_suppresses_non_zero_lead_kxhigh_signal() -> None:
    """The calibration sigma is fit at ~nowcast lead, so future-day (lead>=1)
    brackets are over-confident and must not be emitted; already-settled (lead<0)
    brackets must not be emitted either."""
    c = _live_kxhigh_contract(_BETWEEN)  # settles 2026-05-31
    cal = StationCalibration(station="NY", bias_f=0.5, sigma_f=2.0, n=100)
    as_of = datetime(2026, 5, 31, 12, tzinfo=UTC)
    snapshot = TemperatureForecastSnapshot(
        location=c.location,
        as_of=as_of,
        hourly=(
            HourlyWeatherPoint(timestamp=datetime(2026, 5, 31, 18, tzinfo=UTC), temperature_f=75.0),
        ),
        source="open-meteo",
    )

    # local today is the day before settlement -> lead = +1 -> suppressed
    assert (
        _kxhigh_external_signal(
            c, snapshot=snapshot, as_of=as_of, calibrations={"NY": cal}, local_today=date(2026, 5, 30)
        )
        is None
    )
    # local today is after settlement -> lead = -1 -> suppressed
    assert (
        _kxhigh_external_signal(
            c, snapshot=snapshot, as_of=as_of, calibrations={"NY": cal}, local_today=date(2026, 6, 1)
        )
        is None
    )
    # same local day -> lead 0 -> emitted
    assert (
        _kxhigh_external_signal(
            c, snapshot=snapshot, as_of=as_of, calibrations={"NY": cal}, local_today=date(2026, 5, 31)
        )
        is not None
    )


def test_daily_high_from_snapshot_uses_local_calendar_day() -> None:
    """Day-boundary guard (validation spec Phase 1.2): the daily high must be the
    max over only the target local day; a hotter hour on an adjacent day must not
    leak into the bracket's daily high."""
    from eventcontracts.weather.kxhigh import daily_high_from_snapshot

    loc = KXHIGH_STATIONS["KXHIGHNY"][1]
    as_of = datetime(2026, 5, 31, 12, tzinfo=UTC)
    snapshot = TemperatureForecastSnapshot(
        location=loc,
        as_of=as_of,
        hourly=(
            HourlyWeatherPoint(timestamp=datetime(2026, 5, 31, 18, tzinfo=UTC), temperature_f=70.0),
            HourlyWeatherPoint(timestamp=datetime(2026, 6, 1, 18, tzinfo=UTC), temperature_f=99.0),
        ),
        source="open-meteo",
    )

    assert daily_high_from_snapshot(snapshot, date(2026, 5, 31)) == 70.0
    assert daily_high_from_snapshot(snapshot, date(2026, 6, 1)) == 99.0


def test_live_paper_suppresses_closed_market_and_tags_close_time() -> None:
    """A market that has already closed (race vs discovery) must not be emitted; an
    open market is emitted with absolute close_time + positive seconds_to_close so
    the strategy can apply a fresh near-close gate."""
    cal = StationCalibration(station="NY", bias_f=0.5, sigma_f=2.0, n=100)
    as_of = datetime(2026, 5, 31, 12, tzinfo=UTC)
    snapshot = TemperatureForecastSnapshot(
        location=KXHIGH_STATIONS["KXHIGHNY"][1],
        as_of=as_of,
        hourly=(
            HourlyWeatherPoint(timestamp=datetime(2026, 5, 31, 18, tzinfo=UTC), temperature_f=75.0),
        ),
        source="open-meteo",
    )

    closed = _live_kxhigh_contract({**_BETWEEN, "close_time": "2026-05-31T11:00:00Z"})
    assert (
        _kxhigh_external_signal(
            closed, snapshot=snapshot, as_of=as_of, calibrations={"NY": cal}, local_today=date(2026, 5, 31)
        )
        is None
    )

    open_market = _live_kxhigh_contract({**_BETWEEN, "close_time": "2026-05-31T23:00:00Z"})
    result = _kxhigh_external_signal(
        open_market, snapshot=snapshot, as_of=as_of, calibrations={"NY": cal}, local_today=date(2026, 5, 31)
    )
    assert result is not None
    signal, row = result
    assert signal.payload["close_time"] == "2026-05-31T23:00:00+00:00"
    assert signal.payload["seconds_to_close"] == pytest.approx(11 * 3600)
    assert row["seconds_to_close"] == pytest.approx(11 * 3600)


def test_live_paper_extracts_open_meteo_high_so_far_proxy() -> None:
    payload = {
        "utc_offset_seconds": -4 * 3600,
        "hourly": {
            "time": [
                "2026-05-31T09:00",
                "2026-05-31T12:00",
                "2026-05-31T15:00",
                "2026-05-31T18:00",
            ],
            "temperature_2m": [70.0, 78.0, 76.0, 82.0],
        },
    }
    as_of = datetime(2026, 5, 31, 17, tzinfo=UTC)  # 13:00 provider-local wall clock

    assert _payload_high_so_far_f(payload, date(2026, 5, 31), as_of) == pytest.approx(78.0)
    assert _payload_high_so_far_f(payload, date(2026, 6, 1), as_of) is None


def test_live_paper_kxhigh_uses_high_so_far_distribution() -> None:
    c = _live_kxhigh_contract(_GREATER)
    cal = StationCalibration(station="NY", bias_f=0.5, sigma_f=2.0, n=100)
    as_of = datetime(2026, 5, 31, 19, tzinfo=UTC)
    snapshot = TemperatureForecastSnapshot(
        location=c.location,
        as_of=as_of,
        hourly=(
            HourlyWeatherPoint(timestamp=datetime(2026, 5, 31, 14, tzinfo=UTC), temperature_f=73.0),
            HourlyWeatherPoint(timestamp=datetime(2026, 5, 31, 18, tzinfo=UTC), temperature_f=75.0),
            HourlyWeatherPoint(timestamp=datetime(2026, 5, 31, 21, tzinfo=UTC), temperature_f=76.0),
        ),
        source="open-meteo",
    )

    result = _kxhigh_external_signal(
        c,
        snapshot=snapshot,
        as_of=as_of,
        calibrations={"NY": cal},
        local_today=date(2026, 5, 31),
        high_so_far_f=80.0,
    )

    assert result is not None
    signal, row = result
    assert signal.payload["implied_prob"] == 1.0
    assert signal.payload["high_so_far_f"] == pytest.approx(80.0)
    assert signal.payload["high_so_far_source"] == "open_meteo_hourly_proxy"
    assert signal.payload["distribution_method"] == "normal_calibrated"
    assert row["high_so_far_f"] == pytest.approx(80.0)
    assert row["expected_temperature_f"] > row["latent_expected_high_f"]


def test_weather_paper_clv_is_side_aware() -> None:
    entry = {"side": "NO", "fill_price": 0.42, "size": 10}
    _attach_clv(entry, 0.70)
    assert entry["market_yes_mid_near_close"] == pytest.approx(0.70)
    assert entry["market_mid_near_close"] == pytest.approx(0.30)
    assert entry["clv_per_contract"] == pytest.approx(-0.12)
    assert entry["clv"] == pytest.approx(-1.20)


def test_weather_paper_settlement_pnl_charges_fee() -> None:
    entry = {
        "strike_type": "between",
        "floor_strike": 72.0,
        "cap_strike": 73.0,
        "side": "YES",
        "fill_price": 0.18,
        "size": 10,
    }
    assert _entry_yes_result(entry, 72.1)
    pnl, gross, fee = _entry_realized_pnl(entry, won=True)
    assert gross == pytest.approx(8.2)
    # Kalshi charges ceil-to-cent on the order: ceil(100 * 0.07 * 0.18 * 0.82 * 10)
    # = ceil(10.332) = 11 cents = $0.11. Realized PnL must use the charged fee,
    # not the unrounded marginal approximation (0.10332).
    import math

    assert fee == pytest.approx(math.ceil(0.07 * 0.18 * 0.82 * 10 * 100) / 100)
    assert fee == pytest.approx(0.11)
    assert pnl == pytest.approx(gross - fee)


def test_settlement_rounds_half_up_like_the_pricing_model() -> None:
    # The pricing distribution rounds the high with floor(x + 0.5) (round-half-UP),
    # so settlement must too. Python's built-in round() is banker's rounding and
    # would settle a .5 actual against a different integer than the model priced.
    assert _round_high_to_int(72.5) == 73
    assert _round_high_to_int(70.5) == 71
    assert _round_high_to_int(72.4) == 72
    # A 72.5 actual settles a [73, 74] "between" bracket... no; it settles the
    # bracket whose YES region contains 73, proving the half-up choice flows
    # through _entry_yes_result.
    entry = {"strike_type": "greater", "floor_strike": 72.0, "cap_strike": None, "side": "YES"}
    assert _entry_yes_result(entry, 72.5)  # round-half-up -> 73 >= 72 + 1
