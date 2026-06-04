"""Unit tests for station calibration + reliability metrics."""

from __future__ import annotations

import random
from datetime import date, timedelta
from pathlib import Path

from eventcontracts.weather.calibration import (
    ForecastActual,
    StationCalibration,
    brier_score,
    expected_calibration_error,
    fit_station,
    load_calibration_meta,
    load_calibrations,
    log_loss,
    reliability_bins,
    save_calibrations,
)


def _synthetic_pairs(n: int, bias: float, sigma: float, seed: int = 0) -> list[ForecastActual]:
    rng = random.Random(seed)
    start = date(2024, 1, 1)
    pairs = []
    for i in range(n):
        forecast = 70.0 + 15.0 * rng.random()
        actual = forecast + bias + rng.gauss(0.0, sigma)
        pairs.append(ForecastActual(day=start + timedelta(days=i), forecast_high_f=forecast, actual_high_f=actual))
    return pairs


def test_save_load_calibrations_roundtrips_provenance_without_polluting_stations(
    tmp_path: Path,
) -> None:
    """The reserved `_meta` provenance block must round-trip via the file but must
    never appear as a station on load (and absence stays backward-compatible)."""
    cals = [StationCalibration(station="NY", bias_f=0.5, sigma_f=1.2, n=100)]
    path = tmp_path / "calib.json"

    meta = {"fit_method": "recency_weighted", "half_life_days": 21.0}
    save_calibrations(cals, path, provenance=meta)
    loaded = load_calibrations(path)
    assert set(loaded) == {"NY"}  # `_meta` is not parsed as a station
    assert loaded["NY"].sigma_f == 1.2
    assert load_calibration_meta(path)["fit_method"] == "recency_weighted"

    # No provenance -> no `_meta`; load still works and meta is empty.
    save_calibrations(cals, path)
    assert set(load_calibrations(path)) == {"NY"}
    assert load_calibration_meta(path) == {}


def test_persisted_calibration_file_carries_provenance() -> None:
    """The committed live-calibration artifact must record its provenance so the
    fit's lead semantics / data window are auditable."""
    path = Path(__file__).resolve().parents[2] / "configs" / "weather" / "station_calibrations.json"
    if not path.exists():
        return
    meta = load_calibration_meta(path)
    assert meta, "station_calibrations.json is missing its _meta provenance block"
    assert "lead_semantics" in meta
    assert set(load_calibrations(path)) >= {"NY", "CHI", "MIA"}


def test_fit_recovers_known_bias_and_sigma() -> None:
    pairs = _synthetic_pairs(2000, bias=1.5, sigma=1.2, seed=7)
    cal = fit_station("TEST", pairs, monthly=False)
    assert abs(cal.bias_f - 1.5) < 0.15
    assert abs(cal.sigma_f - 1.2) < 0.15
    assert cal.n == 2000


def test_corrected_high_applies_bias() -> None:
    cal = StationCalibration(station="X", bias_f=1.2, sigma_f=1.3, n=100)
    assert cal.corrected_high(70.0) == 71.2


def test_recency_weighting_tracks_a_regime_shift() -> None:
    # First 200 days the model runs +5F biased; last 40 days the bias is gone.
    rng = random.Random(11)
    start = date(2024, 1, 1)
    pairs = []
    for i in range(240):
        bias = 5.0 if i < 200 else 0.0
        fc = 70.0 + 10.0 * rng.random()
        actual = fc + bias + rng.gauss(0, 0.5)
        pairs.append(ForecastActual(day=start + timedelta(days=i), forecast_high_f=fc, actual_high_f=actual))
    static = fit_station("X", pairs, monthly=False)
    recent = fit_station("X", pairs, half_life_days=10.0)
    # Static averages both regimes (biased high); recency tracks the recent ~0.
    assert static.bias_f > 3.0
    assert abs(recent.bias_f) < 1.0
    assert recent.monthly_bias_f == {}
    assert recent.n == 240


def test_threshold_probability_is_monotone_in_forecast() -> None:
    cal = StationCalibration(station="X", bias_f=0.0, sigma_f=2.0, n=100)
    # Hotter forecast -> higher P(high >= 80)
    p_cool = cal.p_high_at_least(80.0, forecast_high_f=75.0)
    p_warm = cal.p_high_at_least(80.0, forecast_high_f=82.0)
    assert 0.0 < p_cool < p_warm < 1.0


