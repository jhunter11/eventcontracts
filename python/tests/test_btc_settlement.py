"""Validate the Kalshi 60s settlement convergence kernel.

Two independent checks:
  1. the closed form reproduces the thesis' tau-table (deterministic), and
  2. Monte-Carlo simulation of driftless per-second paths agrees with the
     closed-form mean / std / P(above) (the kernel is correct, not just plausible).
"""

from __future__ import annotations

import random
import statistics

from eventcontracts.research.btc_settlement import (
    before_window_forecast,
    forecast_at,
    implied_sigma_per_sec,
    prob_yes_at,
    sigma_per_second_from_annual,
    within_window_forecast,
)


def test_sigma_per_second_matches_worked_example() -> None:
    # $100k at 50% annual vol -> ~$8.9 per second.
    assert abs(sigma_per_second_from_annual(100_000.0, 0.5) - 8.9) < 0.1


def test_settlement_std_collapses_as_tau_three_halves() -> None:
    # The thesis table: s_V at tau = 60 / 30 / 10 s -> $39.8 / $14.1 / $2.71.
    sigma = sigma_per_second_from_annual(100_000.0, 0.5)
    c = 100_000.0
    for tau, expected in ((60, 39.8), (30, 14.1), (10, 2.71)):
        s = within_window_forecast(c * (60 - tau), c, tau, sigma).sigma
        assert abs(s - expected) < 0.1, f"tau={tau}: {s:.2f} vs {expected}"
    # Super-linear collapse: halving tau cuts s_V by more than the sqrt(2) of a
    # vanilla option (it falls by 2**1.5 ~= 2.83x).
    s60 = within_window_forecast(0.0, c, 60, sigma).sigma
    s30 = within_window_forecast(c * 30, c, 30, sigma).sigma
    assert s60 / s30 > 2.7


def test_within_window_matches_monte_carlo() -> None:
    rng = random.Random(0)
    sigma, window, tau, c = 8.9, 60, 30, 100_000.0
    partial_sum = c * (window - tau)  # flat history so far
    strike = 100_005.0
    n = 40_000
    values = []
    for _ in range(n):
        level, remaining = c, 0.0
        for _ in range(tau):
            level += rng.gauss(0.0, sigma)
            remaining += level
        values.append((partial_sum + remaining) / window)

    fc = within_window_forecast(partial_sum, c, tau, sigma, window)
    mu_mc = statistics.fmean(values)
    sd_mc = statistics.pstdev(values)
    p_mc = sum(1 for v in values if v > strike) / n

    assert abs(mu_mc - fc.mu) < 1.0
    # Discrete per-second sum runs ~2-3% above the continuous-integral std.
    assert abs(sd_mc - fc.sigma) / fc.sigma < 0.08
    assert abs(p_mc - fc.prob_above(strike)) < 0.02


def test_before_window_dominates_within_window_uncertainty() -> None:
    # The pre-window level diffusion (minute 13) dwarfs the in-window averaging
    # uncertainty: at the open, s_V is ~$39.8; 3 minutes out it is far larger.
    sigma = sigma_per_second_from_annual(100_000.0, 0.5)
    at_open = within_window_forecast(0.0, 100_000.0, 60, sigma).sigma
    three_min_out = before_window_forecast(100_000.0, 180.0, sigma).sigma
    assert three_min_out > 3.0 * at_open


def test_forecast_at_is_continuous_across_the_window_seam() -> None:
    sigma, c, window = 5.0, 72_000.0, 60
    at = forecast_at(window, c, sigma)
    just_before_open = forecast_at(window + 1e-3, c, sigma)
    just_inside = forecast_at(window - 1e-3, c, sigma)
    assert abs(at.mu - c) < 1e-6
    assert abs(just_before_open.sigma - at.sigma) < 0.01
    assert abs(just_inside.sigma - at.sigma) < 0.01


def test_prob_yes_locks_in_as_time_to_settlement_runs_out() -> None:
    sigma = 5.0
    target = 72_000.0
    # Currently $100 above target: P(Yes) rises toward 1 as u -> 0 (tau^{3/2} lock).
    ps = [prob_yes_at(u, target + 100.0, target, sigma) for u in (900, 300, 60, 10, 1)]
    assert ps == sorted(ps)
    assert ps[0] < 0.85 and ps[-1] > 0.99
    # At-the-money stays 50/50 at every horizon (driftless).
    for u in (900, 300, 60, 10):
        assert abs(prob_yes_at(u, target, target, sigma) - 0.5) < 1e-6


def test_implied_sigma_round_trips_and_is_none_at_the_money() -> None:
    sigma_true, c, target, u = 6.0, 72_100.0, 72_000.0, 600.0
    p = prob_yes_at(u, c, target, sigma_true)
    recovered = implied_sigma_per_sec(p, u, c, target)
    assert recovered is not None and abs(recovered - sigma_true) < 1e-4
    # ATM -> vol unidentified.
    assert implied_sigma_per_sec(0.5, u, target, target) is None
