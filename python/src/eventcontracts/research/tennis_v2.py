"""Tennis pre-match model — v2 feature engineering.

v2 keeps the v1 design discipline (every feature is numeric, ordered, derived
from pre-match state only) but fixes what the v1 diagnostics exposed:

* v1 had three dead odds features and an under-powered form signal.
* v2 adds rolling serve/return effectiveness (the strongest missing class),
  opponent-adjusted and surface-specific form, fatigue depth, hand matchup,
  round, and a market block populated by an odds merge.
* v2 Elo uses a dynamic K, an overall↔surface blend, and a margin-of-victory
  multiplier.

The split between *accumulation* and the *stateless vector* matters for
Python↔Rust parity. All the stateful logic (Elo updates, rolling serve/return
sums, fatigue windows) lives in :func:`build_v2_training_frame` (training) and
would live in a Rust live adapter. The promoted model only ever sees a
:class:`TennisV2Snapshot` of pre-computed values, and
:func:`feature_row_v2` turns that into the ordered vector with plain
arithmetic — that arithmetic is the only thing the Rust runtime must mirror.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict, deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

from eventcontracts.models import evaluation as _evaluation
from eventcontracts.models import onnx_export as _onnx
from eventcontracts.models import parity as _parity
from eventcontracts.research.tennis_xgboost import (
    _normalized_implied_probabilities,
    _number,
    _odds_from_row,
    _optional_float,
    _optional_int,
    _parse_match_date,
    _polars,
    _rank_log_advantage,
    _seed_advantage,
    _smoothed_win_pct,
    _surface_key,
)

if TYPE_CHECKING:
    import polars as pl

FEATURE_SCHEMA_ID = "tennis_xgboost_match_features"
FEATURE_SCHEMA_VERSION = "2"

# Career-rate priors used until a player has accumulated real serve/return
# samples. Tour-typical values so a debutant isn't scored as average-zero.
_PRIOR_SERVE_WON = 0.63
_PRIOR_RETURN_WON = 0.37
_PRIOR_ACE = 0.07
_PRIOR_DF = 0.045
_PRIOR_BP_SAVE = 0.61
_PRIOR_BP_CONVERT = 0.40
_SERVE_PRIOR_STRENGTH = 200.0  # pseudo-points of prior (Beta-style smoothing)

_ROUND_ORDINAL = {
    "R128": 1,
    "R64": 2,
    "R32": 3,
    "R16": 4,
    "QF": 5,
    "SF": 6,
    "F": 7,
    "RR": 3,  # round-robin ≈ early-middle
    "BR": 6,  # bronze/3rd-place ≈ semi
}
_MAX_ROUND = 7.0


@dataclass(frozen=True)
class TennisV2FeatureSpec:
    name: str
    description: str
    default: float = 0.0


TENNIS_V2_FEATURES: tuple[TennisV2FeatureSpec, ...] = (
    # --- strength / ranking ---
    TennisV2FeatureSpec("elo_diff", "Overall Elo difference (dynamic-K, MoV-weighted): p1 − p2."),
    TennisV2FeatureSpec("surface_elo_diff", "Surface-specific Elo difference: p1 − p2."),
    TennisV2FeatureSpec("elo_blend_diff", "Blended overall+surface Elo difference: p1 − p2."),
    TennisV2FeatureSpec("rank_log_advantage", "log1p rank edge for p1 (better rank → positive)."),
    TennisV2FeatureSpec("rank_points_log_diff", "log1p ATP rank-points difference: p1 − p2."),
    TennisV2FeatureSpec("seed_advantage", "Tournament seed edge for p1 (better seed → positive)."),
    # --- physical / style ---
    TennisV2FeatureSpec("age_diff", "p1 age − p2 age (years)."),
    TennisV2FeatureSpec("height_cm_diff", "p1 height − p2 height (cm)."),
    TennisV2FeatureSpec("hand_matchup", "+1 p1 lefty vs righty, −1 righty vs lefty, 0 otherwise."),
    # --- serve / return effectiveness (rolling, pre-match) ---
    TennisV2FeatureSpec("serve_pts_won_diff", "Career serve-points-won% difference: p1 − p2.", 0.0),
    TennisV2FeatureSpec("return_pts_won_diff", "Career return-points-won% difference: p1 − p2.", 0.0),
    TennisV2FeatureSpec("serve_edge_diff", "Total (serve+return) points-won-rate edge: p1 − p2.", 0.0),
    TennisV2FeatureSpec("ace_rate_diff", "Career ace-rate (aces/serve-pts) difference: p1 − p2.", 0.0),
    TennisV2FeatureSpec("df_rate_diff", "Career double-fault-rate difference: p1 − p2.", 0.0),
    TennisV2FeatureSpec("bp_save_pct_diff", "Career break-point-saved% difference: p1 − p2.", 0.0),
    TennisV2FeatureSpec("bp_convert_pct_diff", "Career break-point-conversion% difference: p1 − p2.", 0.0),
    # --- form ---
    TennisV2FeatureSpec("recent_win_pct_diff", "Smoothed recent win-rate edge over the rolling window."),
    TennisV2FeatureSpec("opp_adjusted_form_diff", "Recent performance vs Elo expectation (over/under-perform)."),
    TennisV2FeatureSpec("surface_win_pct_diff", "Smoothed career win-rate on this surface: p1 − p2."),
    TennisV2FeatureSpec("recent_match_count_diff", "Difference in available recent-form sample counts."),
    # --- fatigue ---
    TennisV2FeatureSpec("days_rest_diff", "Days since prior match: p1 − p2 (clamped 0..30)."),
    TennisV2FeatureSpec("recent_matches_14d_diff", "Matches played in the last 14 days: p1 − p2."),
    TennisV2FeatureSpec("recent_games_14d_diff", "Games played in the last 14 days: p1 − p2."),
    TennisV2FeatureSpec("prev_match_long_diff", "Whether the previous match went long (3+/4+ sets): p1 − p2."),
    # --- context ---
    TennisV2FeatureSpec("best_of_5", "1 when best-of-five, else 0."),
    TennisV2FeatureSpec("is_grand_slam", "1 when tourney_level is Grand Slam, else 0."),
    TennisV2FeatureSpec("round_ordinal", "Tournament round normalized to [0,1] (R128→F)."),
    TennisV2FeatureSpec("surface_hard", "1 for hard-court matches, else 0."),
    TennisV2FeatureSpec("surface_clay", "1 for clay-court matches, else 0."),
    TennisV2FeatureSpec("surface_grass", "1 for grass-court matches, else 0."),
    # --- market (populated by the odds merge; neutral + flag when absent) ---
    TennisV2FeatureSpec("p1_implied_prob", "Bookmaker implied probability for p1 (overround-normalized).", 0.5),
    TennisV2FeatureSpec("implied_prob_diff", "Normalized implied probability edge: p1 − p2."),
    TennisV2FeatureSpec("odds_overround", "Raw reciprocal-odds sum before normalization."),
    TennisV2FeatureSpec("odds_present", "1 when real bookmaker odds were available, else 0."),
)
TENNIS_V2_FEATURE_NAMES: tuple[str, ...] = tuple(f.name for f in TENNIS_V2_FEATURES)

# Monotone direction per feature for XGBoost `monotone_constraints` (+1 means
# "higher value must not decrease p1 win prob"; 0 = unconstrained). Only the
# features with an unambiguous economic direction are constrained.
_MONOTONE = {
    "elo_diff": 1,
    "surface_elo_diff": 1,
    "elo_blend_diff": 1,
    "rank_log_advantage": 1,
    "rank_points_log_diff": 1,
    "seed_advantage": 1,
    "serve_edge_diff": 1,
    "p1_implied_prob": 1,
    "implied_prob_diff": 1,
}


def monotone_constraints() -> tuple[int, ...]:
    """`monotone_constraints` tuple aligned to :data:`TENNIS_V2_FEATURE_NAMES`."""

    return tuple(_MONOTONE.get(name, 0) for name in TENNIS_V2_FEATURE_NAMES)


@dataclass(frozen=True)
class TennisV2Snapshot:
    """Stateless pre-match input — every value is computed from prior matches."""

    match_id: str
    match_date: date
    p1_id: str
    p2_id: str
    surface: str = "Unknown"
    tourney_level: str = ""
    best_of: int = 3
    round: str = ""
    # strength
    p1_elo: float = 1500.0
    p2_elo: float = 1500.0
    p1_surface_elo: float = 1500.0
    p2_surface_elo: float = 1500.0
    p1_elo_blend: float = 1500.0
    p2_elo_blend: float = 1500.0
    p1_rank: int | None = None
    p2_rank: int | None = None
    p1_rank_points: float | None = None
    p2_rank_points: float | None = None
    p1_seed: int | None = None
    p2_seed: int | None = None
    # physical / style
    p1_age: float | None = None
    p2_age: float | None = None
    p1_height_cm: float | None = None
    p2_height_cm: float | None = None
    p1_hand: str = "U"
    p2_hand: str = "U"
    # serve / return (pre-computed career rates)
    p1_serve_won: float = _PRIOR_SERVE_WON
    p2_serve_won: float = _PRIOR_SERVE_WON
    p1_return_won: float = _PRIOR_RETURN_WON
    p2_return_won: float = _PRIOR_RETURN_WON
    p1_ace_rate: float = _PRIOR_ACE
    p2_ace_rate: float = _PRIOR_ACE
    p1_df_rate: float = _PRIOR_DF
    p2_df_rate: float = _PRIOR_DF
    p1_bp_save: float = _PRIOR_BP_SAVE
    p2_bp_save: float = _PRIOR_BP_SAVE
    p1_bp_convert: float = _PRIOR_BP_CONVERT
    p2_bp_convert: float = _PRIOR_BP_CONVERT
    # form
    p1_recent_wins: int = 0
    p2_recent_wins: int = 0
    p1_recent_matches: int = 0
    p2_recent_matches: int = 0
    p1_opp_adjusted_form: float = 0.0
    p2_opp_adjusted_form: float = 0.0
    p1_surface_wins: int = 0
    p2_surface_wins: int = 0
    p1_surface_matches: int = 0
    p2_surface_matches: int = 0
    # fatigue
    p1_days_since_match: int | None = None
    p2_days_since_match: int | None = None
    p1_matches_14d: int = 0
    p2_matches_14d: int = 0
    p1_games_14d: int = 0
    p2_games_14d: int = 0
    p1_prev_long: int = 0
    p2_prev_long: int = 0
    # market
    p1_decimal_odds: float | None = None
    p2_decimal_odds: float | None = None
    label: int | None = None


def feature_schema_document() -> dict[str, Any]:
    return {
        "schema_id": FEATURE_SCHEMA_ID,
        "schema_version": FEATURE_SCHEMA_VERSION,
        "description": (
            "Pre-match ATP tennis features (v2) for the XGBoost match-winner model: "
            "dynamic Elo, rolling serve/return effectiveness, fatigue, form, and an "
            "optional bookmaker-odds block. All numeric and ordered for Rust parity."
        ),
        "features": [
            {
                "name": f.name,
                "dtype": "float32",
                "description": f.description,
                "nullable": False,
                "default": f.default,
            }
            for f in TENNIS_V2_FEATURES
        ],
    }


def write_feature_schema(path: str | Path) -> Path:
    """Write the v2 feature schema used by Python/Rust promotion."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(feature_schema_document(), indent=2) + "\n", encoding="utf-8")
    return target


