"""Point-in-time NBA Elo research and calibration.

This module builds custom Elo-like NBA win-probability ratings and evaluates
them chronologically. It is deliberately research-only: a game outcome model is
not a tradable edge unless it beats a market-implied baseline and survives CLV,
fees, spread, and fill checks.
"""

from __future__ import annotations

import csv
import math
import random
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

_EPS = 1e-12


@dataclass(frozen=True)
class NbaEloGame:
    game_id: str
    season: int
    game_date: date
    team: str
    opponent: str
    location: str
    points: int
    opponent_points: int
    is_playoffs: bool
    fivethirtyeight_forecast: float | None = None
    game_start_at: datetime | None = None

    @property
    def outcome(self) -> int:
        return 1 if self.points > self.opponent_points else 0

    @property
    def margin(self) -> int:
        return self.points - self.opponent_points


@dataclass(frozen=True)
class TeamVenueMeta:
    latitude: float
    longitude: float
    utc_offset_hours: float
    altitude_feet: float = 0.0


@dataclass(frozen=True)
class NbaPregameFeatures:
    """Optional pregame-only feature row keyed by ``(game_id, team)``.

    Rows with ``available_at`` after the game cutoff are ignored. The historical
    FiveThirtyEight source only has a game date, so the conservative cutoff is
    midnight at the start of that date unless ``NbaEloGame.game_start_at`` is
    populated by a richer source.
    """

    game_id: str
    team: str
    available_at: datetime
    injury_impact: float = 0.0
    starter_absences: float = 0.0
    minutes_weighted_impact: float = 0.0
    usage_lost: float = 0.0
    usage_returning: float = 0.0
    star_back_to_back_risk: float = 0.0
    moneyline_prob: float | None = None
    spread_implied_prob: float | None = None
    opening_moneyline_prob: float | None = None
    closing_moneyline_prob: float | None = None
    three_point_attempt_rate: float | None = None
    three_point_defense_rate: float | None = None
    offensive_rebound_rate: float | None = None
    defensive_rebound_rate: float | None = None
    turnover_rate: float | None = None
    forced_turnover_rate: float | None = None
    free_throw_rate: float | None = None
    foul_rate: float | None = None
    possessions_per_game: float | None = None


@dataclass(frozen=True)
class EnhancedEloPrediction:
    game_id: str
    season: int
    game_date: date
    team: str
    opponent: str
    location: str
    outcome: int
    probability: float
    base_probability: float
    expected_margin: float
    market_probability: float | None
    model_market_edge: float | None
    fivethirtyeight_forecast: float | None
    features: dict[str, float]


@dataclass(frozen=True)
class LogisticFeatureModel:
    feature_names: tuple[str, ...]
    means: tuple[float, ...]
    scales: tuple[float, ...]
    weights: tuple[float, ...]

    def predict(self, features: dict[str, float]) -> float:
        z = self.weights[0]
        for idx, name in enumerate(self.feature_names):
            value = (features.get(name, 0.0) - self.means[idx]) / self.scales[idx]
            z += self.weights[idx + 1] * value
        return _sigmoid(z)


@dataclass(frozen=True)
class EloParams:
    k: float = 24.0
    home_advantage: float = 70.0
    scale: float = 400.0
    rest_advantage: float = 4.0
    mov_weight: float = 0.6
    season_reversion: float = 0.8
    playoff_home_multiplier: float = 1.0
    base_rating: float = 1500.0


@dataclass(frozen=True)
class EnhancedEloParams:
    base: EloParams = field(default_factory=EloParams)
    league_avg_points: float = 102.0
    offense_defense_k: float = 0.08
    pace_k: float = 0.05
    margin_scale: float = 12.0
    elo_rating_to_points: float = 0.025
    home_points: float = 2.4
    playoff_home_multiplier: float = 1.05
    rest_points: float = 0.25
    back_to_back_penalty_points: float = 0.8
    three_in_four_penalty_points: float = 0.5
    travel_1000_mile_penalty_points: float = 0.20
    timezone_penalty_points: float = 0.25
    altitude_back_to_back_penalty_points: float = 0.7
    home_away_streak_points: float = 0.10
    form_weight: float = 0.14
    form_shrink: float = 5.0
    player_impact_points: float = 1.0
    starter_absence_points: float = 0.75
    usage_lost_points: float = 0.06
    usage_returning_points: float = 0.05
    star_back_to_back_points: float = 0.8
    matchup_points: float = 2.0
    market_shrink: float = 0.20
    uncertainty_initial: float = 8.0
    uncertainty_floor: float = 1.5
    uncertainty_decay: float = 0.985
    uncertainty_result_bump: float = 0.25
    uncertainty_margin_shrink: float = 20.0
    second_stage_l2: float = 0.5


@dataclass(frozen=True)
class EloPrediction:
    game_id: str
    season: int
    game_date: date
    team: str
    opponent: str
    location: str
    outcome: int
    probability: float
    raw_rating_diff: float
    adjusted_rating_diff: float
    fivethirtyeight_forecast: float | None


@dataclass(frozen=True)
class ForecastMetrics:
    n: int
    brier: float
    log_loss: float
    ece: float
    accuracy: float


@dataclass(frozen=True)
class PairedSignificance:
    improvement: float
    ci_low: float
    ci_high: float
    p_value: float
    samples: int

    @property
    def significant(self) -> bool:
        return self.improvement > 0.0 and self.ci_low > 0.0 and self.p_value < 0.05


@dataclass(frozen=True)
class PlattCalibrator:
    """Logistic recalibration on log-odds: ``sigmoid(a * logit(p) + b)``."""

    a: float
    b: float

    @classmethod
    def fit(cls, probs: Sequence[float], outcomes: Sequence[int], *, l2: float = 1e-6) -> PlattCalibrator:
        if len(probs) != len(outcomes) or not probs:
            raise ValueError("probs and outcomes must be non-empty and equal length")
        features = [_logit(p) for p in probs]
        y = [float(v) for v in outcomes]
        a = 1.0
        b = 0.0
        n = float(len(features))

        for _ in range(80):
            g_a = 2.0 * l2 * a
            g_b = 2.0 * l2 * b
            h_aa = 2.0 * l2
            h_ab = 0.0
            h_bb = 2.0 * l2
            for x_i, y_i in zip(features, y, strict=True):
                pred = _sigmoid(a * x_i + b)
                err = pred - y_i
                weight = pred * (1.0 - pred)
                g_a += err * x_i / n
                g_b += err / n
                h_aa += weight * x_i * x_i / n
                h_ab += weight * x_i / n
                h_bb += weight / n

            det = h_aa * h_bb - h_ab * h_ab
            if abs(det) < 1e-14:
                break
            step_a = (h_bb * g_a - h_ab * g_b) / det
            step_b = (-h_ab * g_a + h_aa * g_b) / det
            a -= step_a
            b -= step_b
            if abs(step_a) + abs(step_b) < 1e-10:
                break
        return cls(a=a, b=b)

    def transform(self, p: float) -> float:
        return _sigmoid(self.a * _logit(p) + self.b)

    def transform_many(self, probs: Sequence[float]) -> list[float]:
        return [self.transform(p) for p in probs]


@dataclass(frozen=True)
class IsotonicCalibrator:
    """Monotone non-parametric recalibration via pool-adjacent-violators."""

    thresholds: tuple[float, ...]
    levels: tuple[float, ...]

    @classmethod
    def fit(cls, probs: Sequence[float], outcomes: Sequence[int]) -> IsotonicCalibrator:
        if len(probs) != len(outcomes) or not probs:
            raise ValueError("probs and outcomes must be non-empty and equal length")
        order = sorted(range(len(probs)), key=lambda i: probs[i])
        xs = [float(probs[i]) for i in order]
        ys = [float(outcomes[i]) for i in order]
        blocks: list[list[float]] = []
        for x_val, y_val in zip(xs, ys, strict=True):
            blocks.append([y_val, 1.0, x_val])
            while len(blocks) >= 2 and blocks[-2][0] / blocks[-2][1] >= blocks[-1][0] / blocks[-1][1]:
                sum_2, count_2, upper_2 = blocks.pop()
                sum_1, count_1, upper_1 = blocks.pop()
                blocks.append([sum_1 + sum_2, count_1 + count_2, max(upper_1, upper_2)])
        return cls(
            thresholds=tuple(block[2] for block in blocks),
            levels=tuple(block[0] / block[1] for block in blocks),
        )

    def transform(self, p: float) -> float:
        for threshold, level in zip(self.thresholds, self.levels, strict=True):
            if p <= threshold:
                return _clip01(level)
        return _clip01(self.levels[-1]) if self.levels else _clip01(p)

    def transform_many(self, probs: Sequence[float]) -> list[float]:
        return [self.transform(p) for p in probs]


