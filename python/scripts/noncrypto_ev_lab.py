"""Experimental non-crypto event-contract EV lab.

Read/write research code only: this script scores candidate strategies and
writes ledgers, but it never submits, cancels, or places orders. It is intended
to answer one practical question before allocating more capture/storage:

    Is there a fee/spread-aware expected-profit case worth tick logging?

Current experiment:

* ``tennis-bundle`` scores a live tennis bundle produced by
  ``tennis_pipeline.py`` against Kalshi executable prices using three objective
  families: sharp-only, model-only, and sharp/model blends.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_reconfigure = getattr(sys.stdout, "reconfigure", None)
if callable(_reconfigure):
    _reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "python" / "src"))

from eventcontracts.research import tennis_v2 as v2  # noqa: E402
from eventcontracts.research.ledger import append_jsonl, to_jsonable, write_jsonl  # noqa: E402
from eventcontracts.research.tennis_market_state import (  # noqa: E402
    SharpOddsSnapshot,
    TennisMarketCandidate,
    build_reference_valuation,
    evaluate_tennis_reference_candidate,
)

DEFAULT_TENNIS_MODEL = (
    ROOT
    / "artifacts"
    / "tennis_xgboost"
    / "bundles"
    / "sports_tennis_xgboost__live-candidate-20260530"
    / "model"
    / "model.onnx"
)


@dataclasses.dataclass(frozen=True)
class _Objective:
    name: str
    use_model: bool
    model_weight: float


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def score_tennis_bundle(
    bundle: Path,
    *,
    model_path: Path | None = DEFAULT_TENNIS_MODEL,
    model_weights: Sequence[float] = (0.0, 0.25, 0.5, 0.75, 1.0),
    min_net_edge: float = 0.015,
    observe_net_edge: float = 0.005,
    min_confidence: float = 0.55,
    contracts: int = 5,
) -> dict[str, Any]:
    """Score a tennis bundle under multiple fair-value objectives."""

    manifest_path = bundle / "manifest.json"
    snapshots_path = bundle / "snapshots.jsonl"
    if not manifest_path.exists() or not snapshots_path.exists():
        raise FileNotFoundError(f"bundle must contain manifest.json and snapshots.jsonl: {bundle}")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    snapshots = load_jsonl(snapshots_path)
    matches = manifest.get("matches", [])
    if len(matches) != len(snapshots):
        raise ValueError("manifest matches and snapshots.jsonl row counts differ")

    model_probabilities = _predict_model_probabilities(snapshots, model_path)
    objectives = _objective_grid(model_weights, model_probabilities is not None)
    rows: list[dict[str, Any]] = []

    now = datetime.now(UTC)
    probability_rows: list[float | None] = (
        list(model_probabilities) if model_probabilities is not None else [None] * len(snapshots)
    )
    for match, snapshot, model_probability in zip(matches, snapshots, probability_rows, strict=True):
        candidate = _candidate_from_manifest(match, snapshot)
        sharp = _sharp_from_manifest(match, now=now)
        yes_bid = _opt_float(match.get("kalshi_yes_bid"))
        yes_ask = _opt_float(match.get("kalshi_yes_ask"))
        for objective in objectives:
            valuation = build_reference_valuation(
                candidate,
                sharp,
                model_probability=model_probability if objective.use_model else None,
                model_weight=objective.model_weight,
            )
            decision = evaluate_tennis_reference_candidate(
                valuation,
                yes_bid=yes_bid,
                yes_ask=yes_ask,
                min_net_edge=min_net_edge,
                min_confidence=min_confidence,
            )
            net_edge = decision.net_edge
            expected_profit = net_edge * contracts if net_edge is not None else None
            row = {
                "row_type": "tennis_ev_experiment",
                "created_at": now.isoformat(),
                "bundle": str(bundle),
                "objective": objective.name,
                "market_id": candidate.market_id,
                "players": [candidate.player_1, candidate.player_2],
                "scheduled_start": candidate.scheduled_start.isoformat() if candidate.scheduled_start else None,
                "kalshi_yes_bid": yes_bid,
                "kalshi_yes_ask": yes_ask,
                "sharp_p1_probability": valuation.p1_sharp_probability,
                "model_p1_probability": model_probability,
                "model_weight": valuation.model_weight,
                "fair_yes": valuation.p1_fair_probability,
                "confidence": valuation.confidence,
                "side": decision.side,
                "executable_price": decision.executable_price,
                "raw_edge": decision.raw_edge,
                "fee": decision.fee,
                "net_edge": net_edge,
                "expected_profit_contracts": contracts,
                "expected_profit_dollars": expected_profit,
                "candidate": decision.candidate,
                "reason": decision.reason,
                "tick_logging_recommendation": _tick_logging_recommendation(
                    decision.candidate,
                    net_edge,
                    observe_net_edge=observe_net_edge,
                ),
            }
            rows.append(row)

    best = max(
        (row for row in rows if row["net_edge"] is not None),
        key=lambda row: float(row["net_edge"]),
        default=None,
    )
    candidates = [row for row in rows if row["candidate"]]
    observe_worthy = [
        row
        for row in rows
        if row["net_edge"] is not None and float(row["net_edge"]) >= observe_net_edge
    ]
    return {
        "experiment": "tennis-bundle",
        "bundle": str(bundle),
        "model_path": str(model_path) if model_path is not None else None,
        "model_available": model_probabilities is not None,
        "rows": rows,
        "summary": {
            "markets": len(snapshots),
            "objectives": len(objectives),
            "rows": len(rows),
            "candidates": len(candidates),
            "observe_worthy": len(observe_worthy),
            "best_market_id": best["market_id"] if best else None,
            "best_objective": best["objective"] if best else None,
            "best_net_edge": best["net_edge"] if best else None,
            "best_expected_profit_dollars": best["expected_profit_dollars"] if best else None,
            "tick_logging_recommended": bool(candidates or observe_worthy),
        },
    }


def _predict_model_probabilities(snapshots: list[dict[str, Any]], model_path: Path | None) -> list[float] | None:
    if model_path is None or not model_path.exists():
        return None
    try:
        import polars as pl
    except ImportError:
        return None
    tennis_snapshots = [_snapshot_from_row(row) for row in snapshots]
    frame = pl.DataFrame([v2.feature_row_v2(snapshot) for snapshot in tennis_snapshots])
    return list(v2.predict_v2_onnx_probabilities(model_path, frame))


def _snapshot_from_row(row: dict[str, Any]) -> v2.TennisV2Snapshot:
    fields = {field.name: field for field in dataclasses.fields(v2.TennisV2Snapshot)}
    kwargs: dict[str, Any] = {}
    for name in fields:
        if name not in row:
            continue
        value = row[name]
        if name == "match_date" and isinstance(value, str):
            value = datetime.strptime(value, "%Y-%m-%d").date()
        kwargs[name] = value
    return v2.TennisV2Snapshot(**kwargs)


def _objective_grid(model_weights: Sequence[float], model_available: bool) -> list[_Objective]:
    objectives = [_Objective(name="sharp_only", use_model=False, model_weight=0.0)]
    if not model_available:
        return objectives
    for weight in model_weights:
        if weight <= 0.0:
            continue
        name = "model_only" if weight >= 1.0 else f"blend_{weight:.2f}"
        objectives.append(_Objective(name=name, use_model=True, model_weight=float(weight)))
    return objectives


def _candidate_from_manifest(match: dict[str, Any], snapshot: dict[str, Any]) -> TennisMarketCandidate:
    start = _parse_datetime(match.get("commence_time"))
    return TennisMarketCandidate(
        market_id=str(match["market_id"]),
        player_1=str(match.get("p1") or snapshot.get("p1_name") or ""),
        player_2=str(match.get("p2") or snapshot.get("p2_name") or ""),
        scheduled_start=start,
        lifecycle_status="open",
        tournament="kalshi-atp",
        surface=str(snapshot.get("surface") or "Unknown"),
    )


def _sharp_from_manifest(match: dict[str, Any], *, now: datetime) -> SharpOddsSnapshot:
    return SharpOddsSnapshot(
        market_id=str(match["market_id"]),
        player_1=str(match["p1"]),
        player_2=str(match["p2"]),
        p1_decimal_odds=float(match["p1_odds"]),
        p2_decimal_odds=float(match["p2_odds"]),
        as_of=now,
        source="the_odds_api:pinnacle",
    )


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _opt_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float, str)):
        return float(value)
    return None


def _tick_logging_recommendation(candidate: bool, net_edge: float | None, *, observe_net_edge: float) -> str:
    if candidate:
        return "start_or_continue_tick_logging:fee_net_candidate"
    if net_edge is not None and net_edge >= observe_net_edge:
        return "continue_tick_logging:near_candidate"
    return "no_new_tick_logging"


def _write_outputs(result: dict[str, Any], *, out: Path | None, ledger: Path | None) -> None:
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(to_jsonable(result), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if ledger is not None:
        for row in result["rows"]:
            append_jsonl(ledger, row)


def _run_no_network(args: argparse.Namespace) -> int:
    fixture_dir = (
        args.out.parent / "noncrypto_ev_lab_fixture_bundle"
        if args.out
        else ROOT / "live-test" / "noncrypto_ev_lab_fixture_bundle"
    )
    fixture_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "matches": [
            {
                "market_id": "KXTENNIS-FIXTURE-A",
                "p1": "Carlos Alcaraz",
                "p2": "Novak Djokovic",
                "p1_odds": 1.60,
                "p2_odds": 2.40,
                "commence_time": "2026-06-03T18:00:00Z",
                "kalshi_yes_bid": 0.54,
                "kalshi_yes_ask": 0.56,
            }
        ]
    }
    snapshot = {
        "market_id": "KXTENNIS-FIXTURE-A",
        "match_id": "KXTENNIS-FIXTURE-A:winner",
        "match_date": "2026-06-03",
        "p1_id": "1",
        "p2_id": "2",
        "surface": "Clay",
        "p1_name": "Carlos Alcaraz",
        "p2_name": "Novak Djokovic",
        "p1_decimal_odds": 1.60,
        "p2_decimal_odds": 2.40,
    }
    (fixture_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    write_jsonl(fixture_dir / "snapshots.jsonl", [snapshot])
    result = score_tennis_bundle(
        fixture_dir,
        model_path=None,
        min_net_edge=args.min_net_edge,
        observe_net_edge=args.observe_net_edge,
        min_confidence=args.min_confidence,
        contracts=args.contracts,
    )
    _write_outputs(result, out=args.out, ledger=args.ledger)
    print(json.dumps(to_jsonable(result["summary"]), indent=2, sort_keys=True))
    return 0


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    tennis = sub.add_parser("tennis-bundle")
    tennis.add_argument("--bundle", type=Path, default=ROOT / "data" / "tennis-live" / "bundle-run-20260603")
    tennis.add_argument("--model", type=Path, default=DEFAULT_TENNIS_MODEL)
    tennis.add_argument("--model-weights", default="0.25,0.5,0.75,1.0")
    tennis.add_argument("--min-net-edge", type=float, default=0.015)
    tennis.add_argument("--observe-net-edge", type=float, default=0.005)
    tennis.add_argument("--min-confidence", type=float, default=0.55)
    tennis.add_argument("--contracts", type=int, default=5)
    tennis.add_argument("--out", type=Path, default=ROOT / "live-test" / "noncrypto_ev_tennis_latest.json")
    tennis.add_argument("--ledger", type=Path, default=ROOT / "live-test" / "noncrypto_ev_tennis_ledger.jsonl")
    tennis.add_argument("--no-network", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    if args.command == "tennis-bundle":
        if args.no_network:
            return _run_no_network(args)
        weights = [float(chunk) for chunk in args.model_weights.split(",") if chunk.strip()]
        result = score_tennis_bundle(
            args.bundle,
            model_path=args.model,
            model_weights=weights,
            min_net_edge=args.min_net_edge,
            observe_net_edge=args.observe_net_edge,
            min_confidence=args.min_confidence,
            contracts=args.contracts,
        )
        _write_outputs(result, out=args.out, ledger=args.ledger)
        print(json.dumps(to_jsonable(result["summary"]), indent=2, sort_keys=True))
        return 0
    raise AssertionError(f"unhandled command {args.command!r}")


if __name__ == "__main__":
    raise SystemExit(main())