def feature_row_v2(snapshot: TennisV2Snapshot) -> dict[str, float]:
    """Stateless snapshot → ordered feature row. Mirror this exactly in Rust."""

    p1_prob, p2_prob, overround = _normalized_implied_probabilities(
        snapshot.p1_decimal_odds, snapshot.p2_decimal_odds
    )
    surface = _surface_key(snapshot.surface)
    p1_recent = _smoothed_win_pct(snapshot.p1_recent_wins, snapshot.p1_recent_matches)
    p2_recent = _smoothed_win_pct(snapshot.p2_recent_wins, snapshot.p2_recent_matches)
    p1_surface = _smoothed_win_pct(snapshot.p1_surface_wins, snapshot.p1_surface_matches)
    p2_surface = _smoothed_win_pct(snapshot.p2_surface_wins, snapshot.p2_surface_matches)
    p1_total = snapshot.p1_serve_won + snapshot.p1_return_won
    p2_total = snapshot.p2_serve_won + snapshot.p2_return_won
    row = {
        "elo_diff": snapshot.p1_elo - snapshot.p2_elo,
        "surface_elo_diff": snapshot.p1_surface_elo - snapshot.p2_surface_elo,
        "elo_blend_diff": snapshot.p1_elo_blend - snapshot.p2_elo_blend,
        "rank_log_advantage": _rank_log_advantage(snapshot.p1_rank, snapshot.p2_rank),
        "rank_points_log_diff": math.log1p(_number(snapshot.p1_rank_points, 0.0))
        - math.log1p(_number(snapshot.p2_rank_points, 0.0)),
        "seed_advantage": _seed_advantage(snapshot.p1_seed, snapshot.p2_seed),
        "age_diff": _number(snapshot.p1_age, 0.0) - _number(snapshot.p2_age, 0.0),
        "height_cm_diff": _number(snapshot.p1_height_cm, 0.0) - _number(snapshot.p2_height_cm, 0.0),
        "hand_matchup": _hand_matchup(snapshot.p1_hand, snapshot.p2_hand),
        "serve_pts_won_diff": snapshot.p1_serve_won - snapshot.p2_serve_won,
        "return_pts_won_diff": snapshot.p1_return_won - snapshot.p2_return_won,
        "serve_edge_diff": p1_total - p2_total,
        "ace_rate_diff": snapshot.p1_ace_rate - snapshot.p2_ace_rate,
        "df_rate_diff": snapshot.p1_df_rate - snapshot.p2_df_rate,
        "bp_save_pct_diff": snapshot.p1_bp_save - snapshot.p2_bp_save,
        "bp_convert_pct_diff": snapshot.p1_bp_convert - snapshot.p2_bp_convert,
        "recent_win_pct_diff": p1_recent - p2_recent,
        "opp_adjusted_form_diff": snapshot.p1_opp_adjusted_form - snapshot.p2_opp_adjusted_form,
        "surface_win_pct_diff": p1_surface - p2_surface,
        "recent_match_count_diff": float(snapshot.p1_recent_matches - snapshot.p2_recent_matches),
        "days_rest_diff": _rest_days(snapshot.p1_days_since_match) - _rest_days(snapshot.p2_days_since_match),
        "recent_matches_14d_diff": float(snapshot.p1_matches_14d - snapshot.p2_matches_14d),
        "recent_games_14d_diff": float(snapshot.p1_games_14d - snapshot.p2_games_14d),
        "prev_match_long_diff": float(snapshot.p1_prev_long - snapshot.p2_prev_long),
        "best_of_5": 1.0 if snapshot.best_of >= 5 else 0.0,
        "is_grand_slam": 1.0 if snapshot.tourney_level.upper() == "G" else 0.0,
        "round_ordinal": _round_ordinal(snapshot.round),
        "surface_hard": 1.0 if surface == "hard" else 0.0,
        "surface_clay": 1.0 if surface == "clay" else 0.0,
        "surface_grass": 1.0 if surface == "grass" else 0.0,
        "p1_implied_prob": p1_prob,
        "implied_prob_diff": p1_prob - p2_prob,
        "odds_overround": overround,
        "odds_present": 1.0 if overround > 0.0 else 0.0,
    }
    return {name: float(row[name]) for name in TENNIS_V2_FEATURE_NAMES}


