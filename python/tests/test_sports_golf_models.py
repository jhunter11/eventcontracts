"""Sports predictive model coverage."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from eventcontracts.domain.ids import EventId
from eventcontracts.domain.models import InstrumentId, Venue
from eventcontracts.sports import (
    CutLineBracket,
    GolfCutLineMonteCarloModel,
    GolfPlayerSnapshot,
    GolfTournamentState,
    GolfWinnerMonteCarloModel,
    MarketPriceBar,
    bracket_map_from_mapping,
)

NOW = datetime(2026, 5, 25, 15, 0, tzinfo=UTC)


def test_golf_cut_line_model_returns_pmf_and_strategy_signals() -> None:
    state = _state()
    brackets = bracket_map_from_mapping({-1: "PGA-CUT-NEG1", 0: "PGA-CUT-EVEN", 1: "PGA-CUT-PLUS1"})
    model = GolfCutLineMonteCarloModel(simulations=500, seed=17)

    prediction = model.predict(state, brackets=brackets)
    repeat = model.predict(state, brackets=brackets)

    assert prediction == repeat
    assert abs(sum(item.probability for item in prediction.cut_line_probabilities) - 1.0) < 0.000001
    assert prediction.simulations == 500
    assert {item.market_id for item in prediction.cut_line_probabilities if item.market_id} == {
        "PGA-CUT-NEG1",
        "PGA-CUT-EVEN",
        "PGA-CUT-PLUS1",
    }

    signal = prediction.to_cut_line_signal()

    assert signal.source == "pga-tour"
    assert signal.payload["field_scoring_avg_delta_vs_par"] == prediction.field_scoring_avg_delta_vs_par
    assert signal.payload["afternoon_wind_forecast_mph"] == state.wind_forecast_mph
    assert "cut_line_pmf" in signal.payload
    assert "market_probabilities" in signal.payload


def test_golf_player_cut_probabilities_feed_player_strategy_payload_shape() -> None:
    state = _state()
    prediction = GolfCutLineMonteCarloModel(simulations=500, seed=3).predict(
        state,
        brackets=(CutLineBracket(cut_line=0, market_id="PGA-CUT-EVEN"),),
    )

    by_player = {item.player_id: item for item in prediction.player_cut_probabilities}
    signals = prediction.to_player_cut_signals()

    assert by_player["leader"].probability > by_player["struggler"].probability
    assert len(signals) == len(state.players)
    assert signals[0].source == "datagolf"
    assert {"player_id", "strokes_to_cut", "sg_approach", "sg_putting", "wave_weather_delta"}.issubset(
        signals[0].payload
    )


def test_golf_winner_model_returns_dispersion_distribution() -> None:
    prediction = GolfWinnerMonteCarloModel(simulations=600, seed=11).predict(_state())

    assert abs(sum(item.probability for item in prediction.player_win_probabilities) - 1.0) < 0.000001
    assert prediction.probability_for_player("leader") > prediction.probability_for_player("struggler")
    assert 1.0 <= prediction.effective_contenders <= len(prediction.player_win_probabilities)

    signals = prediction.to_winner_signals()

    assert signals
    assert signals[0].source == "datagolf"
    assert "effective_contenders" in signals[0].payload
    assert "win_probability" in signals[0].payload


def test_market_price_bar_converts_to_quote_event_for_bar_only_backtests() -> None:
    instrument = InstrumentId(venue=Venue.KALSHI, market_id="PGA-CUT-EVEN")
    bar = MarketPriceBar(
        instrument_id=instrument,
        timestamp=NOW,
        open=Decimal("0.40"),
        high=Decimal("0.45"),
        low=Decimal("0.39"),
        close=Decimal("0.42"),
        volume=Decimal("250"),
    )

    event = bar.to_quote_event(event_id=EventId("bar-1"), half_spread=Decimal("0.01"))

    assert event.event_id == EventId("bar-1")
    assert event.quote.instrument_id == instrument
    assert event.quote.bid is not None
    assert event.quote.ask is not None
    assert event.quote.bid.price == Decimal("0.41")
    assert event.quote.ask.price == Decimal("0.43")


def _state() -> GolfTournamentState:
    return GolfTournamentState(
        tournament_id="pga-demo",
        as_of=NOW,
        cut_rule_size=3,
        cut_holes=36,
        tournament_holes=72,
        wind_forecast_mph=13.0,
        course_baseline_cut_line=0.0,
        players=(
            GolfPlayerSnapshot(
                player_id="leader",
                score_to_par=-4.0,
                holes_completed=18,
                sg_approach=2.0,
                sg_putting=0.2,
                baseline_score_to_par_per_hole=-0.02,
                wave_weather_delta_per_round=0.1,
                market_id="PGA-LEADER-CUT",
            ),
            GolfPlayerSnapshot(
                player_id="steady",
                score_to_par=-1.0,
                holes_completed=18,
                sg_approach=0.5,
                sg_putting=0.1,
                baseline_score_to_par_per_hole=0.0,
                wave_weather_delta_per_round=0.0,
                market_id="PGA-STEADY-CUT",
            ),
            GolfPlayerSnapshot(
                player_id="bubble",
                score_to_par=1.0,
                holes_completed=18,
                sg_approach=0.7,
                sg_putting=-1.0,
                baseline_score_to_par_per_hole=0.01,
                wave_weather_delta_per_round=0.2,
                market_id="PGA-BUBBLE-CUT",
            ),
            GolfPlayerSnapshot(
                player_id="volatile",
                score_to_par=2.0,
                holes_completed=18,
                sg_approach=1.5,
                sg_putting=-2.0,
                baseline_score_to_par_per_hole=0.02,
                wave_weather_delta_per_round=0.3,
                market_id="PGA-VOLATILE-CUT",
            ),
            GolfPlayerSnapshot(
                player_id="struggler",
                score_to_par=5.0,
                holes_completed=18,
                sg_approach=-1.5,
                sg_putting=0.4,
                baseline_score_to_par_per_hole=0.04,
                wave_weather_delta_per_round=0.3,
                market_id="PGA-STRUGGLER-CUT",
            ),
        ),
    )
