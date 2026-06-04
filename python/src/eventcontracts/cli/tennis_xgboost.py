"""Train and bundle the pre-match tennis XGBoost model as ONNX."""

from __future__ import annotations

import argparse
import json
import re
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from eventcontracts.artifacts import ArtifactBundleValidator, ArtifactBundleWriter
from eventcontracts.models import evaluation as _evaluation
from eventcontracts.research import tennis_v2
from eventcontracts.research.tennis_odds import load_tennis_data_odds, merge_odds_into_matches, odds_match_rate
from eventcontracts.research.tennis_xgboost import (
    TennisEvaluation,
    build_sackmann_training_frame,
    evaluate_probabilities,
    export_xgboost_onnx,
    predict_onnx_probabilities,
    predict_xgboost_probabilities,
    snapshot_from_mapping,
    snapshots_to_frame,
    temporal_train_validation_test_split,
    train_xgboost_binary,
    write_feature_schema,
    write_parity_cases,
)

REPO_ROOT = Path(__file__).resolve().parents[4]
MAIN_MATCH_PATTERN = re.compile(r"atp_matches_\d{4}\.csv$")
CHALLENGER_MATCH_PATTERN = re.compile(r"atp_matches_qual_chall_\d{4}\.csv$")


def register(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "tennis-xgboost-train",
        help="Train the ATP pre-match XGBoost model and export an ONNX paper bundle.",
    )
    parser.add_argument("--data-dir", type=Path, required=True, help="Directory containing Sackmann ATP CSV files.")
    parser.add_argument("--out-root", type=Path, default=REPO_ROOT / "artifacts" / "tennis_xgboost")
    parser.add_argument(
        "--strategy-spec",
        type=Path,
        default=REPO_ROOT / "configs" / "strategies" / "sports-tennis-xgboost.toml",
    )
    parser.add_argument(
        "--sleeve-spec",
        type=Path,
        default=REPO_ROOT / "configs" / "sleeves" / "sports-tennis-kalshi-paper-a.toml",
    )
    parser.add_argument(
        "--bundle-id", default=None, help="Immutable bundle ID; defaults to a timestamped paper candidate."
    )
    parser.add_argument("--since-year", type=int, default=2000)
    parser.add_argument("--through-year", type=int, default=None)
    parser.add_argument("--include-challengers", action="store_true")
    parser.add_argument("--max-matches", type=int, default=None, help="Limit sorted matches for smoke experiments.")
    parser.add_argument(
        "--model-version",
        choices=("v1", "v2"),
        default="v1",
        help=(
            "Feature/model contract to train. v1 preserves the 20-feature production path; "
            "v2 uses the 34-feature research path."
        ),
    )
    parser.add_argument(
        "--odds-dir",
        type=Path,
        default=None,
        help="Optional directory of tennis-data.co.uk ATP odds .xlsx files to merge before feature generation.",
    )
    parser.add_argument(
        "--train-since-year",
        type=int,
        default=None,
        help="After the temporal split, drop training rows before this year for recency-window experiments.",
    )
    parser.add_argument(
        "--recent-window",
        type=int,
        default=None,
        help="Rolling form window. Defaults to 10 for v1 and 14 for v2.",
    )
    parser.add_argument("--num-boost-round", type=int, default=500)
    parser.add_argument("--early-stopping-rounds", type=int, default=50)
    parser.add_argument(
        "--disable-monotone",
        action="store_true",
        help="For v2 only, train without monotone constraints. Recent diagnostics favored this on OOS accuracy.",
    )
    parser.add_argument(
        "--recency-half-life-years",
        type=float,
        default=3.0,
        help="For v2 only, exponential sample-weight half-life. Use <=0 to disable recency weights.",
    )
    parser.add_argument(
        "--confidence-cutoffs",
        default="0.55,0.57,0.60,0.62,0.65,0.67,0.70",
        help="Comma-separated probability-confidence cutoffs included in the JSON report.",
    )
    parser.add_argument("--parity-rows", type=int, default=100)
    parser.set_defaults(handler=_handle)

    score_parser = subparsers.add_parser(
        "tennis-xgboost-score",
        help="Score manually curated upcoming tennis matches with a promoted ONNX model.",
    )
    score_parser.add_argument("--model", type=Path, required=True, help="Path to model.onnx.")
    score_parser.add_argument(
        "--input",
        type=Path,
        required=True,
        help="CSV of upcoming match snapshots. Requires market_id, match_date, p1_id, p2_id.",
    )
    score_parser.add_argument("--out", type=Path, default=None, help="Optional JSONL output path.")
    score_parser.add_argument("--source", default="tennis_xgboost_onnx")
    score_parser.add_argument("--schema-version", default="tennis-prediction-v1")
    score_parser.set_defaults(handler=_handle_score)


