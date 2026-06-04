"""Macro fair-value models: Fed futures-implied distribution, CPI nowcast."""

from __future__ import annotations

from eventcontracts.research.cpi_nowcast import CpiNowcast, surprise_sigma_from_history
from eventcontracts.research.fed_watch import (
    fedwatch_distribution,
    grid_distribution,
    implied_month_average_rate,
    post_meeting_rate,
)


def test_implied_month_average_rate() -> None:
    assert abs(implied_month_average_rate(95.50) - 4.50) < 1e-12


def test_post_meeting_rate_backs_out_the_meeting_step() -> None:
    # month avg 4.40, current 4.50, meeting 10 days into a 30-day month
    r = post_meeting_rate(4.40, 4.50, days_before_meeting=10, days_in_month=30)
    assert abs(r - 4.35) < 1e-9  # (4.40*30 - 4.50*10) / 20


def test_grid_distribution_splits_between_adjacent_levels() -> None:
    d = grid_distribution(4.35, 4.50, step=0.25)  # between 4.25 and 4.50
    assert abs(d.prob_above(4.40) - 0.40) < 1e-9  # mass on 4.50
    assert abs(d.cdf(4.25) - 0.60) < 1e-9  # mass on 4.25
    assert abs(d.mean - 4.35) < 1e-9  # mean recovers the implied rate


def test_fedwatch_distribution_end_to_end_prices_a_cut_bias() -> None:
    # futures 95.65 -> avg 4.35; meeting 10/30 days; current 4.50 -> post 4.275
    d = fedwatch_distribution(95.65, 4.50, days_before_meeting=10, days_in_month=30)
    assert abs(d.mean - 4.275) < 1e-9
    # cut bias: most mass below the current 4.50 level
    assert d.prob_above(4.49) < 0.2
    assert abs(sum(d.masses.values()) - 1.0) < 1e-9


def test_cpi_nowcast_distribution_and_delta() -> None:
    nc = CpiNowcast(nowcast=3.2, consensus=3.0, surprise_sigma=0.15)
    assert abs(nc.delta - 0.2) < 1e-12  # hotter than consensus
    dist = nc.distribution()
    assert abs(dist.prob_above(3.2) - 0.5) < 1e-9  # normal centered on the nowcast
    assert dist.prob_above(3.0) > 0.5  # market anchored to 3.0 underprices the upside
    payload = nc.ladder_signal_payload()
    assert payload == {"mean": 3.2, "sigma": 0.15, "dist": "normal"}


def test_cpi_surprise_sigma_from_history() -> None:
    nowcasts = [3.0, 3.1, 3.2, 3.0, 2.9]
    actuals = [3.1, 3.1, 3.4, 2.9, 3.0]  # errors: +.1, 0, +.2, -.1, +.1
    sigma = surprise_sigma_from_history(nowcasts, actuals)
    assert sigma > 0