@dataclass(frozen=True)
class IdentityCalibrator:
    """No-op calibrator used when validation says raw Elo is already calibrated."""

    def transform(self, p: float) -> float:
        return _clip01(p)

    def transform_many(self, probs: Sequence[float]) -> list[float]:
        return [self.transform(p) for p in probs]


@dataclass(frozen=True)
class EloRunReport:
    ok: bool
    decision: str
    reason: str
    data_source: str
    downloaded_path: str | None
    train_years: tuple[int, int]
    validation_years: tuple[int, int]
    test_years: tuple[int, int]
    best_params: EloParams
    selected_calibration: str
    train_metrics: ForecastMetrics
    validation_metrics_raw: ForecastMetrics
    validation_metrics_calibrated: ForecastMetrics
    test_metrics_raw: ForecastMetrics
    test_metrics_calibrated: ForecastMetrics
    test_metrics_fivethirtyeight: ForecastMetrics | None
    test_metrics_home_prior: ForecastMetrics
    significance_vs_home_prior: PairedSignificance
    significance_vs_fivethirtyeight: PairedSignificance | None
    calibrator: dict[str, Any]
    selected_from_grid: int
    notes: tuple[str, ...]


def load_fivethirtyeight_games(path: str | Path) -> list[NbaEloGame]:
    """Load one row per game from FiveThirtyEight's ``nbaallelo.csv``.

    The source CSV is mirrored team/opponent rows. Keeping ``_iscopy == 0``
    avoids double counting while preserving the row's own forecast probability.
    """

    games: list[NbaEloGame] = []
    with Path(path).open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            if str(row.get("_iscopy", "")).strip() != "0":
                continue
            points = _parse_int(row.get("pts"))
            opponent_points = _parse_int(row.get("opp_pts"))
            if points is None or opponent_points is None or points == opponent_points:
                continue
            games.append(
                NbaEloGame(
                    game_id=str(row["game_id"]),
                    season=int(row["year_id"]),
                    game_date=datetime.strptime(str(row["date_game"]), "%m/%d/%Y").date(),
                    team=str(row.get("fran_id") or row["team_id"]),
                    opponent=str(row.get("opp_fran") or row["opp_id"]),
                    location=str(row.get("game_location") or "N"),
                    points=points,
                    opponent_points=opponent_points,
                    is_playoffs=str(row.get("is_playoffs", "0")).strip() in {"1", "TRUE", "True", "true"},
                    fivethirtyeight_forecast=_parse_float(row.get("forecast")),
                    game_start_at=_parse_datetime(row.get("game_start_at")),
                )
            )
    return sorted(games, key=lambda g: (g.game_date, g.game_id))


def load_pregame_features_csv(path: str | Path) -> dict[tuple[str, str], NbaPregameFeatures]:
    """Load optional player, market, and matchup features with availability timestamps.

    Expected columns are ``game_id``, ``team``, and ``available_at`` plus any
    field on :class:`NbaPregameFeatures`. Probabilities may be decimal
    probabilities or American odds for ``*_moneyline`` columns.
    """

    out: dict[tuple[str, str], NbaPregameFeatures] = {}
    with Path(path).open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            game_id = str(row.get("game_id", "")).strip()
            team = str(row.get("team", "")).strip()
            available_at = _parse_datetime(row.get("available_at"))
            if not game_id or not team or available_at is None:
                raise ValueError("pregame feature rows require game_id, team, and available_at")
            feature = NbaPregameFeatures(
                game_id=game_id,
                team=team,
                available_at=available_at,
                injury_impact=_parse_float(row.get("injury_impact")) or 0.0,
                starter_absences=_parse_float(row.get("starter_absences")) or 0.0,
                minutes_weighted_impact=_parse_float(row.get("minutes_weighted_impact")) or 0.0,
                usage_lost=_parse_float(row.get("usage_lost")) or 0.0,
                usage_returning=_parse_float(row.get("usage_returning")) or 0.0,
                star_back_to_back_risk=_parse_float(row.get("star_back_to_back_risk")) or 0.0,
                moneyline_prob=_parse_probability(row.get("moneyline_prob") or row.get("moneyline")),
                spread_implied_prob=_parse_probability(row.get("spread_implied_prob")),
                opening_moneyline_prob=_parse_probability(
                    row.get("opening_moneyline_prob") or row.get("opening_moneyline")
                ),
                closing_moneyline_prob=_parse_probability(
                    row.get("closing_moneyline_prob") or row.get("closing_moneyline")
                ),
                three_point_attempt_rate=_parse_float(row.get("three_point_attempt_rate")),
                three_point_defense_rate=_parse_float(row.get("three_point_defense_rate")),
                offensive_rebound_rate=_parse_float(row.get("offensive_rebound_rate")),
                defensive_rebound_rate=_parse_float(row.get("defensive_rebound_rate")),
                turnover_rate=_parse_float(row.get("turnover_rate")),
                forced_turnover_rate=_parse_float(row.get("forced_turnover_rate")),
                free_throw_rate=_parse_float(row.get("free_throw_rate")),
                foul_rate=_parse_float(row.get("foul_rate")),
                possessions_per_game=_parse_float(row.get("possessions_per_game")),
            )
            out[(game_id, team)] = feature
    return out


def run_elo(games: Sequence[NbaEloGame], params: EloParams) -> list[EloPrediction]:
    """Generate no-leakage Elo probabilities and update ratings after outcomes."""

    ratings: dict[str, float] = {}
    last_game_date: dict[str, date] = {}
    current_season: int | None = None
    predictions: list[EloPrediction] = []

    for game in sorted(games, key=lambda g: (g.game_date, g.game_id)):
        if current_season is None:
            current_season = game.season
        elif game.season != current_season:
            ratings = {
                team: params.base_rating + params.season_reversion * (rating - params.base_rating)
                for team, rating in ratings.items()
            }
            current_season = game.season

        team_rating = ratings.get(game.team, params.base_rating)
        opponent_rating = ratings.get(game.opponent, params.base_rating)
        raw_diff = team_rating - opponent_rating
        adjusted_diff = raw_diff + _location_adjustment(game, params) + _rest_adjustment(game, last_game_date, params)
        probability = _elo_probability(adjusted_diff, params.scale)
        predictions.append(
            EloPrediction(
                game_id=game.game_id,
                season=game.season,
                game_date=game.game_date,
                team=game.team,
                opponent=game.opponent,
                location=game.location,
                outcome=game.outcome,
                probability=probability,
                raw_rating_diff=raw_diff,
                adjusted_rating_diff=adjusted_diff,
                fivethirtyeight_forecast=game.fivethirtyeight_forecast,
            )
        )

        update = params.k * _mov_multiplier(game.margin, adjusted_diff, params) * (game.outcome - probability)
        ratings[game.team] = team_rating + update
        ratings[game.opponent] = opponent_rating - update
        last_game_date[game.team] = game.game_date
        last_game_date[game.opponent] = game.game_date

    return predictions


