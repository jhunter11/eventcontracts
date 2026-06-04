"""Golf prediction primitives for non-latency-sensitive sports edges.

The models in this module intentionally work from coarse tournament state and
1-minute market bars. They do not require live order-book depth, so they are
usable on the current laptop data footprint while the tick/LOB cache is built
elsewhere.

Two predictive surfaces are provided:

* ``GolfCutLineMonteCarloModel`` simulates the 36-hole Top-N-and-ties cut line,
  returning an integer cut-line PMF plus per-player make-cut probabilities.
* ``GolfWinnerMonteCarloModel`` simulates the remaining 72-hole distribution and
  returns win probabilities and a concentration/dispersion summary.

Both models are deterministic for a fixed seed and state. Signal builders emit
``ExternalSignalEvent`` payloads compatible with the current sports strategies:
``sports_cut_line_shifter`` consumes aggregate cut-line fields and
``sports_player_cut_lgbm`` consumes per-player SG/cut fields.
"""

from __future__ import annotations

import math
import random
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from eventcontracts.domain.events import EventProvenance, ExternalSignalEvent, QuoteEvent
from eventcontracts.domain.ids import EventId
from eventcontracts.domain.models import InstrumentId, OrderBookLevel, OutcomeSide, Quote
from eventcontracts.domain.validation import require_aware_datetime, require_non_empty, require_probability_decimal


@dataclass(frozen=True)
class GolfPlayerSnapshot:
    """Current tournament state for one player.

    ``score_to_par`` and ``holes_completed`` are sufficient for coarse
    simulations. SG fields and weather deltas refine the forecast when available
    but default to neutral values, which keeps the model usable with sparse REST
    feeds or manually curated CSVs.
    """

    player_id: str
    score_to_par: float
    holes_completed: int
    sg_approach: float = 0.0
    sg_putting: float = 0.0
    baseline_score_to_par_per_hole: float = 0.0
    wave_weather_delta_per_round: float = 0.0
    market_id: str | None = None
    injury_strokes_per_round: float = 0.0
    rest_fatigue_strokes_per_round: float = 0.0
    caddie_absence_strokes_per_round: float = 0.0

    def __post_init__(self) -> None:
        require_non_empty(self.player_id, "player_id")
        if self.holes_completed < 0:
            raise ValueError("holes_completed must be >= 0")
        if self.market_id is not None:
            require_non_empty(self.market_id, "market_id")
        for name, value in (
            ("injury_strokes_per_round", self.injury_strokes_per_round),
            ("rest_fatigue_strokes_per_round", self.rest_fatigue_strokes_per_round),
            ("caddie_absence_strokes_per_round", self.caddie_absence_strokes_per_round),
        ):
            if not math.isfinite(value):
                raise ValueError(f"{name} must be finite")


@dataclass(frozen=True)
class CutLineBracket:
    """One tradable integer cut-line bracket."""

    cut_line: int
    market_id: str

    def __post_init__(self) -> None:
        require_non_empty(self.market_id, "market_id")


@dataclass(frozen=True)
class CutLineProbability:
    """Probability mass for one integer cut-line value."""

    cut_line: int
    probability: float
    market_id: str | None = None

    def __post_init__(self) -> None:
        _require_probability_float(self.probability, "probability")
        if self.market_id is not None:
            require_non_empty(self.market_id, "market_id")


@dataclass(frozen=True)
class PlayerCutPrediction:
    """Projected make-cut probability for one player."""

    player_id: str
    probability: float
    projected_strokes_to_cut: float
    sg_approach: float
    sg_putting: float
    wave_weather_delta_per_round: float
    market_id: str | None = None

    def __post_init__(self) -> None:
        require_non_empty(self.player_id, "player_id")
        _require_probability_float(self.probability, "probability")
        if self.market_id is not None:
            require_non_empty(self.market_id, "market_id")


@dataclass(frozen=True)
class PlayerWinPrediction:
    """Projected tournament-win probability for one player."""

    player_id: str
    probability: float
    projected_finish_score_to_par: float
    market_id: str | None = None

    def __post_init__(self) -> None:
        require_non_empty(self.player_id, "player_id")
        _require_probability_float(self.probability, "probability")
        if self.market_id is not None:
            require_non_empty(self.market_id, "market_id")


