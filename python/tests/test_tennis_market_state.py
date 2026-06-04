from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from eventcontracts.research.tennis_market_state import (
    SharpOddsSnapshot,
    TennisMarketCandidate,
    build_reference_valuation,
    evaluate_tennis_reference_candidate,
    external_signal_payload,
    is_tradeable_lifecycle,
    normalize_player_name,
)

NOW = datetime(2026, 6, 2, 12, 0, tzinfo=UTC)


def _candidate() -> TennisMarketCandidate:
    return TennisMarketCandidate(
        market_id="KXTENNIS-DEMO",
        player_1="Carlos Alcaraz",
        player_2="Novak Djokovic",
        scheduled_start=NOW + timedelta(hours=4),
        lifecycle_status="open",
        tournament="Roland Garros",
        surface="Clay",
    )


def _sharp() -> SharpOddsSnapshot:
    return SharpOddsSnapshot(
        market_id="KXTENNIS-DEMO",
        player_1="Carlos Alcaraz",
        player_2="Novak Djokovic",
        p1_decimal_odds=1.60,
        p2_decimal_odds=2.40,
        as_of=NOW,
        source="fixture-sharp",
    )


def test_normalize_player_name_is_accent_and_punctuation_insensitive() -> None:
    assert normalize_player_name("Carlos Alcaraz") == "carlos alcaraz"
    assert normalize_player_name("Cárlos  Alcaraz!") == "carlos alcaraz"


def test_tradeable_lifecycle_requires_open_upcoming_market() -> None:
    assert is_tradeable_lifecycle(_candidate(), now=NOW)
    assert not is_tradeable_lifecycle(
        TennisMarketCandidate(
            market_id="KXTENNIS-DEMO",
            player_1="Carlos Alcaraz",
            player_2="Novak Djokovic",
            scheduled_start=NOW - timedelta(minutes=1),
            lifecycle_status="open",
        ),
        now=NOW,
    )
    assert not is_tradeable_lifecycle(
        TennisMarketCandidate(
            market_id="KXTENNIS-DEMO",
            player_1="Carlos Alcaraz",
            player_2="Novak Djokovic",
            scheduled_start=NOW + timedelta(hours=4),
            lifecycle_status="closed",
        ),
        now=NOW,
    )


def test_sharp_odds_devig_and_reference_blend() -> None:
    sharp = _sharp()
    valuation = build_reference_valuation(_candidate(), sharp, model_probability=0.70, model_weight=0.25)

    # Raw implieds: 0.625 and 0.4167, normalized p1 = 0.6.
    assert sharp.p1_devig_probability == pytest.approx(0.60)
    assert valuation.p1_fair_probability == pytest.approx(0.625)
    assert valuation.model_sharp_disagreement == pytest.approx(0.10)
    assert valuation.odds_present
    assert valuation.feature_hash


def test_reference_decision_blocks_large_model_sharp_disagreement() -> None:
    valuation = build_reference_valuation(_candidate(), _sharp(), model_probability=0.95, model_weight=0.35)
    decision = evaluate_tennis_reference_candidate(
        valuation,
        yes_bid=0.60,
        yes_ask=0.62,
        max_model_sharp_disagreement=0.18,
    )

    assert not decision.candidate
    assert decision.reason == "model_sharp_disagreement_too_large"


def test_reference_decision_flags_fee_net_paper_candidate() -> None:
    valuation = build_reference_valuation(_candidate(), _sharp(), model_probability=0.70, model_weight=0.35)
    decision = evaluate_tennis_reference_candidate(
        valuation,
        yes_bid=0.54,
        yes_ask=0.56,
        min_net_edge=0.015,
        min_confidence=0.55,
    )

    assert decision.candidate
    assert decision.side == "YES"
    assert decision.net_edge is not None and decision.net_edge > 0.015


def test_external_signal_payload_matches_live_strategy_contract() -> None:
    valuation = build_reference_valuation(_candidate(), _sharp(), model_probability=0.70, model_weight=0.35)
    payload = external_signal_payload(valuation)

    assert payload["market_id"] == "KXTENNIS-DEMO"
    assert payload["player_1_win_probability"] == pytest.approx(valuation.p1_fair_probability)
    assert payload["model_confidence"] == pytest.approx(valuation.confidence)
    assert payload["odds_present"] is True
