"""Tests for the live odds → matches merge (audit F8)."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from eventcontracts.research.tennis_odds_feed import (
    OddsMergeReport,
    attach_odds,
    load_operator_odds,
    merge_odds_file,
    name_keys,
)


def _write_csv(path: Path, header: list[str], rows: list[list[str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)


def test_name_keys_index_both_conventions() -> None:
    # Sackmann "First Last" and tennis-data "Last F." must both resolve so a
    # vendor using either spelling matches the matches table.
    sackmann = name_keys("Carlos Alcaraz")
    tennis_data = name_keys("Alcaraz C.")
    assert sackmann & tennis_data, "the two spellings must share a key"


def test_load_operator_odds_rejects_non_payout_odds(tmp_path: Path) -> None:
    odds = tmp_path / "odds.csv"
    _write_csv(
        odds,
        ["player", "decimal_odds"],
        [
            ["Carlos Alcaraz", "1.50"],
            ["Novak Djokovic", "2.60"],
            ["Certain Winner", "1.0"],  # <= 1.0 → dropped
            ["No Price", ""],  # blank → dropped
        ],
    )
    loaded = load_operator_odds(odds)
    assert _lookup(loaded, "Carlos Alcaraz") == 1.50
    assert _lookup(loaded, "Novak Djokovic") == 2.60
    assert _lookup(loaded, "Certain Winner") is None
    assert _lookup(loaded, "No Price") is None


def test_load_operator_odds_accepts_alternate_headers(tmp_path: Path) -> None:
    odds = tmp_path / "odds.csv"
    _write_csv(odds, ["name", "price"], [["Carlos Alcaraz", "1.8"]])
    loaded = load_operator_odds(odds)
    assert _lookup(loaded, "Carlos Alcaraz") == 1.8


def test_attach_odds_requires_both_players() -> None:
    odds = {**_keyed("Carlos Alcaraz", 1.5), **_keyed("Novak Djokovic", 2.6)}
    matches: list[dict[str, object]] = [
        {"p1_name": "Carlos Alcaraz", "p2_name": "Novak Djokovic"},
        {"p1_name": "Carlos Alcaraz", "p2_name": "Unknown Player"},
    ]
    report = attach_odds(matches, odds)
    assert report.total_matches == 2
    assert report.matched_matches == 1
    assert report.match_rate == 0.5
    # Fully matched row gets both odds.
    assert matches[0]["p1_decimal_odds"] == 1.5
    assert matches[0]["p2_decimal_odds"] == 2.6
    # One-sided row is left untouched (the live gate needs both > 1.0).
    assert "p1_decimal_odds" not in matches[1]
    assert "p2_decimal_odds" not in matches[1]
    assert report.unmatched == [("Carlos Alcaraz", "Unknown Player")]


def test_merge_odds_file_round_trip_is_score_ready(tmp_path: Path) -> None:
    matches = tmp_path / "matches.csv"
    _write_csv(
        matches,
        ["market_id", "p1_name", "p2_name", "surface"],
        [
            ["KXATP-1", "Carlos Alcaraz", "Novak Djokovic", "Hard"],
            ["KXATP-2", "Jannik Sinner", "Daniil Medvedev", "Hard"],
        ],
    )
    odds = tmp_path / "odds.csv"
    _write_csv(
        odds,
        ["player", "decimal_odds"],
        [
            ["Carlos Alcaraz", "1.50"],
            ["Novak Djokovic", "2.60"],
            ["Jannik Sinner", "1.40"],
            ["Daniil Medvedev", "3.10"],
        ],
    )
    out = tmp_path / "merged.csv"
    report = merge_odds_file(matches, odds, out)

    assert report.is_complete
    assert report.match_rate == 1.0
    with out.open(newline="", encoding="utf-8") as handle:
        produced = list(csv.DictReader(handle))
    # Odds columns are appended and populated for every match.
    assert produced[0]["p1_decimal_odds"] == "1.5"
    assert produced[0]["p2_decimal_odds"] == "2.6"
    assert produced[1]["p1_decimal_odds"] == "1.4"
    assert produced[1]["p2_decimal_odds"] == "3.1"
    # Original columns survive in order.
    assert list(produced[0].keys())[:4] == ["market_id", "p1_name", "p2_name", "surface"]


def test_merge_odds_file_reports_partial_coverage(tmp_path: Path) -> None:
    matches = tmp_path / "matches.csv"
    _write_csv(
        matches,
        ["market_id", "p1_name", "p2_name"],
        [
            ["KXATP-1", "Carlos Alcaraz", "Novak Djokovic"],
            ["KXATP-2", "Jannik Sinner", "Someone Unlisted"],
        ],
    )
    odds = tmp_path / "odds.csv"
    _write_csv(
        odds,
        ["player", "decimal_odds"],
        [["Carlos Alcaraz", "1.50"], ["Novak Djokovic", "2.60"]],
    )
    out = tmp_path / "merged.csv"
    report = merge_odds_file(matches, odds, out)
    assert report.total_matches == 2
    assert report.matched_matches == 1
    assert not report.is_complete
    assert ("Jannik Sinner", "Someone Unlisted") in report.unmatched


def test_merge_requires_player_columns(tmp_path: Path) -> None:
    matches = tmp_path / "matches.csv"
    _write_csv(matches, ["market_id", "surface"], [["KXATP-1", "Hard"]])
    odds = tmp_path / "odds.csv"
    _write_csv(odds, ["player", "decimal_odds"], [["X Y", "1.5"]])
    with pytest.raises(ValueError, match="p1_name"):
        merge_odds_file(matches, odds, tmp_path / "out.csv")


def test_report_match_rate_handles_empty() -> None:
    assert OddsMergeReport().match_rate == 0.0


def _keyed(name: str, value: float) -> dict[str, float]:
    return {key: value for key in name_keys(name)}


def _lookup(odds_by_key: dict[str, float], name: str) -> float | None:
    for key in name_keys(name):
        if key in odds_by_key:
            return odds_by_key[key]
    return None
