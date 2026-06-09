from __future__ import annotations

from datetime import date, datetime

import pytest

from eventcontracts.research.nba_elo import (
    EloParams,
    EnhancedEloParams,
    IsotonicCalibrator,
    NbaEloGame,
    NbaPregameFeatures,
    PlattCalibrator,
    expected_calibration_error,
    fit_logistic_feature_model,
    metrics_for,
    paired_bootstrap_improvement,
    run_elo,
    run_enhanced_elo,
)


def _game(
    game_id: str,
    day: int,
    team: str,
    opponent: str,
    points: int,
    opponent_points: int,
    *,
    location: str = "H",
    season: int = 2024,
) -> NbaEloGame:
    return NbaEloGame(
        game_id=game_id,
        season=season,
        game_date=date(season, 1, day),
        team=team,
        opponent=opponent,
        location=location,
        points=points,
        opponent_points=opponent_points,
        is_playoffs=False,
        fivethirtyeight_forecast=0.5,
    )


def test_elo_predictions_are_point_in_time_no_result_leakage() -> None:
    games = [
        _game("g1", 1, "A", "B", 110, 100, location="N"),
        _game("g2", 2, "A", "B", 90, 100, location="N"),
    ]

    preds = run_elo(games, EloParams(k=24.0, home_advantage=0.0, rest_advantage=0.0, mov_weight=0.0))

    assert preds[0].raw_rating_diff == pytest.approx(0.0)
    assert preds[0].probability == pytest.approx(0.5)
    assert preds[1].raw_rating_diff > 0.0
    assert preds[1].probability > 0.5


def test_home_and_rest_adjustments_are_pregame_only() -> None:
    games = [
        _game("g1", 1, "A", "B", 100, 90, location="H"),
        _game("g2", 2, "C", "B", 90, 100, location="H"),
        _game("g3", 5, "A", "B", 100, 90, location="H"),
    ]

    preds = run_elo(games, EloParams(k=0.0, home_advantage=70.0, rest_advantage=4.0, mov_weight=0.0))

    assert preds[0].adjusted_rating_diff == pytest.approx(70.0)
    assert preds[2].adjusted_rating_diff == pytest.approx(74.0)


def test_metrics_and_ece_identify_good_calibration() -> None:
    outcomes = [0, 0, 1, 1]
    good = [0.1, 0.2, 0.8, 0.9]
    bad = [0.9, 0.8, 0.2, 0.1]

    good_metrics = metrics_for(good, outcomes)
    bad_metrics = metrics_for(bad, outcomes)

    assert good_metrics.brier < bad_metrics.brier
    assert expected_calibration_error(good, outcomes, bins=2) < expected_calibration_error([0.9] * 4, outcomes, bins=2)


def test_calibrators_transform_probabilities_without_new_labels() -> None:
    probs = [0.05, 0.2, 0.3, 0.7, 0.8, 0.95]
    outcomes = [0, 0, 0, 1, 1, 1]

    platt = PlattCalibrator.fit(probs, outcomes)
    isotonic = IsotonicCalibrator.fit(probs, outcomes)

    assert platt.transform(0.8) > platt.transform(0.2)
    assert isotonic.transform(0.8) >= isotonic.transform(0.2)
    assert all(0.0 < p < 1.0 for p in platt.transform_many(probs))
    assert all(0.0 < p < 1.0 for p in isotonic.transform_many(probs))


def test_paired_bootstrap_detects_significant_brier_improvement() -> None:
    outcomes = [0, 1] * 200
    baseline = [0.5] * len(outcomes)
    candidate = [0.2 if y == 0 else 0.8 for y in outcomes]

    sig = paired_bootstrap_improvement(
        baseline_probs=baseline,
        candidate_probs=candidate,
        outcomes=outcomes,
        samples=400,
        seed=3,
    )

    assert sig.improvement > 0
    assert sig.ci_low > 0
    assert sig.significant


def test_enhanced_features_apply_only_before_cutoff() -> None:
    game = _game(
        "g1",
        2,
        "Lakers",
        "Celtics",
        110,
        100,
        location="H",
        season=2024,
    )
    params = EnhancedEloParams(market_shrink=0.0, uncertainty_initial=0.0, uncertainty_floor=0.0)
    timely = {
        ("g1", "Lakers"): NbaPregameFeatures(
            game_id="g1",
            team="Lakers",
            available_at=datetime(2024, 1, 1, 12, 0),
            minutes_weighted_impact=8.0,
        )
    }
    late = {
        ("g1", "Lakers"): NbaPregameFeatures(
            game_id="g1",
            team="Lakers",
            available_at=datetime(2024, 1, 2, 12, 0),
            minutes_weighted_impact=8.0,
        )
    }

    base = run_enhanced_elo([game], params)[0]
    with_timely = run_enhanced_elo([game], params, pregame_features=timely)[0]
    with_late = run_enhanced_elo([game], params, pregame_features=late)[0]

    assert with_timely.expected_margin > base.expected_margin
    assert with_late.expected_margin == pytest.approx(base.expected_margin)


def test_market_probability_is_blended_and_edge_recorded() -> None:
    game = _game("g1", 2, "Lakers", "Celtics", 110, 100, location="H", season=2024)
    params = EnhancedEloParams(market_shrink=0.5, uncertainty_initial=0.0, uncertainty_floor=0.0)
    features = {
        ("g1", "Lakers"): NbaPregameFeatures(
            game_id="g1",
            team="Lakers",
            available_at=datetime(2024, 1, 1, 12, 0),
            moneyline_prob=0.75,
        )
    }

    pred = run_enhanced_elo([game], params, pregame_features=features)[0]

    assert pred.market_probability == pytest.approx(0.75)
    assert pred.probability == pytest.approx(0.5 * pred.base_probability + 0.5 * 0.75)
    assert pred.model_market_edge == pytest.approx(pred.base_probability - 0.75)


def test_second_stage_model_learns_feature_interaction() -> None:
    predictions = []
    for idx in range(200):
        strong = idx % 2 == 0
        predictions.append(
            type(
                "P",
                (),
                {
                    "outcome": 1 if strong else 0,
                    "features": {
                        "base_probability": 0.5,
                        "expected_margin": 8.0 if strong else -8.0,
                        "elo_points": 0.0,
                        "offense_defense_points": 0.0,
                        "rest_diff": 0.0,
                        "team_back_to_back": 0.0,
                        "opponent_back_to_back": 0.0,
                        "team_three_in_four": 0.0,
                        "opponent_three_in_four": 0.0,
                        "travel_diff_1000": 0.0,
                        "timezone_diff": 0.0,
                        "altitude_penalty": 0.0,
                        "form_points": 0.0,
                        "player_points": 0.0,
                        "matchup_points": 0.0,
                        "market_probability": 0.0,
                        "model_market_edge": 0.0,
                        "uncertainty": 0.0,
                    },
                },
            )()
        )

    model = fit_logistic_feature_model(predictions)

    assert model.predict({"expected_margin": 8.0, "base_probability": 0.5}) > 0.8
    assert model.predict({"expected_margin": -8.0, "base_probability": 0.5}) < 0.2
