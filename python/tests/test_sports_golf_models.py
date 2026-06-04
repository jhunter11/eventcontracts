"""Sports predictive model coverage."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from eventcontracts.domain.decisions import PlaceOrder
from eventcontracts.domain.events import QuoteEvent
from eventcontracts.domain.ids import EventId
from eventcontracts.domain.models import (
    InstrumentId,
    OrderBookLevel,
    OutcomeSide,
    Quote,
    Venue,
)
from eventcontracts.domain.orders import OrderSide
from eventcontracts.domain.spec import EventSubscription, StrategySpec
from eventcontracts.sports import (
    CutLineBracket,
    GolfCutLineMonteCarloModel,
    GolfFirstRoundMonteCarloModel,
    GolfPlayerSnapshot,
    GolfTournamentState,
    GolfWinnerMonteCarloModel,
    MarketPriceBar,
    TeeTimeWaveForecast,
    WaveAssignment,
    bracket_map_from_mapping,
)
from eventcontracts.strategy.registry import create_from_spec, load_entry_points

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


# --- first-round (pre-round) model ------------------------------------------


class _Ctx:
    @property
    def now(self) -> datetime:
        return NOW


def _player(
    player_id: str,
    *,
    sg_approach: float = 0.0,
    sg_putting: float = 0.0,
    market_id: str | None = None,
) -> GolfPlayerSnapshot:
    return GolfPlayerSnapshot(
        player_id=player_id,
        score_to_par=0.0,
        holes_completed=0,
        sg_approach=sg_approach,
        sg_putting=sg_putting,
        baseline_score_to_par_per_hole=0.0,
        market_id=market_id,
    )


def _frl_state(players: list[GolfPlayerSnapshot]) -> GolfTournamentState:
    return GolfTournamentState(
        tournament_id="pga-frl-demo",
        as_of=NOW,
        players=tuple(players),
        wind_forecast_mph=0.0,
    )


def _quote(market_id: str, bid: str, ask: str) -> QuoteEvent:
    instrument = InstrumentId(venue=Venue.KALSHI, market_id=market_id)
    return QuoteEvent(
        event_id=EventId(f"q-{market_id}"),
        quote=Quote(
            instrument_id=instrument,
            side=OutcomeSide.YES,
            bid=OrderBookLevel(price=Decimal(bid), quantity=Decimal("100")),
            ask=OrderBookLevel(price=Decimal(ask), quantity=Decimal("100")),
            exchange_ts=None,
            received_at=NOW,
        ),
    )


def test_first_round_frl_probabilities_are_deterministic_and_sum_to_one() -> None:
    state = _frl_state([_player("a", sg_approach=1.5), _player("b"), _player("c", sg_approach=-1.0)])
    model = GolfFirstRoundMonteCarloModel(simulations=4000, seed=7)

    prediction = model.predict(state)
    repeat = model.predict(state)

    assert prediction == repeat
    assert abs(sum(p.frl_probability for p in prediction.player_probabilities) - 1.0) < 1e-9
    by_player = {p.player_id: p.frl_probability for p in prediction.player_probabilities}
    assert by_player["a"] > by_player["b"] > by_player["c"]


def test_first_round_top_n_probability_is_one_when_field_fits_in_top_n() -> None:
    state = _frl_state([_player("a"), _player("b"), _player("c")])
    prediction = GolfFirstRoundMonteCarloModel(simulations=1000, seed=1, top_n=5).predict(state)

    for p in prediction.player_probabilities:
        assert p.top_n_probability == 1.0  # 3-player field is entirely inside top-5


def test_first_round_wave_asymmetry_favors_the_calm_wave() -> None:
    state = _frl_state([_player("am_player", market_id="FRL-AM"), _player("pm_player", market_id="FRL-PM")])
    assignments = (
        WaveAssignment("am_player", "am", market_id="FRL-AM"),
        WaveAssignment("pm_player", "pm", market_id="FRL-PM"),
    )
    calm = TeeTimeWaveForecast(am_wave_wind_mph=10.0, pm_wave_wind_mph=10.0, assignments=assignments)
    blown_pm = TeeTimeWaveForecast(am_wave_wind_mph=8.0, pm_wave_wind_mph=28.0, assignments=assignments)
    model = GolfFirstRoundMonteCarloModel(simulations=6000, seed=5)

    calm_am = model.predict(state, forecast=calm).frl_probability_for("am_player")
    blown = model.predict(state, forecast=blown_pm)
    blown_am = blown.frl_probability_for("am_player")

    assert abs(calm_am - 0.5) < 0.06  # equal winds -> ~symmetric
    assert blown_am > 0.58  # AM wave gains a structural edge when PM is blown out
    assert blown_am > blown.frl_probability_for("pm_player")


def test_first_round_round_score_cdf_is_monotone_and_centered() -> None:
    state = _frl_state([_player("a", sg_approach=2.0), _player("b")])
    prediction = GolfFirstRoundMonteCarloModel(simulations=500, seed=4).predict(state)
    mean_a = next(p.projected_round_score_to_par for p in prediction.player_probabilities if p.player_id == "a")

    lo = prediction.round_score_probability_at_most("a", mean_a - 3)
    mid = prediction.round_score_probability_at_most("a", mean_a)
    hi = prediction.round_score_probability_at_most("a", mean_a + 3)

    assert lo < mid < hi
    assert abs(mid - 0.5) < 1e-9


def test_first_round_signal_matches_frl_strategy_schema() -> None:
    state = _frl_state([_player("am_player", market_id="FRL-AM"), _player("pm_player", market_id="FRL-PM")])
    forecast = TeeTimeWaveForecast(
        am_wave_wind_mph=8.0,
        pm_wave_wind_mph=28.0,
        assignments=(
            WaveAssignment("am_player", "am", wind_sg_baseline=0.3, market_id="FRL-AM"),
            WaveAssignment("pm_player", "pm", market_id="FRL-PM"),
        ),
    )
    prediction = GolfFirstRoundMonteCarloModel(simulations=2000, seed=2).predict(state, forecast=forecast)

    signal = prediction.to_frl_signal(forecast)

    assert signal.source == "weather_tee_combined"
    assert signal.payload["am_wave_wind_mph"] == 8.0
    assert signal.payload["pm_wave_wind_mph"] == 28.0
    entries = {entry["player_id"]: entry for entry in signal.payload["players"]}
    assert entries["am_player"]["wave"] == "am"
    assert entries["am_player"]["wind_sg_baseline"] == 0.3
    assert "model_frl_probability" in entries["am_player"]


def test_first_round_signal_drives_frl_weather_arb_strategy() -> None:
    load_entry_points()
    state = _frl_state([_player("am_player", market_id="FRL-AM"), _player("pm_player", market_id="FRL-PM")])
    forecast = TeeTimeWaveForecast(
        am_wave_wind_mph=8.0,
        pm_wave_wind_mph=28.0,
        assignments=(
            WaveAssignment("am_player", "am", market_id="FRL-AM"),
            WaveAssignment("pm_player", "pm", market_id="FRL-PM"),
        ),
    )
    signal = GolfFirstRoundMonteCarloModel(simulations=2000, seed=2).predict(state, forecast=forecast).to_frl_signal(
        forecast
    )
    spec = StrategySpec(
        strategy_id="frl-e2e",
        name="sports_frl_weather_arb",
        version="1",
        description="producer->strategy e2e",
        feature_schema_id="sports_frl_weather_features",
        subscription=EventSubscription(
            venues=("kalshi",),
            instrument_patterns=("FRL-*",),
            event_kinds=("external", "quote"),
            external_sources=("weather_tee_combined",),
        ),
        parameters={
            "player_market_map": "am_player:FRL-AM;pm_player:FRL-PM",
            "signal_source": "weather_tee_combined",
            "field_size": "156",
            "min_edge_bps": "100",
            "size": "5",
            "venue": "kalshi",
        },
    )
    strat = create_from_spec(spec)
    ctx = _Ctx()

    strat.on_event(_quote("FRL-AM", "0.010", "0.014"), ctx)  # mid 0.012, cheap AM player
    strat.on_event(_quote("FRL-PM", "0.040", "0.060"), ctx)  # mid 0.050
    decisions = strat.on_event(signal, ctx)

    orders = [d for d in decisions if isinstance(d, PlaceOrder)]
    assert orders, "producer signal should drive at least one FRL order"
    am_orders = [o for o in orders if o.instrument_id.market_id == "FRL-AM"]
    assert am_orders, "the favored AM-wave player should be a tradable edge"
    assert am_orders[0].outcome_side is OutcomeSide.YES
    assert am_orders[0].order_side is OrderSide.BUY