def run_enhanced_elo(
    games: Sequence[NbaEloGame],
    params: EnhancedEloParams | None = None,
    *,
    pregame_features: dict[tuple[str, str], NbaPregameFeatures] | None = None,
) -> list[EnhancedEloPrediction]:
    """Generate no-leakage enhanced Elo probabilities.

    Enhancements include separate offense/defense/pace state, schedule and
    travel context, controlled rolling form, optional player/market/matchup
    rows, and Glicko-style uncertainty shrinkage. Every value is read before the
    current game's result is applied.
    """

    if params is None:
        params = EnhancedEloParams()
    pregame_features = pregame_features or {}
    states: dict[str, _EnhancedTeamState] = {}
    current_season: int | None = None
    league_points_total = 0.0
    league_team_games = 0
    predictions: list[EnhancedEloPrediction] = []

    for game in sorted(games, key=lambda g: (g.game_date, g.game_id)):
        if current_season is None:
            current_season = game.season
        elif game.season != current_season:
            for state in states.values():
                state.elo = params.base.base_rating + params.base.season_reversion * (
                    state.elo - params.base.base_rating
                )
                state.offense *= params.base.season_reversion
                state.defense *= params.base.season_reversion
                state.pace *= params.base.season_reversion
                state.uncertainty = min(params.uncertainty_initial, state.uncertainty + 1.0)
            current_season = game.season

        team_state = _state_for(states, game.team, params)
        opponent_state = _state_for(states, game.opponent, params)
        team_feature = _pregame_feature_for(game, game.team, pregame_features)
        opponent_feature = _pregame_feature_for(game, game.opponent, pregame_features)
        league_avg = league_points_total / league_team_games if league_team_games else params.league_avg_points
        margin, features = _enhanced_expected_margin(
            game,
            team_state,
            opponent_state,
            team_feature,
            opponent_feature,
            params,
            league_avg,
        )
        uncertainty = (team_state.uncertainty + opponent_state.uncertainty) / 2.0
        margin *= 1.0 / (1.0 + uncertainty / params.uncertainty_margin_shrink)
        base_probability = _clip01(_sigmoid(margin / params.margin_scale))
        market_probability = _market_probability(team_feature)
        probability = base_probability
        if market_probability is not None:
            probability = _clip01(
                (1.0 - params.market_shrink) * probability + params.market_shrink * market_probability
            )
        features["base_probability"] = base_probability
        features["market_probability"] = market_probability if market_probability is not None else 0.0
        features["model_market_edge"] = base_probability - market_probability if market_probability is not None else 0.0
        features["uncertainty"] = uncertainty
        predictions.append(
            EnhancedEloPrediction(
                game_id=game.game_id,
                season=game.season,
                game_date=game.game_date,
                team=game.team,
                opponent=game.opponent,
                location=game.location,
                outcome=game.outcome,
                probability=probability,
                base_probability=base_probability,
                expected_margin=margin,
                market_probability=market_probability,
                model_market_edge=base_probability - market_probability if market_probability is not None else None,
                fivethirtyeight_forecast=game.fivethirtyeight_forecast,
                features=features,
            )
        )

        _update_enhanced_states(game, team_state, opponent_state, margin, params, league_avg)
        league_points_total += game.points + game.opponent_points
        league_team_games += 2

    return predictions


def fit_logistic_feature_model(
    predictions: Sequence[EnhancedEloPrediction],
    *,
    feature_names: Sequence[str] | None = None,
    l2: float = 0.5,
    iterations: int = 300,
    learning_rate: float = 0.12,
) -> LogisticFeatureModel:
    """Fit a small second-stage logistic model on point-in-time feature rows."""

    import numpy as np

    if not predictions:
        raise ValueError("predictions must be non-empty")
    if feature_names is None:
        feature_names = DEFAULT_SECOND_STAGE_FEATURES
    names = tuple(feature_names)
    x = np.array([[p.features.get(name, 0.0) for name in names] for p in predictions], dtype=float)
    means_array = x.mean(axis=0)
    scales_array = x.std(axis=0)
    scales_array[scales_array < 1e-6] = 1.0
    x_norm = (x - means_array) / scales_array
    design = np.column_stack([np.ones(len(predictions)), x_norm])
    y = np.array([float(p.outcome) for p in predictions], dtype=float)
    weights_array = np.zeros(design.shape[1], dtype=float)
    penalty = np.ones_like(weights_array)
    penalty[0] = 0.0
    n = float(len(predictions))

    for _ in range(iterations):
        pred = 1.0 / (1.0 + np.exp(-(design @ weights_array)))
        gradient = (design.T @ (pred - y)) / n + (2.0 * l2 / n) * penalty * weights_array
        step = learning_rate * gradient
        weights_array -= step
        if float(np.max(np.abs(step))) < 1e-8:
            break
    return LogisticFeatureModel(
        feature_names=names,
        means=tuple(float(v) for v in means_array),
        scales=tuple(float(v) for v in scales_array),
        weights=tuple(float(v) for v in weights_array),
    )


def select_best_params(
    games: Sequence[NbaEloGame],
    grid: Iterable[EloParams],
    *,
    train_years: tuple[int, int],
    validation_years: tuple[int, int],
    calibration_candidates: int = 25,
) -> tuple[EloParams, str, ForecastMetrics, ForecastMetrics, int]:
    """Pick hyperparameters by validation Brier, then choose calibration on validation only."""

    raw_candidates: list[tuple[float, EloParams, list[EloPrediction], list[EloPrediction]]] = []
    count = 0
    for params in grid:
        count += 1
        preds = run_elo(games, params)
        train = _filter_years(preds, train_years)
        validation = _filter_years(preds, validation_years)
        if len(train) < 100 or len(validation) < 100:
            continue
        raw_score = metrics_for([p.probability for p in validation], [p.outcome for p in validation]).brier
        raw_candidates.append((raw_score, params, train, validation))

    best: tuple[float, EloParams, str, ForecastMetrics, ForecastMetrics] | None = None
    for _raw_score, params, train, validation in sorted(raw_candidates, key=lambda x: x[0])[:calibration_candidates]:
        train_probs = [p.probability for p in train]
        train_outcomes = [p.outcome for p in train]
        validation_probs = [p.probability for p in validation]
        validation_outcomes = [p.outcome for p in validation]
        variants = _calibration_variants(train_probs, train_outcomes, validation_probs)
        for method, calibrated_validation_probs, _calibrator in variants:
            train_eval_probs = _fit_calibrator(method, train_probs, train_outcomes).transform_many(train_probs)
            train_metrics = metrics_for(train_eval_probs, train_outcomes)
            validation_metrics = metrics_for(calibrated_validation_probs, validation_outcomes)
            score = validation_metrics.brier
            if best is None or score < best[0]:
                best = (score, params, method, train_metrics, validation_metrics)

    if best is None:
        raise ValueError("no valid parameter set evaluated")
    return best[1], best[2], best[3], best[4], count


