"""Quant modeling backbone: distributions, HAR-RV vol, BTC terminal pricer."""

from __future__ import annotations

import math

from eventcontracts.research.btc_terminal import (
    BtcTerminalModel,
    horizon_sigma_from_annual_vol,
    horizon_sigma_from_daily_vol,
)
from eventcontracts.research.distributions import (
    DiscreteDistribution,
    Logistic,
    Normal,
    StudentT,
    build_continuous,
)
from eventcontracts.research.har_rv import (
    HARRV,
    HARFamily,
    continuous_jump,
    log_returns,
    realized_semivariance,
    realized_variance,
)

# --- distributions ----------------------------------------------------------


def test_normal_and_logistic_match_reference() -> None:
    from scipy.stats import logistic as _logi
    from scipy.stats import norm as _norm

    nd = Normal(74.0, 3.0)
    assert abs(nd.cdf(77.0) - _norm.cdf(77.0, 74.0, 3.0)) < 1e-12
    assert abs(nd.prob_above(74.0) - 0.5) < 1e-12
    lo = Logistic(74.0, 3.0)
    assert abs(lo.cdf(77.0) - _logi.cdf(77.0, loc=74.0, scale=3.0 * math.sqrt(3) / math.pi)) < 1e-12


def test_studentt_has_requested_moments_and_fatter_tails() -> None:
    from scipy.stats import t as _t

    mu, sigma, dof = 0.0, 2.0, 4.0
    st = StudentT(mu, sigma, dof)
    scale = sigma * math.sqrt((dof - 2) / dof)
    assert abs(_t(dof, loc=mu, scale=scale).std() - sigma) < 1e-9  # scaled to sigma
    assert abs(st.cdf(0.0) - 0.5) < 1e-9
    # at 4 sigma out, the t4 tail is heavier than the normal tail
    far = mu + 4.0 * sigma
    assert st.prob_above(far) > Normal(mu, sigma).prob_above(far)


def test_discrete_distribution_normalizes_and_prices() -> None:
    d = DiscreteDistribution({4.00: 4.0, 3.75: 1.0})  # unnormalized 0.8/0.2
    assert abs(d.mean - (4.00 * 0.8 + 3.75 * 0.2)) < 1e-12
    assert abs(d.prob_above(3.80) - 0.8) < 1e-12
    assert abs(d.cdf(3.75) - 0.2) < 1e-12
    assert abs(d.prob_in(3.74, 4.00) - 1.0) < 1e-12
    assert d.stddev > 0


def test_build_continuous_factory() -> None:
    assert isinstance(build_continuous(0.0, 1.0, "normal"), Normal)
    assert isinstance(build_continuous(0.0, 1.0, "logistic"), Logistic)
    assert isinstance(build_continuous(0.0, 1.0, "student_t", dof=5.0), StudentT)


# --- HAR-RV ------------------------------------------------------------------


def test_har_rv_recovers_constant_variance() -> None:
    rv = [0.0004] * 40  # constant realized variance
    fit = HARRV(weekly=5, monthly=22, log_space=True).fit(rv)
    assert abs(fit.forecast(rv) - 0.0004) < 1e-7
    assert 0.0 <= fit.r_squared <= 1.0 + 1e-9


def test_har_rv_forecast_is_deterministic_and_scales_to_horizon() -> None:
    rng_rv = [0.0002 + 0.0001 * math.sin(i / 3.0) ** 2 for i in range(60)]
    fit = HARRV().fit(rng_rv)
    f1 = fit.forecast(rng_rv)
    assert f1 == fit.forecast(rng_rv)
    assert f1 > 0
    assert abs(fit.horizon_sigma(rng_rv, 4.0) - math.sqrt(f1 * 4.0)) < 1e-12


def test_realized_variance_and_log_returns() -> None:
    prices = [100.0, 101.0, 100.5, 102.0]
    rets = log_returns(prices)
    assert len(rets) == 3
    assert abs(realized_variance(rets) - sum(r * r for r in rets)) < 1e-15


def test_realized_semivariance_and_jump_split() -> None:
    rets = [0.01, -0.02, 0.005, -0.001, 0.03]
    pos, neg = realized_semivariance(rets)
    assert abs((pos + neg) - realized_variance(rets)) < 1e-15
    cv, jump = continuous_jump(rets)
    assert cv >= 0 and jump >= 0
    assert abs((cv + jump) - realized_variance(rets)) < 1e-12


def _synthetic_daily_returns(n_days: int = 45, per_day: int = 24, seed: int = 0) -> list[list[float]]:
    import random

    rng = random.Random(seed)
    days = []
    for d in range(n_days):
        vol = 0.005 + 0.003 * math.sin(d / 4.0) ** 2
        days.append([rng.gauss(0.0, vol) for _ in range(per_day)])
    return days


def test_har_family_variants_fit_forecast_and_scale() -> None:
    days = _synthetic_daily_returns(45)
    fits = {v: HARFamily(variant=v).fit(days) for v in ("har", "har_rs", "har_cj")}
    for fit in fits.values():
        f = fit.forecast(days)
        assert f > 0
        assert f == fit.forecast(days)  # deterministic
        assert 0.0 <= fit.r_squared <= 1.0 + 1e-9
        assert len(fit.coefficients) == len(fit.feature_names)
    assert len(fits["har"].feature_names) == 4  # const + d/w/m
    assert len(fits["har_rs"].feature_names) == 7  # const + 2 components x d/w/m
    assert len(fits["har_cj"].feature_names) == 7
    har_rs = fits["har_rs"]
    assert abs(har_rs.horizon_sigma(days, 0.25) - math.sqrt(har_rs.forecast(days) * 0.25)) < 1e-12


# --- BTC terminal ------------------------------------------------------------


def test_btc_terminal_is_monotone_and_centered() -> None:
    m = BtcTerminalModel(spot=66000.0, horizon_sigma=0.02, dof=4.0)
    # driftless martingale -> P(above spot) just under 0.5
    p_atm = m.prob_above(66000.0)
    assert 0.45 < p_atm < 0.50
    assert m.prob_above(60000.0) > m.prob_above(66000.0) > m.prob_above(72000.0)
    # prob_in is consistent with the tail probabilities
    assert abs((m.prob_above(64000.0) - m.prob_above(68000.0)) - m.prob_in(64000.0, 68000.0)) < 1e-9


def test_btc_terminal_fat_tails_lift_otm_strikes() -> None:
    sigma = 0.03
    fat = BtcTerminalModel(spot=66000.0, horizon_sigma=sigma, dof=3.5)
    gauss = BtcTerminalModel(spot=66000.0, horizon_sigma=sigma, dof=math.inf)
    otm = 66000.0 * math.exp(4.0 * sigma)
    assert fat.prob_above(otm) > gauss.prob_above(otm)


def test_horizon_sigma_scalers() -> None:
    # 4% daily vol over a quarter-day -> 0.04 * sqrt(1/4)
    assert abs(horizon_sigma_from_daily_vol(0.04, 86400.0 / 4) - 0.04 * 0.5) < 1e-12
    # 60% annual over one day -> 0.60 * sqrt(1/365)
    assert abs(horizon_sigma_from_annual_vol(0.60, 86400.0) - 0.60 * math.sqrt(1 / 365)) < 1e-12
