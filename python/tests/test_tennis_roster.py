"""Tests for canonical tennis player-roster resolution (hyphen-robust)."""

from __future__ import annotations

import polars as pl
import pytest

from eventcontracts.research.tennis_roster import (
    PlayerNotFound,
    build_player_table,
    normalize_name,
    resolve_player,
)


def _hist() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "winner_name": ["Felix Auger Aliassime", "Frances Tiafoe"],
            "winner_id": ["200000", "126203"],
            "loser_name": ["Matteo Arnaldi", "Jesper De Jong"],
            "loser_id": ["207989", "207411"],
            "tourney_date": [20260501, 20260501],
            "winner_rank": [9, 12],
            "loser_rank": [35, 102],
            "winner_hand": ["R", "R"],
            "loser_hand": ["R", "R"],
        }
    )


def test_normalize_name_treats_hyphen_as_space() -> None:
    assert normalize_name("Felix Auger-Aliassime") == "felix auger aliassime"
    assert normalize_name("  Carreno-Busta ") == "carreno busta"


def test_resolve_hyphenated_name_against_spaced_history() -> None:
    table = build_player_table(_hist())
    # Kalshi/odds spell it with a hyphen; Sackmann stores a space.
    row = resolve_player(table, "Felix Auger-Aliassime")
    assert row["name"] == "Felix Auger Aliassime"
    assert str(row["pid"]) == "200000"


def test_resolve_by_id_and_by_name() -> None:
    table = build_player_table(_hist())
    assert resolve_player(table, "126203")["name"] == "Frances Tiafoe"
    assert str(resolve_player(table, "Frances Tiafoe")["pid"]) == "126203"


def test_unknown_player_raises_with_suggestions() -> None:
    table = build_player_table(_hist())
    with pytest.raises(PlayerNotFound) as exc:
        resolve_player(table, "Nonexistent Player")
    assert exc.value.query == "Nonexistent Player"