def run_research(
    games: Sequence[NbaEloGame],
    *,
    data_source: str,
    downloaded_path: str | None = None,
    train_years: tuple[int, int] = (1947, 2008),
    validation_years: tuple[int, int] = (2009, 2011),
    test_years: tuple[int, int] = (2012, 2015),
    grid: Iterable[EloParams] | None = None,
    bootstrap_samples: int = 2000,
    calibration_candidates: int = 25,
    require_fivethirtyeight_edge: bool = True,
) -> EloRunReport:
    if grid is None:
        grid = default_param_grid()
    best_params, calibration, train_metrics, validation_metrics_calibrated, selected_from_grid = select_best_params(
        games,
        grid,
        train_years=train_years,
        validation_years=validation_years,
        calibration_candidates=calibration_candidates,
    )
    preds = run_elo(games, best_params)
    train = _filter_years(preds, train_years)
    validation = _filter_years(preds, validation_years)
    test = _filter_years(preds, test_years)
    if len(test) < 500:
        raise ValueError(f"test sample too small: {len(test)}")

    final_calibrator = _fit_calibrator(
        calibration,
        [p.probability for p in [*train, *validation]],
        [p.outcome for p in [*train, *validation]],
    )
    validation_raw_probs = [p.probability for p in validation]
    test_raw_probs = [p.probability for p in test]
    test_calibrated_probs = final_calibrator.transform_many(test_raw_probs)
    outcomes = [p.outcome for p in test]

    home_prior_probs = home_prior_forecast([*train, *validation], test)
    sig_home = paired_bootstrap_improvement(
        baseline_probs=home_prior_probs,
        candidate_probs=test_calibrated_probs,
        outcomes=outcomes,
        samples=bootstrap_samples,
        seed=17,
    )

    fte_test = [p.fivethirtyeight_forecast for p in test]
    fte_metrics: ForecastMetrics | None = None
    sig_fte: PairedSignificance | None = None
    if all(p is not None for p in fte_test):
        fte_probs = [float(p) for p in fte_test if p is not None]
        fte_metrics = metrics_for(fte_probs, outcomes)
        sig_fte = paired_bootstrap_improvement(
            baseline_probs=fte_probs,
            candidate_probs=test_calibrated_probs,
            outcomes=outcomes,
            samples=bootstrap_samples,
            seed=23,
        )

    raw_metrics = metrics_for(test_raw_probs, outcomes)
    calibrated_metrics = metrics_for(test_calibrated_probs, outcomes)
    home_metrics = metrics_for(home_prior_probs, outcomes)
    home_significant = sig_home.significant
    fte_significant = sig_fte.significant if sig_fte is not None else False
    ok = bool(home_significant and (not require_fivethirtyeight_edge or sig_fte is None or fte_significant))
    reason = (
        "calibrated custom Elo significantly beat home-prior and FiveThirtyEight reference on holdout"
        if ok and sig_fte is not None and require_fivethirtyeight_edge
        else "calibrated custom Elo significantly beat home-prior on holdout; no FiveThirtyEight edge required"
        if ok
        else "custom Elo did not clear the full significance bar against the strongest available reference"
    )
    notes = (
        "FiveThirtyEight forecast is a strong historical reference, not a live market-implied baseline.",
        "A trading edge still requires venue quote capture, executable touch, fees, spread, fill, and 1m/5m/15m CLV.",
        "All Elo probabilities are emitted before updating ratings with that game's result.",
        "The decision fails closed unless the holdout paired bootstrap confidence interval is strictly positive.",
    )
    return EloRunReport(
        ok=ok,
        decision="SIGNIFICANT_CALIBRATION" if ok else "CONTINUE_RESEARCH",
        reason=reason,
        data_source=data_source,
        downloaded_path=downloaded_path,
        train_years=train_years,
        validation_years=validation_years,
        test_years=test_years,
        best_params=best_params,
        selected_calibration=calibration,
        train_metrics=train_metrics,
        validation_metrics_raw=metrics_for(validation_raw_probs, [p.outcome for p in validation]),
        validation_metrics_calibrated=validation_metrics_calibrated,
        test_metrics_raw=raw_metrics,
        test_metrics_calibrated=calibrated_metrics,
        test_metrics_fivethirtyeight=fte_metrics,
        test_metrics_home_prior=home_metrics,
        significance_vs_home_prior=sig_home,
        significance_vs_fivethirtyeight=sig_fte,
        calibrator=_calibrator_payload(final_calibrator),
        selected_from_grid=selected_from_grid,
        notes=notes,
    )


def run_enhanced_research(
    games: Sequence[NbaEloGame],
    *,
    data_source: str,
    downloaded_path: str | None = None,
    pregame_features: dict[tuple[str, str], NbaPregameFeatures] | None = None,
    train_years: tuple[int, int] = (1947, 2008),
    validation_years: tuple[int, int] = (2009, 2011),
    test_years: tuple[int, int] = (2012, 2015),
    params: EnhancedEloParams | None = None,
    bootstrap_samples: int = 2000,
    require_fivethirtyeight_edge: bool = True,
) -> EloRunReport:
    """Run the enhanced model with walk-forward second-stage selection.

    The validation split selects between base enhanced Elo and a logistic
    interaction model, then selects identity/Platt/isotonic calibration. The
    final selected stack is refit on train+validation and evaluated once on the
    holdout test years.
    """

    if params is None:
        params = EnhancedEloParams()
    preds = run_enhanced_elo(games, params, pregame_features=pregame_features)
    train = _filter_enhanced_years(preds, train_years)
    validation = _filter_enhanced_years(preds, validation_years)
    test = _filter_enhanced_years(preds, test_years)
    if len(test) < 500:
        raise ValueError(f"test sample too small: {len(test)}")

    selected_stage, selected_calibration, validation_metrics = _select_second_stage(train, validation, params)
    train_validation = [*train, *validation]
    if selected_stage == "second_stage":
        final_model = fit_logistic_feature_model(train_validation, l2=params.second_stage_l2)
        final_train_probs = [final_model.predict(p.features) for p in train_validation]
        final_test_raw_probs = [final_model.predict(p.features) for p in test]
    else:
        final_train_probs = [p.probability for p in train_validation]
        final_test_raw_probs = [p.probability for p in test]
    final_calibrator = _fit_calibrator(
        selected_calibration,
        final_train_probs,
        [p.outcome for p in train_validation],
    )
    test_calibrated_probs = final_calibrator.transform_many(final_test_raw_probs)
    outcomes = [p.outcome for p in test]

    home_prior_probs = _enhanced_home_prior_forecast(train_validation, test)
    sig_home = paired_bootstrap_improvement(
        baseline_probs=home_prior_probs,
        candidate_probs=test_calibrated_probs,
        outcomes=outcomes,
        samples=bootstrap_samples,
        seed=17,
    )

    fte_test = [p.fivethirtyeight_forecast for p in test]
    fte_metrics: ForecastMetrics | None = None
    sig_fte: PairedSignificance | None = None
    if all(p is not None for p in fte_test):
        fte_probs = [float(p) for p in fte_test if p is not None]
        fte_metrics = metrics_for(fte_probs, outcomes)
        sig_fte = paired_bootstrap_improvement(
            baseline_probs=fte_probs,
            candidate_probs=test_calibrated_probs,
            outcomes=outcomes,
            samples=bootstrap_samples,
            seed=23,
        )

    raw_metrics = metrics_for(final_test_raw_probs, outcomes)
    calibrated_metrics = metrics_for(test_calibrated_probs, outcomes)
    home_metrics = metrics_for(home_prior_probs, outcomes)
    home_significant = sig_home.significant
    fte_significant = sig_fte.significant if sig_fte is not None else False
    ok = bool(home_significant and (not require_fivethirtyeight_edge or sig_fte is None or fte_significant))
    coverage = _enhanced_feature_coverage(preds)
    reason = (
        "enhanced NBA model significantly beat home-prior and FiveThirtyEight reference on holdout"
        if ok and sig_fte is not None and require_fivethirtyeight_edge
        else "enhanced NBA model significantly beat home-prior on holdout; no FiveThirtyEight edge required"
        if ok
        else "enhanced NBA model did not clear the full significance bar against the strongest available reference"
    )
    notes = (
        "Enhanced features include offense/defense/pace state, schedule/travel, rolling form, uncertainty shrinkage, "
        "optional player availability, optional market baselines, and optional matchup stats.",
        "Pregame feature rows are ignored unless available_at is at or before the game cutoff.",
        f"Feature coverage: market_rows={coverage['market_rows']}, player_rows={coverage['player_rows']}, "
        f"matchup_rows={coverage['matchup_rows']}.",
        "FiveThirtyEight forecast is a strong historical reference, not a live market-implied baseline.",
        "A trading edge still requires venue quote capture, executable touch, fees, spread, fill, and 1m/5m/15m CLV.",
        "The decision fails closed unless the holdout paired bootstrap confidence interval is strictly positive.",
    )
    return EloRunReport(
        ok=ok,
        decision="SIGNIFICANT_CALIBRATION" if ok else "CONTINUE_RESEARCH",
        reason=reason,
        data_source=data_source,
        downloaded_path=downloaded_path,
        train_years=train_years,
        validation_years=validation_years,
        test_years=test_years,
        best_params=params.base,
        selected_calibration=f"{selected_stage}+{selected_calibration}",
        train_metrics=metrics_for(final_train_probs, [p.outcome for p in train_validation]),
        validation_metrics_raw=metrics_for([p.probability for p in validation], [p.outcome for p in validation]),
        validation_metrics_calibrated=validation_metrics,
        test_metrics_raw=raw_metrics,
        test_metrics_calibrated=calibrated_metrics,
        test_metrics_fivethirtyeight=fte_metrics,
        test_metrics_home_prior=home_metrics,
        significance_vs_home_prior=sig_home,
        significance_vs_fivethirtyeight=sig_fte,
        calibrator={
            "stage": selected_stage,
            "calibration": _calibrator_payload(final_calibrator),
            "enhanced_params": _to_jsonable(params),
            "feature_coverage": coverage,
        },
        selected_from_grid=1,
        notes=notes,
    )