def _handle(args: argparse.Namespace) -> int:
    pl = _polars()
    try:
        confidence_cutoffs = _confidence_cutoffs(args.confidence_cutoffs)
    except ValueError as exc:
        print(f"tennis-xgboost-train: {exc}")
        return 2
    paths = _match_paths(
        args.data_dir,
        include_challengers=args.include_challengers,
        since_year=args.since_year,
        through_year=args.through_year,
    )
    if not paths:
        print(f"tennis-xgboost-train: no ATP match files found under {args.data_dir}")
        return 2
    matches = _load_matches(
        pl,
        paths,
        since_year=args.since_year,
        through_year=args.through_year,
        max_matches=args.max_matches,
    )
    if matches.height < 20:
        print("tennis-xgboost-train: need at least 20 valid historical matches after filters")
        return 2
    odds_report: dict[str, Any] | None = None
    if args.odds_dir is not None:
        odds = load_tennis_data_odds(args.odds_dir)
        matches = merge_odds_into_matches(matches, odds)
        odds_report = {
            "source": str(args.odds_dir),
            "odds_rows": odds.height,
            "match_rate": odds_match_rate(matches),
        }

    recent_window = args.recent_window if args.recent_window is not None else (14 if args.model_version == "v2" else 10)
    if args.model_version == "v2":
        frame = tennis_v2.build_v2_training_frame(
            matches,
            include_mirrored=True,
            recent_window=recent_window,
        )
    else:
        frame = build_sackmann_training_frame(
            matches,
            include_mirrored=True,
            recent_window=recent_window,
        )
    train, validation, test = temporal_train_validation_test_split(frame)
    if not train.height or not validation.height or not test.height:
        print("tennis-xgboost-train: temporal split produced an empty partition")
        return 2
    train_rows_before_window = train.height
    if args.train_since_year is not None:
        train = train.filter(pl.col("match_date") >= date(args.train_since_year, 1, 1))
        if not train.height:
            print("tennis-xgboost-train: train-since-year removed all training rows")
            return 2

    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    bundle_id = args.bundle_id or f"sports_tennis_xgboost/paper-candidate-{timestamp}"
    staging = args.out_root / "staging" / _safe_dir(bundle_id)
    staging.mkdir(parents=True, exist_ok=True)

    # v1 emits a tennis-specific TennisEvaluation, v2 the generic
    # ClassificationMetrics; both feed _metrics_payload below. Annotate the union
    # so mypy accepts the per-branch assignment.
    metrics: _evaluation.ClassificationMetrics | TennisEvaluation
    if args.model_version == "v2":
        recency_half_life = args.recency_half_life_years if args.recency_half_life_years > 0 else None
        model = tennis_v2.train_v2(
            train,
            validation,
            num_boost_round=args.num_boost_round,
            early_stopping_rounds=args.early_stopping_rounds,
            use_monotone=not args.disable_monotone,
            recency_half_life_years=recency_half_life,
        )
        model_path = tennis_v2.export_v2_onnx(model, staging / "model.onnx")
        schema_path = tennis_v2.write_feature_schema(staging / "feature_schema.json")
        booster_probability = tennis_v2.predict_v2(model, test)
        onnx_probability = tennis_v2.predict_v2_onnx_probabilities(model_path, test)
        parity_path = tennis_v2.write_v2_parity_cases(
            test,
            onnx_probability,
            staging / "parity_cases.jsonl",
            max_rows=args.parity_rows,
        )
        metrics = tennis_v2.evaluate_v2_probabilities(test["label"].to_list(), onnx_probability)
        confidence_gates = tennis_v2.confidence_gate_metrics(
            test["label"].to_list(),
            onnx_probability,
            cutoffs=confidence_cutoffs,
        )
        antisymmetric_probability = tennis_v2.predict_v2_antisymmetric(model, test)
        antisymmetric_metrics = tennis_v2.evaluate_v2_probabilities(
            test["label"].to_list(),
            antisymmetric_probability,
        )
    else:
        model = train_xgboost_binary(
            train,
            validation,
            num_boost_round=args.num_boost_round,
            early_stopping_rounds=args.early_stopping_rounds,
        )
        model_path = export_xgboost_onnx(model, staging / "model.onnx")
        schema_path = write_feature_schema(staging / "feature_schema.json")
        booster_probability = predict_xgboost_probabilities(model, test)
        onnx_probability = predict_onnx_probabilities(model_path, test)
        parity_path = write_parity_cases(
            test,
            onnx_probability,
            staging / "parity_cases.jsonl",
            max_rows=args.parity_rows,
        )
        metrics = evaluate_probabilities(test["label"].to_list(), onnx_probability)
        confidence_gates = _confidence_gate_metrics(
            test["label"].to_list(),
            onnx_probability,
            cutoffs=confidence_cutoffs,
        )
        antisymmetric_metrics = None

    maximum_export_delta = max(
        abs(left - right) for left, right in zip(booster_probability, onnx_probability, strict=True)
    )
    if maximum_export_delta > 1e-6:
        raise RuntimeError(f"ONNX export parity exceeded tolerance: {maximum_export_delta}")

    bundle = ArtifactBundleWriter(args.out_root / "bundles").write_from_files(
        strategy_spec_path=args.strategy_spec,
        sleeve_spec_path=args.sleeve_spec,
        feature_schema_path=schema_path,
        model_path=model_path,
        parity_cases_path=parity_path,
        bundle_id=bundle_id,
        created_by="eventcontracts.cli.tennis_xgboost",
    )
    ArtifactBundleValidator().validate(bundle)
    report = {
        "bundle_id": bundle.bundle_id,
        "bundle_root": bundle.root_path,
        "manifest": bundle.manifest_path,
        "model": str(Path(bundle.root_path) / "model" / "model.onnx"),
        "model_version": args.model_version,
        "matches": matches.height,
        "feature_rows": frame.height,
        "recent_window": recent_window,
        "train_rows_before_window": train_rows_before_window,
        "train_since_year": args.train_since_year,
        "train_rows": train.height,
        "validation_rows": validation.height,
        "test_rows": test.height,
        "parity_rows": bundle.parity.expected_rows if bundle.parity is not None else 0,
        "onnx_max_abs_probability_delta": maximum_export_delta,
        "metrics": _metrics_payload(metrics),
        "confidence_gates": confidence_gates,
        "data_files": [str(path) for path in paths],
    }
    if odds_report is not None:
        report["odds"] = odds_report
    if antisymmetric_metrics is not None:
        report["antisymmetric_native_metrics"] = _metrics_payload(antisymmetric_metrics)
    report_path = args.out_root / "reports" / f"{_safe_dir(bundle_id)}.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    report["report"] = str(report_path)
    print(json.dumps(report, indent=2))
    return 0


