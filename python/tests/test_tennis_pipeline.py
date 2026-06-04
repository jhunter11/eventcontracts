"""Tests for the tennis automation pipeline (Plane A producer + Plane B launcher).

Network-free: exercises the pure parsing/odds/freshness logic. The live Kalshi /
Odds API paths are validated by running the script against the real venues.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import tennis_pipeline as tp  # noqa: E402
import tennis_run_from_bundle as trb  # noqa: E402


def test_date_from_event_ticker() -> None:
    assert tp._date_from_event("KXATPMATCH-26JUN01TIAARN") == "2026-06-01"
    assert tp._date_from_event("KXATPMATCH-26MAY31DEJZVE") == "2026-05-31"
    assert tp._date_from_event("garbage") == ""


def test_surname_and_norm_handle_hyphens() -> None:
    assert tp._surname("Felix Auger-Aliassime") == "auger-aliassime"
    assert tp._norm("  Frances   Tiafoe ") == "frances tiafoe"


def test_round_auto_detection_from_title() -> None:
    f = "R32"
    assert tp._round_from_title("Will X win the A vs B: Round Of 16 match?", f) == "R16"
    assert tp._round_from_title("...: Round Of 32 match?", f) == "R32"
    assert tp._round_from_title("...: Quarterfinal match?", f) == "QF"
    assert tp._round_from_title("...: Semifinal match?", f) == "SF"
    assert tp._round_from_title("...: Final match?", f) == "F"
    assert tp._round_from_title("no round phrase", f) == "R32"  # fallback


def test_manual_csv_provider_matches_both_players(tmp_path: Path) -> None:
    csv_path = tmp_path / "odds.csv"
    csv_path.write_text("player,decimal_odds\nFrances Tiafoe,1.80\nMatteo Arnaldi,2.05\n", encoding="utf-8")
    provider = tp.ManualCsvProvider(csv_path)
    match = tp.Match(
        event_ticker="KXATPMATCH-26JUN01TIAARN",
        legs={"Frances Tiafoe": "T-TIA", "Matteo Arnaldi": "T-ARN"},
        target_date="2026-06-01",
    )
    mo = provider.odds_for_match(match)
    assert mo is not None
    assert mo.odds_for("Frances Tiafoe") == 1.80
    assert mo.odds_for("Matteo Arnaldi") == 2.05


def test_manual_csv_provider_returns_none_when_a_player_missing(tmp_path: Path) -> None:
    csv_path = tmp_path / "odds.csv"
    csv_path.write_text("player,decimal_odds\nFrances Tiafoe,1.80\n", encoding="utf-8")
    provider = tp.ManualCsvProvider(csv_path)
    match = tp.Match(event_ticker="E", legs={"Frances Tiafoe": "a", "Matteo Arnaldi": "b"}, target_date="2026-06-01")
    assert provider.odds_for_match(match) is None


def test_the_odds_api_extract_prefers_pinnacle() -> None:
    provider = tp.TheOddsApiProvider.__new__(tp.TheOddsApiProvider)
    provider.book = "pinnacle"
    event = {
        "bookmakers": [
            {"key": "betfair", "markets": [{"key": "h2h", "outcomes": [
                {"name": "Frances Tiafoe", "price": 1.90}, {"name": "Matteo Arnaldi", "price": 2.00}]}]},
            {"key": "pinnacle", "markets": [{"key": "h2h", "outcomes": [
                {"name": "Frances Tiafoe", "price": 1.81}, {"name": "Matteo Arnaldi", "price": 2.10}]}]},
        ]
    }
    priced = provider._extract(event)
    assert priced["frances tiafoe"] == 1.81  # pinnacle preferred over betfair mean


def _manifest(gen_delta_min: float, commence_deltas_min: list[float]) -> dict:
    now = datetime.now(UTC)
    return {
        "generated_at": (now - timedelta(minutes=gen_delta_min)).isoformat(),
        "matches": [
            {"commence_time": (now + timedelta(minutes=d)).isoformat().replace("+00:00", "Z")}
            for d in commence_deltas_min
        ],
    }


def test_freshness_fresh_with_upcoming_match_ok() -> None:
    ok, _ = trb._freshness_ok(_manifest(5, [-30, 120]), max_age_min=60, include_started=False)
    assert ok  # one match still upcoming (+120)


def test_freshness_refuses_stale_bundle() -> None:
    ok, why = trb._freshness_ok(_manifest(120, [120]), max_age_min=60, include_started=False)
    assert not ok and "old" in why


def test_freshness_refuses_when_all_started() -> None:
    ok, why = trb._freshness_ok(_manifest(1, [-30, -10]), max_age_min=60, include_started=False)
    assert not ok and "started" in why
