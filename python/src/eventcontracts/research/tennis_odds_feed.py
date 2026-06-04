"""Vendor-neutral live odds → upcoming-matches merge for the tennis sleeve.

The live tennis sleeve sets ``require_odds_present = true``: the Rust live path
only trades a market when the scored snapshot carried both players' decimal
odds (> 1.0). The promoted v2 model leans on the market block
(``p1_implied_prob`` etc.), so a run without odds is a deliberate no-op.

The upcoming-matches table fed to ``tennis-xgboost-score`` carries ``player_1``
and ``player_2`` plus empty ``p1_decimal_odds`` / ``p2_decimal_odds`` columns.
Nothing populated those columns from a real odds source — that is the gap this
module closes.

Design goals:

* **Vendor-neutral.** The operator supplies a plain two-column CSV
  (``player,decimal_odds``) — one row per player — exported from whatever book
  or aggregator they use (The Odds API, Pinnacle, Betfair, a manual sheet).
  No vendor SDK or API key is baked into the trading code.
* **Reuse the proven name matching.** Player names are matched with the same
  accent/punctuation-insensitive ``surname|firstinitial`` keys used by
  :mod:`eventcontracts.research.tennis_odds`, and both the Sackmann
  ("First Last") and tennis-data ("Last F.") spellings are indexed so a vendor
  using either convention still matches.
* **Observable coverage.** The merge returns an :class:`OddsMergeReport` with
  the per-match match rate and the list of unmatched matches, so a thin odds
  feed is loud, never silent. This mirrors the Rust live runner's
  ``tennis_snapshots_missing_odds`` metric on the scoring side.
"""

from __future__ import annotations

import csv
from collections.abc import Iterable, Mapping, MutableMapping
from dataclasses import dataclass, field
from pathlib import Path

from eventcontracts.research.tennis_odds import (
    sackmann_name_key,
    tennis_data_name_key,
)

# Columns the merge reads/writes on the upcoming-matches table. The defaults
# match the committed scoring template
# (contracts/examples/tennis_xgboost/upcoming_matches_template.csv), which
# carries player names in ``p1_name``/``p2_name`` and empty
# ``p1_decimal_odds``/``p2_decimal_odds`` columns. An operator whose matches
# table uses different name columns can override them at the call site.
PLAYER_1_COLUMN = "p1_name"
PLAYER_2_COLUMN = "p2_name"
P1_ODDS_COLUMN = "p1_decimal_odds"
P2_ODDS_COLUMN = "p2_decimal_odds"

# Accepted header spellings for the operator odds file (case-insensitive).
_ODDS_PLAYER_HEADERS = ("player", "name", "competitor", "runner")
_ODDS_PRICE_HEADERS = ("decimal_odds", "odds", "price", "decimal")


def name_keys(full_name: str) -> set[str]:
    """All normalized lookup keys for a player name.

    Indexes both the Sackmann ("First Last") and tennis-data ("Last F.")
    conventions so an operator odds file using either spelling matches the
    upcoming-matches table. Returns an empty set for an unusable name.
    """

    keys: set[str] = set()
    for builder in (sackmann_name_key, tennis_data_name_key):
        key = builder(full_name)
        if key:
            keys.add(key)
    return keys


@dataclass
class OddsMergeReport:
    """Outcome of merging an odds file into an upcoming-matches table."""

    total_matches: int = 0
    matched_matches: int = 0
    unmatched: list[tuple[str, str]] = field(default_factory=list)
    odds_players_loaded: int = 0

    @property
    def match_rate(self) -> float:
        """Fraction of matches that received odds for *both* players."""

        if self.total_matches == 0:
            return 0.0
        return self.matched_matches / self.total_matches

    @property
    def is_complete(self) -> bool:
        return self.total_matches > 0 and self.matched_matches == self.total_matches

    def summary(self) -> str:
        pct = self.match_rate * 100.0
        return (
            f"odds merge: {self.matched_matches}/{self.total_matches} matches "
            f"({pct:.1f}%) got both players' odds from "
            f"{self.odds_players_loaded} loaded player quote(s)"
        )


