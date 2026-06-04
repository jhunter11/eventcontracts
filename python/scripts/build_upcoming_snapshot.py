"""Build the live-runner v2 snapshot JSONL for an upcoming ATP match.

Removes the 34-feature hand-fill footgun: given two player NAMES (or ids), a
surface and a date, it reconstructs both players' current state (Elo, surface
Elo, blend, serve/return, form, fatigue, surface record, rest) from the canonical
Sackmann history via ``tennis_v2.build_upcoming_snapshot`` and emits exactly the
JSON shape the Rust live runner parses from ``--tennis-snapshots-jsonl``:

    {"market_id": "<KALSHI TICKER>", "source": "tennis_xgboost_onnx",
     "surface": "...", "best_of": 3, "p1_elo": ..., ..., "p1_decimal_odds": ...}

The runner re-scores this with the promoted v2 bundle (so we do NOT pre-compute a
probability here, and we do NOT use the v1 ``tennis-xgboost-score`` path).

Static/recent fields (rank, points, age, height, hand, seed) auto-fill from each
player's most recent appearance; override with flags if you have fresher values.

Odds: pass --p1-odds/--p2-odds (decimal) or --odds-csv (player,decimal_odds). The
live sleeve sets require_odds_present=true, so WITHOUT odds the snapshot scores but
the sleeve is a deliberate no-op (this script warns loudly in that case).

Example:
    python python/scripts/build_upcoming_snapshot.py \
        --p1 "Carlos Alcaraz" --p2 "Alexander Zverev" \
        --surface Clay --date 2026-06-05 --best-of 5 --round QF \
        --tourney-level G --market-id KXATPMATCH-26JUN05ALCZVE-ALC \
        --odds-csv odds.csv --out snapshots.jsonl
"""

from __future__ import annotations

import argparse
import dataclasses
import io
import json
import sys
import warnings
from datetime import datetime
from pathlib import Path

import polars as pl

warnings.filterwarnings("ignore")
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "python" / "src"))
DEFAULT_HISTORY = ROOT / "data" / "tennis" / "tennis_atp" / "tennis_atp-master"

from eventcontracts.research import tennis_v2 as t2  # noqa: E402


def _load_history(history_dir: Path) -> pl.DataFrame:
    # Main-tour singles only (strict atp_matches_YYYY.csv), like the trainer.
    files = sorted(
        f for f in history_dir.glob("atp_matches_[12][0-9][0-9][0-9].csv") if f.stem[-4:].isdigit()
    )
    if not files:
        raise SystemExit(f"no atp_matches_YYYY.csv under {history_dir}")
    frames = [pl.read_csv(f, infer_schema_length=20000, ignore_errors=True) for f in files]
    return pl.concat(frames, how="diagonal_relaxed")


def _norm(expr: pl.Expr) -> pl.Expr:
    return expr.cast(pl.Utf8).str.to_lowercase().str.strip_chars().str.replace_all(r"\s+", " ")


def _player_table(hist: pl.DataFrame) -> pl.DataFrame:
    parts = []
    for side in ("winner", "loser"):
        cols = {
            "name": f"{side}_name",
            "pid": f"{side}_id",
            "date": "tourney_date",
            "rank": f"{side}_rank",
            "rank_points": f"{side}_rank_points",
            "age": f"{side}_age",
            "ht": f"{side}_ht",
            "hand": f"{side}_hand",
            "seed": f"{side}_seed",
        }
        present = {k: v for k, v in cols.items() if v in hist.columns}
        parts.append(hist.select([pl.col(v).alias(k) for k, v in present.items()]))
    long = pl.concat(parts, how="diagonal_relaxed")
    return long.with_columns(_norm(pl.col("name")).alias("nname"), pl.col("pid").cast(pl.Utf8))


def _resolve(table: pl.DataFrame, query: str) -> dict:
    q = " ".join(query.strip().lower().split())
    by_id = table.filter(pl.col("pid") == query)
    hit = by_id if by_id.height else table.filter(pl.col("nname") == q)
    if not hit.height:
        sample = (
            table.filter(pl.col("nname").str.contains(q.split(" ")[-1], literal=True))
            .select("name")
            .unique()
            .head(8)["name"]
            .to_list()
        )
        raise SystemExit(f"could not resolve player '{query}'. Close names: {sample or '(none)'}")
    return hit.sort("date").tail(1).to_dicts()[0]


def _odds_from_csv(path: Path, p1_name: str, p2_name: str) -> tuple[float | None, float | None]:
    rows = pl.read_csv(path, infer_schema_length=1000).to_dicts()
    norm = {" ".join(str(r.get("player", "")).strip().lower().split()): r.get("decimal_odds") for r in rows}
    n1, n2 = " ".join(p1_name.lower().split()), " ".join(p2_name.lower().split())
    o1 = norm.get(n1)
    o2 = norm.get(n2)
    return (float(o1) if o1 else None), (float(o2) if o2 else None)


