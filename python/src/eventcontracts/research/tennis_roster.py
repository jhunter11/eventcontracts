"""Canonical player-roster resolution for tennis snapshot building.

Both ``python/scripts/build_upcoming_snapshot.py`` (single-match) and
``python/scripts/tennis_pipeline.py`` (auto-discovery producer) need to turn a
player NAME into the Sackmann id + latest static fields (rank, age, height,
hand) that ``tennis_v2.build_upcoming_snapshot`` consumes. Keeping that logic in
one importable place stops the two entry points from drifting (the project's
recurring "two code paths must agree" hazard).

Pure feature/state reconstruction — no model, no ONNX. This is the read-only
half of the research plane.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import polars as pl

# Static/recent fields pulled from a player's most recent appearance.
_PLAYER_COLS = ("name", "pid", "date", "rank", "rank_points", "age", "ht", "hand", "seed")


def load_history(history_dir: Path) -> pl.DataFrame:
    """Main-tour singles only (strict ``atp_matches_YYYY.csv``), like the trainer."""
    files = sorted(
        f for f in history_dir.glob("atp_matches_[12][0-9][0-9][0-9].csv") if f.stem[-4:].isdigit()
    )
    if not files:
        raise FileNotFoundError(f"no atp_matches_YYYY.csv under {history_dir}")
    frames = [pl.read_csv(f, infer_schema_length=20000, ignore_errors=True) for f in files]
    return pl.concat(frames, how="diagonal_relaxed")


def _norm(expr: pl.Expr) -> pl.Expr:
    # Lowercase, treat hyphens as spaces (Sackmann "Auger Aliassime" vs Kalshi
    # "Auger-Aliassime"), collapse whitespace.
    return (
        expr.cast(pl.Utf8)
        .str.to_lowercase()
        .str.replace_all("-", " ")
        .str.strip_chars()
        .str.replace_all(r"\s+", " ")
    )


def build_player_table(hist: pl.DataFrame) -> pl.DataFrame:
    """One row per (player, appearance) with a normalized-name key column."""
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


def normalize_name(name: str) -> str:
    # Mirror `_norm`: hyphens -> spaces so hyphenated surnames resolve.
    return " ".join(name.replace("-", " ").strip().lower().split())


class PlayerNotFound(LookupError):
    """Raised when a player name/id cannot be resolved; carries close matches."""

    def __init__(self, query: str, suggestions: list[str]) -> None:
        self.query = query
        self.suggestions = suggestions
        super().__init__(f"could not resolve player '{query}'. Close names: {suggestions or '(none)'}")


def resolve_player(table: pl.DataFrame, query: str) -> dict[str, Any]:
    """Resolve a Sackmann id or player name to its latest appearance row.

    Tries id match, then exact normalized-name match. Raises
    :class:`PlayerNotFound` (with surname-based suggestions) on a miss so callers
    can fail loudly rather than silently mis-map a player.
    """
    q = normalize_name(query)
    by_id = table.filter(pl.col("pid") == query)
    hit = by_id if by_id.height else table.filter(pl.col("nname") == q)
    if not hit.height:
        surname = q.split(" ")[-1] if q else ""
        suggestions = (
            table.filter(pl.col("nname").str.contains(surname, literal=True))
            .select("name")
            .unique()
            .head(8)["name"]
            .to_list()
            if surname
            else []
        )
        raise PlayerNotFound(query, suggestions)
    latest: dict[str, Any] = hit.sort("date").tail(1).to_dicts()[0]
    return latest