def default_param_grid() -> list[EloParams]:
    out: list[EloParams] = []
    for k in (16.0, 22.0, 28.0):
        for home_advantage in (55.0, 75.0):
            for scale in (380.0, 420.0):
                for rest_advantage in (0.0, 6.0):
                    for mov_weight in (0.4, 0.8):
                        for season_reversion in (0.75, 0.85):
                            out.append(
                                EloParams(
                                    k=k,
                                    home_advantage=home_advantage,
                                    scale=scale,
                                    rest_advantage=rest_advantage,
                                    mov_weight=mov_weight,
                                    season_reversion=season_reversion,
                                )
                            )
    return out


def metrics_for(probs: Sequence[float], outcomes: Sequence[int]) -> ForecastMetrics:
    if len(probs) != len(outcomes) or not probs:
        raise ValueError("probs and outcomes must be non-empty and equal length")
    clipped = [_clip01(p) for p in probs]
    n = len(clipped)
    brier = sum((p - y) ** 2 for p, y in zip(clipped, outcomes, strict=True)) / n
    log_loss = sum(
        -(y * math.log(p) + (1 - y) * math.log(1 - p))
        for p, y in zip(clipped, outcomes, strict=True)
    ) / n
    ece = expected_calibration_error(clipped, outcomes)
    accuracy = sum((p >= 0.5) == bool(y) for p, y in zip(clipped, outcomes, strict=True)) / n
    return ForecastMetrics(n=n, brier=brier, log_loss=log_loss, ece=ece, accuracy=accuracy)


def expected_calibration_error(probs: Sequence[float], outcomes: Sequence[int], *, bins: int = 10) -> float:
    n = len(probs)
    total = 0.0
    for idx in range(bins):
        lo = idx / bins
        hi = (idx + 1) / bins
        members = [
            (p, y)
            for p, y in zip(probs, outcomes, strict=True)
            if (lo <= p < hi) or (idx == bins - 1 and p <= hi)
        ]
        if not members:
            continue
        mean_p = sum(p for p, _ in members) / len(members)
        mean_y = sum(y for _, y in members) / len(members)
        total += (len(members) / n) * abs(mean_p - mean_y)
    return total


def paired_bootstrap_improvement(
    *,
    baseline_probs: Sequence[float],
    candidate_probs: Sequence[float],
    outcomes: Sequence[int],
    samples: int = 2000,
    seed: int = 1,
) -> PairedSignificance:
    """Paired bootstrap for Brier improvement: baseline Brier minus candidate Brier."""

    if len(baseline_probs) != len(candidate_probs) or len(candidate_probs) != len(outcomes):
        raise ValueError("baseline_probs, candidate_probs, and outcomes must have equal length")
    n = len(outcomes)
    deltas = [
        (_clip01(baseline) - y) ** 2 - (_clip01(candidate) - y) ** 2
        for baseline, candidate, y in zip(baseline_probs, candidate_probs, outcomes, strict=True)
    ]
    improvement = sum(deltas) / n
    rng = random.Random(seed)
    boot: list[float] = []
    for _ in range(samples):
        boot.append(sum(deltas[rng.randrange(n)] for _ in range(n)) / n)
    boot.sort()
    ci_low = boot[int(0.025 * (samples - 1))]
    ci_high = boot[int(0.975 * (samples - 1))]
    p_value = (1 + sum(1 for value in boot if value <= 0.0)) / (samples + 1)
    return PairedSignificance(improvement=improvement, ci_low=ci_low, ci_high=ci_high, p_value=p_value, samples=samples)


def home_prior_forecast(reference: Sequence[EloPrediction], target: Sequence[EloPrediction]) -> list[float]:
    home_rows = [p.outcome for p in reference if p.location == "H"]
    away_rows = [p.outcome for p in reference if p.location == "A"]
    neutral_rows = [p.outcome for p in reference if p.location == "N"]
    home_rate = sum(home_rows) / len(home_rows) if home_rows else 0.6
    away_rate = sum(away_rows) / len(away_rows) if away_rows else 0.4
    neutral_rate = sum(neutral_rows) / len(neutral_rows) if neutral_rows else 0.5
    out: list[float] = []
    for p in target:
        if p.location == "H":
            out.append(home_rate)
        elif p.location == "A":
            out.append(away_rate)
        else:
            out.append(neutral_rate)
    return out


def report_to_dict(report: EloRunReport) -> dict[str, Any]:
    return _to_jsonable(report)


def render_markdown(report: EloRunReport) -> str:
    fte = report.test_metrics_fivethirtyeight
    sig_fte = report.significance_vs_fivethirtyeight
    lines = [
        "# NBA Elo Calibration Research",
        "",
        f"- Decision: **{report.decision}**",
        f"- Reason: {report.reason}",
        f"- Data source: {report.data_source}",
        f"- Train years: {report.train_years[0]}-{report.train_years[1]}",
        f"- Validation years: {report.validation_years[0]}-{report.validation_years[1]}",
        f"- Test years: {report.test_years[0]}-{report.test_years[1]}",
        f"- Grid size: {report.selected_from_grid}",
        f"- Selected calibration: {report.selected_calibration}",
        "",
        "## Best Parameters",
        "",
        f"- k: {report.best_params.k}",
        f"- home_advantage: {report.best_params.home_advantage}",
        f"- scale: {report.best_params.scale}",
        f"- rest_advantage: {report.best_params.rest_advantage}",
        f"- mov_weight: {report.best_params.mov_weight}",
        f"- season_reversion: {report.best_params.season_reversion}",
        f"- calibrator: {_calibrator_summary(report.calibrator)}",
        "",
        "## Holdout Metrics",
        "",
        "| model | n | Brier | logloss | ECE | accuracy |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
        _metric_row("custom raw", report.test_metrics_raw),
        _metric_row("custom calibrated", report.test_metrics_calibrated),
        _metric_row("home prior", report.test_metrics_home_prior),
    ]
    if fte is not None:
        lines.append(_metric_row("FiveThirtyEight reference", fte))
    lines.extend(
        [
            "",
            "## Significance",
            "",
            _sig_line("vs home prior", report.significance_vs_home_prior),
        ]
    )
    if sig_fte is not None:
        lines.append(_sig_line("vs FiveThirtyEight reference", sig_fte))
    lines.extend(
        [
            "",
            "## Trading Interpretation",
            "",
            "- This is a game-outcome calibration study, not a venue edge claim.",
            "- A venue edge still needs point-in-time market-implied probabilities, executable touch, fees, "
            "spread, fill, and 1m/5m/15m CLV.",
            "- If the decision is CONTINUE_RESEARCH, do not route this into paper trading as an edge.",
            "",
            "## Notes",
            "",
        ]
    )
    lines.extend(f"- {note}" for note in report.notes)
    lines.append("")
    return "\n".join(lines)


def _select_second_stage(
    train: Sequence[EnhancedEloPrediction],
    validation: Sequence[EnhancedEloPrediction],
    params: EnhancedEloParams,
) -> tuple[str, str, ForecastMetrics]:
    train_outcomes = [p.outcome for p in train]
    validation_outcomes = [p.outcome for p in validation]
    second_stage = fit_logistic_feature_model(train, l2=params.second_stage_l2)
    candidates = {
        "enhanced": (
            [p.probability for p in train],
            [p.probability for p in validation],
        ),
        "second_stage": (
            [second_stage.predict(p.features) for p in train],
            [second_stage.predict(p.features) for p in validation],
        ),
    }
    best: tuple[float, str, str, ForecastMetrics] | None = None
    for stage, (train_probs, validation_probs) in candidates.items():
        for calibration, calibrated_validation_probs, _calibrator in _calibration_variants(
            train_probs,
            train_outcomes,
            validation_probs,
        ):
            metrics = metrics_for(calibrated_validation_probs, validation_outcomes)
            if best is None or metrics.brier < best[0]:
                best = (metrics.brier, stage, calibration, metrics)
    if best is None:
        raise ValueError("no second-stage candidate evaluated")
    return best[1], best[2], best[3]