def _opt_float(v) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _opt_int(v) -> int | None:
    f = _opt_float(v)
    return None if f is None else int(f)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--p1", required=True, help="Player 1 name or Sackmann id (the YES side).")
    ap.add_argument("--p2", required=True, help="Player 2 name or Sackmann id.")
    ap.add_argument("--surface", required=True, choices=["Hard", "Clay", "Grass", "Carpet"])
    ap.add_argument("--date", required=True, help="Match date YYYY-MM-DD (>= last history date).")
    ap.add_argument("--best-of", type=int, default=3, choices=[3, 5])
    ap.add_argument("--round", default="")
    ap.add_argument("--tourney-level", default="", help="A/M/G/F etc. (G = Grand Slam).")
    ap.add_argument("--market-id", required=True, help="The Kalshi ticker this snapshot maps to.")
    ap.add_argument("--history-dir", type=Path, default=DEFAULT_HISTORY)
    ap.add_argument("--out", type=Path, required=True, help="Output JSONL (one line per match).")
    ap.add_argument("--append", action="store_true", help="Append to --out instead of overwriting.")
    ap.add_argument("--p1-odds", type=float, default=None, help="Decimal odds for p1 (overrides CSV).")
    ap.add_argument("--p2-odds", type=float, default=None)
    ap.add_argument("--odds-csv", type=Path, default=None, help="player,decimal_odds rows.")
    args = ap.parse_args()

    match_date = datetime.strptime(args.date, "%Y-%m-%d").date()
    hist = _load_history(args.history_dir)
    table = _player_table(hist)
    p1 = _resolve(table, args.p1)
    p2 = _resolve(table, args.p2)

    last_hist = int(hist.select(pl.col("tourney_date").max()).item())
    if int(match_date.strftime("%Y%m%d")) < last_hist:
        print(
            f"WARNING: --date {args.date} precedes the latest history date {last_hist}; "
            "state features will reflect matches AFTER your match. Use a current/future date."
        )

    # odds: explicit flags win, else CSV by name.
    o1, o2 = args.p1_odds, args.p2_odds
    if (o1 is None or o2 is None) and args.odds_csv:
        c1, c2 = _odds_from_csv(args.odds_csv, str(p1["name"]), str(p2["name"]))
        o1 = o1 if o1 is not None else c1
        o2 = o2 if o2 is not None else c2

    snap = t2.build_upcoming_snapshot(
        hist,
        p1_id=str(p1["pid"]),
        p2_id=str(p2["pid"]),
        match_date=match_date,
        surface=args.surface,
        best_of=args.best_of,
        round=args.round,
        tourney_level=args.tourney_level,
        p1_rank=_opt_int(p1.get("rank")),
        p2_rank=_opt_int(p2.get("rank")),
        p1_rank_points=_opt_float(p1.get("rank_points")),
        p2_rank_points=_opt_float(p2.get("rank_points")),
        p1_age=_opt_float(p1.get("age")),
        p2_age=_opt_float(p2.get("age")),
        p1_height_cm=_opt_float(p1.get("ht")),
        p2_height_cm=_opt_float(p2.get("ht")),
        p1_hand=str(p1.get("hand") or "U"),
        p2_hand=str(p2.get("hand") or "U"),
        p1_decimal_odds=o1,
        p2_decimal_odds=o2,
        match_id=f"{args.market_id}",
    )

    payload = dataclasses.asdict(snap)
    payload["match_date"] = snap.match_date.isoformat()
    payload["p1_name"] = p1["name"]
    payload["p2_name"] = p2["name"]
    row = {"market_id": args.market_id, "source": "tennis_xgboost_onnx", **payload}

    mode = "a" if args.append else "w"
    with open(args.out, mode, encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")

    odds_present = bool(snap.p1_decimal_odds and snap.p2_decimal_odds)
    print(f"resolved p1={p1['name']} (id {p1['pid']}) vs p2={p2['name']} (id {p2['pid']})")
    print(
        f"features: p1_elo={snap.p1_elo:.1f} p2_elo={snap.p2_elo:.1f} "
        f"surf_elo={snap.p1_surface_elo:.1f}/{snap.p2_surface_elo:.1f} "
        f"blend_diff={snap.p1_elo_blend - snap.p2_elo_blend:.1f} "
        f"rank={snap.p1_rank}/{snap.p2_rank} "
        f"form={snap.p1_opp_adjusted_form:.3f}/{snap.p2_opp_adjusted_form:.3f}"
    )
    print(f"odds: p1={snap.p1_decimal_odds} p2={snap.p2_decimal_odds} -> odds_present={odds_present}")
    if not odds_present:
        print(
            "WARNING: no odds attached. The live sleeve sets require_odds_present=true, so it "
            "will NOT trade this market. Provide --p1-odds/--p2-odds or --odds-csv."
        )
    print(f"wrote {'(appended) ' if args.append else ''}{args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
