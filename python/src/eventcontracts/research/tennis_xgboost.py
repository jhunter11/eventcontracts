"""Tennis XGBoost research helpers.

The model contract here is intentionally plain: every feature is numeric,
ordered, and derived from pre-match state only. That makes the Python research
path easy to mirror in the Rust runner:

1. Maintain the same Elo/H2H/recent-form state maps.
2. Emit features in ``TENNIS_XGBOOST_FEATURE_NAMES`` order.
3. Feed the vector to the promoted model artifact.

The optional training/export functions import XGBoost and ONNX tooling lazily
so the base framework can still run in environments that only need feature
generation and tests.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict, deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import polars as pl


FEATURE_SCHEMA_ID = "tennis_xgboost_match_features"
FEATURE_SCHEMA_VERSION = "1"


@dataclass(frozen=True)
class TennisFeatureSpec:
    name: str
    description: str
    default: float = 0.0


TENNIS_XGBOOST_FEATURES: tuple[TennisFeatureSpec, ...] = (
    TennisFeatureSpec("elo_diff", "Overall Elo difference: player_1 minus player_2."),
    TennisFeatureSpec(
        "surface_elo_diff",
        "Surface-specific Elo difference: player_1 minus player_2.",
    ),
    TennisFeatureSpec(
        "rank_log_advantage",
        "Positive when player_1 has the better ATP rank, using log1p ranks.",
    ),
    TennisFeatureSpec(
        "seed_advantage",
        "Positive when player_1 has the better tournament seed.",
    ),
    TennisFeatureSpec("age_diff", "Player_1 age minus player_2 age in years."),
    TennisFeatureSpec(
        "height_cm_diff",
        "Player_1 listed height minus player_2 listed height in centimeters.",
    ),
    TennisFeatureSpec(
        "h2h_win_pct_diff",
        "Smoothed head-to-head win-rate edge for player_1 before the match.",
    ),
    TennisFeatureSpec(
        "recent_win_pct_diff",
        "Smoothed recent-form win-rate edge over the configured rolling window.",
    ),
    TennisFeatureSpec(
        "recent_match_count_diff",
        "Difference in available recent-form sample counts.",
    ),
    TennisFeatureSpec(
        "days_rest_diff",
        "Player_1 days since prior match minus player_2 days since prior match.",
    ),
    TennisFeatureSpec("best_of_5", "1 when the match is best-of-five, else 0."),
    TennisFeatureSpec("is_grand_slam", "1 when tourney_level is Grand Slam, else 0."),
    TennisFeatureSpec(
        "p1_implied_prob",
        "Bookmaker implied probability for player_1 after overround normalization.",
        default=0.5,
    ),
    TennisFeatureSpec(
        "implied_prob_diff",
        "Normalized implied probability edge: p1_implied_prob - p2_implied_prob.",
    ),
    TennisFeatureSpec("odds_overround", "Raw reciprocal-odds sum before normalization."),
    TennisFeatureSpec("surface_hard", "1 for hard-court matches, else 0."),
    TennisFeatureSpec("surface_clay", "1 for clay-court matches, else 0."),
    TennisFeatureSpec("surface_grass", "1 for grass-court matches, else 0."),
    TennisFeatureSpec("surface_carpet", "1 for carpet matches, else 0."),
    TennisFeatureSpec(
        "surface_unknown",
        "1 when the surface is missing or outside the known surface set.",
    ),
)
TENNIS_XGBOOST_FEATURE_NAMES: tuple[str, ...] = tuple(feature.name for feature in TENNIS_XGBOOST_FEATURES)


@dataclass(frozen=True)
class TennisMatchSnapshot:
    """Point-in-time, pre-match state for one player_1 vs player_2 row."""

    match_id: str
    match_date: date
    p1_id: str
    p2_id: str
    surface: str = "Unknown"
    tourney_level: str = ""
    best_of: int = 3
    p1_elo: float = 1500.0
    p2_elo: float = 1500.0
    p1_surface_elo: float = 1500.0
    p2_surface_elo: float = 1500.0
    p1_rank: int | None = None
    p2_rank: int | None = None
    p1_seed: int | None = None
    p2_seed: int | None = None
    p1_age: float | None = None
    p2_age: float | None = None
    p1_height_cm: float | None = None
    p2_height_cm: float | None = None
    p1_h2h_wins: int = 0
    p2_h2h_wins: int = 0
    p1_recent_wins: int = 0
    p2_recent_wins: int = 0
    p1_recent_matches: int = 0
    p2_recent_matches: int = 0
    p1_days_since_match: int | None = None
    p2_days_since_match: int | None = None
    p1_decimal_odds: float | None = None
    p2_decimal_odds: float | None = None
    label: int | None = None


@dataclass(frozen=True)
class TennisEvaluation:
    accuracy: float
    roc_auc: float
    log_loss: float
    brier_score: float
    samples: int


def feature_schema_document() -> dict[str, Any]:
    """JSON feature schema for ArtifactBundle export and Rust parity tests."""

    return {
        "schema_id": FEATURE_SCHEMA_ID,
        "schema_version": FEATURE_SCHEMA_VERSION,
        "description": (
            "Pre-match ATP tennis features for the XGBoost match-winner model. "
            "All values are numeric and ordered for direct Rust runner parity."
        ),
        "features": [
            {
                "name": feature.name,
                "dtype": "float32",
                "description": feature.description,
                "nullable": False,
                "default": feature.default,
            }
            for feature in TENNIS_XGBOOST_FEATURES
        ],
    }


def write_feature_schema(path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(feature_schema_document(), indent=2) + "\n", encoding="utf-8")
    return target


def feature_row(snapshot: TennisMatchSnapshot) -> dict[str, float]:
    """Return one Rust-portable feature row keyed by schema name."""

    p1_prob, p2_prob, overround = _normalized_implied_probabilities(
        snapshot.p1_decimal_odds,
        snapshot.p2_decimal_odds,
    )
    surface = _surface_key(snapshot.surface)
    h2h_total = snapshot.p1_h2h_wins + snapshot.p2_h2h_wins
    h2h_p1 = (snapshot.p1_h2h_wins + 0.5) / (h2h_total + 1.0)
    p1_recent = _smoothed_win_pct(snapshot.p1_recent_wins, snapshot.p1_recent_matches)
    p2_recent = _smoothed_win_pct(snapshot.p2_recent_wins, snapshot.p2_recent_matches)
    row = {
        "elo_diff": snapshot.p1_elo - snapshot.p2_elo,
        "surface_elo_diff": snapshot.p1_surface_elo - snapshot.p2_surface_elo,
        "rank_log_advantage": _rank_log_advantage(snapshot.p1_rank, snapshot.p2_rank),
        "seed_advantage": _seed_advantage(snapshot.p1_seed, snapshot.p2_seed),
        "age_diff": _diff(snapshot.p1_age, snapshot.p2_age),
        "height_cm_diff": _diff(snapshot.p1_height_cm, snapshot.p2_height_cm),
        "h2h_win_pct_diff": (2.0 * h2h_p1) - 1.0,
        "recent_win_pct_diff": p1_recent - p2_recent,
        "recent_match_count_diff": float(snapshot.p1_recent_matches - snapshot.p2_recent_matches),
        "days_rest_diff": _rest_days(snapshot.p1_days_since_match) - _rest_days(snapshot.p2_days_since_match),
        "best_of_5": 1.0 if snapshot.best_of >= 5 else 0.0,
        "is_grand_slam": 1.0 if snapshot.tourney_level.upper() == "G" else 0.0,
        "p1_implied_prob": p1_prob,
        "implied_prob_diff": p1_prob - p2_prob,
        "odds_overround": overround,
        "surface_hard": 1.0 if surface == "hard" else 0.0,
        "surface_clay": 1.0 if surface == "clay" else 0.0,
        "surface_grass": 1.0 if surface == "grass" else 0.0,
        "surface_carpet": 1.0 if surface == "carpet" else 0.0,
        "surface_unknown": 1.0 if surface == "unknown" else 0.0,
    }
    return {name: float(row[name]) for name in TENNIS_XGBOOST_FEATURE_NAMES}


def feature_vector(snapshot: TennisMatchSnapshot) -> tuple[float, ...]:
    row = feature_row(snapshot)
    return tuple(row[name] for name in TENNIS_XGBOOST_FEATURE_NAMES)


def snapshot_from_mapping(row: Mapping[str, Any]) -> TennisMatchSnapshot:
    """Create a live/deployment snapshot from a plain mapping or CSV row."""

    missing = [
        name for name in ("match_date", "p1_id", "p2_id") if row.get(name) is None or str(row.get(name)).strip() == ""
    ]
    if missing:
        raise ValueError(f"missing required tennis snapshot field(s): {missing}")
    match_id = str(row.get("match_id") or row.get("market_id") or f"{row['p1_id']}-{row['p2_id']}")
    return TennisMatchSnapshot(
        match_id=match_id,
        match_date=_parse_match_date(row["match_date"]),
        p1_id=str(row["p1_id"]),
        p2_id=str(row["p2_id"]),
        surface=str(row.get("surface") or "Unknown"),
        tourney_level=str(row.get("tourney_level") or ""),
        best_of=int(_number(row.get("best_of"), 3)),
        p1_elo=_number(row.get("p1_elo"), 1500.0),
        p2_elo=_number(row.get("p2_elo"), 1500.0),
        p1_surface_elo=_number(row.get("p1_surface_elo"), 1500.0),
        p2_surface_elo=_number(row.get("p2_surface_elo"), 1500.0),
        p1_rank=_optional_int(row.get("p1_rank")),
        p2_rank=_optional_int(row.get("p2_rank")),
        p1_seed=_optional_int(row.get("p1_seed")),
        p2_seed=_optional_int(row.get("p2_seed")),
        p1_age=_optional_float(row.get("p1_age")),
        p2_age=_optional_float(row.get("p2_age")),
        p1_height_cm=_optional_float(row.get("p1_height_cm")),
        p2_height_cm=_optional_float(row.get("p2_height_cm")),
        p1_h2h_wins=int(_number(row.get("p1_h2h_wins"), 0.0)),
        p2_h2h_wins=int(_number(row.get("p2_h2h_wins"), 0.0)),
        p1_recent_wins=int(_number(row.get("p1_recent_wins"), 0.0)),
        p2_recent_wins=int(_number(row.get("p2_recent_wins"), 0.0)),
        p1_recent_matches=int(_number(row.get("p1_recent_matches"), 0.0)),
        p2_recent_matches=int(_number(row.get("p2_recent_matches"), 0.0)),
        p1_days_since_match=_optional_int(row.get("p1_days_since_match")),
        p2_days_since_match=_optional_int(row.get("p2_days_since_match")),
        p1_decimal_odds=_optional_float(row.get("p1_decimal_odds")),
        p2_decimal_odds=_optional_float(row.get("p2_decimal_odds")),
        label=_optional_int(row.get("label")),
    )


def snapshots_to_frame(snapshots: Sequence[TennisMatchSnapshot]) -> pl.DataFrame:
    pl = _polars()
    rows: list[dict[str, Any]] = []
    for snapshot in snapshots:
        row: dict[str, Any] = {
            "match_id": snapshot.match_id,
            "match_date": snapshot.match_date,
            "p1_id": snapshot.p1_id,
            "p2_id": snapshot.p2_id,
            "label": snapshot.label,
        }
        row.update(feature_row(snapshot))
        rows.append(row)
    return pl.DataFrame(rows) if rows else _empty_feature_frame()


def build_sackmann_training_frame(
    matches: pl.DataFrame,
    *,
    include_mirrored: bool = True,
    recent_window: int = 10,
    elo_k: float = 32.0,
) -> pl.DataFrame:
    """Build temporal, pre-match features from Jeff Sackmann-style ATP rows.

    Rows must include ``winner_id``, ``loser_id``, and ``tourney_date``. Optional
    columns such as ``winner_rank``, ``winner_age``, ``AvgW``/``AvgL`` are used
    when present. State is updated only after emitting each match row.
    """

    required = {"winner_id", "loser_id", "tourney_date"}
    missing = required.difference(matches.columns)
    if missing:
        raise ValueError(f"missing required Sackmann columns: {sorted(missing)}")
    if recent_window <= 0:
        raise ValueError("recent_window must be > 0")
    sort_columns = [name for name in ("tourney_date", "tourney_id", "match_num") if name in matches.columns]
    ordered = matches.sort(sort_columns or ["tourney_date"])

    overall_elo: dict[str, float] = defaultdict(lambda: 1500.0)
    surface_elo: dict[tuple[str, str], float] = defaultdict(lambda: 1500.0)
    recent: dict[str, deque[int]] = defaultdict(lambda: deque(maxlen=recent_window))
    last_played: dict[str, date] = {}
    h2h: dict[tuple[str, str], dict[str, int]] = defaultdict(lambda: defaultdict(int))

    snapshots: list[TennisMatchSnapshot] = []
    for index, row in enumerate(ordered.to_dicts()):
        winner = str(row["winner_id"])
        loser = str(row["loser_id"])
        match_date = _parse_match_date(row["tourney_date"])
        surface = str(row.get("surface") or "Unknown")
        match_id = str(row.get("match_id") or row.get("tourney_id") or f"match-{index:08d}")
        h2h_left, h2h_right = sorted((winner, loser))
        h2h_key = (h2h_left, h2h_right)
        wins = h2h[h2h_key]

        win_snapshot = _snapshot_from_row(
            row,
            match_id=f"{match_id}:winner",
            match_date=match_date,
            p1_id=winner,
            p2_id=loser,
            p1_prefix="winner",
            p2_prefix="loser",
            p1_elo=overall_elo[winner],
            p2_elo=overall_elo[loser],
            p1_surface_elo=surface_elo[(winner, _surface_key(surface))],
            p2_surface_elo=surface_elo[(loser, _surface_key(surface))],
            p1_h2h_wins=wins[winner],
            p2_h2h_wins=wins[loser],
            p1_recent=recent[winner],
            p2_recent=recent[loser],
            p1_days_since=_days_since(last_played.get(winner), match_date),
            p2_days_since=_days_since(last_played.get(loser), match_date),
            label=1,
        )
        snapshots.append(win_snapshot)
        if include_mirrored:
            snapshots.append(_mirror_snapshot(win_snapshot))

        overall_elo[winner], overall_elo[loser] = _elo_update(
            overall_elo[winner],
            overall_elo[loser],
            k=elo_k,
        )
        surface_key = _surface_key(surface)
        surface_elo[(winner, surface_key)], surface_elo[(loser, surface_key)] = _elo_update(
            surface_elo[(winner, surface_key)],
            surface_elo[(loser, surface_key)],
            k=elo_k,
        )
        recent[winner].append(1)
        recent[loser].append(0)
        wins[winner] += 1
        last_played[winner] = match_date
        last_played[loser] = match_date

    return snapshots_to_frame(snapshots)


def temporal_train_validation_test_split(
    frame: pl.DataFrame,
    *,
    train_fraction: float = 0.70,
    validation_fraction: float = 0.15,
) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    """Split chronologically without putting one match date in two partitions.

    Tennis training frames contain mirrored rows for each match. Splitting
    those rows by position can accidentally place the original and mirrored
    observations on opposite sides of a boundary. When ``match_date`` is
    present, whole dates are assigned together so no same-day match state can
    leak across train, validation, or test partitions.
    """

    if not 0 < train_fraction < 1:
        raise ValueError("train_fraction must be in (0, 1)")
    if not 0 <= validation_fraction < 1:
        raise ValueError("validation_fraction must be in [0, 1)")
    if train_fraction + validation_fraction >= 1:
        raise ValueError("train + validation fractions must leave test rows")
    if "match_date" in frame.columns:
        pl = _polars()
        date_groups = frame.group_by("match_date").len().sort("match_date").rows()
        if len(date_groups) >= 3:
            cumulative: list[int] = []
            total = 0
            for _match_date, row_count in date_groups:
                total += int(row_count)
                cumulative.append(total)
            train_group_count = min(
                range(1, len(date_groups) - 1),
                key=lambda end: abs(cumulative[end - 1] - frame.height * train_fraction),
            )
            validation_target = frame.height * (train_fraction + validation_fraction)
            validation_group_count = min(
                range(train_group_count + 1, len(date_groups)),
                key=lambda end: abs(cumulative[end - 1] - validation_target),
            )
            train_end_date = date_groups[train_group_count - 1][0]
            validation_end_date = date_groups[validation_group_count - 1][0]
            return (
                frame.filter(pl.col("match_date") <= train_end_date),
                frame.filter((pl.col("match_date") > train_end_date) & (pl.col("match_date") <= validation_end_date)),
                frame.filter(pl.col("match_date") > validation_end_date),
            )
    n_rows = frame.height
    train_end = int(n_rows * train_fraction)
    validation_end = train_end + int(n_rows * validation_fraction)
    return (
        frame.slice(0, train_end),
        frame.slice(train_end, validation_end - train_end),
        frame.slice(validation_end),
    )


def train_xgboost_binary(
    train: pl.DataFrame,
    validation: pl.DataFrame | None = None,
    *,
    label_col: str = "label",
    params: Mapping[str, Any] | None = None,
    num_boost_round: int = 500,
    early_stopping_rounds: int = 50,
) -> Any:
    """Train an XGBoost binary classifier from the stable feature columns."""

    try:
        xgb = import_module("xgboost")
    except ImportError as exc:  # pragma: no cover - optional research dependency
        raise RuntimeError("xgboost is required for train_xgboost_binary; install it in the research env.") from exc

    model_params = {
        "objective": "binary:logistic",
        "eval_metric": ["logloss", "auc"],
        "max_depth": 4,
        "eta": 0.05,
        "subsample": 0.85,
        "colsample_bytree": 0.85,
        "seed": 42,
    }
    if params:
        model_params.update(dict(params))
    dtrain = xgb.DMatrix(
        train.select(TENNIS_XGBOOST_FEATURE_NAMES).to_numpy(),
        label=train[label_col].to_numpy(),
    )
    evals = [(dtrain, "train")]
    if validation is not None and validation.height:
        dval = xgb.DMatrix(
            validation.select(TENNIS_XGBOOST_FEATURE_NAMES).to_numpy(),
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


def predict_xgboost_probabilities(model: Any, frame: pl.DataFrame) -> tuple[float, ...]:
    """Predict player-1 win probabilities using the fixed feature order."""

    try:
        xgb = import_module("xgboost")
    except ImportError as exc:  # pragma: no cover - optional research dependency
        raise RuntimeError(
            "xgboost is required for predict_xgboost_probabilities; install it in the research env."
        ) from exc
    matrix = xgb.DMatrix(frame.select(TENNIS_XGBOOST_FEATURE_NAMES).to_numpy())
    return tuple(float(value) for value in model.predict(matrix))


def export_xgboost_onnx(
    model: Any,
    path: str | Path,
    *,
    input_name: str = "features",
    target_opset: int = 15,
) -> Path:
    """Export a trained XGBoost booster to ONNX for Rust live inference.

    The ONNX graph input is a ``[N, len(TENNIS_XGBOOST_FEATURE_NAMES)]`` float
    tensor. The feature names are embedded as ONNX metadata, but the promoted
    ArtifactBundle should still include ``feature_schema.json`` as the source of
    truth for Rust parity checks.
    """

    try:
        onnxmltools = import_module("onnxmltools")
        data_types = import_module("onnxmltools.convert.common.data_types")
        FloatTensorType = data_types.FloatTensorType
    except ImportError as exc:  # pragma: no cover - optional research dependency
        raise RuntimeError("onnxmltools is required for export_xgboost_onnx; install it in the research env.") from exc

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    onnx_model = onnxmltools.convert_xgboost(
        model,
        initial_types=[(input_name, FloatTensorType([None, len(TENNIS_XGBOOST_FEATURE_NAMES)]))],
        target_opset=target_opset,
    )
    _set_onnx_metadata(
        onnx_model,
        {
            "eventcontracts.feature_schema_id": FEATURE_SCHEMA_ID,
            "eventcontracts.feature_schema_version": FEATURE_SCHEMA_VERSION,
            "eventcontracts.feature_names_json": json.dumps(TENNIS_XGBOOST_FEATURE_NAMES),
            "eventcontracts.model_family": "xgboost_binary_onnx",
            "eventcontracts.input_name": input_name,
        },
    )
    onnxmltools.utils.save_model(onnx_model, str(target))
    return target


def predict_onnx_probabilities(model_path: str | Path, frame: pl.DataFrame) -> tuple[float, ...]:
    """Run the exported artifact through ONNX Runtime for export parity checks."""

    try:
        np = import_module("numpy")
        ort = import_module("onnxruntime")
    except ImportError as exc:  # pragma: no cover - optional deployment dependency
        raise RuntimeError(
            "onnxruntime is required for predict_onnx_probabilities; install it in the research env."
        ) from exc

    session = ort.InferenceSession(str(model_path), providers=["CPUExecutionProvider"])
    inputs = np.asarray(
        frame.select(TENNIS_XGBOOST_FEATURE_NAMES).to_numpy(),
        dtype=np.float32,
    )
    outputs = session.run(None, {"features": inputs})
    probabilities = outputs[1]
    if hasattr(probabilities, "shape") and len(probabilities.shape) == 2:
        return tuple(float(value) for value in probabilities[:, 1])
    if isinstance(probabilities, list):
        return tuple(float(row[1]) for row in probabilities)
    raise ValueError("ONNX model did not return binary probability output")


def write_parity_cases(
    frame: pl.DataFrame,
    probabilities: Sequence[float],
    path: str | Path,
    *,
    max_rows: int = 100,
) -> Path:
    """Write inference parity rows consumed during Rust model promotion."""

    if frame.height != len(probabilities):
        raise ValueError("frame and probabilities must contain the same number of rows")
    if max_rows <= 0:
        raise ValueError("max_rows must be > 0")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    documents: list[str] = []
    for row, probability in zip(frame.head(max_rows).to_dicts(), probabilities, strict=False):
        documents.append(
            json.dumps(
                {
                    "case_id": str(row["match_id"]),
                    "match_date": str(row["match_date"]),
                    "feature_schema_id": FEATURE_SCHEMA_ID,
                    "feature_schema_version": FEATURE_SCHEMA_VERSION,
                    "features": [float(row[name]) for name in TENNIS_XGBOOST_FEATURE_NAMES],
                    "expected_player_1_win_probability": float(probability),
                    "label": int(row["label"]),
                },
                separators=(",", ":"),
            )
        )
    target.write_text("\n".join(documents) + ("\n" if documents else ""), encoding="utf-8")
    return target


def onnx_deployment_metadata(*, model_path: str = "model.onnx") -> dict[str, Any]:
    """Manifest snippet for a tennis XGBoost ONNX artifact."""

    return {
        "model": {
            "format": "onnx",
            "path": model_path,
            "input_name": "features",
            "feature_schema_id": FEATURE_SCHEMA_ID,
            "feature_schema_version": FEATURE_SCHEMA_VERSION,
            "feature_names": list(TENNIS_XGBOOST_FEATURE_NAMES),
            "output": "player_1_win_probability",
        },
        "rust_runner_contract": {
            "feature_order": list(TENNIS_XGBOOST_FEATURE_NAMES),
            "input_tensor": [None, len(TENNIS_XGBOOST_FEATURE_NAMES)],
            "dtype": "float32",
        },
    }


def evaluate_probabilities(
    y_true: Sequence[int],
    y_probability: Sequence[float],
    *,
    threshold: float = 0.5,
) -> TennisEvaluation:
    labels = [int(value) for value in y_true]
    probabilities = [float(value) for value in y_probability]
    if len(labels) != len(probabilities):
        raise ValueError("y_true and y_probability must have the same shape")
    if not labels:
        raise ValueError("at least one sample is required")
    clipped = [min(max(probability, 1e-15), 1.0 - 1e-15) for probability in probabilities]
    predictions = [1 if probability >= threshold else 0 for probability in probabilities]
    accuracy = sum(int(prediction == label) for prediction, label in zip(predictions, labels, strict=True)) / len(
        labels
    )
    log_loss = -sum(
        label * math.log(probability) + (1 - label) * math.log(1.0 - probability)
        for label, probability in zip(labels, clipped, strict=True)
    ) / len(labels)
    brier = sum((probability - label) ** 2 for label, probability in zip(labels, probabilities, strict=True)) / len(
        labels
    )
    return TennisEvaluation(
        accuracy=float(accuracy),
        roc_auc=_roc_auc(labels, probabilities),
        log_loss=float(log_loss),
        brier_score=float(brier),
        samples=len(labels),
    )


def _snapshot_from_row(
    row: Mapping[str, Any],
    *,
    match_id: str,
    match_date: date,
    p1_id: str,
    p2_id: str,
    p1_prefix: str,
    p2_prefix: str,
    p1_elo: float,
    p2_elo: float,
    p1_surface_elo: float,
    p2_surface_elo: float,
    p1_h2h_wins: int,
    p2_h2h_wins: int,
    p1_recent: deque[int],
    p2_recent: deque[int],
    p1_days_since: int | None,
    p2_days_since: int | None,
    label: int,
) -> TennisMatchSnapshot:
    p1_odds, p2_odds = _odds_from_row(row, p1_prefix=p1_prefix, p2_prefix=p2_prefix)
    return TennisMatchSnapshot(
        match_id=match_id,
        match_date=match_date,
        p1_id=p1_id,
        p2_id=p2_id,
        surface=str(row.get("surface") or "Unknown"),
        tourney_level=str(row.get("tourney_level") or ""),
        best_of=int(_number(row.get("best_of"), 3)),
        p1_elo=p1_elo,
        p2_elo=p2_elo,
        p1_surface_elo=p1_surface_elo,
        p2_surface_elo=p2_surface_elo,
        p1_rank=_optional_int(row.get(f"{p1_prefix}_rank")),
        p2_rank=_optional_int(row.get(f"{p2_prefix}_rank")),
        p1_seed=_optional_int(row.get(f"{p1_prefix}_seed")),
        p2_seed=_optional_int(row.get(f"{p2_prefix}_seed")),
        p1_age=_optional_float(row.get(f"{p1_prefix}_age")),
        p2_age=_optional_float(row.get(f"{p2_prefix}_age")),
        p1_height_cm=_optional_float(row.get(f"{p1_prefix}_ht")),
        p2_height_cm=_optional_float(row.get(f"{p2_prefix}_ht")),
        p1_h2h_wins=p1_h2h_wins,
        p2_h2h_wins=p2_h2h_wins,
        p1_recent_wins=sum(p1_recent),
        p2_recent_wins=sum(p2_recent),
        p1_recent_matches=len(p1_recent),
        p2_recent_matches=len(p2_recent),
        p1_days_since_match=p1_days_since,
        p2_days_since_match=p2_days_since,
        p1_decimal_odds=p1_odds,
        p2_decimal_odds=p2_odds,
        label=label,
    )


def _mirror_snapshot(snapshot: TennisMatchSnapshot) -> TennisMatchSnapshot:
    return TennisMatchSnapshot(
        match_id=f"{snapshot.match_id}:mirror",
        match_date=snapshot.match_date,
        p1_id=snapshot.p2_id,
        p2_id=snapshot.p1_id,
        surface=snapshot.surface,
        tourney_level=snapshot.tourney_level,
        best_of=snapshot.best_of,
        p1_elo=snapshot.p2_elo,
        p2_elo=snapshot.p1_elo,
        p1_surface_elo=snapshot.p2_surface_elo,
        p2_surface_elo=snapshot.p1_surface_elo,
        p1_rank=snapshot.p2_rank,
        p2_rank=snapshot.p1_rank,
        p1_seed=snapshot.p2_seed,
        p2_seed=snapshot.p1_seed,
        p1_age=snapshot.p2_age,
        p2_age=snapshot.p1_age,
        p1_height_cm=snapshot.p2_height_cm,
        p2_height_cm=snapshot.p1_height_cm,
        p1_h2h_wins=snapshot.p2_h2h_wins,
        p2_h2h_wins=snapshot.p1_h2h_wins,
        p1_recent_wins=snapshot.p2_recent_wins,
        p2_recent_wins=snapshot.p1_recent_wins,
        p1_recent_matches=snapshot.p2_recent_matches,
        p2_recent_matches=snapshot.p1_recent_matches,
        p1_days_since_match=snapshot.p2_days_since_match,
        p2_days_since_match=snapshot.p1_days_since_match,
        p1_decimal_odds=snapshot.p2_decimal_odds,
        p2_decimal_odds=snapshot.p1_decimal_odds,
        label=0 if snapshot.label == 1 else 1 if snapshot.label == 0 else None,
    )


def _empty_feature_frame() -> pl.DataFrame:
    pl = _polars()
    schema: dict[str, type[Any]] = {
        "match_id": str,
        "match_date": date,
        "p1_id": str,
        "p2_id": str,
        "label": int,
    }
    schema.update({name: float for name in TENNIS_XGBOOST_FEATURE_NAMES})
    return pl.DataFrame(schema=schema)


def _elo_update(winner_elo: float, loser_elo: float, *, k: float) -> tuple[float, float]:
    expected_winner = 1.0 / (1.0 + 10.0 ** ((loser_elo - winner_elo) / 400.0))
    delta = k * (1.0 - expected_winner)
    return winner_elo + delta, loser_elo - delta


def _parse_match_date(value: object) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    raw = str(value)
    if len(raw) == 8 and raw.isdigit():
        return datetime.strptime(raw, "%Y%m%d").date()
    return datetime.fromisoformat(raw).date()


def _days_since(previous: date | None, current: date) -> int | None:
    if previous is None:
        return None
    return max((current - previous).days, 0)


def _surface_key(surface: str) -> str:
    value = surface.strip().lower()
    if value in {"hard", "clay", "grass", "carpet"}:
        return value
    return "unknown"


def _diff(left: float | None, right: float | None) -> float:
    return _number(left, 0.0) - _number(right, 0.0)


def _rank_log_advantage(p1_rank: int | None, p2_rank: int | None) -> float:
    default_rank = 2500
    return math.log1p(p2_rank or default_rank) - math.log1p(p1_rank or default_rank)


def _seed_advantage(p1_seed: int | None, p2_seed: int | None) -> float:
    default_seed = 64
    return float((p2_seed or default_seed) - (p1_seed or default_seed))


def _smoothed_win_pct(wins: int, matches: int) -> float:
    return (wins + 2.5) / (matches + 5.0)


def _rest_days(value: int | None) -> float:
    if value is None:
        return 0.0
    return float(max(min(value, 30), 0))


def _normalized_implied_probabilities(
    p1_decimal_odds: float | None,
    p2_decimal_odds: float | None,
) -> tuple[float, float, float]:
    p1_raw = 1.0 / p1_decimal_odds if p1_decimal_odds and p1_decimal_odds > 1.0 else 0.0
    p2_raw = 1.0 / p2_decimal_odds if p2_decimal_odds and p2_decimal_odds > 1.0 else 0.0
    overround = p1_raw + p2_raw
    if overround <= 0.0:
        return 0.5, 0.5, 0.0
    return p1_raw / overround, p2_raw / overround, overround


def _odds_from_row(row: Mapping[str, Any], *, p1_prefix: str, p2_prefix: str) -> tuple[float | None, float | None]:
    suffix_by_prefix = {"winner": "W", "loser": "L"}
    p1_suffix = suffix_by_prefix.get(p1_prefix)
    p2_suffix = suffix_by_prefix.get(p2_prefix)
    p1 = _first_number(row, (f"{p1_prefix}_decimal_odds", f"{p1_prefix}_odds"))
    p2 = _first_number(row, (f"{p2_prefix}_decimal_odds", f"{p2_prefix}_odds"))
    if p1 is None and p1_suffix is not None:
        p1 = _first_number(row, (f"Avg{p1_suffix}", f"B365{p1_suffix}", f"PS{p1_suffix}"))
    if p2 is None and p2_suffix is not None:
        p2 = _first_number(row, (f"Avg{p2_suffix}", f"B365{p2_suffix}", f"PS{p2_suffix}"))
    return p1, p2


def _first_number(row: Mapping[str, Any], names: Sequence[str]) -> float | None:
    for name in names:
        value = _optional_float(row.get(name))
        if value is not None:
            return value
    return None


def _optional_int(value: object) -> int | None:
    number = _optional_float(value)
    return None if number is None else int(number)


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    if isinstance(value, str) and not value.strip():
        return None
    if isinstance(value, int | float | str):
        return float(value)
    return float(str(value))


def _number(value: object, default: float) -> float:
    parsed = _optional_float(value)
    return default if parsed is None else parsed


def _roc_auc(labels: Sequence[int], probabilities: Sequence[float]) -> float:
    n_pos = sum(1 for label in labels if label == 1)
    n_neg = sum(1 for label in labels if label == 0)
    if n_pos == 0 or n_neg == 0:
        return math.nan
    ranked = sorted(enumerate(probabilities), key=lambda item: item[1])
    ranks = [0.0] * len(probabilities)
    cursor = 0
    while cursor < len(ranked):
        end = cursor + 1
        while end < len(ranked) and ranked[end][1] == ranked[cursor][1]:
            end += 1
        avg_rank = (cursor + 1 + end) / 2.0
        for original_index, _probability in ranked[cursor:end]:
            ranks[original_index] = avg_rank
        cursor = end
    pos_rank_sum = sum(rank for rank, label in zip(ranks, labels, strict=True) if label == 1)
    return (pos_rank_sum - (n_pos * (n_pos + 1) / 2.0)) / (n_pos * n_neg)


def _polars() -> Any:
    try:
        return import_module("polars")
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise RuntimeError("polars is required for tennis dataframe helpers; install requirements.txt.") from exc


def _set_onnx_metadata(model: Any, values: Mapping[str, str]) -> None:
    del model.metadata_props[:]
    for key, value in values.items():
        prop = model.metadata_props.add()
        prop.key = key
        prop.value = value