def load_operator_odds(path: str | Path) -> dict[str, float]:
    """Load a vendor-neutral ``player,decimal_odds`` CSV into a key→odds map.

    Each player name is expanded into all of its :func:`name_keys`, so a single
    quote is reachable under every spelling convention. Odds of ``<= 1.0`` are
    rejected (a decimal odd of 1.0 implies a certain outcome / no payout and is
    treated as "no real quote", matching the live ``odds_present`` gate).
    """

    resolved = Path(path)
    with resolved.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"operator odds file is empty: {resolved}")
        player_col = _pick_column(reader.fieldnames, _ODDS_PLAYER_HEADERS)
        price_col = _pick_column(reader.fieldnames, _ODDS_PRICE_HEADERS)
        if player_col is None or price_col is None:
            raise ValueError(
                f"operator odds file {resolved} needs a player column "
                f"({_ODDS_PLAYER_HEADERS}) and a decimal-odds column "
                f"({_ODDS_PRICE_HEADERS}); found {reader.fieldnames}"
            )
        odds_by_key: dict[str, float] = {}
        for row in reader:
            name = (row.get(player_col) or "").strip()
            value = _parse_decimal_odds(row.get(price_col))
            if not name or value is None:
                continue
            for key in name_keys(name):
                # Keep the first quote seen for a key; later duplicate keys
                # (e.g. two players sharing a surname|initial) do not overwrite.
                odds_by_key.setdefault(key, value)
    return odds_by_key


def attach_odds(
    matches: Iterable[MutableMapping[str, object]],
    odds_by_key: Mapping[str, float],
) -> OddsMergeReport:
    """Fill ``p1/p2_decimal_odds`` on each match row from the odds map.

    A match is counted as matched only when *both* players resolve to a quote;
    a one-sided match leaves both odds untouched so the live ``odds_present``
    gate (which requires both > 1.0) stays honest. Mutates the rows in place.
    """

    report = OddsMergeReport(odds_players_loaded=len(odds_by_key))
    for row in matches:
        report.total_matches += 1
        p1_name = str(row.get(PLAYER_1_COLUMN) or "").strip()
        p2_name = str(row.get(PLAYER_2_COLUMN) or "").strip()
        p1_odds = _lookup(odds_by_key, p1_name)
        p2_odds = _lookup(odds_by_key, p2_name)
        if p1_odds is not None and p2_odds is not None:
            row[P1_ODDS_COLUMN] = p1_odds
            row[P2_ODDS_COLUMN] = p2_odds
            report.matched_matches += 1
        else:
            report.unmatched.append((p1_name, p2_name))
    return report


def merge_odds_file(
    matches_in: str | Path,
    odds_in: str | Path,
    matches_out: str | Path,
) -> OddsMergeReport:
    """Read a matches CSV, attach operator odds, write the enriched CSV.

    The output preserves the input column order and always includes the two
    odds columns (appended if the input omitted them), so the result is a
    drop-in ``--matches`` input for ``tennis-xgboost-score``.
    """

    matches_path = Path(matches_in)
    with matches_path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"matches file is empty: {matches_path}")
        fieldnames = list(reader.fieldnames)
        rows: list[MutableMapping[str, object]] = [dict(r) for r in reader]

    for required in (PLAYER_1_COLUMN, PLAYER_2_COLUMN):
        if required not in fieldnames:
            raise ValueError(
                f"matches file {matches_path} needs a `{required}` column to "
                f"merge odds; found {fieldnames}"
            )
    for odds_col in (P1_ODDS_COLUMN, P2_ODDS_COLUMN):
        if odds_col not in fieldnames:
            fieldnames.append(odds_col)

    odds_by_key = load_operator_odds(odds_in)
    report = attach_odds(rows, odds_by_key)

    out_path = Path(matches_out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in fieldnames})
    return report


def _pick_column(fieldnames: Iterable[str], candidates: Iterable[str]) -> str | None:
    lowered = {name.strip().lower(): name for name in fieldnames}
    for candidate in candidates:
        if candidate in lowered:
            return lowered[candidate]
    return None


def _lookup(odds_by_key: Mapping[str, float], name: str) -> float | None:
    for key in name_keys(name):
        if key in odds_by_key:
            return odds_by_key[key]
    return None


def _parse_decimal_odds(value: object) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = float(text)
    except ValueError:
        return None
    if parsed <= 1.0:
        return None
    return parsed
