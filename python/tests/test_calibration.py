"""Probability calibration + cost-aware edge gate."""

from __future__ import annotations

import math
import random

from eventcontracts.research.calibration import (
    IsotonicCalibrator,
    PlattCalibrator,
    brier_score,
    calibrate,
    kalshi_fee,
    log_loss,
    net_edge,
    reliability_table,
)


def _sig(z: float) -> float:
    return 1.0 / (1.0 + math.exp(-z))


def _logit(p: float) -> float:
    return math.log(p / (1.0 - p))


def _overconfident(n: int = 400, slope: float = 1.8, seed: int = 0) -> tuple[list[float], list[float]]:
    rng = random.Random(seed)
    probs: list[float] = []
    outcomes: list[float] = []
    for _ in range(n):
        true = min(0.98, max(0.02, rng.random()))
        probs.append(_sig(slope * _logit(true)))  # overconfident vs true
        outcomes.append(1.0 if rng.random() < true else 0.0)
    return probs, outcomes


def test_brier_and_log_loss_known_values() -> None:
    assert abs(brier_score([1.0, 0.0], [1.0, 0.0])) < 1e-12
    assert abs(brier_score([0.5, 0.5], [1.0, 0.0]) - 0.25) < 1e-12
    assert abs(log_loss([0.5, 0.5], [1.0, 0.0]) - math.log(2)) < 1e-9


def test_platt_calibration_improves_overconfident_model() -> None:
    probs, outcomes = _overconfident()
    cal, report = calibrate(probs, outcomes, method="platt")
    assert isinstance(cal, PlattCalibrator)
    assert report.logloss_after <= report.logloss_before  # identity is in the family
    assert report.logloss_after < report.logloss_before  # and overconfidence is correctable
    assert report.brier_after < report.brier_before
    # shrinks the slope back toward calibrated (a < 1 corrects overconfidence)
    assert cal.a < 1.0


def test_isotonic_calibration_reduces_brier_and_is_monotone() -> None:
    probs, outcomes = _overconfident(seed=3)
    cal, report = calibrate(probs, outcomes, method="isotonic")
    assert isinstance(cal, IsotonicCalibrator)
    assert report.brier_after <= report.brier_before
    grid = [i / 20 for i in range(21)]
    transformed = [cal.transform(p) for p in grid]
    assert all(b >= a - 1e-9 for a, b in zip(transformed, transformed[1:], strict=False))  # monotone
    assert all(0.0 <= t <= 1.0 for t in transformed)


def test_reliability_table_partitions_all_points() -> None:
    probs, outcomes = _overconfident(n=200)
    table = reliability_table(probs, outcomes, bins=10)
    assert sum(b.count for b in table) == 200


def test_net_edge_gate_matches_fee_curve() -> None:
    assert abs(kalshi_fee(0.5) - 0.07 * 0.25) < 1e-12
    # fair 0.40, ask 0.34 -> YES net = 0.40 - 0.34 - 0.07*0.34*0.66 > 0
    e = net_edge(0.40, yes_bid=0.32, yes_ask=0.34)
    assert e.side == "YES"
    assert e.tradable
    assert e.net_edge is not None and e.net_edge > 0
    # fair equals mid -> no edge clears cost
    flat = net_edge(0.33, yes_bid=0.32, yes_ask=0.34)
    assert flat.side == "NONE"
    assert not flat.tradable


def test_net_edge_picks_the_no_side_when_model_is_low() -> None:
    e = net_edge(0.20, yes_bid=0.44, yes_ask=0.46)  # model << market -> buy NO
    assert e.side == "NO"
    assert e.tradable