def feature_vector_v2(snapshot: TennisV2Snapshot) -> tuple[float, ...]:
    row = feature_row_v2(snapshot)
    return tuple(row[name] for name in TENNIS_V2_FEATURE_NAMES)


# Path of the committed Python<->Rust feature-parity fixture. The Rust
# feature-builder test loads this and asserts agreement; a Python drift-guard
# test asserts the committed file still equals a fresh generation.
PARITY_FIXTURE_RELPATH = "contracts/parity/tennis_v2_features/feature_cases.json"


def _parity_snapshots() -> list[TennisV2Snapshot]:
    """Diverse snapshots that exercise every branch of ``feature_row_v2``."""

    return [
        # 0: pure defaults / priors, no odds, unknown surface.
        TennisV2Snapshot(match_id="v2-0", match_date=date(2025, 1, 6), p1_id="a", p2_id="b"),
        # 1: strong p1 favorite, slam, clay, QF, best-of-5, lefty vs righty, vigged odds.
        TennisV2Snapshot(
            match_id="v2-1", match_date=date(2025, 5, 26), p1_id="a", p2_id="b",
            surface="Clay", tourney_level="G", best_of=5, round="QF",
            p1_elo=1850.0, p2_elo=1600.0, p1_surface_elo=1900.0, p2_surface_elo=1550.0,
            p1_elo_blend=1870.0, p2_elo_blend=1580.0, p1_rank=2, p2_rank=25,
            p1_rank_points=8500.0, p2_rank_points=1800.0, p1_seed=1, p2_seed=12,
            p1_age=24.3, p2_age=31.8, p1_height_cm=185.0, p2_height_cm=178.0,
            p1_hand="L", p2_hand="R", p1_serve_won=0.69, p2_serve_won=0.61,
            p1_return_won=0.42, p2_return_won=0.35, p1_ace_rate=0.11, p2_ace_rate=0.05,
            p1_df_rate=0.03, p2_df_rate=0.06, p1_bp_save=0.66, p2_bp_save=0.58,
            p1_bp_convert=0.44, p2_bp_convert=0.38, p1_recent_wins=9, p2_recent_wins=4,
            p1_recent_matches=11, p2_recent_matches=10, p1_opp_adjusted_form=0.18,
            p2_opp_adjusted_form=-0.05, p1_surface_wins=40, p2_surface_wins=15,
            p1_surface_matches=50, p2_surface_matches=30, p1_days_since_match=2,
            p2_days_since_match=9, p1_matches_14d=4, p2_matches_14d=1, p1_games_14d=58,
            p2_games_14d=14, p1_prev_long=1, p2_prev_long=0, p1_decimal_odds=1.4,
            p2_decimal_odds=2.9,
        ),
        # 2: p2 favorite, grass final, righty vs lefty, p2 vigged odds.
        TennisV2Snapshot(
            match_id="v2-2", match_date=date(2025, 7, 13), p1_id="a", p2_id="b",
            surface="Grass", tourney_level="G", best_of=5, round="F",
            p1_elo=1580.0, p2_elo=1820.0, p1_surface_elo=1560.0, p2_surface_elo=1880.0,
            p1_elo_blend=1575.0, p2_elo_blend=1840.0, p1_rank=30, p2_rank=3,
            p1_hand="R", p2_hand="L", p1_days_since_match=100, p2_days_since_match=4,
            p1_decimal_odds=3.5, p2_decimal_odds=1.3,
        ),
        # 3: missing odds + missing ranks, hard court, R32, varied serve.
        TennisV2Snapshot(
            match_id="v2-3", match_date=date(2025, 3, 10), p1_id="a", p2_id="b",
            surface="Hard", tourney_level="M", best_of=3, round="R32",
            p1_elo=1700.0, p2_elo=1680.0, p1_serve_won=0.64, p2_serve_won=0.67,
            p1_return_won=0.39, p2_return_won=0.36, p1_recent_wins=6, p2_recent_wins=7,
            p1_recent_matches=10, p2_recent_matches=12,
        ),
        # 4: carpet (no surface one-hot fires), round-robin, rank_points only.
        TennisV2Snapshot(
            match_id="v2-4", match_date=date(2025, 11, 12), p1_id="a", p2_id="b",
            surface="Carpet", tourney_level="F", best_of=3, round="RR",
            p1_rank_points=3200.0, p2_rank_points=3400.0, p1_matches_14d=3,
            p2_matches_14d=3, p1_games_14d=40, p2_games_14d=44, p1_prev_long=0,
            p2_prev_long=1, p1_decimal_odds=2.0, p2_decimal_odds=2.0,
        ),
    ]


def feature_parity_fixture() -> dict[str, Any]:
    """Snapshots + expected vectors pinning Python<->Rust v2 feature parity."""

    from dataclasses import asdict

    cases = []
    for snapshot in _parity_snapshots():
        fields = asdict(snapshot)
        for non_feature in ("match_id", "match_date", "p1_id", "p2_id", "label"):
            fields.pop(non_feature, None)
        cases.append(
            {
                "case_id": snapshot.match_id,
                "snapshot": fields,
                "expected": list(feature_vector_v2(snapshot)),
            }
        )
    return {
        "schema_id": FEATURE_SCHEMA_ID,
        "schema_version": FEATURE_SCHEMA_VERSION,
        "feature_names": list(TENNIS_V2_FEATURE_NAMES),
        "cases": cases,
    }


