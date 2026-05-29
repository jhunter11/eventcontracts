"""Merge tennis-data.co.uk bookmaker odds into Sackmann ATP match rows.

Sackmann match files carry no betting odds — the v1 diagnostics showed the
three odds features were dead because of it. tennis-data.co.uk publishes per-year
ATP spreadsheets with average / Pinnacle / Bet365 decimal odds. This module
ingests those files and fuzzy-matches each odds row to a Sackmann match so the
v2 market block (``p1_implied_prob`` etc.) lights up.

Two mismatches make this non-trivial and are handled here:

* **Names.** Sackmann uses ``"Marcos Giron"`` (first last); tennis-data uses
  ``"Giron M."`` (surname + initials). Both are normalized to a
  ``surname|firstinitial`` key (accent/punctuation-insensitive).
* **Dates.** Sackmann ``tourney_date`` is the tournament *start* (Monday);
  tennis-data ``Date`` is the actual *match* day, often days later. We match on
  the player-pair key within a forward date window from the tournament start.

``openpyxl`` is required to read the ``.xlsx`` files; install it from the
research extras. Reads are lazy/guarded.
"""

from __future__ import annotations

import re
import unicodedata
import warnings
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from eventcontracts.research.tennis_xgboost import _polars

_NON_ALNUM = re.compile(r"[^a-z0-9]")
_ODDS_PREFERENCE = (("AvgW", "AvgL"), ("PSW", "PSL"), ("B365W", "B365L"), ("MaxW", "MaxL"))


def normalize_surname(value: str) -> str:
    """Accent/punctuation-insensitive surname token (``O'Connell`` → ``oconnell``)."""

    decomposed = unicodedata.normalize("NFKD", value)
    ascii_only = decomposed.encode("ascii", "ignore").decode("ascii")
    return _NON_ALNUM.sub("", ascii_only.lower())


def sackmann_name_key(full_name: str) -> str | None:
    """``"Marcos Giron"`` / ``"Felix Auger-Aliassime"`` → ``surname|firstinitial``."""

    tokens = str(full_name or "").strip().split()
    if len(tokens) < 2:
        return None
    first_initial = tokens[0][:1].lower()
    surname = normalize_surname("".join(tokens[1:]))
    if not surname or not first_initial:
        return None
    return f"{surname}|{first_initial}"


def tennis_data_name_key(name: str) -> str | None:
    """``"Giron M."`` / ``"Auger Aliassime F."`` → ``surname|firstinitial``."""

    tokens = str(name or "").strip().split()
    if len(tokens) < 2:
        return None
    initial = tokens[-1].replace(".", "")[:1].lower()
    surname = normalize_surname("".join(tokens[:-1]))
    if not surname or not initial:
        return None
    return f"{surname}|{initial}"


def load_tennis_data_odds(source: str | Path | Sequence[str | Path]) -> Any:
    """Load one or more tennis-data.co.uk ATP ``.xlsx`` files into a tidy frame.

    Returns columns: ``match_date`` (date), ``winner_key``, ``loser_key``,
    ``winner_decimal_odds``, ``loser_decimal_odds``, ``surface``.
    """

    pl = _polars()
    paths = _resolve_paths(source)
    if not paths:
        raise FileNotFoundError(f"no tennis-data .xlsx files found at {source}")
    frames = []
    skipped: list[str] = []
    for path in paths:
        # tennis-data.co.uk serves some older seasons as legacy OLE2 .xls under
        # an .xlsx name; openpyxl raises BadZipFile on those. A single bad season
        # must not abort the whole merge — skip it, warn, and keep going. Only a
        # total failure (no readable file) is fatal.
        if not _looks_like_xlsx(path):
            skipped.append(f"{path.name} (not an OOXML/.xlsx zip)")
            continue
        try:
            raw = pl.read_excel(path, engine="openpyxl")
        except Exception as exc:  # noqa: BLE001 - want to skip any unreadable file
            skipped.append(f"{path.name} ({type(exc).__name__})")
            continue
        frames.append(_tidy_odds_frame(pl, raw))
    if skipped:
        warnings.warn(
            "skipped unreadable tennis-data odds file(s): " + ", ".join(skipped),
            stacklevel=2,
        )
    if not frames:
        raise FileNotFoundError(
            f"no readable tennis-data .xlsx files at {source}; skipped {len(skipped)}"
        )
    return pl.concat(frames, how="diagonal_relaxed")


