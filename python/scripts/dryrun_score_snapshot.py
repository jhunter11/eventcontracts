"""Offline dry-run: score a live snapshot JSONL through the PROMOTED v2 bundle.

Proves the legs of the funded path that don't need Kalshi egress:
  1. the snapshot parses into a TennisV2Snapshot,
  2. the promoted bundle's model.onnx scores it (same ONNX file the Rust live
     runner loads; feature builder is Python<->Rust parity-proven),
  3. the odds gate opens (require_odds_present),
  4. the confidence gate (min_model_confidence) is evaluated,
  5. against a sample BBO, what intent the taker WOULD emit and whether it clears
     min_edge_bps after the real Kalshi fee.

Usage: dryrun_score_snapshot.py <snapshot.jsonl> <bundle_dir> [yes_bid] [yes_ask]
"""

from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "python" / "src"))

from eventcontracts.research import tennis_v2 as tv2  # noqa: E402

MIN_EDGE_BPS = 250
MIN_CONF = 0.62
FEE_RATE = 0.07


def _fee(p: float) -> float:
    return FEE_RATE * p * (1.0 - p)


def main() -> int:
    snap_path = Path(sys.argv[1])
    bundle_dir = Path(sys.argv[2])
    yes_bid = float(sys.argv[3]) if len(sys.argv) > 3 else 0.60
    yes_ask = float(sys.argv[4]) if len(sys.argv) > 4 else 0.62

    model_path = bundle_dir / "model" / "model.onnx"
    schema = json.loads((bundle_dir / "feature_schema.json").read_text())
    print(f"bundle={bundle_dir.name}  schema_version={schema.get('schema_version')}")
    assert str(schema.get("schema_version")) == "2", "bundle is not v2 (F9 would refuse)"

    row = json.loads(snap_path.read_text().splitlines()[0])
    field_names = {f.name for f in dataclasses.fields(tv2.TennisV2Snapshot)}
    kwargs = {k: v for k, v in row.items() if k in field_names}
    snap = tv2.TennisV2Snapshot(**kwargs)
    print(f"snapshot: {row.get('market_id')}  surface={snap.surface} best_of={snap.best_of}")

    # Leg 2: score the EXACT promoted ONNX (production scoring path). Build the
    # ordered 34-feature row via the public feature builder, then run the bundle.
    import polars as pl

    feat_row = tv2.feature_row_v2(snap)  # {feature_name: value} for all 34
    frame = pl.DataFrame([feat_row]).select(tv2.TENNIS_V2_FEATURE_NAMES)
    prob = float(tv2.predict_v2_onnx_probabilities(model_path, frame)[0])
    conf = max(prob, 1.0 - prob)

    # Leg 3: odds gate.
    odds_present = bool(
        snap.p1_decimal_odds and snap.p2_decimal_odds
        and snap.p1_decimal_odds > 1.0 and snap.p2_decimal_odds > 1.0
    )

    print("\n=== SCORING ===")
    print(f"player_1_win_probability = {prob:.4f}")
    print(f"model_confidence         = {conf:.4f}   (gate >= {MIN_CONF}: "
          f"{'OPEN' if conf >= MIN_CONF else 'BLOCKED'})")
    print(f"odds_present             = {odds_present}   (require_odds_present gate: "
          f"{'OPEN' if odds_present else 'BLOCKED'})")

    # Leg 5: edge vs a sample BBO, mirroring the Rust taker (buy the opposite touch).
    no_ask = 1.0 - yes_bid
    yes_edge = prob - yes_ask
    no_edge = (1.0 - prob) - no_ask
    print("\n=== EDGE vs SAMPLE BBO ===")
    print(f"yes_bid={yes_bid} yes_ask={yes_ask}  (no_ask={no_ask:.2f})")
    print(f"YES edge = p - yes_ask   = {yes_edge:+.4f}")
    print(f"NO  edge = (1-p) - no_ask= {no_edge:+.4f}")
    thresh = MIN_EDGE_BPS / 10000.0
    if not (conf >= MIN_CONF and odds_present):
        print("\nVERDICT: gates closed -> NO intent (expected unless conf+odds both pass)")
        return 0
    if yes_edge >= no_edge and yes_edge >= thresh:
        side, price, edge = "BUY YES", yes_ask, yes_edge
    elif no_edge > yes_edge and no_edge >= thresh:
        side, price, edge = "BUY NO", no_ask, no_edge
    else:
        print(f"\nVERDICT: max edge {max(yes_edge, no_edge):+.4f} < {thresh:.4f} "
              f"({MIN_EDGE_BPS}bps) -> NO intent at this BBO")
        return 0
    net = edge - _fee(price)
    print(f"\nVERDICT: would emit IOC {side} @ {price:.2f}  "
          f"edge={edge:+.4f} net-after-fee={net:+.4f}  "
          f"({'CLEARS' if net > 0 else 'BELOW'} fee-adjusted gate)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