def _hand_matchup(p1_hand: str, p2_hand: str) -> float:
    p1 = (p1_hand or "U").strip().upper()[:1]
    p2 = (p2_hand or "U").strip().upper()[:1]
    if p1 == "L" and p2 == "R":
        return 1.0
    if p1 == "R" and p2 == "L":
        return -1.0
    return 0.0


def _round_ordinal(round_name: str) -> float:
    return _ROUND_ORDINAL.get((round_name or "").strip().upper(), 0) / _MAX_ROUND


def _rest_days(value: int | None) -> float:
    if value is None:
        return 0.0
    return float(max(min(value, 30), 0))


# ---------------------------------------------------------------------------
# Stateful training-frame construction (Python-only; never crosses to Rust).
# ---------------------------------------------------------------------------


class _ServeSums:
    """Cumulative serve/return tallies for one player, Beta-smoothed on read."""

    __slots__ = ("svpt", "serve_won", "ace", "df", "ret_pts", "ret_won", "bp_faced", "bp_saved", "bp_opp", "bp_conv")

    def __init__(self) -> None:
        self.svpt = self.serve_won = self.ace = self.df = 0.0
        self.ret_pts = self.ret_won = 0.0
        self.bp_faced = self.bp_saved = self.bp_opp = self.bp_conv = 0.0

    def rates(self) -> dict[str, float]:
        return {
            "serve_won": _smooth(self.serve_won, self.svpt, _PRIOR_SERVE_WON),
            "return_won": _smooth(self.ret_won, self.ret_pts, _PRIOR_RETURN_WON),
            "ace_rate": _smooth(self.ace, self.svpt, _PRIOR_ACE),
            "df_rate": _smooth(self.df, self.svpt, _PRIOR_DF),
            "bp_save": _smooth(self.bp_saved, self.bp_faced, _PRIOR_BP_SAVE),
            "bp_convert": _smooth(self.bp_conv, self.bp_opp, _PRIOR_BP_CONVERT),
        }


def _smooth(won: float, total: float, prior: float) -> float:
    return (won + prior * _SERVE_PRIOR_STRENGTH) / (total + _SERVE_PRIOR_STRENGTH)


def build_v2_training_frame(
    matches: pl.DataFrame,
    *,
    include_mirrored: bool = True,
    recent_window: int = 14,
    elo_base_k: float = 250.0,
    elo_layoff_boost: float = 0.0,
    surface_blend: float = 0.4,
) -> pl.DataFrame:
    """Build v2 pre-match features from Sackmann-style ATP rows.

    State (Elo with dynamic K [experience] and margin-of-victory, rolling
    serve/return, fatigue windows, surface form, opponent-adjusted form) is updated
    only *after* each match row is emitted, so a match never sees its own outcome.
    Reads ``winner_decimal_odds`` / ``loser_decimal_odds`` (or ``AvgW``/``AvgL``)
    when present so a downstream odds merge lights up the market block.

    ``elo_layoff_boost`` adds an inactivity term to the dynamic K (stale ratings
    adapt faster after a long break). It is **default-off (0.0)**: a 2025-26 ATP
    holdout showed no calibration gain (deltas within noise — long layoffs are rare
    on the main tour), so it stays opt-in to avoid a forced model retrain. Whatever
    value is used here, ``build_upcoming_snapshot`` must use the same so the live
    features match the model's training distribution.
    """

    snapshots = _build_v2_snapshots(
        matches,
        include_mirrored=include_mirrored,
        recent_window=recent_window,
        elo_base_k=elo_base_k,
        elo_layoff_boost=elo_layoff_boost,
        surface_blend=surface_blend,
    )
    return _v2_snapshots_to_frame(snapshots)


def _build_v2_snapshots(
    matches: pl.DataFrame,
    *,
    include_mirrored: bool = True,
    recent_window: int = 14,
    elo_base_k: float = 250.0,
    elo_layoff_boost: float = 0.0,
    surface_blend: float = 0.4,
) -> list[TennisV2Snapshot]:
    """Replay history and emit one (or two, mirrored) snapshot per match.

    Shared by ``build_v2_training_frame`` and ``build_upcoming_snapshot`` so the
    two paths can never drift in how per-player state is accumulated.
    """

    required = {"winner_id", "loser_id", "tourney_date"}
    missing = required.difference(matches.columns)
    if missing:
        raise ValueError(f"missing required Sackmann columns: {sorted(missing)}")
    if recent_window <= 0:
        raise ValueError("recent_window must be > 0")
    sort_columns = [c for c in ("tourney_date", "tourney_id", "match_num") if c in matches.columns]
    ordered = matches.sort(sort_columns or ["tourney_date"])

    elo: dict[str, float] = defaultdict(lambda: 1500.0)
    elo_n: dict[str, int] = defaultdict(int)
    surf_elo: dict[tuple[str, str], float] = defaultdict(lambda: 1500.0)
    recent: dict[str, deque[int]] = defaultdict(lambda: deque(maxlen=recent_window))
    recent_perf: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=recent_window))
    surf_wins: dict[tuple[str, str], int] = defaultdict(int)
    surf_matches: dict[tuple[str, str], int] = defaultdict(int)
    serve: dict[str, _ServeSums] = defaultdict(_ServeSums)
    last_played: dict[str, date] = {}
    fatigue: dict[str, deque[tuple[date, int, int]]] = defaultdict(lambda: deque(maxlen=20))

    snapshots: list[TennisV2Snapshot] = []
    for index, row in enumerate(ordered.to_dicts()):
        winner, loser = str(row["winner_id"]), str(row["loser_id"])
        match_date = _parse_match_date(row["tourney_date"])
        surface = _surface_key(str(row.get("surface") or "Unknown"))
        match_id = str(row.get("match_id") or row.get("tourney_id") or f"match-{index:08d}")
        w_games, l_games, _n_sets, long_flag = _parse_score(
            str(row.get("score") or ""), int(_number(row.get("best_of"), 3))
        )

        win_snap = _v2_snapshot(
            row,
            match_id=f"{match_id}:winner",
            match_date=match_date,
            surface=surface,
            p1=winner,
            p2=loser,
            p1_prefix="winner",
            p2_prefix="loser",
            elo=elo,
            surf_elo=surf_elo,
            surface_blend=surface_blend,
            recent=recent,
            recent_perf=recent_perf,
            surf_wins=surf_wins,
            surf_matches=surf_matches,
            serve=serve,
            last_played=last_played,
            fatigue=fatigue,
            match_date_for_fatigue=match_date,
            label=1,
        )
        snapshots.append(win_snap)
        if include_mirrored:
            snapshots.append(_mirror_v2(win_snap))

        # --- update state after emitting (no leakage) ---
        # layoff is read from last_played BEFORE its post-match update below.
        w_days_since = _days_since(last_played.get(winner), match_date)
        l_days_since = _days_since(last_played.get(loser), match_date)
        _update_elo(
            elo, elo_n, surf_elo, winner, loser, surface, w_games, l_games,
            base_k=elo_base_k, w_days_since=w_days_since, l_days_since=l_days_since,
            layoff_boost=elo_layoff_boost,
        )
        exp_w = 1.0 / (1.0 + 10.0 ** ((elo[loser] - elo[winner]) / 400.0))
        recent_perf[winner].append(1.0 - exp_w)
        recent_perf[loser].append(0.0 - (1.0 - exp_w))
        recent[winner].append(1)
        recent[loser].append(0)
        surf_wins[(winner, surface)] += 1
        surf_matches[(winner, surface)] += 1
        surf_matches[(loser, surface)] += 1
        _accumulate_serve(serve[winner], serve[loser], row)
        total_games = w_games + l_games
        fatigue[winner].append((match_date, total_games, long_flag))
        fatigue[loser].append((match_date, total_games, long_flag))
        last_played[winner] = match_date
        last_played[loser] = match_date

    return snapshots