def _candidate_probs(
    stage: str,
    train: Sequence[EnhancedEloPrediction],
    target: Sequence[EnhancedEloPrediction],
    params: EnhancedEloParams,
) -> list[float]:
    if stage == "enhanced":
        return [p.probability for p in target]
    if stage == "second_stage":
        model = fit_logistic_feature_model(train, l2=params.second_stage_l2)
        return [model.predict(p.features) for p in target]
    raise ValueError(f"unknown model stage: {stage}")


def _filter_enhanced_years(
    preds: Sequence[EnhancedEloPrediction],
    years: tuple[int, int],
) -> list[EnhancedEloPrediction]:
    lo, hi = years
    return [p for p in preds if lo <= p.season <= hi]


def _enhanced_home_prior_forecast(
    reference: Sequence[EnhancedEloPrediction],
    target: Sequence[EnhancedEloPrediction],
) -> list[float]:
    home_rows = [p.outcome for p in reference if p.location == "H"]
    away_rows = [p.outcome for p in reference if p.location == "A"]
    neutral_rows = [p.outcome for p in reference if p.location == "N"]
    home_rate = sum(home_rows) / len(home_rows) if home_rows else 0.6
    away_rate = sum(away_rows) / len(away_rows) if away_rows else 0.4
    neutral_rate = sum(neutral_rows) / len(neutral_rows) if neutral_rows else 0.5
    out: list[float] = []
    for p in target:
        if p.location == "H":
            out.append(home_rate)
        elif p.location == "A":
            out.append(away_rate)
        else:
            out.append(neutral_rate)
    return out


def _enhanced_feature_coverage(preds: Sequence[EnhancedEloPrediction]) -> dict[str, int]:
    return {
        "market_rows": sum(1 for p in preds if p.market_probability is not None),
        "player_rows": sum(1 for p in preds if abs(p.features.get("player_points", 0.0)) > 0.0),
        "matchup_rows": sum(1 for p in preds if abs(p.features.get("matchup_points", 0.0)) > 0.0),
    }


DEFAULT_SECOND_STAGE_FEATURES: tuple[str, ...] = (
    "base_probability",
    "expected_margin",
    "elo_points",
    "offense_defense_points",
    "rest_diff",
    "team_back_to_back",
    "opponent_back_to_back",
    "team_three_in_four",
    "opponent_three_in_four",
    "travel_diff_1000",
    "timezone_diff",
    "altitude_penalty",
    "form_points",
    "player_points",
    "matchup_points",
    "market_probability",
    "model_market_edge",
    "uncertainty",
)

_NBA_TEAM_META: dict[str, TeamVenueMeta] = {
    "Hawks": TeamVenueMeta(33.7573, -84.3963, -5),
    "Celtics": TeamVenueMeta(42.3662, -71.0621, -5),
    "Nets": TeamVenueMeta(40.6826, -73.9754, -5),
    "Hornets": TeamVenueMeta(35.2251, -80.8392, -5),
    "Bulls": TeamVenueMeta(41.8807, -87.6742, -6),
    "Cavaliers": TeamVenueMeta(41.4965, -81.6882, -5),
    "Mavericks": TeamVenueMeta(32.7905, -96.8103, -6),
    "Nuggets": TeamVenueMeta(39.7487, -105.0077, -7, 5280),
    "Pistons": TeamVenueMeta(42.3411, -83.0554, -5),
    "Warriors": TeamVenueMeta(37.7680, -122.3877, -8),
    "Rockets": TeamVenueMeta(29.7508, -95.3621, -6),
    "Pacers": TeamVenueMeta(39.7639, -86.1555, -5),
    "Clippers": TeamVenueMeta(34.0430, -118.2673, -8),
    "Lakers": TeamVenueMeta(34.0430, -118.2673, -8),
    "Grizzlies": TeamVenueMeta(35.1382, -90.0506, -6),
    "Heat": TeamVenueMeta(25.7814, -80.1870, -5),
    "Bucks": TeamVenueMeta(43.0451, -87.9172, -6),
    "Timberwolves": TeamVenueMeta(44.9795, -93.2761, -6),
    "Pelicans": TeamVenueMeta(29.9490, -90.0821, -6),
    "Knicks": TeamVenueMeta(40.7505, -73.9934, -5),
    "Thunder": TeamVenueMeta(35.4634, -97.5151, -6),
    "Magic": TeamVenueMeta(28.5392, -81.3839, -5),
    "76ers": TeamVenueMeta(39.9012, -75.1720, -5),
    "Suns": TeamVenueMeta(33.4457, -112.0712, -7),
    "Trailblazers": TeamVenueMeta(45.5316, -122.6668, -8),
    "Kings": TeamVenueMeta(38.5802, -121.4997, -8),
    "Spurs": TeamVenueMeta(29.4270, -98.4375, -6),
    "Raptors": TeamVenueMeta(43.6435, -79.3791, -5),
    "Jazz": TeamVenueMeta(40.7683, -111.9011, -7, 4226),
    "Wizards": TeamVenueMeta(38.8981, -77.0209, -5),
    "Bobcats": TeamVenueMeta(35.2251, -80.8392, -5),
    "SuperSonics": TeamVenueMeta(47.6221, -122.3540, -8),
    "Bullets": TeamVenueMeta(38.8981, -77.0209, -5),
}


@dataclass
class _EnhancedTeamState:
    elo: float
    offense: float
    defense: float
    pace: float
    uncertainty: float
    game_dates: list[date] = field(default_factory=list)
    margins: list[float] = field(default_factory=list)
    points_for: list[float] = field(default_factory=list)
    points_against: list[float] = field(default_factory=list)
    locations: list[str] = field(default_factory=list)
    last_venue: TeamVenueMeta | None = None


def _state_for(states: dict[str, _EnhancedTeamState], team: str, params: EnhancedEloParams) -> _EnhancedTeamState:
    if team not in states:
        states[team] = _EnhancedTeamState(
            elo=params.base.base_rating,
            offense=0.0,
            defense=0.0,
            pace=0.0,
            uncertainty=params.uncertainty_initial,
        )
    return states[team]


def _enhanced_expected_margin(
    game: NbaEloGame,
    team_state: _EnhancedTeamState,
    opponent_state: _EnhancedTeamState,
    team_feature: NbaPregameFeatures | None,
    opponent_feature: NbaPregameFeatures | None,
    params: EnhancedEloParams,
    league_avg_points: float,
) -> tuple[float, dict[str, float]]:
    elo_points = (team_state.elo - opponent_state.elo) * params.elo_rating_to_points
    offdef_points = team_state.offense + team_state.defense - opponent_state.offense - opponent_state.defense
    location_points = _location_points(game, params)
    schedule_points, schedule_features = _schedule_points(game, team_state, opponent_state, params)
    form_points = _rolling_form_points(team_state, opponent_state, params)
    player_points = _player_availability_points(team_feature, opponent_feature, params)
    matchup_points = _matchup_points(team_feature, opponent_feature, params)
    pace_mismatch = (team_state.pace - opponent_state.pace) / max(1.0, league_avg_points)
    expected_margin = (
        elo_points
        + offdef_points
        + location_points
        + schedule_points
        + form_points
        + player_points
        + matchup_points
        + 0.25 * pace_mismatch
    )
    features = {
        "expected_margin": expected_margin,
        "elo_points": elo_points,
        "offense_defense_points": offdef_points,
        "location_points": location_points,
        "schedule_points": schedule_points,
        "form_points": form_points,
        "player_points": player_points,
        "matchup_points": matchup_points,
        "pace_mismatch": pace_mismatch,
        **schedule_features,
    }
    return expected_margin, features