def _metrics_payload(metrics: Any) -> dict[str, float | int]:
    payload: dict[str, float | int] = {
        "accuracy": float(metrics.accuracy),
        "roc_auc": float(metrics.roc_auc),
        "log_loss": float(metrics.log_loss),
        "brier_score": float(metrics.brier_score),
        "samples": int(metrics.samples),
    }
    ece = getattr(metrics, "expected_calibration_error", None)
    if ece is not None:
        payload["expected_calibration_error"] = float(ece)
    return payload


def _confidence_cutoffs(value: str) -> tuple[float, ...]:
    cutoffs: list[float] = []
    for raw in value.split(","):
        stripped = raw.strip()
        if not stripped:
            continue
        cutoff = float(stripped)
        if not 0.5 <= cutoff < 1.0:
            raise ValueError("confidence cutoffs must be in [0.5, 1.0)")
        cutoffs.append(cutoff)
    if not cutoffs:
        raise ValueError("confidence cutoffs must contain at least one value")
    return tuple(cutoffs)


def _confidence_gate_metrics(
    y_true: list[int],
    y_probability: tuple[float, ...],
    *,
    cutoffs: tuple[float, ...],
) -> list[dict[str, float | int]]:
    metrics = _evaluation.evaluate_classification
    rows: list[dict[str, float | int]] = []
    probabilities = [float(value) for value in y_probability]
    labels = [int(value) for value in y_true]
    for cutoff in cutoffs:
        kept = [
            (label, probability)
            for label, probability in zip(labels, probabilities, strict=True)
            if max(probability, 1.0 - probability) >= cutoff
        ]
        if not kept:
            continue
        kept_labels = [label for label, _probability in kept]
        kept_probabilities = [probability for _label, probability in kept]
        evaluated = metrics(kept_labels, kept_probabilities)
        rows.append(
            {
                "cutoff": float(cutoff),
                "coverage": float(len(kept) / len(labels)),
                "accuracy": evaluated.accuracy,
                "samples": len(kept),
            }
        )
    return rows


