"""Build tennis sharp-reference signals and paper edge ledgers.

Read-only. This script does not discover markets from Kalshi directly; it turns
operator-provided candidate/odds rows into the signal payload consumed by
``sports_tennis_xgboost`` and records the fee-net paper decision. Use
``--no-network`` for a deterministic fixture self-test.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

with suppress(AttributeError):
    sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "python" / "src"))

from eventcontracts.research.ledger import append_jsonl, to_jsonable, write_jsonl  # noqa: E402
from eventcontracts.research.tennis_market_state import (  # noqa: E402
    SharpOddsSnapshot,
    TennisMarketCandidate,
    build_reference_valuation,
    evaluate_tennis_reference_candidate,
    external_signal_payload,
    is_tradeable_lifecycle,
)

DEFAULT_LEDGER = ROOT / "live-test" / "tennis_sharp_reference_ledger.jsonl"
DEFAULT_SIGNALS = ROOT / "live-test" / "tennis_sharp_reference_signals.jsonl"


def _run_no_network(args: argparse.Namespace) -> int:
    now = datetime(2026, 6, 2, 12, tzinfo=UTC)
    candidate = TennisMarketCandidate(
        market_id=args.market_id,
        player_1="Carlos Alcaraz",
        player_2="Novak Djokovic",
        scheduled_start=now + timedelta(hours=4),
        lifecycle_status="open",
        tournament="fixture",
        surface="Clay",
    )
    sharp = SharpOddsSnapshot(
        market_id=args.market_id,
        player_1="Carlos Alcaraz",
        player_2="Novak Djokovic",
        p1_decimal_odds=1.60,
        p2_decimal_odds=2.40,
        as_of=now,
        source="fixture-sharp",
    )
    row = _build_row(args, candidate, sharp, now=now, model_probability=args.model_probability)
    append_jsonl(args.ledger, row)
    write_jsonl(args.signals_out, [row["signal_payload"]])
    print(json.dumps(to_jsonable(row), sort_keys=True))
    return 0


def _run_csv(args: argparse.Namespace) -> int:
    if args.candidates_csv is None or args.odds_csv is None:
        raise SystemExit("--candidates-csv and --odds-csv are required unless --no-network is set")
    now = datetime.now(UTC)
    candidates = [_candidate_from_row(row) for row in _read_csv(args.candidates_csv)]
    odds_by_market = {_required(row, "market_id"): row for row in _read_csv(args.odds_csv)}
    rows: list[dict[str, Any]] = []
    signals: list[dict[str, Any]] = []
    for candidate in candidates:
        odds_row = odds_by_market.get(candidate.market_id)
        if odds_row is None:
            append_jsonl(
                args.ledger,
                {
                    "row_type": "tennis_sharp_reference_missing_odds",
                    "market_id": candidate.market_id,
                    "reason": "missing_market_id_in_odds_csv",
                },
            )
            continue
        sharp = _sharp_from_row(odds_row)
        model_probability = _optional_float(odds_row.get("model_probability"))
        row = _build_row(args, candidate, sharp, now=now, model_probability=model_probability)
        rows.append(row)
        signals.append(row["signal_payload"])
        append_jsonl(args.ledger, row)
    if args.signals_out is not None:
        write_jsonl(args.signals_out, signals)
    print(f"wrote {len(rows)} tennis sharp-reference row(s) to {args.ledger}")
    return 0


def _build_row(
    args: argparse.Namespace,
    candidate: TennisMarketCandidate,
    sharp: SharpOddsSnapshot,
    *,
    now: datetime,
    model_probability: float | None,
) -> dict[str, Any]:
    lifecycle_ok = is_tradeable_lifecycle(candidate, now=now, max_hours_to_start=args.max_hours_to_start)
    valuation = build_reference_valuation(
        candidate,
        sharp,
        model_probability=model_probability,
        model_weight=args.model_weight,
    )
    decision = evaluate_tennis_reference_candidate(
        valuation,
        yes_bid=args.yes_bid,
        yes_ask=args.yes_ask,
        min_net_edge=args.min_net_edge,
        min_confidence=args.min_confidence,
        max_model_sharp_disagreement=args.max_model_sharp_disagreement,
    )
    signal_payload = external_signal_payload(valuation)
    return {
        "row_type": "tennis_sharp_reference",
        "candidate": candidate,
        "lifecycle_ok": lifecycle_ok,
        "valuation": valuation,
        "decision": (
            decision
            if lifecycle_ok
            else {**to_jsonable(decision), "candidate": False, "reason": "lifecycle_blocked"}
        ),
        "signal_payload": signal_payload,
    }


def _candidate_from_row(row: dict[str, str]) -> TennisMarketCandidate:
    return TennisMarketCandidate(
        market_id=_required(row, "market_id"),
        player_1=row.get("p1_name") or row.get("player_1") or row.get("p1") or "",
        player_2=row.get("p2_name") or row.get("player_2") or row.get("p2") or "",
        scheduled_start=_optional_datetime(row.get("scheduled_start")),
        lifecycle_status=row.get("lifecycle_status") or row.get("status") or "open",
        tournament=row.get("tournament") or None,
        surface=row.get("surface") or None,
    )


def _sharp_from_row(row: dict[str, str]) -> SharpOddsSnapshot:
    return SharpOddsSnapshot(
        market_id=_required(row, "market_id"),
        player_1=row.get("p1_name") or row.get("player_1") or row.get("p1") or "",
        player_2=row.get("p2_name") or row.get("player_2") or row.get("p2") or "",
        p1_decimal_odds=float(_required(row, "p1_decimal_odds")),
        p2_decimal_odds=float(_required(row, "p2_decimal_odds")),
        as_of=_optional_datetime(row.get("as_of")) or datetime.now(UTC),
        source=row.get("source") or "operator-sharp-csv",
    )


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"empty CSV: {path}")
        return [dict(row) for row in reader]


def _required(row: dict[str, str], field: str) -> str:
    value = (row.get(field) or "").strip()
    if not value:
        raise ValueError(f"missing required field {field!r}")
    return value


def _optional_float(value: object) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    return float(value)


def _optional_datetime(value: object) -> datetime | None:
    if value is None or str(value).strip() == "":
        return None
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-network", action="store_true")
    parser.add_argument("--candidates-csv", type=Path, default=None)
    parser.add_argument("--odds-csv", type=Path, default=None)
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument("--signals-out", type=Path, default=DEFAULT_SIGNALS)
    parser.add_argument("--market-id", default="KXTENNIS-PAPER")
    parser.add_argument("--model-probability", type=float, default=0.70)
    parser.add_argument("--model-weight", type=float, default=0.35)
    parser.add_argument("--yes-bid", type=float, default=0.54)
    parser.add_argument("--yes-ask", type=float, default=0.56)
    parser.add_argument("--min-net-edge", type=float, default=0.015)
    parser.add_argument("--min-confidence", type=float, default=0.55)
    parser.add_argument("--max-model-sharp-disagreement", type=float, default=0.18)
    parser.add_argument("--max-hours-to-start", type=float, default=72.0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    if args.no_network:
        return _run_no_network(args)
    return _run_csv(args)


if __name__ == "__main__":
    raise SystemExit(main())