def _schedule_points(
    game: NbaEloGame,
    team_state: _EnhancedTeamState,
    opponent_state: _EnhancedTeamState,
    params: EnhancedEloParams,
) -> tuple[float, dict[str, float]]:
    team_rest = _state_rest_days(team_state, game.game_date)
    opponent_rest = _state_rest_days(opponent_state, game.game_date)
    rest_diff = 0.0 if team_rest is None or opponent_rest is None else max(-3.0, min(3.0, team_rest - opponent_rest))
    team_b2b = 1.0 if team_rest is not None and team_rest <= 1.0 else 0.0
    opponent_b2b = 1.0 if opponent_rest is not None and opponent_rest <= 1.0 else 0.0
    team_three_in_four = 1.0 if _games_in_days(team_state, game.game_date, 4) >= 2 else 0.0
    opponent_three_in_four = 1.0 if _games_in_days(opponent_state, game.game_date, 4) >= 2 else 0.0
    venue = _venue_for_game(game)
    team_travel = _travel_1000(team_state.last_venue, venue)
    opponent_travel = _travel_1000(opponent_state.last_venue, venue)
    team_tz = _timezone_delta(team_state.last_venue, venue)
    opponent_tz = _timezone_delta(opponent_state.last_venue, venue)
    altitude_penalty = 0.0
    if venue is not None and venue.altitude_feet >= 3000:
        altitude_penalty = params.altitude_back_to_back_penalty_points * (team_b2b - opponent_b2b)
    streak_diff = _home_away_streak(team_state) - _home_away_streak(opponent_state)
    points = (
        params.rest_points * rest_diff
        - params.back_to_back_penalty_points * team_b2b
        + params.back_to_back_penalty_points * opponent_b2b
        - params.three_in_four_penalty_points * team_three_in_four
        + params.three_in_four_penalty_points * opponent_three_in_four
        - params.travel_1000_mile_penalty_points * (team_travel - opponent_travel)
        - params.timezone_penalty_points * (team_tz - opponent_tz)
        - altitude_penalty
        + params.home_away_streak_points * streak_diff
    )
    return points, {
        "rest_diff": rest_diff,
        "team_back_to_back": team_b2b,
        "opponent_back_to_back": opponent_b2b,
        "team_three_in_four": team_three_in_four,
        "opponent_three_in_four": opponent_three_in_four,
        "travel_diff_1000": team_travel - opponent_travel,
        "timezone_diff": team_tz - opponent_tz,
        "altitude_penalty": altitude_penalty,
        "home_away_streak_diff": float(streak_diff),
    }


def _rolling_form_points(
    team_state: _EnhancedTeamState,
    opponent_state: _EnhancedTeamState,
    params: EnhancedEloParams,
) -> float:
    team_form = _shrunk_average(team_state.margins[-5:], params.form_shrink)
    opponent_form = _shrunk_average(opponent_state.margins[-5:], params.form_shrink)
    team_long = _shrunk_average(team_state.margins[-10:], params.form_shrink)
    opponent_long = _shrunk_average(opponent_state.margins[-10:], params.form_shrink)
    return params.form_weight * ((0.65 * team_form + 0.35 * team_long) - (0.65 * opponent_form + 0.35 * opponent_long))


def _player_availability_points(
    team_feature: NbaPregameFeatures | None,
    opponent_feature: NbaPregameFeatures | None,
    params: EnhancedEloParams,
) -> float:
    return _single_team_player_points(team_feature, params) - _single_team_player_points(opponent_feature, params)


def _single_team_player_points(feature: NbaPregameFeatures | None, params: EnhancedEloParams) -> float:
    if feature is None:
        return 0.0
    return (
        params.player_impact_points * (feature.minutes_weighted_impact - feature.injury_impact)
        - params.starter_absence_points * feature.starter_absences
        - params.usage_lost_points * feature.usage_lost
        + params.usage_returning_points * feature.usage_returning
        - params.star_back_to_back_points * feature.star_back_to_back_risk
    )


def _matchup_points(
    team_feature: NbaPregameFeatures | None,
    opponent_feature: NbaPregameFeatures | None,
    params: EnhancedEloParams,
) -> float:
    if team_feature is None or opponent_feature is None:
        return 0.0
    edges = [
        _optional_diff(team_feature.three_point_attempt_rate, opponent_feature.three_point_defense_rate),
        _optional_diff(team_feature.offensive_rebound_rate, opponent_feature.defensive_rebound_rate),
        _optional_diff(opponent_feature.turnover_rate, team_feature.forced_turnover_rate),
        _optional_diff(team_feature.free_throw_rate, opponent_feature.foul_rate),
        -_optional_diff(opponent_feature.three_point_attempt_rate, team_feature.three_point_defense_rate),
        -_optional_diff(opponent_feature.offensive_rebound_rate, team_feature.defensive_rebound_rate),
        -_optional_diff(team_feature.turnover_rate, opponent_feature.forced_turnover_rate),
        -_optional_diff(opponent_feature.free_throw_rate, team_feature.foul_rate),
    ]
    return params.matchup_points * sum(edges)


def _update_enhanced_states(
    game: NbaEloGame,
    team_state: _EnhancedTeamState,
    opponent_state: _EnhancedTeamState,
    expected_margin: float,
    params: EnhancedEloParams,
    league_avg_points: float,
) -> None:
    probability = _clip01(_sigmoid(expected_margin / params.margin_scale))
    update = params.base.k * _mov_multiplier(game.margin, expected_margin / params.elo_rating_to_points, params.base)
    update *= game.outcome - probability
    team_state.elo += update
    opponent_state.elo -= update

    expected_team_points = league_avg_points + team_state.offense - opponent_state.defense + expected_margin / 2.0
    expected_opponent_points = league_avg_points + opponent_state.offense - team_state.defense - expected_margin / 2.0
    team_error = game.points - expected_team_points
    opponent_error = game.opponent_points - expected_opponent_points
    team_state.offense += params.offense_defense_k * team_error
    opponent_state.defense -= params.offense_defense_k * team_error
    opponent_state.offense += params.offense_defense_k * opponent_error
    team_state.defense -= params.offense_defense_k * opponent_error
    pace_error = (game.points + game.opponent_points) - (2.0 * league_avg_points)
    team_state.pace += params.pace_k * pace_error
    opponent_state.pace += params.pace_k * pace_error

    venue = _venue_for_game(game)
    _record_team_game(
        team_state,
        game.game_date,
        game.margin,
        game.points,
        game.opponent_points,
        game.location,
        venue,
        params,
    )
    _record_team_game(
        opponent_state,
        game.game_date,
        -game.margin,
        game.opponent_points,
        game.points,
        _opponent_location(game.location),
        venue,
        params,
    )


def _record_team_game(
    state: _EnhancedTeamState,
    game_date: date,
    margin: float,
    points_for: float,
    points_against: float,
    location: str,
    venue: TeamVenueMeta | None,
    params: EnhancedEloParams,
) -> None:
    state.game_dates.append(game_date)
    state.margins.append(margin)
    state.points_for.append(points_for)
    state.points_against.append(points_against)
    state.locations.append(location)
    if venue is not None:
        state.last_venue = venue
    state.uncertainty = max(
        params.uncertainty_floor,
        state.uncertainty * params.uncertainty_decay
        + params.uncertainty_result_bump / math.sqrt(len(state.game_dates)),
    )


def _pregame_feature_for(
    game: NbaEloGame,
    team: str,
    pregame_features: dict[tuple[str, str], NbaPregameFeatures],
) -> NbaPregameFeatures | None:
    feature = pregame_features.get((game.game_id, team))
    if feature is None:
        return None
    return feature if feature.available_at <= _feature_cutoff(game) else None


def _feature_cutoff(game: NbaEloGame) -> datetime:
    return game.game_start_at or datetime.combine(game.game_date, time.min)


def _market_probability(feature: NbaPregameFeatures | None) -> float | None:
    if feature is None:
        return None
    return feature.moneyline_prob or feature.spread_implied_prob or feature.opening_moneyline_prob


def _location_points(game: NbaEloGame, params: EnhancedEloParams) -> float:
    home = params.home_points * (params.playoff_home_multiplier if game.is_playoffs else 1.0)
    if game.location == "H":
        return home
    if game.location == "A":
        return -home
    return 0.0