def _v2_snapshot(
    row: Mapping[str, Any],
    *,
    match_id: str,
    match_date: date,
    surface: str,
    p1: str,
    p2: str,
    p1_prefix: str,
    p2_prefix: str,
    elo: dict[str, float],
    surf_elo: dict[tuple[str, str], float],
    surface_blend: float,
    recent: dict[str, deque[int]],
    recent_perf: dict[str, deque[float]],
    surf_wins: dict[tuple[str, str], int],
    surf_matches: dict[tuple[str, str], int],
    serve: dict[str, _ServeSums],
    last_played: dict[str, date],
    fatigue: dict[str, deque[tuple[date, int, int]]],
    match_date_for_fatigue: date,
    label: int,
) -> TennisV2Snapshot:
    p1_rates = serve[p1].rates()
    p2_rates = serve[p2].rates()
    p1_odds, p2_odds = _odds_from_row(row, p1_prefix=p1_prefix, p2_prefix=p2_prefix)
    p1_m14, p1_g14 = _fatigue_window(fatigue.get(p1), match_date_for_fatigue)
    p2_m14, p2_g14 = _fatigue_window(fatigue.get(p2), match_date_for_fatigue)
    return TennisV2Snapshot(
        match_id=match_id,
        match_date=match_date,
        p1_id=p1,
        p2_id=p2,
        surface=surface,
        tourney_level=str(row.get("tourney_level") or ""),
        best_of=int(_number(row.get("best_of"), 3)),
        round=str(row.get("round") or ""),
        p1_elo=elo[p1],
        p2_elo=elo[p2],
        p1_surface_elo=surf_elo[(p1, surface)],
        p2_surface_elo=surf_elo[(p2, surface)],
        p1_elo_blend=(1.0 - surface_blend) * elo[p1] + surface_blend * surf_elo[(p1, surface)],
        p2_elo_blend=(1.0 - surface_blend) * elo[p2] + surface_blend * surf_elo[(p2, surface)],
        p1_rank=_optional_int(row.get(f"{p1_prefix}_rank")),
        p2_rank=_optional_int(row.get(f"{p2_prefix}_rank")),
        p1_rank_points=_optional_float(row.get(f"{p1_prefix}_rank_points")),
        p2_rank_points=_optional_float(row.get(f"{p2_prefix}_rank_points")),
        p1_seed=_optional_int(row.get(f"{p1_prefix}_seed")),
        p2_seed=_optional_int(row.get(f"{p2_prefix}_seed")),
        p1_age=_optional_float(row.get(f"{p1_prefix}_age")),
        p2_age=_optional_float(row.get(f"{p2_prefix}_age")),
        p1_height_cm=_optional_float(row.get(f"{p1_prefix}_ht")),
        p2_height_cm=_optional_float(row.get(f"{p2_prefix}_ht")),
        p1_hand=str(row.get(f"{p1_prefix}_hand") or "U"),
        p2_hand=str(row.get(f"{p2_prefix}_hand") or "U"),
        p1_serve_won=p1_rates["serve_won"],
        p2_serve_won=p2_rates["serve_won"],
        p1_return_won=p1_rates["return_won"],
        p2_return_won=p2_rates["return_won"],
        p1_ace_rate=p1_rates["ace_rate"],
        p2_ace_rate=p2_rates["ace_rate"],
        p1_df_rate=p1_rates["df_rate"],
        p2_df_rate=p2_rates["df_rate"],
        p1_bp_save=p1_rates["bp_save"],
        p2_bp_save=p2_rates["bp_save"],
        p1_bp_convert=p1_rates["bp_convert"],
        p2_bp_convert=p2_rates["bp_convert"],
        p1_recent_wins=sum(recent[p1]),
        p2_recent_wins=sum(recent[p2]),
        p1_recent_matches=len(recent[p1]),
        p2_recent_matches=len(recent[p2]),
        p1_opp_adjusted_form=_mean(recent_perf.get(p1)),
        p2_opp_adjusted_form=_mean(recent_perf.get(p2)),
        p1_surface_wins=surf_wins[(p1, surface)],
        p2_surface_wins=surf_wins[(p2, surface)],
        p1_surface_matches=surf_matches[(p1, surface)],
        p2_surface_matches=surf_matches[(p2, surface)],
        p1_days_since_match=_days_since(last_played.get(p1), match_date),
        p2_days_since_match=_days_since(last_played.get(p2), match_date),
        p1_matches_14d=p1_m14,
        p2_matches_14d=p2_m14,
        p1_games_14d=p1_g14,
        p2_games_14d=p2_g14,
        p1_prev_long=_prev_long(fatigue.get(p1)),
        p2_prev_long=_prev_long(fatigue.get(p2)),
        p1_decimal_odds=p1_odds,
        p2_decimal_odds=p2_odds,
        label=label,
    )


def _mirror_v2(s: TennisV2Snapshot) -> TennisV2Snapshot:
    return TennisV2Snapshot(
        match_id=f"{s.match_id}:mirror",
        match_date=s.match_date,
        p1_id=s.p2_id,
        p2_id=s.p1_id,
        surface=s.surface,
        tourney_level=s.tourney_level,
        best_of=s.best_of,
        round=s.round,
        p1_elo=s.p2_elo,
        p2_elo=s.p1_elo,
        p1_surface_elo=s.p2_surface_elo,
        p2_surface_elo=s.p1_surface_elo,
        p1_elo_blend=s.p2_elo_blend,
        p2_elo_blend=s.p1_elo_blend,
        p1_rank=s.p2_rank,
        p2_rank=s.p1_rank,
        p1_rank_points=s.p2_rank_points,
        p2_rank_points=s.p1_rank_points,
        p1_seed=s.p2_seed,
        p2_seed=s.p1_seed,
        p1_age=s.p2_age,
        p2_age=s.p1_age,
        p1_height_cm=s.p2_height_cm,
        p2_height_cm=s.p1_height_cm,
        p1_hand=s.p2_hand,
        p2_hand=s.p1_hand,
        p1_serve_won=s.p2_serve_won,
        p2_serve_won=s.p1_serve_won,
        p1_return_won=s.p2_return_won,
        p2_return_won=s.p1_return_won,
        p1_ace_rate=s.p2_ace_rate,
        p2_ace_rate=s.p1_ace_rate,
        p1_df_rate=s.p2_df_rate,
        p2_df_rate=s.p1_df_rate,
        p1_bp_save=s.p2_bp_save,
        p2_bp_save=s.p1_bp_save,
        p1_bp_convert=s.p2_bp_convert,
        p2_bp_convert=s.p1_bp_convert,
        p1_recent_wins=s.p2_recent_wins,
        p2_recent_wins=s.p1_recent_wins,
        p1_recent_matches=s.p2_recent_matches,
        p2_recent_matches=s.p1_recent_matches,
        p1_opp_adjusted_form=s.p2_opp_adjusted_form,
        p2_opp_adjusted_form=s.p1_opp_adjusted_form,
        p1_surface_wins=s.p2_surface_wins,
        p2_surface_wins=s.p1_surface_wins,
        p1_surface_matches=s.p2_surface_matches,
        p2_surface_matches=s.p1_surface_matches,
        p1_days_since_match=s.p2_days_since_match,
        p2_days_since_match=s.p1_days_since_match,
        p1_matches_14d=s.p2_matches_14d,
        p2_matches_14d=s.p1_matches_14d,
        p1_games_14d=s.p2_games_14d,
        p2_games_14d=s.p1_games_14d,
        p1_prev_long=s.p2_prev_long,
        p2_prev_long=s.p1_prev_long,
        p1_decimal_odds=s.p2_decimal_odds,
        p2_decimal_odds=s.p1_decimal_odds,
        label=0 if s.label == 1 else 1 if s.label == 0 else None,
    )


