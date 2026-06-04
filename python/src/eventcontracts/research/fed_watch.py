"""Fed funds futures-implied FOMC rate distribution (CME FedWatch methodology).

The fair value for a Kalshi ``KXFED`` contract is the risk-neutral probability
over post-meeting target-rate levels. CME's FedWatch derives it from 30-day Fed
Funds futures: the futures price implies the month-average EFFR (``100 - price``);
within a month containing an FOMC meeting that average is the day-weighted blend
of the pre- and post-meeting target, so the post-meeting rate backs out, and the
fractional position between adjacent 25 bp grid levels is the move probability.

This is the *pure* pricing core -> a :class:`DiscreteDistribution` (the right
object for a discrete decision; a continuous CDF is not). Wiring live Fed Funds
futures quotes is separate plumbing. Reference: CME Group, "Understanding the
CME Group FedWatch Tool Methodology" (2023).
"""

from __future__ import annotations

import math

from eventcontracts.research.distributions import DiscreteDistribution

STEP = 0.25


def implied_month_average_rate(futures_price: float) -> float:
    """Month-average EFFR implied by a 30-day Fed Funds future (``100 - price``)."""

    if not 0.0 <= futures_price <= 100.0:
        raise ValueError("futures_price must be in [0, 100]")
    return 100.0 - futures_price


def post_meeting_rate(
    month_average_rate: float,
    current_rate: float,
    *,
    days_before_meeting: int,
    days_in_month: int,
) -> float:
    """Back out the post-meeting target from the day-weighted month average.

    ``avg = (days_before/N) * current + (days_after/N) * post`` solved for ``post``.
    """

    if not 0 <= days_before_meeting < days_in_month:
        raise ValueError("require 0 <= days_before_meeting < days_in_month")
    days_after = days_in_month - days_before_meeting
    return (month_average_rate * days_in_month - current_rate * days_before_meeting) / days_after


def grid_distribution(implied_rate: float, current_rate: float, *, step: float = STEP) -> DiscreteDistribution:
    """Distribute the implied post-meeting rate across adjacent 25 bp grid levels."""

    if step <= 0:
        raise ValueError("step must be > 0")
    k = (implied_rate - current_rate) / step
    lower_k = math.floor(k)
    frac = k - lower_k  # in [0, 1)
    lower = round(current_rate + lower_k * step, 4)
    upper = round(lower + step, 4)
    if frac <= 1e-9:
        return DiscreteDistribution({lower: 1.0})
    return DiscreteDistribution({lower: 1.0 - frac, upper: frac})


def fedwatch_distribution(
    futures_price: float,
    current_rate: float,
    *,
    days_before_meeting: int,
    days_in_month: int,
    step: float = STEP,
) -> DiscreteDistribution:
    """End-to-end: futures price -> post-meeting target-rate distribution."""

    avg = implied_month_average_rate(futures_price)
    post = post_meeting_rate(
        avg, current_rate, days_before_meeting=days_before_meeting, days_in_month=days_in_month
    )
    return grid_distribution(post, current_rate, step=step)