@dataclass(frozen=True)
class GolfTournamentState:
    """State needed to project cut-line and winner distributions."""

    tournament_id: str
    as_of: datetime
    players: tuple[GolfPlayerSnapshot, ...]
    cut_rule_size: int = 65
    cut_holes: int = 36
    tournament_holes: int = 72
    wind_forecast_mph: float = 0.0
    course_baseline_cut_line: float = 0.0

    def __post_init__(self) -> None:
        require_non_empty(self.tournament_id, "tournament_id")
        require_aware_datetime(self.as_of, "as_of")
        if not self.players:
            raise ValueError("players must not be empty")
        if self.cut_rule_size <= 0:
            raise ValueError("cut_rule_size must be > 0")
        if self.cut_holes <= 0:
            raise ValueError("cut_holes must be > 0")
        if self.tournament_holes < self.cut_holes:
            raise ValueError("tournament_holes must be >= cut_holes")

    @property
    def field_scoring_avg_delta_vs_par(self) -> float:
        """Average current score normalized to 18 holes."""

        deltas: list[float] = []
        for player in self.players:
            completed = max(player.holes_completed, 1)
            deltas.append(player.score_to_par / (completed / 18.0))
        return sum(deltas) / len(deltas)

    @property
    def top_cut_current_score(self) -> float:
        """Current score at the cut-rule rank, before ties."""

        scores = sorted(player.score_to_par for player in self.players)
        index = min(self.cut_rule_size, len(scores)) - 1
        return scores[index]


@dataclass(frozen=True)
class GolfTournamentPrediction:
    """Cut-line PMF plus per-player make-cut probabilities."""

    tournament_id: str
    as_of: datetime
    cut_line_probabilities: tuple[CutLineProbability, ...]
    player_cut_probabilities: tuple[PlayerCutPrediction, ...]
    field_scoring_avg_delta_vs_par: float
    afternoon_wind_forecast_mph: float
    top_65_current_score: float
    simulations: int

    def __post_init__(self) -> None:
        require_non_empty(self.tournament_id, "tournament_id")
        require_aware_datetime(self.as_of, "as_of")
        if not self.cut_line_probabilities:
            raise ValueError("cut_line_probabilities must not be empty")
        if self.simulations <= 0:
            raise ValueError("simulations must be > 0")

    @property
    def expected_cut_line(self) -> float:
        return sum(item.cut_line * item.probability for item in self.cut_line_probabilities)

    def probability_for_cut_line(self, cut_line: int) -> float:
        for item in self.cut_line_probabilities:
            if item.cut_line == cut_line:
                return item.probability
        return 0.0

    def to_cut_line_signal(
        self,
        *,
        source: str = "pga-tour",
        schema_version: str = "sports-golf-cut-line-prediction-v1",
        received_at: datetime | None = None,
    ) -> ExternalSignalEvent:
        """Build a signal consumed by ``sports_cut_line_shifter``."""

        event_time = received_at or self.as_of
        probabilities = {str(item.cut_line): item.probability for item in self.cut_line_probabilities}
        market_probabilities = {
            item.market_id: item.probability
            for item in self.cut_line_probabilities
            if item.market_id is not None
        }
        return ExternalSignalEvent(
            event_id=EventId(f"sports-golf-cut-line-{self.tournament_id}-{_event_ts(self.as_of)}"),
            source=source,
            exchange_ts=self.as_of,
            received_at=event_time,
            schema_version=schema_version,
            payload={
                "tournament_id": self.tournament_id,
                "field_scoring_avg_delta_vs_par": self.field_scoring_avg_delta_vs_par,
                "afternoon_wind_forecast_mph": self.afternoon_wind_forecast_mph,
                "top_65_current_score": self.top_65_current_score,
                "expected_cut_line": self.expected_cut_line,
                "cut_line_pmf": probabilities,
                "market_probabilities": market_probabilities,
                "simulations": self.simulations,
            },
            provenance=_sports_model_provenance(source=source, schema_version=schema_version),
        )

    def to_player_cut_signals(
        self,
        *,
        source: str = "datagolf",
        schema_version: str = "sports-golf-player-cut-prediction-v1",
        received_at: datetime | None = None,
    ) -> tuple[ExternalSignalEvent, ...]:
        """Build per-player signals consumed by ``sports_player_cut_lgbm``."""

        event_time = received_at or self.as_of
        signals: list[ExternalSignalEvent] = []
        for prediction in self.player_cut_probabilities:
            signals.append(
                ExternalSignalEvent(
                    event_id=EventId(
                        f"sports-golf-player-cut-{self.tournament_id}-{prediction.player_id}-{_event_ts(self.as_of)}"
                    ),
                    source=source,
                    exchange_ts=self.as_of,
                    received_at=event_time,
                    schema_version=schema_version,
                    payload={
                        "tournament_id": self.tournament_id,
                        "player_id": prediction.player_id,
                        "make_cut_probability": prediction.probability,
                        "strokes_to_cut": prediction.projected_strokes_to_cut,
                        "sg_approach": prediction.sg_approach,
                        "sg_putting": prediction.sg_putting,
                        "wave_weather_delta": prediction.wave_weather_delta_per_round,
                        "market_id": prediction.market_id,
                    },
                    provenance=_sports_model_provenance(source=source, schema_version=schema_version),
                )
            )
        return tuple(signals)