def test_between_bracket_probability_in_unit_interval() -> None:
    cal = StationCalibration(station="X", bias_f=0.0, sigma_f=2.5, n=100)
    p = cal.p_between(74.0, 75.0, forecast_high_f=74.5)
    assert 0.0 < p < 1.0
    # The bracket containing the forecast should beat a far-away bracket.
    p_far = cal.p_between(90.0, 91.0, forecast_high_f=74.5)
    assert p > p_far


def test_min_sigma_floor_enforced() -> None:
    cal = StationCalibration(station="X", bias_f=0.0, sigma_f=0.1, n=100, min_sigma_f=0.75)
    assert cal.effective_sigma() == 0.75


def test_roundtrip_serialization() -> None:
    cal = fit_station("NY", _synthetic_pairs(300, 0.5, 1.4, seed=3), monthly=True)
    restored = StationCalibration.from_dict(cal.to_dict())
    assert restored.station == cal.station
    # to_dict() rounds to 4 dp for stable JSON; tolerance must match.
    assert abs(restored.bias_f - cal.bias_f) < 1e-4
    assert abs(restored.sigma_f - cal.sigma_f) < 1e-4


def test_brier_and_logloss_reward_calibration() -> None:
    # Perfectly-calibrated coin-flip predictions vs always-0.5 obs.
    obs = [1, 0, 1, 0, 1, 0]
    good = [0.9, 0.1, 0.8, 0.2, 0.7, 0.3]
    bad = [0.1, 0.9, 0.2, 0.8, 0.3, 0.7]
    assert brier_score(good, obs) < brier_score(bad, obs)
    assert log_loss(good, obs) < log_loss(bad, obs)


def test_reliability_bins_and_ece() -> None:
    pred = [0.05, 0.15, 0.25, 0.95, 0.85]
    obs = [0, 0, 0, 1, 1]
    bins = reliability_bins(pred, obs, n_bins=10)
    assert bins  # non-empty
    ece = expected_calibration_error(bins)
    assert 0.0 <= ece <= 1.0


def test_calibration_changes_model_prediction() -> None:
    """The fitted calibration must actually flow into the daily-high model
    prediction (otherwise Phase-1 work is unused)."""
    from datetime import UTC, datetime

    from eventcontracts.domain.models import InstrumentId, Venue
    from eventcontracts.weather import (
        TemperatureThresholdMarket,
        TemperatureThresholdModel,
        WeatherLocation,
        snapshot_from_open_meteo_payload,
    )

    as_of = datetime(2026, 5, 24, 6, tzinfo=UTC)
    loc = WeatherLocation(name="CHI", latitude=41.79, longitude=-87.75)
    # Flat-ish hourly forecast peaking at 74 F.
    times = [f"2026-05-24T{h:02d}:00" for h in range(24)]
    temps = [60 + 14 * (1 - abs(h - 15) / 15) for h in range(24)]  # peak ~74 at 15:00
    payload = {"hourly": {"time": times, "temperature_2m": temps}}
    snap = snapshot_from_open_meteo_payload(payload, location=loc, as_of=as_of)
    market = TemperatureThresholdMarket(
        instrument_id=InstrumentId(venue=Venue.KALSHI, market_id="KXHIGHCHI-26MAY24-T75"),
        threshold_f=75.0,
        target_day=as_of.date(),
        direction="above",
    )

    base = TemperatureThresholdModel().predict(snap, market)
    # Chicago Midway runs ~+1.24 F cold in the model -> calibration lifts the
    # expected high, raising P(high >= 75).
    cal = StationCalibration(station="CHI", bias_f=1.24, sigma_f=1.21, n=731)
    calibrated = TemperatureThresholdModel(calibration=cal).predict(snap, market)

    assert calibrated.expected_high_f > base.expected_high_f
    assert calibrated.implied_probability > base.implied_probability
    assert abs(calibrated.uncertainty_f - cal.effective_sigma()) < 1e-9
    assert "calib:CHI" in calibrated.model_family