def build_upcoming_snapshot(
    history: pl.DataFrame,
    *,
    p1_id: str,
    p2_id: str,
    match_date: date,
    surface: str,
    best_of: int = 3,
    round: str = "",
    tourney_level: str = "",
    p1_rank: int | None = None,
    p2_rank: int | None = None,
    p1_rank_points: float | None = None,
    p2_rank_points: float | None = None,
    p1_seed: int | None = None,
    p2_seed: int | None = None,
    p1_age: float | None = None,
    p2_age: float | None = None,
    p1_height_cm: float | None = None,
    p2_height_cm: float | None = None,
    p1_hand: str = "U",
    p2_hand: str = "U",
    p1_decimal_odds: float | None = None,
    p2_decimal_odds: float | None = None,
    recent_window: int = 14,
    match_id: str | None = None,
) -> TennisV2Snapshot:
    """Build a pre-match v2 snapshot for an UPCOMING match by replaying ``history``.

    ``history`` is Sackmann-style rows (the same schema ``build_v2_training_frame``
    consumes). Per-player *state* features (Elo, surface Elo, blend, serve/return,
    form, fatigue, surface record, rest days) are reconstructed from all prior
    matches; *static/recent* fields (rank, points, age, height, hand, seed) come
    from the args — the operator script auto-fills them from each player's most
    recent appearance. ``match_date`` must be on/after the last history date so the
    synthetic row sorts last and sees every prior match. The result is a
    ``TennisV2Snapshot`` ready to serialize into the runner's snapshot JSONL.
    """
    pl = _polars()
    synthetic = {
        "winner_id": p1_id,
        "loser_id": p2_id,
        "tourney_date": int(match_date.strftime("%Y%m%d")),
        "tourney_id": "zzz-upcoming",  # sorts after real tourney ids on a date tie
        "match_num": 1_000_000_000,  # sorts strictly last among same-date rows
        "match_id": match_id or f"{p1_id}-vs-{p2_id}-{match_date.isoformat()}",
        "surface": surface,
        "best_of": best_of,
        "round": round,
        "tourney_level": tourney_level,
        "score": "",  # outcome unknown; state-update after the row is irrelevant
        "winner_rank": p1_rank,
        "loser_rank": p2_rank,
        "winner_rank_points": p1_rank_points,
        "loser_rank_points": p2_rank_points,
        "winner_seed": p1_seed,
        "loser_seed": p2_seed,
        "winner_age": p1_age,
        "loser_age": p2_age,
        "winner_ht": p1_height_cm,
        "loser_ht": p2_height_cm,
        "winner_hand": p1_hand,
        "loser_hand": p2_hand,
        "winner_decimal_odds": p1_decimal_odds,
        "loser_decimal_odds": p2_decimal_odds,
    }
    combined = pl.concat([history, pl.DataFrame([synthetic])], how="diagonal_relaxed")
    snapshots = _build_v2_snapshots(combined, include_mirrored=False, recent_window=recent_window)
    # Synthetic row sorts last → its (single, non-mirrored) snapshot is the tail.
    return snapshots[-1]


def _v2_snapshots_to_frame(snapshots: Sequence[TennisV2Snapshot]) -> pl.DataFrame:
    pl = _polars()
    if not snapshots:
        schema: dict[str, type[Any]] = {"match_id": str, "match_date": date, "p1_id": str, "p2_id": str, "label": int}
        schema.update({name: float for name in TENNIS_V2_FEATURE_NAMES})
        return pl.DataFrame(schema=schema)
    rows: list[dict[str, Any]] = []
    for s in snapshots:
        row: dict[str, Any] = {
            "match_id": s.match_id,
            "match_date": s.match_date,
            "p1_id": s.p1_id,
            "p2_id": s.p2_id,
            "label": s.label,
            "odds_present_flag": 1 if (s.p1_decimal_odds and s.p2_decimal_odds) else 0,
        }
        row.update(feature_row_v2(s))
        rows.append(row)
    return pl.DataFrame(rows)


# Layoff (inactivity) sensitivity for the dynamic Elo K-factor. A rating unused
# for a long stretch is stale (rust / injury / age), so a returning result carries
# more information and should move it more. No boost inside the grace window
# (normal tour cadence has multi-week gaps), ramping to the cap.
_ELO_LAYOFF_GRACE_DAYS = 30
_ELO_LAYOFF_CAP_DAYS = 365


def _dynamic_k(matches_played: int, days_since_last: int | None, *, base_k: float, layoff_boost: float) -> float:
    """Elo K-factor that shrinks with experience and grows after a layoff.

    Experience term is the canonical 538 tennis form ``base_k / (matches + 5)**0.4``
    — provisional players adapt fast, veterans are stable. Layoff term scales K up
    linearly from the grace window to ``(1 + layoff_boost)x`` at the cap.
    ``days_since_last`` is None for a player's first match (already max K via m=0),
    so the layoff term never applies there."""
    k = base_k / ((matches_played + 5) ** 0.4)
    if layoff_boost > 0.0 and days_since_last is not None and days_since_last > _ELO_LAYOFF_GRACE_DAYS:
        span = _ELO_LAYOFF_CAP_DAYS - _ELO_LAYOFF_GRACE_DAYS
        frac = min(days_since_last - _ELO_LAYOFF_GRACE_DAYS, span) / span
        k *= 1.0 + layoff_boost * frac
    return k


