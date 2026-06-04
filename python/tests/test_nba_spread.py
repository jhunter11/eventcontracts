from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from eventcontracts.research.nba_spread import (
    NbaSpreadValidationConfig,
    american_to_probability,
    devig_two_way,
    evaluate_spread_ladder,
    fit_margin_distribution,
    fixture_anchor,
    fixture_game_state,
    fixture_markets,
    markout_report_from_entry_report,
    parse_kalshi_spread_market,
    settlement_report_from_entry_report,
)


def test_american_odds_devig_live_anchor_probabilities() -> None:
    anchor = fixture_anchor()
    home_cover_raw = american_to_probability(-120)
    away_cover_raw = american_to_probability(-110)
    home_cover, away_cover = devig_two_way(home_cover_raw, away_cover_raw)

    assert home_cover == pytest.approx(anchor.home_cover_probability)
    assert away_cover == pytest.approx(1.0 - anchor.home_cover_probability)
    assert anchor.home_win_probability == pytest.approx(0.74496644295)


def test_fit_margin_distribution_hits_anchor_points() -> None:
    anchor = fixture_anchor()
    distribution = fit_margin_distribution(anchor)

    assert distribution.sigma_home_margin > 0
    assert distribution.probability_home_margin_gt(0.0) == pytest.approx(anchor.home_win_probability)
    assert distribution.probability_home_margin_gt(8.5) == pytest.approx(anchor.home_cover_probability)


def test_parse_kalshi_spread_market_maps_home_and_away() -> None:
    game = fixture_game_state()
    now = datetime(2026, 6, 4, 3, 55, tzinfo=UTC)

    home = parse_kalshi_spread_market(
        {
            "ticker": "KXNBASPREAD-FIXTURE-SAS8",
            "title": "San Antonio wins by over 8.5 points?",
            "yes_bid_dollars": "0.55",
            "yes_ask_dollars": "0.56",
        },
        game=game,
        received_at=now,
    )
    away = parse_kalshi_spread_market(
        {
            "ticker": "KXNBASPREAD-FIXTURE-NYK8",
            "title": "New York wins by over 8.5 points?",
            "yes_bid_dollars": "0.07",
            "yes_ask_dollars": "0.09",
        },
        game=game,
        received_at=now,
    )

    assert home is not None
    assert home.team_role == "home"
    assert home.threshold == pytest.approx(8.5)
    assert away is not None
    assert away.team_role == "away"


def test_parse_kalshi_spread_market_accepts_mlb_run_titles() -> None:
    game = fixture_game_state()
    now = datetime(2026, 6, 4, 3, 55, tzinfo=UTC)

    quote = parse_kalshi_spread_market(
        {
            "ticker": "KXMLBSPREAD-FIXTURE-SAS4",
            "title": "San Antonio wins by over 4.5 runs?",
            "yes_bid_dollars": "0.19",
            "yes_ask_dollars": "0.21",
        },
        game=game,
        received_at=now,
    )

    assert quote is not None
    assert quote.team_role == "home"
    assert quote.threshold == pytest.approx(4.5)


def test_evaluate_spread_ladder_flags_fixture_no_candidate() -> None:
    report = evaluate_spread_ladder(
        game=fixture_game_state(),
        anchor=fixture_anchor(),
        markets=fixture_markets(),
        config=NbaSpreadValidationConfig(min_net_edge=0.015, paper_contracts=5),
        as_of=datetime(2026, 6, 4, 3, 55, tzinfo=UTC),
    )

    candidates = [row for row in report.decisions if row.candidate]
    assert len(candidates) == 1
    assert candidates[0].ticker == "KXNBASPREAD-FIXTURE-SAS8"
    assert candidates[0].side == "NO"
    assert candidates[0].net_edge is not None and candidates[0].net_edge > 0.02
    assert candidates[0].as_signal_payload()["market_id"] == candidates[0].ticker


def test_scoreboard_win_probability_disagreement_blocks_candidates() -> None:
    game = replace(fixture_game_state(), scoreboard_home_win_probability=0.59)
    report = evaluate_spread_ladder(
        game=game,
        anchor=fixture_anchor(),
        markets=fixture_markets(),
        config=NbaSpreadValidationConfig(
            min_net_edge=0.015,
            max_scoreboard_win_probability_disagreement=0.12,
        ),
        as_of=datetime(2026, 6, 4, 3, 55, tzinfo=UTC),
    )

    assert not [row for row in report.decisions if row.candidate]
    assert any(
        row.reason == "reference_scoreboard_win_probability_disagreement"
        and row.net_edge is not None
        and row.net_edge > 0
        for row in report.decisions
    )


def test_require_source_timestamp_blocks_proxy_only_candidates() -> None:
    anchor = replace(fixture_anchor(), timestamp_basis="espn_api_received_at_no_odds_last_modified")
    report = evaluate_spread_ladder(
        game=fixture_game_state(),
        anchor=anchor,
        markets=fixture_markets(),
        config=NbaSpreadValidationConfig(
            min_net_edge=0.0,
            min_executable_size=0.0,
            require_source_timestamp=True,
        ),
        as_of=datetime(2026, 6, 4, 3, 55, tzinfo=UTC),
    )

    assert not [row for row in report.decisions if row.candidate]
    assert any(row.reason == "source_timestamp_missing" for row in report.decisions)


def test_markout_report_marks_fixture_candidate_to_bid() -> None:
    entry = evaluate_spread_ladder(
        game=fixture_game_state(),
        anchor=fixture_anchor(),
        markets=fixture_markets(),
        config=NbaSpreadValidationConfig(min_net_edge=0.015, paper_contracts=5),
        as_of=datetime(2026, 6, 4, 3, 55, tzinfo=UTC),
    )
    current_quotes = {quote.ticker: quote for quote in fixture_markets()}

    markout = markout_report_from_entry_report(
        entry.as_dict(),
        current_quotes=current_quotes,
        as_of=datetime(2026, 6, 4, 3, 57, tzinfo=UTC),
        entry_report_name="fixture",
    )

    assert markout.decision == "kill_or_defer:short_markout_negative"
    assert len(markout.rows) == 1
    assert markout.rows[0].markout_after_entry_fee is not None
    assert markout.rows[0].markout_after_entry_fee < 0


def test_settlement_report_computes_hold_to_settlement_pnl() -> None:
    entry = evaluate_spread_ladder(
        game=fixture_game_state(),
        anchor=fixture_anchor(),
        markets=fixture_markets(),
        config=NbaSpreadValidationConfig(min_net_edge=0.015, paper_contracts=5),
        as_of=datetime(2026, 6, 4, 3, 55, tzinfo=UTC),
    )
    final_game = replace(fixture_game_state(), home_score=101, away_score=96, completed=True, status_detail="Final")

    settlement = settlement_report_from_entry_report(
        entry.as_dict(),
        game=final_game,
        as_of=datetime(2026, 6, 4, 6, 0, tzinfo=UTC),
        entry_report_name="fixture",
    )

    assert settlement.decision == "paper_edge_supported:settlement_positive"
    assert len(settlement.rows) == 1
    assert settlement.rows[0].yes_settled is False
    assert settlement.rows[0].pnl_after_entry_fee is not None
    assert settlement.rows[0].pnl_after_entry_fee > 0