def _handle_score(args: argparse.Namespace) -> int:
    pl = _polars()
    rows = pl.read_csv(args.input, infer_schema_length=10000).to_dicts()
    if not rows:
        print("tennis-xgboost-score: input CSV contains no rows")
        return 2
    missing_market = [index for index, row in enumerate(rows) if not str(row.get("market_id") or "").strip()]
    if missing_market:
        print(f"tennis-xgboost-score: rows missing market_id: {missing_market[:10]}")
        return 2
    snapshots = [snapshot_from_mapping(row) for row in rows]
    frame = snapshots_to_frame(snapshots)
    probabilities = predict_onnx_probabilities(args.model, frame)
    now = datetime.now(UTC).isoformat()
    lines: list[str] = []
    for row, snapshot, probability in zip(rows, snapshots, probabilities, strict=True):
        confidence = max(float(probability), 1.0 - float(probability))
        odds_present = bool(snapshot.p1_decimal_odds and snapshot.p2_decimal_odds)
        payload = {
            "market_id": str(row["market_id"]),
            "match_id": snapshot.match_id,
            "match_date": snapshot.match_date.isoformat(),
            "player_1_id": snapshot.p1_id,
            "player_2_id": snapshot.p2_id,
            "player_1_name": row.get("p1_name"),
            "player_2_name": row.get("p2_name"),
            "player_1_win_probability": probability,
            "model_confidence": confidence,
            "odds_present": odds_present,
            "feature_schema_id": "tennis_xgboost_match_features",
            "feature_schema_version": "1",
        }
        lines.append(
            json.dumps(
                {
                    "event_kind": "external",
                    "source": args.source,
                    "schema_version": args.schema_version,
                    "received_at": now,
                    "payload": payload,
                },
                sort_keys=True,
                default=str,
            )
        )
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "model": str(args.model),
                "input": str(args.input),
                "out": str(args.out) if args.out is not None else None,
                "rows": len(lines),
                "min_probability": min(probabilities),
                "max_probability": max(probabilities),
            },
            indent=2,
        )
    )
    return 0


def _match_paths(
    data_dir: Path,
    *,
    include_challengers: bool,
    since_year: int,
    through_year: int | None,
) -> list[Path]:
    paths: list[Path] = []
    for path in sorted(data_dir.glob("*.csv")):
        if not (
            MAIN_MATCH_PATTERN.fullmatch(path.name)
            or (include_challengers and CHALLENGER_MATCH_PATTERN.fullmatch(path.name))
        ):
            continue
        year = int(path.stem[-4:])
        if year < since_year or (through_year is not None and year > through_year):
            continue
        paths.append(path)
    return paths


def _load_matches(
    pl: Any,
    paths: list[Path],
    *,
    since_year: int,
    through_year: int | None,
    max_matches: int | None,
) -> Any:
    frames = [pl.read_csv(path, infer_schema_length=10000) for path in paths]
    matches = pl.concat(frames, how="diagonal_relaxed")
    date_number = pl.col("tourney_date").cast(pl.Int64, strict=False)
    matches = matches.filter(
        pl.col("winner_id").is_not_null(),
        pl.col("loser_id").is_not_null(),
        date_number.is_not_null(),
        date_number >= since_year * 10000,
    )
    if through_year is not None:
        matches = matches.filter(date_number < (through_year + 1) * 10000)
    matches = matches.sort([name for name in ("tourney_date", "tourney_id", "match_num") if name in matches.columns])
    if max_matches is not None:
        if max_matches <= 0:
            raise ValueError("max_matches must be > 0")
        matches = matches.tail(max_matches)
    return matches


def _safe_dir(value: str) -> str:
    return value.replace("/", "__").replace("\\", "__").strip(".")


def _polars() -> Any:
    try:
        import polars as pl
    except ImportError as exc:  # pragma: no cover - research dependency
        raise RuntimeError("polars is required for tennis training") from exc
    return pl