def _update_elo(
    elo: dict[str, float],
    elo_n: dict[str, int],
    surf_elo: dict[tuple[str, str], float],
    winner: str,
    loser: str,
    surface: str,
    w_games: int,
    l_games: int,
    *,
    base_k: float,
    w_days_since: int | None,
    l_days_since: int | None,
    layoff_boost: float,
) -> None:
    k_w = _dynamic_k(elo_n[winner], w_days_since, base_k=base_k, layoff_boost=layoff_boost)
    k_l = _dynamic_k(elo_n[loser], l_days_since, base_k=base_k, layoff_boost=layoff_boost)
    mov = _mov_multiplier(w_games, l_games)
    exp_w = 1.0 / (1.0 + 10.0 ** ((elo[loser] - elo[winner]) / 400.0))
    delta = (1.0 - exp_w) * mov
    elo[winner] += k_w * delta
    elo[loser] -= k_l * delta
    elo_n[winner] += 1
    elo_n[loser] += 1
    sw, sl = (winner, surface), (loser, surface)
    exp_sw = 1.0 / (1.0 + 10.0 ** ((surf_elo[sl] - surf_elo[sw]) / 400.0))
    s_delta = (1.0 - exp_sw) * mov
    surf_elo[sw] += k_w * s_delta
    surf_elo[sl] -= k_l * s_delta


def _mov_multiplier(w_games: int, l_games: int) -> float:
    """Bounded margin-of-victory multiplier from the game differential."""

    total = w_games + l_games
    if total <= 0:
        return 1.0
    return 1.0 + 0.4 * math.tanh((w_games - l_games) / 6.0)


def _accumulate_serve(win: _ServeSums, lose: _ServeSums, row: Mapping[str, Any]) -> None:
    w_svpt = _number(row.get("w_svpt"), 0.0)
    l_svpt = _number(row.get("l_svpt"), 0.0)
    if w_svpt <= 0 or l_svpt <= 0:
        return  # match has no serve stats; skip rather than poison the rates
    w_serve_won = _number(row.get("w_1stWon"), 0.0) + _number(row.get("w_2ndWon"), 0.0)
    l_serve_won = _number(row.get("l_1stWon"), 0.0) + _number(row.get("l_2ndWon"), 0.0)
    win.svpt += w_svpt
    win.serve_won += w_serve_won
    win.ace += _number(row.get("w_ace"), 0.0)
    win.df += _number(row.get("w_df"), 0.0)
    win.ret_pts += l_svpt
    win.ret_won += l_svpt - l_serve_won
    win.bp_faced += _number(row.get("w_bpFaced"), 0.0)
    win.bp_saved += _number(row.get("w_bpSaved"), 0.0)
    win.bp_opp += _number(row.get("l_bpFaced"), 0.0)
    win.bp_conv += _number(row.get("l_bpFaced"), 0.0) - _number(row.get("l_bpSaved"), 0.0)
    lose.svpt += l_svpt
    lose.serve_won += l_serve_won
    lose.ace += _number(row.get("l_ace"), 0.0)
    lose.df += _number(row.get("l_df"), 0.0)
    lose.ret_pts += w_svpt
    lose.ret_won += w_svpt - w_serve_won
    lose.bp_faced += _number(row.get("l_bpFaced"), 0.0)
    lose.bp_saved += _number(row.get("l_bpSaved"), 0.0)
    lose.bp_opp += _number(row.get("w_bpFaced"), 0.0)
    lose.bp_conv += _number(row.get("w_bpFaced"), 0.0) - _number(row.get("w_bpSaved"), 0.0)


def _parse_score(score: str, best_of: int) -> tuple[int, int, int, int]:
    if not score:
        return 0, 0, 0, 0
    w_games = l_games = n_sets = 0
    for token in score.split():
        core = token.split("(")[0].strip()
        if "-" not in core:
            continue
        left, right = core.split("-", 1)
        try:
            a, b = int(left), int(right)
        except ValueError:
            continue
        w_games += a
        l_games += b
        n_sets += 1
    long_flag = 1 if ((best_of >= 5 and n_sets >= 4) or (best_of < 5 and n_sets >= 3)) else 0
    return w_games, l_games, n_sets, long_flag


def _fatigue_window(records: deque[tuple[date, int, int]] | None, as_of: date, *, days: int = 14) -> tuple[int, int]:
    if not records:
        return 0, 0
    matches = games = 0
    for record_date, record_games, _long in records:
        if 0 <= (as_of - record_date).days <= days:
            matches += 1
            games += record_games
    return matches, games


def _prev_long(records: deque[tuple[date, int, int]] | None) -> int:
    if not records:
        return 0
    return int(records[-1][2])


def _mean(values: deque[float] | None) -> float:
    if not values:
        return 0.0
    return float(sum(values) / len(values))


def _days_since(previous: date | None, current: date) -> int | None:
    if previous is None:
        return None
    return max((current - previous).days, 0)


# ---------------------------------------------------------------------------
# Training + inference (design tier: monotonic constraints, recency weighting,
# antisymmetric inference).
# ---------------------------------------------------------------------------

# Features that flip sign when the two players are swapped (antisymmetric);
# the rest are match-context and stay fixed. p1_implied_prob is handled
# specially (x → 1 − x).
_ANTISYMMETRIC = {
    name
    for name in TENNIS_V2_FEATURE_NAMES
    if name.endswith("_diff")
    or name in {"rank_log_advantage", "seed_advantage", "hand_matchup"}
}


def train_v2(
    train: pl.DataFrame,
    validation: pl.DataFrame | None = None,
    *,
    label_col: str = "label",
    date_col: str = "match_date",
    params: Mapping[str, Any] | None = None,
    num_boost_round: int = 600,
    early_stopping_rounds: int = 50,
    use_monotone: bool = True,
    recency_half_life_years: float | None = 3.0,
) -> Any:
    """Train the v2 XGBoost classifier with monotone constraints + recency weights."""

    from importlib import import_module

    xgb = import_module("xgboost")
    model_params: dict[str, Any] = {
        "objective": "binary:logistic",
        "eval_metric": ["logloss", "auc"],
        "max_depth": 5,
        "eta": 0.04,
        "subsample": 0.85,
        "colsample_bytree": 0.8,
        "min_child_weight": 5,
        "seed": 42,
    }
    if use_monotone:
        model_params["monotone_constraints"] = "(" + ",".join(str(v) for v in monotone_constraints()) + ")"
    if params:
        model_params.update(dict(params))

    dtrain = xgb.DMatrix(
        train.select(TENNIS_V2_FEATURE_NAMES).to_numpy(),
        label=train[label_col].to_numpy(),
        weight=_recency_weights(train, date_col, recency_half_life_years),
    )
    evals = [(dtrain, "train")]
    if validation is not None and validation.height:
        dval = xgb.DMatrix(
            validation.select(TENNIS_V2_FEATURE_NAMES).to_numpy(),
            label=validation[label_col].to_numpy(),
        )
        evals.append((dval, "validation"))
    return xgb.train(
        model_params,
        dtrain,
        num_boost_round=num_boost_round,
        evals=evals,
        early_stopping_rounds=early_stopping_rounds if len(evals) > 1 else None,
        verbose_eval=False,
    )


