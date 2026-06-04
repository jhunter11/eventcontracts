from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from eventcontracts.weather.calibration import StationCalibration
from eventcontracts.weather.distribution import (
    EnsembleMemberHigh,
    StationObservationSnapshot,
    build_daily_high_distribution,
    price_kxhigh_ladder,
    probability_for_contract,
)
from eventcontracts.weather.kxhigh import KalshiHighContract, parse_kxhigh_market
from eventcontracts.weather.temperature import (
    HourlyWeatherPoint,
    TemperatureForecastSnapshot,
    WeatherLocation,
)

DAY = date(2026, 5, 31)
AS_OF = datetime(2026, 5, 31, 12, tzinfo=UTC)


def _snapshot() -> TemperatureForecastSnapshot:
    return TemperatureForecastSnapshot(
        location=WeatherLocation(
            name="NYC Central Park",
            latitude=40.779,
            longitude=-73.969,
            timezone="America/New_York",
            station_id="USW00094728",
        ),
        as_of=AS_OF,
        hourly=(
            HourlyWeatherPoint(timestamp=datetime(2026, 5, 31, 14, tzinfo=UTC), temperature_f=73.0),
            HourlyWeatherPoint(timestamp=datetime(2026, 5, 31, 18, tzinfo=UTC), temperature_f=75.0),
            HourlyWeatherPoint(timestamp=datetime(2026, 5, 31, 21, tzinfo=UTC), temperature_f=74.0),
        ),
    )


def _calibration() -> StationCalibration:
    return StationCalibration(station="NY", bias_f=0.5, sigma_f=2.0, n=100)


def _contract(payload: dict[str, object]) -> KalshiHighContract:
    parsed = parse_kxhigh_market(payload)
    assert parsed is not None
    return parsed


def test_normal_distribution_matches_existing_kxhigh_contract_pricing() -> None:
    contract = _contract(
        {
            "ticker": "KXHIGHNY-26MAY31-B74.5",
            "strike_type": "between",
            "floor_strike": 74,
            "cap_strike": 75,
        }
    )
    dist = build_daily_high_distribution(_snapshot(), DAY, _calibration())

    assert dist.method == "normal_calibrated"
    assert dist.latent_mean_f == pytest.approx(75.5)
    assert probability_for_contract(contract, dist) == pytest.approx(contract.yes_probability(75.0, _calibration()))


def test_high_so_far_makes_already_hit_greater_bracket_deterministic() -> None:
    contract = _contract({"ticker": "KXHIGHNY-26MAY31-T79", "strike_type": "greater", "floor_strike": 79})
    observation = StationObservationSnapshot(
        station_code="NY",
        target_day=DAY,
        observed_high_f=80.0,
        as_of=datetime(2026, 5, 31, 19, tzinfo=UTC),
        source="fixture-station",
    )
    dist = build_daily_high_distribution(_snapshot(), DAY, _calibration(), observation=observation)

    assert probability_for_contract(contract, dist) == 1.0
    assert dist.latent_mean_f is not None
    assert dist.mean_f > dist.latent_mean_f


def test_high_so_far_kills_less_bracket_after_station_has_exceeded_cap() -> None:
    contract = _contract({"ticker": "KXHIGHNY-26MAY31-T72", "strike_type": "less", "cap_strike": 72})
    observation = StationObservationSnapshot(
        station_code="NY",
        target_day=DAY,
        observed_high_f=75.0,
        as_of=datetime(2026, 5, 31, 19, tzinfo=UTC),
        source="fixture-station",
    )
    dist = build_daily_high_distribution(_snapshot(), DAY, _calibration(), observation=observation)

    assert probability_for_contract(contract, dist) == 0.0


def test_empirical_ensemble_ladder_partition_sums_to_one() -> None:
    ladder = [
        {"ticker": "KXHIGHNY-26MAY31-T72", "strike_type": "less", "cap_strike": 72},
        {"ticker": "KXHIGHNY-26MAY31-B72.5", "strike_type": "between", "floor_strike": 72, "cap_strike": 73},
        {"ticker": "KXHIGHNY-26MAY31-B74.5", "strike_type": "between", "floor_strike": 74, "cap_strike": 75},
        {"ticker": "KXHIGHNY-26MAY31-B76.5", "strike_type": "between", "floor_strike": 76, "cap_strike": 77},
        {"ticker": "KXHIGHNY-26MAY31-B78.5", "strike_type": "between", "floor_strike": 78, "cap_strike": 79},
        {"ticker": "KXHIGHNY-26MAY31-T79", "strike_type": "greater", "floor_strike": 79},
    ]
    contracts = tuple(_contract(payload) for payload in ladder)
    members = (
        EnsembleMemberHigh(source="fixture", member_id="m0", forecast_high_f=70.7),
        EnsembleMemberHigh(source="fixture", member_id="m1", forecast_high_f=72.0),
        EnsembleMemberHigh(source="fixture", member_id="m2", forecast_high_f=74.0),
        EnsembleMemberHigh(source="fixture", member_id="m3", forecast_high_f=76.0),
        EnsembleMemberHigh(source="fixture", member_id="m4", forecast_high_f=78.0),
        EnsembleMemberHigh(source="fixture", member_id="m5", forecast_high_f=81.0),
    )
    dist = build_daily_high_distribution(_snapshot(), DAY, _calibration(), ensemble_members=members)
    valuations = price_kxhigh_ladder(contracts, dist)

    assert dist.method == "empirical_ensemble_calibrated"
    assert sum(v.yes_probability for v in valuations) == pytest.approx(1.0)
    assert {v.ticker for v in valuations} == {c.ticker for c in contracts}


def test_observation_station_and_day_must_match() -> None:
    observation = StationObservationSnapshot(
        station_code="CHI",
        target_day=DAY,
        observed_high_f=75.0,
        as_of=datetime(2026, 5, 31, 19, tzinfo=UTC),
        source="fixture-station",
    )

    with pytest.raises(ValueError, match="station"):
        build_daily_high_distribution(_snapshot(), DAY, _calibration(), observation=observation)
