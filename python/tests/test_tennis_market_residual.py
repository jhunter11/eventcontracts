"""Tests for the independent tennis market-residual validator.

All deterministic and offline (synthetic fixtures only). They pin the two
findings the harness exists to encode:
  * an efficient market cannot be beaten by an outcome-derived model;
  * a real ~2.5pp gross edge is wiped out by a realistic Kalshi spread + fee.
"""
from __future__ import annotations

import math

import numpy as np

from eventcontracts.research.tennis_market_residual import (
    EloBook,
    MarketResidualEvaluator,
    assess_prematch,
    devig_two_way,
    executable_edge_gate,
    kalshi_taker_fee_per_contract,
    synthetic_matches,
)


# --------------------------- core market math ------------------------------ #
def test_devig_complementary_and_overround() -> None:
    pa, over = devig_two_way(2.0, 2.0)
    assert pa == 0.5
    assert over == 1.0
    pa, over = devig_two_way(1.5, 3.0)  # raw 0.6667 + 0.3333 = 1.0 exactly
    assert math.isclose(pa, 2 / 3, rel_tol=1e-9)
    # a real market with margin -> overround > 1, probs still sum to 1
    pa, over = devig_two_way(1.8, 2.1)
    pb, _ = devig_two_way(2.1, 1.8)
    assert over > 1.0
    assert math.isclose(pa + pb, 1.0, rel_tol=1e-9)


def test_devig_invalid_inputs() -> None:
    assert all(math.isnan(v) for v in devig_two_way(1.0, 2.0))
    assert all(math.isnan(v) for v in devig_two_way(float("nan"), 2.0))


def test_kalshi_fee_matches_canonical_curve() -> None:
    # parity with rust/crates/risk/src/fees.rs unit tests (per-contract dollars)
    assert kalshi_taker_fee_per_contract(0.50, 1) == 0.02  # 0.0175 -> ceil 0.02
    assert kalshi_taker_fee_per_contract(0.10, 1) == 0.01  # 0.0063 -> ceil 0.01
    assert kalshi_taker_fee_per_contract(0.90, 1) == 0.01  # 0.0063 -> ceil 0.01
    assert kalshi_taker_fee_per_contract(0.50, 10) == 0.018  # ceil(0.175)=0.18 /10
    # boundaries pay nothing
    assert kalshi_taker_fee_per_contract(1.0, 10) == 0.0
    assert kalshi_taker_fee_per_contract(0.5, 0) == 0.0


def test_fee_ceiling_bites_at_extremes() -> None:
    # at a 0.95 favorite the smooth fee is ~0.0033 but the cent ceiling forces 0.01
    assert kalshi_taker_fee_per_contract(0.95, 1) == 0.01


# ------------------------------- Elo --------------------------------------- #
def test_elo_symmetry_and_monotonicity() -> None:
    elo = EloBook()
    # equal ratings -> 50/50 expectation
    assert elo.expected(1500.0, 1500.0) == 0.5
    # a win raises the winner and lowers the loser
    elo.update("A", "B", "Hard")
    ra, _, _ = elo.rating("A", "Hard")
    rb, _, _ = elo.rating("B", "Hard")
    assert ra > 1500.0 > rb
    # surface rating only moved on the played surface
    _, sa_hard, _ = elo.rating("A", "Hard")
    _, sa_clay, _ = elo.rating("A", "Clay")
    assert sa_hard > 1500.0
    assert sa_clay == 1500.0


# ------------------- evaluator: the central findings ----------------------- #
def test_efficient_market_is_not_beaten() -> None:
    recs = synthetic_matches(n_matches=4000, market_calibration=1.0, seed=1)
    res = MarketResidualEvaluator(min_train=800).walk_forward(recs)
    assert res.market.n > 500
    # market is well calibrated...
    assert res.market.ece < 0.03
    # ...and an outcome-derived anchored model cannot meaningfully beat it
    assert res.brier_skill_score < 0.01


def test_underconfident_market_is_beatable() -> None:
    # market prices a deflated (underconfident) line -> recoverable residual
    recs = synthetic_matches(n_matches=4000, market_calibration=0.6, seed=2)
    res = MarketResidualEvaluator(min_train=800).walk_forward(recs)
    assert res.beats_market
    assert res.brier_skill_score > 0.01


# ------------------- executable-edge gate: the kill ------------------------ #
def _favorite_scenario() -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    n = 2000
    market_p = np.full(n, 0.90)
    model_p = np.full(n, 0.95)  # model says the favorite is underpriced by 5pp
    y = np.zeros(n)
    y[: int(0.925 * n)] = 1.0  # favorite actually wins 92.5% -> real 2.5pp edge
    return model_p, market_p, y


def test_real_edge_survives_zero_cost_but_dies_on_spread_and_fee() -> None:
    model_p, market_p, y = _favorite_scenario()
    free = executable_edge_gate(model_p, market_p, y, edge_threshold=0.03, cross=0.0)
    assert free["n_bets"] == 2000
    assert free["ev_per_contract"] > 0.0  # the 2.5pp edge is real at a free fill
    real = executable_edge_gate(model_p, market_p, y, edge_threshold=0.03, cross=0.02)
    assert real["ev_per_contract"] < 0.0  # ...but a 4c spread + fee erases it
    assert real["is_positive"] == 0.0


def test_gate_no_bets_when_no_disagreement() -> None:
    p = np.full(500, 0.6)
    y = (np.arange(500) % 2).astype(float)
    out = executable_edge_gate(p, p.copy(), y, edge_threshold=0.03)
    assert out["n_bets"] == 0


# ----------------------- runner-compatible payload ------------------------- #
def test_assess_prematch_no_odds_is_no_trade() -> None:
    a = assess_prematch(
        probability=0.72,
        market_implied_prob=None,
        odds_present=False,
        odds_source=None,
        executable_price=None,
    )
    assert a.recommend == "no_trade"
    assert a.odds_present is False
    assert a.edge_vs_market is None
    assert a.requires_market_snapshot is True
    assert math.isclose(a.confidence, 0.44, rel_tol=1e-9)


def test_assess_prematch_candidate_only_if_fee_adjusted_positive() -> None:
    # fair value 0.80, executable 0.74 -> 6pp gross, fee ~0.01 -> still positive
    good = assess_prematch(
        probability=0.80,
        market_implied_prob=0.75,
        odds_present=True,
        odds_source="pinnacle",
        executable_price=0.74,
        side="yes",
    )
    assert good.recommend == "candidate"
    assert good.fee_adjusted_edge is not None and good.fee_adjusted_edge > 0.0
    # fair value 0.80 but executable 0.795 -> gross 0.5pp < fee -> no trade
    thin = assess_prematch(
        probability=0.80,
        market_implied_prob=0.79,
        odds_present=True,
        odds_source="pinnacle",
        executable_price=0.795,
        side="yes",
    )
    assert thin.recommend == "no_trade"
    assert thin.fee_adjusted_edge is not None and thin.fee_adjusted_edge < 0.0