def _state_rest_days(state: _EnhancedTeamState, game_date: date) -> float | None:
    if not state.game_dates:
        return None
    return float(max(0, min(7, (game_date - state.game_dates[-1]).days)))


def _games_in_days(state: _EnhancedTeamState, game_date: date, days: int) -> int:
    return sum(1 for prior in state.game_dates if 0 < (game_date - prior).days < days)


def _home_away_streak(state: _EnhancedTeamState) -> int:
    if not state.locations:
        return 0
    latest = state.locations[-1]
    sign = 1 if latest == "H" else -1 if latest == "A" else 0
    count = 0
    for loc in reversed(state.locations):
        if loc != latest:
            break
        count += sign
    return count


def _venue_for_game(game: NbaEloGame) -> TeamVenueMeta | None:
    if game.location == "H":
        return _NBA_TEAM_META.get(game.team)
    if game.location == "A":
        return _NBA_TEAM_META.get(game.opponent)
    return None


def _travel_1000(previous: TeamVenueMeta | None, current: TeamVenueMeta | None) -> float:
    if previous is None or current is None:
        return 0.0
    return _haversine_miles(previous.latitude, previous.longitude, current.latitude, current.longitude) / 1000.0


def _timezone_delta(previous: TeamVenueMeta | None, current: TeamVenueMeta | None) -> float:
    if previous is None or current is None:
        return 0.0
    return abs(current.utc_offset_hours - previous.utc_offset_hours)


def _haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 3958.8
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2.0 * radius * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))


def _shrunk_average(values: Sequence[float], shrink: float) -> float:
    if not values:
        return 0.0
    return (sum(values) / len(values)) * (len(values) / (len(values) + shrink))


def _optional_diff(left: float | None, right: float | None) -> float:
    if left is None or right is None:
        return 0.0
    return float(left) - float(right)


def _opponent_location(location: str) -> str:
    if location == "H":
        return "A"
    if location == "A":
        return "H"
    return "N"


def _calibration_variants(
    train_probs: Sequence[float],
    train_outcomes: Sequence[int],
    target_probs: Sequence[float],
) -> list[tuple[str, list[float], PlattCalibrator | IsotonicCalibrator | IdentityCalibrator]]:
    identity = IdentityCalibrator()
    platt = PlattCalibrator.fit(train_probs, train_outcomes)
    isotonic = IsotonicCalibrator.fit(train_probs, train_outcomes)
    return [
        ("identity", identity.transform_many(target_probs), identity),
        ("platt", platt.transform_many(target_probs), platt),
        ("isotonic", isotonic.transform_many(target_probs), isotonic),
    ]


def _fit_calibrator(
    method: str,
    probs: Sequence[float],
    outcomes: Sequence[int],
) -> PlattCalibrator | IsotonicCalibrator | IdentityCalibrator:
    if method == "identity":
        return IdentityCalibrator()
    if method == "platt":
        return PlattCalibrator.fit(probs, outcomes)
    if method == "isotonic":
        return IsotonicCalibrator.fit(probs, outcomes)
    raise ValueError(f"unknown calibration method: {method}")


def _calibrator_payload(calibrator: PlattCalibrator | IsotonicCalibrator | IdentityCalibrator) -> dict[str, Any]:
    if isinstance(calibrator, IdentityCalibrator):
        return {"method": "identity"}
    if isinstance(calibrator, PlattCalibrator):
        return {"method": "platt", "a": calibrator.a, "b": calibrator.b}
    return {
        "method": "isotonic",
        "blocks": len(calibrator.thresholds),
        "thresholds": calibrator.thresholds,
        "levels": calibrator.levels,
    }


def _metric_row(label: str, metrics: ForecastMetrics) -> str:
    return (
        f"| {label} | {metrics.n} | {metrics.brier:.6f} | {metrics.log_loss:.6f} | "
        f"{metrics.ece:.6f} | {metrics.accuracy:.4f} |"
    )


def _calibrator_summary(payload: dict[str, Any]) -> str:
    stage = payload.get("stage")
    calibration = payload.get("calibration", payload)
    method = calibration.get("method") if isinstance(calibration, dict) else None
    if method == "isotonic":
        return f"stage={stage or 'baseline'}, method=isotonic, blocks={calibration.get('blocks')}"
    if method == "platt":
        return (
            f"stage={stage or 'baseline'}, method=platt, "
            f"a={float(calibration.get('a', 0.0)):.6f}, b={float(calibration.get('b', 0.0)):.6f}"
        )
    return f"stage={stage or 'baseline'}, method={method or calibration}"


def _sig_line(label: str, sig: PairedSignificance) -> str:
    return (
        f"- {label}: Brier improvement={sig.improvement:.6f}, "
        f"95% CI [{sig.ci_low:.6f}, {sig.ci_high:.6f}], p={sig.p_value:.4f}, "
        f"significant={str(sig.significant).lower()}"
    )


def _filter_years(preds: Sequence[EloPrediction], years: tuple[int, int]) -> list[EloPrediction]:
    lo, hi = years
    return [p for p in preds if lo <= p.season <= hi]


def _location_adjustment(game: NbaEloGame, params: EloParams) -> float:
    home = params.home_advantage * (params.playoff_home_multiplier if game.is_playoffs else 1.0)
    if game.location == "H":
        return home
    if game.location == "A":
        return -home
    return 0.0


def _rest_adjustment(game: NbaEloGame, last_game_date: dict[str, date], params: EloParams) -> float:
    team_rest = _rest_days(game.team, game.game_date, last_game_date)
    opponent_rest = _rest_days(game.opponent, game.game_date, last_game_date)
    if team_rest is None or opponent_rest is None:
        return 0.0
    return params.rest_advantage * max(-3.0, min(3.0, team_rest - opponent_rest))


def _rest_days(team: str, game_date: date, last_game_date: dict[str, date]) -> float | None:
    prior = last_game_date.get(team)
    if prior is None:
        return None
    return float(max(0, min(7, (game_date - prior).days)))


def _mov_multiplier(margin: int, adjusted_diff: float, params: EloParams) -> float:
    base = 1.0 + params.mov_weight * min(1.0, math.log(abs(margin) + 1.0) / 3.0)
    favorite_won = (margin > 0 and adjusted_diff > 0) or (margin < 0 and adjusted_diff < 0)
    upset_adjust = 0.94 if favorite_won else 1.06
    return max(0.5, base * upset_adjust)


def _elo_probability(diff: float, scale: float) -> float:
    return _clip01(1.0 / (1.0 + 10 ** (-diff / scale)))


def _clip01(p: float) -> float:
    return min(1.0 - _EPS, max(_EPS, float(p)))


def _logit(p: float) -> float:
    clipped = _clip01(p)
    return math.log(clipped / (1.0 - clipped))


def _sigmoid(z: float) -> float:
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    exp_z = math.exp(z)
    return exp_z / (1.0 + exp_z)


def _parse_int(value: object) -> int | None:
    try:
        if value is None or str(value).strip() == "":
            return None
        return int(float(str(value)))
    except ValueError:
        return None


def _parse_float(value: object) -> float | None:
    try:
        if value is None or str(value).strip() == "":
            return None
        return float(str(value))
    except ValueError:
        return None


def _parse_probability(value: object) -> float | None:
    raw = _parse_float(value)
    if raw is None:
        return None
    if 0.0 <= raw <= 1.0:
        return _clip01(raw)
    if raw > 1.0:
        return _clip01(100.0 / (raw + 100.0))
    return _clip01(abs(raw) / (abs(raw) + 100.0))


def _parse_datetime(value: object) -> datetime | None:
    if value is None or str(value).strip() == "":
        return None
    raw = str(value).strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    raise ValueError(f"could not parse datetime: {value!r}")


def _to_jsonable(value: Any) -> Any:
    if hasattr(value, "__dataclass_fields__"):
        return {key: _to_jsonable(raw) for key, raw in asdict(value).items()}
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, tuple | list):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _to_jsonable(raw) for key, raw in value.items()}
    return value
