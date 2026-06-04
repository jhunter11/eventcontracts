"""Kalshi crypto 60-second settlement convergence model.

Kalshi settles short-duration crypto contracts on the simple **mean of the CF
Benchmarks Real-Time Index (RTI) over the final ``T`` (=60) seconds** before
expiry. This module prices a binary "settle above ``K``" given the partial state
of that averaging window. It is the *pricing lens* for the arbitrage thesis, not
the edge: settlement is a deterministic average of observed prints, so you and
the market makers compute the same number from the same data. The edge (if any)
lives in two parameters only — a faster current index ``c`` (constituent lead)
and a better view of vol/drift ``sigma`` — which this model isolates.

Notation. Inside the window, at ``t`` seconds elapsed: ``S_t`` = sum of observed
per-second prints, ``tau = T - t`` remaining, ``c`` = current index. Model the
remaining index as a driftless arithmetic diffusion with per-second vol
``sigma``. Then the settlement

    V = (1/T) * sum_{i=1..T} p_i        is Gaussian:
    mu_V = (S_t + c * tau) / T
    s_V  = sigma * tau**1.5 / (T * sqrt(3))        # Var = sigma^2 * tau^3 / (3 T^2)
    P(settle above K) = Phi((mu_V - K) / s_V)

``s_V`` collapses as ``tau**1.5`` (the Asian-average kernel) — far faster than the
``sqrt(tau)`` of a vanilla option. So the settlement is nearly pinned the instant
the window opens; the real uncertainty is *where c lands when the window opens*
(ordinary ``sqrt(t)`` diffusion of the level), priced by `before_window_forecast`.

``delta_to_index`` (dP/dc) scales as ``1/sqrt(tau)`` — most valuable in the final
seconds, exactly when an order round-trip is least likely to land. That tension is
the whole game; this module makes it measurable rather than asserted.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import NormalDist

WINDOW_SECONDS = 60
_SECS_PER_YEAR = 365.0 * 24.0 * 3600.0


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def sigma_per_second_from_annual(price: float, annual_vol: float) -> float:
    """Per-second price vol (USD/s) from a spot level and annualised vol.

    $100k at 50% annual -> ~$8.9/s (the worked example in the thesis).
    """
    return price * annual_vol * math.sqrt(1.0 / _SECS_PER_YEAR)


@dataclass(frozen=True)
class SettlementForecast:
    """Gaussian forecast of the settlement value ``V``."""

    mu: float
    sigma: float

    def prob_above(self, strike: float) -> float:
        if self.sigma <= 0.0:
            return 1.0 if self.mu > strike else 0.0
        return _norm_cdf((self.mu - strike) / self.sigma)

    def prob_below(self, strike: float) -> float:
        return 1.0 - self.prob_above(strike)

    def delta_to_index(self, strike: float, d_mu_d_c: float) -> float:
        """dP/dc. ``d_mu_d_c`` = d(mu)/d(c): within-window it is ``tau / T``."""
        if self.sigma <= 0.0:
            return 0.0
        z = (self.mu - strike) / self.sigma
        return _norm_pdf(z) * d_mu_d_c / self.sigma


def within_window_forecast(
    partial_sum: float,
    current_index: float,
    tau: float,
    sigma_per_sec: float,
    window: int = WINDOW_SECONDS,
) -> SettlementForecast:
    """Forecast ``V`` from *inside* the settlement window.

    ``partial_sum`` = ``S_t`` summed over the ``window - tau`` elapsed seconds;
    ``current_index`` = ``c``; ``tau`` = seconds remaining in the window.
    """
    if not (0.0 <= tau <= window):
        raise ValueError(f"tau must be in [0, {window}], got {tau}")
    mu = (partial_sum + current_index * tau) / window
    s = sigma_per_sec * (tau**1.5) / (window * math.sqrt(3.0))
    return SettlementForecast(mu=mu, sigma=s)


def before_window_forecast(
    current_index: float,
    secs_to_window_open: float,
    sigma_per_sec: float,
    window: int = WINDOW_SECONDS,
) -> SettlementForecast:
    """Forecast ``V`` from *before* the window opens (e.g. minute 13).

    The level diffuses ``secs_to_window_open`` to the open (Var ``sigma^2 * t``),
    then the full-window average adds its own dispersion (Var ``sigma^2 * window/3``)
    around the opening level. The two are approximately independent, so
    ``Var(V) ~= sigma^2 * (secs_to_window_open + window/3)``; ``E[V] = c`` (driftless).
    This is the dominant uncertainty — ordinary ``sqrt(t)`` level diffusion — that
    the averaging kernel does *not* remove.
    """
    if secs_to_window_open < 0.0:
        raise ValueError("secs_to_window_open must be >= 0")
    var = sigma_per_sec**2 * (secs_to_window_open + window / 3.0)
    return SettlementForecast(mu=current_index, sigma=math.sqrt(var))


def forecast_at(
    seconds_to_expiry: float,
    current_index: float,
    sigma_per_sec: float,
    window: int = WINDOW_SECONDS,
    partial_sum: float | None = None,
) -> SettlementForecast:
    """Unified settlement forecast as a function of **time-to-settlement**.

    Spans the whole 15-minute contract life with one call, dispatching to the two
    exact regimes (they agree at the seam ``seconds_to_expiry == window``):

    * ``u >= window`` — *before the averaging window opens*. The level diffuses
      ``u - window`` seconds to the open, then the 60 s average runs. Exact:
      ``E[V]=c``, ``Var(V) = sigma^2 ((u-window) + window/3)`` (the cubic terms of
      the BM-integral cancel — this is the integral of the index over the window,
      not an approximation).
    * ``0 <= u < window`` — *inside the window*. With ``partial_sum`` ``S_t``
      observed over the elapsed ``window-u`` seconds, ``mu=(S_t + c*u)/window`` and
      ``Var = sigma^2 u^3/(3 window^2)``. If ``partial_sum`` is ``None`` we assume
      the window opened at the current level (``S_t = c*(window-u)`` -> ``mu=c``).
    """
    if seconds_to_expiry < 0:
        raise ValueError("seconds_to_expiry must be >= 0")
    if seconds_to_expiry >= window:
        return before_window_forecast(current_index, seconds_to_expiry - window, sigma_per_sec, window)
    tau = seconds_to_expiry
    s_t = partial_sum if partial_sum is not None else current_index * (window - tau)
    return within_window_forecast(s_t, current_index, tau, sigma_per_sec, window)


def prob_yes_at(
    seconds_to_expiry: float,
    current_index: float,
    target: float,
    sigma_per_sec: float,
    window: int = WINDOW_SECONDS,
    partial_sum: float | None = None,
) -> float:
    """P(settle >= target) for an 'up/down' contract at ``seconds_to_expiry`` out.

    For ``KXBTC15M`` the ``target`` is the index at window-open and YES pays if the
    60 s-average settlement is >= target.
    """
    return forecast_at(seconds_to_expiry, current_index, sigma_per_sec, window, partial_sum).prob_above(
        target
    )


def implied_sigma_per_sec(
    market_prob: float,
    seconds_to_expiry: float,
    current_index: float,
    target: float,
    window: int = WINDOW_SECONDS,
) -> float | None:
    """Back out the market-implied per-second vol from a quote (the sigma knob).

    Inverts ``P = Phi((c - K)/s_V)`` in the driftless ``u >= window`` regime. Returns
    ``None`` when ill-defined (P at the bounds, at-the-money where vol is
    unidentified, or inside the window). Comparing this to realized sigma is the
    honest 'are we disagreeing on vol?' check — the only knob besides ``c``.
    """
    if not (0.0 < market_prob < 1.0) or seconds_to_expiry < window:
        return None
    z = NormalDist().inv_cdf(market_prob)
    if abs(z) < 1e-6:
        return None  # ATM: P insensitive to sigma, vol unidentified
    s_v = (current_index - target) / z
    if s_v <= 0.0:
        return None
    base = (seconds_to_expiry - window) + window / 3.0
    return s_v / math.sqrt(base) if base > 0.0 else None