def _looks_like_xlsx(path: Path) -> bool:
    """True when the file starts with the ZIP magic ``PK`` (OOXML .xlsx).

    Legacy OLE2 .xls files start with ``\\xd0\\xcf\\x11\\xe0`` and are rejected
    cheaply here before openpyxl raises a noisier BadZipFile.
    """

    try:
        with path.open("rb") as fh:
            return fh.read(2) == b"PK"
    except OSError:
        return False


def merge_odds_into_matches(
    matches: Any,
    odds: Any,
    *,
    forward_days: int = 21,
    backward_days: int = 2,
) -> Any:
    """Attach ``winner_decimal_odds`` / ``loser_decimal_odds`` to Sackmann rows.

    Matches on the (winner_key, loser_key) pair, then keeps the odds row whose
    match date falls in ``[tourney_date − backward_days, tourney_date + forward_days]``
    and is closest to the tournament start. Returns ``matches`` with two new
    columns; unmatched rows get nulls (which default to neutral odds downstream).
    """

    pl = _polars()
    if "winner_name" not in matches.columns or "loser_name" not in matches.columns:
        raise ValueError("matches frame needs winner_name and loser_name to merge odds")

    base = matches.with_row_index("_row").with_columns(
        _tourney_date=pl.col("tourney_date").cast(pl.Utf8).str.strptime(pl.Date, "%Y%m%d", strict=False),
        winner_key=pl.col("winner_name").map_elements(lambda v: sackmann_name_key(v) or "", return_dtype=pl.Utf8),
        loser_key=pl.col("loser_name").map_elements(lambda v: sackmann_name_key(v) or "", return_dtype=pl.Utf8),
    )
    joined = base.join(
        odds.select(["winner_key", "loser_key", "match_date", "winner_decimal_odds", "loser_decimal_odds"]),
        on=["winner_key", "loser_key"],
        how="left",
    )
    delta = (pl.col("match_date") - pl.col("_tourney_date")).dt.total_days()
    matched = (
        joined.filter(pl.col("match_date").is_not_null() & (delta >= -backward_days) & (delta <= forward_days))
        .with_columns(_delta_abs=delta.abs())
        .sort(["_row", "_delta_abs"])
        .group_by("_row", maintain_order=True)
        .first()
        .select(["_row", "winner_decimal_odds", "loser_decimal_odds"])
    )
    out = base.join(matched, on="_row", how="left").drop(["_row", "_tourney_date", "winner_key", "loser_key"])
    return out


def odds_match_rate(merged: Any) -> float:
    pl = _polars()
    if "winner_decimal_odds" not in merged.columns:
        return 0.0
    matched = merged.filter(pl.col("winner_decimal_odds").is_not_null()).height
    return matched / merged.height if merged.height else 0.0


def _tidy_odds_frame(pl: Any, raw: Any) -> Any:
    winner_col, loser_col = _odds_columns(raw.columns)
    surface = pl.col("Surface") if "Surface" in raw.columns else pl.lit(None)
    return raw.select(
        match_date=pl.col("Date").cast(pl.Date, strict=False),
        winner_name=pl.col("Winner").cast(pl.Utf8),
        loser_name=pl.col("Loser").cast(pl.Utf8),
        winner_decimal_odds=pl.col(winner_col).cast(pl.Float64, strict=False),
        loser_decimal_odds=pl.col(loser_col).cast(pl.Float64, strict=False),
        surface=surface,
    ).with_columns(
        winner_key=pl.col("winner_name").map_elements(lambda v: tennis_data_name_key(v) or "", return_dtype=pl.Utf8),
        loser_key=pl.col("loser_name").map_elements(lambda v: tennis_data_name_key(v) or "", return_dtype=pl.Utf8),
    ).filter(
        (pl.col("winner_key") != "") & (pl.col("loser_key") != "") & pl.col("winner_decimal_odds").is_not_null()
    )


def _odds_columns(columns: Sequence[str]) -> tuple[str, str]:
    for win_col, lose_col in _ODDS_PREFERENCE:
        if win_col in columns and lose_col in columns:
            return win_col, lose_col
    raise ValueError(f"no recognized odds columns in {list(columns)}")


def _resolve_paths(source: str | Path | Sequence[str | Path]) -> list[Path]:
    if isinstance(source, (str, Path)):
        path = Path(source)
        if path.is_dir():
            return sorted(path.glob("*.xlsx"))
        return [path] if path.is_file() else []
    return [Path(p) for p in source if Path(p).is_file()]