def export_v2_onnx(
    model: Any,
    path: str | Path,
    *,
    input_name: str = "features",
    target_opset: int = 15,
) -> Path:
    """Export a v2 XGBoost booster to ONNX with the 34-feature schema pinned."""

    export = _onnx.export_model_onnx(
        model,
        TENNIS_V2_FEATURE_NAMES,
        path,
        model_family=_onnx.ModelFamily.XGBOOST,
        task=_onnx.ModelTask.BINARY_CLASSIFICATION,
        feature_schema_id=FEATURE_SCHEMA_ID,
        feature_schema_version=FEATURE_SCHEMA_VERSION,
        input_name=input_name,
        target_opset=target_opset,
    )
    return export.path


def predict_v2(model: Any, frame: pl.DataFrame) -> tuple[float, ...]:
    from importlib import import_module

    xgb = import_module("xgboost")
    matrix = frame.select(TENNIS_V2_FEATURE_NAMES).to_numpy()
    return tuple(float(v) for v in model.predict(xgb.DMatrix(matrix)))


def predict_v2_onnx_probabilities(model_path: str | Path, frame: pl.DataFrame) -> tuple[float, ...]:
    """Run a v2 ONNX export through ONNX Runtime."""

    features = frame.select(TENNIS_V2_FEATURE_NAMES).to_numpy()
    probabilities = _onnx.predict_onnx(model_path, features, output_select="scalar:1")
    return tuple(float(value) for value in probabilities)


def predict_v2_antisymmetric(model: Any, frame: pl.DataFrame) -> tuple[float, ...]:
    """Coherent two-sided probability: ½·(f(x) + (1 − f(swap(x)))).

    Enforces P(p1) + P(p2) = 1 exactly and halves the model's swap-variance —
    directly relevant to YES/NO edge pricing.
    """

    from importlib import import_module

    import numpy as np

    xgb = import_module("xgboost")
    forward = np.asarray(frame.select(TENNIS_V2_FEATURE_NAMES).to_numpy(), dtype=np.float64)
    swapped = _swap_features(forward)
    p_forward = np.asarray(model.predict(xgb.DMatrix(forward)), dtype=np.float64)
    p_back = np.asarray(model.predict(xgb.DMatrix(swapped)), dtype=np.float64)
    return tuple(float(v) for v in 0.5 * (p_forward + (1.0 - p_back)))


def write_v2_parity_cases(
    frame: pl.DataFrame,
    probabilities: Sequence[float],
    path: str | Path,
    *,
    max_rows: int = 100,
) -> Path:
    """Write v2 ONNX export-parity rows for promotion bundles."""

    if frame.height != len(probabilities):
        raise ValueError("frame and probabilities must contain the same number of rows")
    rows = frame.to_dicts()
    return _parity.write_parity_cases(
        path,
        feature_names=TENNIS_V2_FEATURE_NAMES,
        rows=[[float(row[name]) for name in TENNIS_V2_FEATURE_NAMES] for row in rows],
        expected=[float(value) for value in probabilities],
        schema_id=FEATURE_SCHEMA_ID,
        schema_version=FEATURE_SCHEMA_VERSION,
        case_ids=[str(row["match_id"]) for row in rows],
        labels=[int(row["label"]) for row in rows],
        scalar_field="expected_player_1_win_probability",
        extra={"match_date": [str(row["match_date"]) for row in rows]},
        max_rows=max_rows,
    )


def confidence_gate_metrics(
    y_true: Sequence[int],
    y_probability: Sequence[float],
    *,
    cutoffs: Sequence[float] = (0.55, 0.57, 0.60, 0.62, 0.65, 0.67, 0.70),
) -> list[dict[str, float | int]]:
    """Accuracy/coverage table for abstaining on low-confidence predictions."""

    import numpy as np

    labels = np.asarray([int(value) for value in y_true], dtype=np.int8)
    probabilities = np.asarray([float(value) for value in y_probability], dtype=np.float64)
    if labels.shape != probabilities.shape:
        raise ValueError(f"labels and probabilities differ in shape: {labels.shape} != {probabilities.shape}")
    confidence = np.maximum(probabilities, 1.0 - probabilities)
    predicted = (probabilities >= 0.5).astype(np.int8)
    rows: list[dict[str, float | int]] = []
    for cutoff in cutoffs:
        threshold = float(cutoff)
        mask = confidence >= threshold
        samples = int(mask.sum())
        if samples == 0:
            continue
        rows.append(
            {
                "cutoff": threshold,
                "coverage": float(samples / len(labels)),
                "accuracy": float((predicted[mask] == labels[mask]).mean()),
                "samples": samples,
            }
        )
    return rows


def evaluate_v2_probabilities(
    y_true: Sequence[int],
    y_probability: Sequence[float],
    *,
    threshold: float = 0.5,
) -> _evaluation.ClassificationMetrics:
    return _evaluation.evaluate_classification(
        [int(value) for value in y_true],
        [float(value) for value in y_probability],
        threshold=threshold,
    )


def _swap_features(matrix: Any) -> Any:
    import numpy as np

    out = np.array(matrix, dtype=np.float64, copy=True)
    for col, name in enumerate(TENNIS_V2_FEATURE_NAMES):
        if name in _ANTISYMMETRIC:
            out[:, col] = -out[:, col]
        elif name == "p1_implied_prob":
            out[:, col] = 1.0 - out[:, col]
    return out


def _recency_weights(frame: pl.DataFrame, date_col: str, half_life_years: float | None) -> Any | None:
    if half_life_years is None or date_col not in frame.columns:
        return None
    import numpy as np

    pl = _polars()
    days = frame.select(pl.col(date_col).cast(pl.Date).cast(pl.Int64)).to_numpy().ravel().astype(np.float64)
    age_years = (days.max() - days) / 365.25
    return np.power(0.5, age_years / half_life_years)


def rolling_origin_backtest(
    frame: pl.DataFrame,
    *,
    season_col: str = "match_date",
    first_test_year: int = 2018,
    antisymmetric: bool = True,
    **train_kwargs: Any,
) -> list[dict[str, Any]]:
    """Walk-forward: for each season Y ≥ ``first_test_year``, train on < Y, test on Y.

    Returns one metrics dict per test season — a far more honest estimate than a
    single split, and it surfaces year-over-year decay.
    """

    from eventcontracts.models.evaluation import evaluate_classification

    pl = _polars()
    years = sorted({d.year for d in frame[season_col].to_list()})
    results: list[dict[str, Any]] = []
    for year in [y for y in years if y >= first_test_year]:
        train = frame.filter(pl.col(season_col).dt.year() < year)
        test = frame.filter(pl.col(season_col).dt.year() == year)
        if train.height < 500 or test.height < 100:
            continue
        model = train_v2(train, None, **train_kwargs)
        probs = predict_v2_antisymmetric(model, test) if antisymmetric else predict_v2(model, test)
        metrics = evaluate_classification(test["label"].to_numpy().astype(int), list(probs))
        results.append(
            {
                "season": year,
                "test_rows": test.height,
                "accuracy": metrics.accuracy,
                "roc_auc": metrics.roc_auc,
                "log_loss": metrics.log_loss,
                "brier_score": metrics.brier_score,
                "ece": metrics.expected_calibration_error,
            }
        )
    return results