@dataclass(frozen=True)
class GolfWinnerPrediction:
    """Tournament winner distribution."""

    tournament_id: str
    as_of: datetime
    player_win_probabilities: tuple[PlayerWinPrediction, ...]
    simulations: int

    def __post_init__(self) -> None:
        require_non_empty(self.tournament_id, "tournament_id")
        require_aware_datetime(self.as_of, "as_of")
        if not self.player_win_probabilities:
            raise ValueError("player_win_probabilities must not be empty")
        if self.simulations <= 0:
            raise ValueError("simulations must be > 0")

    @property
    def herfindahl(self) -> float:
        """Concentration measure; lower values mean wider winner dispersion."""

        return sum(item.probability * item.probability for item in self.player_win_probabilities)

    @property
    def effective_contenders(self) -> float:
        if self.herfindahl <= 0.0:
            return 0.0
        return 1.0 / self.herfindahl

    def probability_for_player(self, player_id: str) -> float:
        for item in self.player_win_probabilities:
            if item.player_id == player_id:
                return item.probability
        return 0.0

    def to_winner_signals(
        self,
        *,
        source: str = "datagolf",
        schema_version: str = "sports-golf-winner-prediction-v1",
        received_at: datetime | None = None,
    ) -> tuple[ExternalSignalEvent, ...]:
        event_time = received_at or self.as_of
        signals: list[ExternalSignalEvent] = []
        for prediction in self.player_win_probabilities:
            signals.append(
                ExternalSignalEvent(
                    event_id=EventId(
                        f"sports-golf-winner-{self.tournament_id}-{prediction.player_id}-{_event_ts(self.as_of)}"
                    ),
                    source=source,
                    exchange_ts=self.as_of,
                    received_at=event_time,
                    schema_version=schema_version,
                    payload={
                        "tournament_id": self.tournament_id,
                        "player_id": prediction.player_id,
                        "win_probability": prediction.probability,
                        "projected_finish_score_to_par": prediction.projected_finish_score_to_par,
                        "effective_contenders": self.effective_contenders,
                        "herfindahl": self.herfindahl,
                        "market_id": prediction.market_id,
                    },
                    provenance=_sports_model_provenance(source=source, schema_version=schema_version),
                )
            )
        return tuple(signals)


@dataclass(frozen=True)
class MarketPriceBar:
    """OHLCV-style one-minute market bar that can stand in for quote data."""

    instrument_id: InstrumentId
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        require_aware_datetime(self.timestamp, "timestamp")
        for name, value in (
            ("open", self.open),
            ("high", self.high),
            ("low", self.low),
            ("close", self.close),
        ):
            require_probability_decimal(value, name)
        if self.volume < 0:
            raise ValueError("volume must be >= 0")

    def to_quote_event(
        self,
        *,
        event_id: EventId | None = None,
        half_spread: Decimal = Decimal("0.005"),
    ) -> QuoteEvent:
        """Convert a 1-minute close into a synthetic tight quote.

        This keeps bar-only backtests on the same strategy path as live quote
        data. The spread is configurable so slippage/fill assumptions can be
        stressed without needing full LOB replay.
        """

        if half_spread < 0:
            raise ValueError("half_spread must be >= 0")
        bid = max(Decimal("0.01"), self.close - half_spread)
        ask = min(Decimal("0.99"), self.close + half_spread)
        return QuoteEvent(
            event_id=event_id or EventId(f"bar-quote-{self.instrument_id.market_id}-{_event_ts(self.timestamp)}"),
            quote=Quote(
                instrument_id=self.instrument_id,
                side=OutcomeSide.YES,
                bid=OrderBookLevel(price=bid, quantity=max(self.volume, Decimal("1"))),
                ask=OrderBookLevel(price=ask, quantity=max(self.volume, Decimal("1"))),
                exchange_ts=self.timestamp,
                received_at=self.timestamp,
            ),
            provenance=EventProvenance(
                source="market-price-bars",
                channel="synthetic_quote",
                schema_version="market-price-bar-v1",
                venue=self.instrument_id.venue,
            ),
        )


