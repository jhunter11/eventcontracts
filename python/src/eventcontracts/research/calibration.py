"""Probability calibration + cost-aware edge gate.

A model's raw probability is not a tradable edge. Two things stand between
``p_model`` and a trade:

1. **Calibration.** The model may be systematically over/under-confident. We
   recalibrate against settled outcomes with Platt scaling (logistic on the
   log-odds) or isotonic regression (monotone, non-parametric), and *measure*
   the improvement with Brier score + log-loss + a reliability table rather than
   assuming it. This generalizes the weather KXHIGH->NOAA calibration to any
   model, and is the only honest way to read a model-vs-market gap: an
   uncalibrated gap is not edge.
2. **Cost.** The calibrated gap to the market must clear the Kalshi fee curve
   (``0.07 * p * (1 - p)``) plus half-spread and slippage. ``net_edge`` is the
   shared gate (the same arithmetic as ``btc_lead`` / ``ladder_cdf``): trade only
   on the side whose post-cost edge is positive.

Pure stdlib + a local scipy.optimize call for the 2-parameter Platt fit.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

_EPS = 1e-12


def _clip01(p: float) -> float:
    return min(1.0 - _EPS, max(_EPS, p))


def _logit(p: float) -> float:
    p = _clip01(p)
    return math.log(p / (1.0 - p))


def _sigmoid(z: float) -> float:
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)


def _log1pexp(z: float) -> float:
    """Numerically stable ``log(1 + exp(z))``."""

    if z > 0:
        return z + math.log1p(math.exp(-z))
    return math.log1p(math.exp(z))


def brier_score(probs: Sequence[float], outcomes: Sequence[float]) -> float:
    """Mean squared error of probabilistic forecasts (lower is better)."""

    if len(probs) != len(outcomes) or not probs:
        raise ValueError("probs and outcomes must be non-empty and equal length")
    return float(sum((p - y) ** 2 for p, y in zip(probs, outcomes, strict=True)) / len(probs))


def log_loss(probs: Sequence[float], outcomes: Sequence[float]) -> float:
    """Mean binary cross-entropy (lower is better)."""

    if len(probs) != len(outcomes) or not probs:
        raise ValueError("probs and outcomes must be non-empty and equal length")
    total = 0.0
    for p, y in zip(probs, outcomes, strict=True):
        pc = _clip01(p)
        total += -(y * math.log(pc) + (1.0 - y) * math.log(1.0 - pc))
    return float(total / len(probs))


@dataclass(frozen=True)
class ReliabilityBin:
    lo: float
    hi: float
    mean_pred: float
    obs_freq: float
    count: int


def reliability_table(
    probs: Sequence[float], outcomes: Sequence[float], *, bins: int = 10
) -> list[ReliabilityBin]:
    """Bin forecasts and compare mean predicted vs observed frequency."""

    if bins <= 0:
        raise ValueError("bins must be > 0")
    edges = [i / bins for i in range(bins + 1)]
    out: list[ReliabilityBin] = []
    for b in range(bins):
        lo, hi = edges[b], edges[b + 1]
        last = b == bins - 1
        members = [(p, y) for p, y in zip(probs, outcomes, strict=True) if lo <= p < hi or (last and p == hi)]
        if not members:
            out.append(ReliabilityBin(lo, hi, float("nan"), float("nan"), 0))
            continue
        mp = sum(p for p, _ in members) / len(members)
        of = sum(y for _, y in members) / len(members)
        out.append(ReliabilityBin(lo, hi, mp, of, len(members)))
    return out


@dataclass(frozen=True)
class PlattCalibrator:
    """Logistic recalibration on the log-odds: ``sigmoid(a * logit(p) + b)``."""

    a: float
    b: float

    @classmethod
    def fit(cls, probs: Sequence[float], outcomes: Sequence[float], *, l2: float = 1e-6) -> PlattCalibrator:
        from scipy.optimize import minimize  # type: ignore[import-untyped]

        f = [_logit(p) for p in probs]
        y = list(outcomes)
        n = len(f)

        def nll(params: Sequence[float]) -> float:
            a, b = float(params[0]), float(params[1])
            total = 0.0
            for fi, yi in zip(f, y, strict=True):
                z = a * fi + b
                total += _log1pexp(z) - yi * z
            return total / n + l2 * (a * a + b * b)

        res = minimize(nll, [1.0, 0.0], method="BFGS")
        return cls(a=float(res.x[0]), b=float(res.x[1]))

    def transform(self, p: float) -> float:
        return _sigmoid(self.a * _logit(p) + self.b)

    def transform_many(self, probs: Sequence[float]) -> list[float]:
        return [self.transform(p) for p in probs]


@dataclass(frozen=True)
class IsotonicCalibrator:
    """Monotone non-parametric recalibration via pool-adjacent-violators."""

    thresholds: tuple[float, ...]  # ascending block upper-bounds in raw-prob space
    levels: tuple[float, ...]  # calibrated value per block (non-decreasing)

    @classmethod
    def fit(cls, probs: Sequence[float], outcomes: Sequence[float]) -> IsotonicCalibrator:
        order = sorted(range(len(probs)), key=lambda i: probs[i])
        xs = [probs[i] for i in order]
        ys = [float(outcomes[i]) for i in order]
        # PAV: blocks of [sum_y, count, x_upper]
        blocks: list[list[float]] = []
        for x, y in zip(xs, ys, strict=True):
            blocks.append([y, 1.0, x])
            while len(blocks) >= 2 and blocks[-2][0] / blocks[-2][1] >= blocks[-1][0] / blocks[-1][1]:
                s2, c2, _x2 = blocks.pop()
                s1, c1, x1b = blocks.pop()
                blocks.append([s1 + s2, c1 + c2, max(x1b, _x2)])
        thresholds = tuple(b[2] for b in blocks)
        levels = tuple(b[0] / b[1] for b in blocks)
        return cls(thresholds=thresholds, levels=levels)

    def transform(self, p: float) -> float:
        for thr, lvl in zip(self.thresholds, self.levels, strict=True):
            if p <= thr:
                return _clip01(lvl) if lvl in (0.0, 1.0) else min(1.0, max(0.0, lvl))
        return min(1.0, max(0.0, self.levels[-1])) if self.levels else p


@dataclass(frozen=True)
class CalibrationReport:
    method: str
    n: int
    brier_before: float
    brier_after: float
    logloss_before: float
    logloss_after: float


def calibrate(
    probs: Sequence[float], outcomes: Sequence[float], *, method: str = "platt"
) -> tuple[PlattCalibrator | IsotonicCalibrator, CalibrationReport]:
    """Fit a calibrator and report Brier + log-loss before/after (in-sample)."""

    if method == "platt":
        cal: PlattCalibrator | IsotonicCalibrator = PlattCalibrator.fit(probs, outcomes)
    elif method == "isotonic":
        cal = IsotonicCalibrator.fit(probs, outcomes)
    else:
        raise ValueError("method must be 'platt' or 'isotonic'")
    after = [cal.transform(p) for p in probs]
    report = CalibrationReport(
        method=method,
        n=len(probs),
        brier_before=brier_score(probs, outcomes),
        brier_after=brier_score(after, outcomes),
        logloss_before=log_loss(probs, outcomes),
        logloss_after=log_loss(after, outcomes),
    )
    return cal, report


# --- cost-aware edge gate ----------------------------------------------------


@dataclass(frozen=True)
class NetEdge:
    """Best-side post-cost edge for a binary contract."""

    side: str  # "YES" | "NO" | "NONE"
    fair_yes: float
    executable_price: float | None
    fee: float | None
    net_edge: float | None

    @property
    def tradable(self) -> bool:
        return self.net_edge is not None and self.net_edge > 0.0


def kalshi_fee(price: float, fee_coeff: float = 0.07) -> float:
    """Kalshi-style fee curve: ``fee_coeff * price * (1 - price)`` (max near 0.5)."""

    return fee_coeff * price * (1.0 - price)


def net_edge(
    fair_yes: float,
    *,
    yes_bid: float,
    yes_ask: float,
    fee_coeff: float = 0.07,
    slippage: float = 0.0,
    min_edge: float = 0.0,
) -> NetEdge:
    """Cost-aware edge gate (same arithmetic as ``btc_lead`` / ``ladder_cdf``).

    Compares the calibrated fair value to the executable price on each side, nets
    the fee curve + slippage, and returns the better side iff it clears
    ``min_edge``. ``fair_yes`` should already be **calibrated**.
    """

    yes_net = fair_yes - yes_ask - kalshi_fee(yes_ask, fee_coeff) - slippage
    no_price = 1.0 - yes_bid
    fair_no = 1.0 - fair_yes
    no_net = fair_no - no_price - kalshi_fee(no_price, fee_coeff) - slippage
    if max(yes_net, no_net) < min_edge:
        return NetEdge(side="NONE", fair_yes=fair_yes, executable_price=None, fee=None, net_edge=max(yes_net, no_net))
    if yes_net >= no_net:
        return NetEdge("YES", fair_yes, yes_ask, kalshi_fee(yes_ask, fee_coeff), yes_net)
    return NetEdge("NO", fair_yes, no_price, kalshi_fee(no_price, fee_coeff), no_net)