class GolfCutLineMonteCarloModel:
    """Monte Carlo cut-line and player make-cut model."""

    def __init__(
        self,
        *,
        simulations: int = 5000,
        seed: int = 0,
        remaining_hole_volatility: float = 0.32,
        approach_weight_per_sg: float = 0.018,
        putting_weight_per_sg: float = 0.006,
        weather_weight_per_round: float = 1.0,
    ) -> None:
        if simulations <= 0:
            raise ValueError("simulations must be > 0")
        if remaining_hole_volatility <= 0:
            raise ValueError("remaining_hole_volatility must be > 0")
        self.simulations = simulations
        self.seed = seed
        self.remaining_hole_volatility = remaining_hole_volatility
        self.approach_weight_per_sg = approach_weight_per_sg
        self.putting_weight_per_sg = putting_weight_per_sg
        self.weather_weight_per_round = weather_weight_per_round

    def predict(
        self,
        state: GolfTournamentState,
        *,
        brackets: Sequence[CutLineBracket] = (),
    ) -> GolfTournamentPrediction:
        bracket_by_cut = {bracket.cut_line: bracket.market_id for bracket in brackets}
        rng = random.Random(self.seed)
        cut_counts: Counter[int] = Counter()
        make_counts: Counter[str] = Counter()
        score_sum: defaultdict[str, float] = defaultdict(float)

        for _ in range(self.simulations):
            final_scores = self._simulate_scores_to_hole(state, state.cut_holes, rng)
            ranked_scores = sorted(score for _player_id, score in final_scores)
            cut_index = min(state.cut_rule_size, len(ranked_scores)) - 1
            cut_score = ranked_scores[cut_index]
            integer_cut = int(round(cut_score))
            cut_counts[integer_cut] += 1
            for player_id, score in final_scores:
                score_sum[player_id] += score
                if score <= cut_score:
                    make_counts[player_id] += 1

        cut_lines = sorted(set(cut_counts).union(bracket_by_cut))
        cut_probabilities = tuple(
            CutLineProbability(
                cut_line=cut_line,
                probability=cut_counts[cut_line] / self.simulations,
                market_id=bracket_by_cut.get(cut_line),
            )
            for cut_line in cut_lines
        )
        expected_cut = sum(item.cut_line * item.probability for item in cut_probabilities)
        player_predictions = tuple(
            PlayerCutPrediction(
                player_id=player.player_id,
                probability=make_counts[player.player_id] / self.simulations,
                projected_strokes_to_cut=(score_sum[player.player_id] / self.simulations) - expected_cut,
                sg_approach=player.sg_approach,
                sg_putting=player.sg_putting,
                wave_weather_delta_per_round=player.wave_weather_delta_per_round,
                market_id=player.market_id,
            )
            for player in sorted(state.players, key=lambda p: p.player_id)
        )
        return GolfTournamentPrediction(
            tournament_id=state.tournament_id,
            as_of=state.as_of,
            cut_line_probabilities=cut_probabilities,
            player_cut_probabilities=player_predictions,
            field_scoring_avg_delta_vs_par=state.field_scoring_avg_delta_vs_par,
            afternoon_wind_forecast_mph=state.wind_forecast_mph,
            top_65_current_score=state.top_cut_current_score,
            simulations=self.simulations,
        )

    def _simulate_scores_to_hole(
        self,
        state: GolfTournamentState,
        target_holes: int,
        rng: random.Random,
    ) -> list[tuple[str, float]]:
        scores: list[tuple[str, float]] = []
        for player in state.players:
            remaining_holes = max(target_holes - player.holes_completed, 0)
            projected = player.score_to_par
            if remaining_holes > 0:
                mean = self._remaining_score_mean(player, remaining_holes)
                stddev = self.remaining_hole_volatility * math.sqrt(remaining_holes)
                projected += rng.gauss(mean, stddev)
            scores.append((player.player_id, projected))
        return scores

    def _remaining_score_mean(self, player: GolfPlayerSnapshot, remaining_holes: int) -> float:
        weather_per_hole = (player.wave_weather_delta_per_round * self.weather_weight_per_round) / 18.0
        skill_per_hole = -(player.sg_approach * self.approach_weight_per_sg)
        skill_per_hole -= player.sg_putting * self.putting_weight_per_sg
        context_per_hole = _availability_context_strokes_per_round(player) / 18.0
        return remaining_holes * (
            player.baseline_score_to_par_per_hole + weather_per_hole + skill_per_hole + context_per_hole
        )


class GolfWinnerMonteCarloModel:
    """Monte Carlo 72-hole winner distribution model."""

    def __init__(
        self,
        *,
        simulations: int = 10000,
        seed: int = 0,
        remaining_hole_volatility: float = 0.34,
        approach_weight_per_sg: float = 0.018,
        putting_weight_per_sg: float = 0.006,
        weather_weight_per_round: float = 1.0,
    ) -> None:
        if simulations <= 0:
            raise ValueError("simulations must be > 0")
        if remaining_hole_volatility <= 0:
            raise ValueError("remaining_hole_volatility must be > 0")
        self.simulations = simulations
        self.seed = seed
        self.remaining_hole_volatility = remaining_hole_volatility
        self.approach_weight_per_sg = approach_weight_per_sg
        self.putting_weight_per_sg = putting_weight_per_sg
        self.weather_weight_per_round = weather_weight_per_round

    def predict(self, state: GolfTournamentState) -> GolfWinnerPrediction:
        rng = random.Random(self.seed)
        win_counts: Counter[str] = Counter()
        score_sum: defaultdict[str, float] = defaultdict(float)

        for _ in range(self.simulations):
            final_scores = self._simulate_scores_to_hole(state, state.tournament_holes, rng)
            winner_id, _winner_score = min(final_scores, key=lambda item: (item[1], item[0]))
            win_counts[winner_id] += 1
            for player_id, score in final_scores:
                score_sum[player_id] += score

        predictions = tuple(
            PlayerWinPrediction(
                player_id=player.player_id,
                probability=win_counts[player.player_id] / self.simulations,
                projected_finish_score_to_par=score_sum[player.player_id] / self.simulations,
                market_id=player.market_id,
            )
            for player in sorted(state.players, key=lambda p: p.player_id)
        )
        return GolfWinnerPrediction(
            tournament_id=state.tournament_id,
            as_of=state.as_of,
            player_win_probabilities=predictions,
            simulations=self.simulations,
        )

    def _simulate_scores_to_hole(
        self,
        state: GolfTournamentState,
        target_holes: int,
        rng: random.Random,
    ) -> list[tuple[str, float]]:
        scores: list[tuple[str, float]] = []
        for player in state.players:
            remaining_holes = max(target_holes - player.holes_completed, 0)
            projected = player.score_to_par
            if remaining_holes > 0:
                mean = self._remaining_score_mean(player, remaining_holes)
                stddev = self.remaining_hole_volatility * math.sqrt(remaining_holes)
                projected += rng.gauss(mean, stddev)
            scores.append((player.player_id, projected))
        return scores

    def _remaining_score_mean(self, player: GolfPlayerSnapshot, remaining_holes: int) -> float:
        weather_per_hole = (player.wave_weather_delta_per_round * self.weather_weight_per_round) / 18.0
        skill_per_hole = -(player.sg_approach * self.approach_weight_per_sg)
        skill_per_hole -= player.sg_putting * self.putting_weight_per_sg
        context_per_hole = _availability_context_strokes_per_round(player) / 18.0
        return remaining_holes * (
            player.baseline_score_to_par_per_hole + weather_per_hole + skill_per_hole + context_per_hole
        )


def bracket_map_from_mapping(mapping: Mapping[int, str]) -> tuple[CutLineBracket, ...]:
    """Build sorted cut-line brackets from ``{cut_line: market_id}``."""

    return tuple(
        CutLineBracket(cut_line=cut_line, market_id=market_id)
        for cut_line, market_id in sorted(mapping.items())
    )


def _sports_model_provenance(*, source: str, schema_version: str) -> EventProvenance:
    return EventProvenance(
        source=source,
        channel="predictive_model",
        schema_version=schema_version,
        metadata={"model_family": "sports_golf_monte_carlo"},
    )


def _availability_context_strokes_per_round(player: GolfPlayerSnapshot) -> float:
    return (
        player.injury_strokes_per_round
        + player.rest_fatigue_strokes_per_round
        + player.caddie_absence_strokes_per_round
    )


def _event_ts(value: datetime) -> str:
    return value.isoformat().replace("+", "p").replace(":", "").replace("-", "").replace(".", "")


def _require_probability_float(value: float, name: str) -> None:
    if not math.isfinite(value) or value < 0.0 or value > 1.0:
        raise ValueError(f"{name} must be finite and in [0, 1]")


def _payload_float(payload: Mapping[str, Any], name: str, default: float = 0.0) -> float:
    raw = payload.get(name)
    if raw is None:
        return default
    return float(raw)
