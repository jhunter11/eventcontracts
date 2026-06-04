"""First-round (pre-round) golf prediction.

Where :mod:`eventcontracts.sports.golf` projects the cut line (36 holes) and the
winner (72 holes), this module models the *single opening round*: first-round
leader (FRL), top-N after round 1, and the per-player round-score distribution.

Edge thesis (``docs/golf-strategy-specs.md`` #3): retail FRL / "top-5 after R1"
markets are priced off long-term baseline skill and underprice the structural
stroke advantage of the favorable tee-time *wave* when a weather front makes one
wave (AM or PM) materially windier. Two players of equal skill do not have equal
FRL odds if one plays the calm wave and the other plays a 25 mph afternoon. The
books are slow to re-price that asymmetry; the distribution is the edge, not
latency.

This is the producer that ``sports_frl_weather_arb`` was missing:
:meth:`GolfFirstRoundPrediction.to_frl_signal` emits the exact
``weather_tee_combined`` payload that strategy consumes, enriched with the
model's simulated FRL probability so a model-mode sleeve can use a principled
fair value instead of the strategy's linear rules-mode boost.

Design notes:

* FRL / top-N come from a joint Monte Carlo of the field's round score, with
  scores **rounded to integers** so ties (dead-heats, very common in a golf R1)
  are modelled and split. FRL probabilities therefore sum to 1.0 by construction.
* The per-player round-score percentiles and CDF are computed analytically from
  the Gaussian round model (``statistics.NormalDist``) -- exact, no sampling
  noise, and enough for round over/under markets.
* Deterministic for a fixed seed + state. Pure stdlib; runnable on the current
  laptop footprint like the other research models.
"""

from __future__ import annotations

import random
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from statistics import NormalDist

from eventcontracts.domain.events import EventProvenance, ExternalSignalEvent
from eventcontracts.domain.ids import EventId
from eventcontracts.domain.validation import require_aware_datetime, require_non_empty
from eventcontracts.sports.golf import GolfPlayerSnapshot, GolfTournamentState

_WAVES = ("am", "pm")


def _require_prob(value: float, name: str) -> None:
    if not (0.0 <= value <= 1.0):
        raise ValueError(f"{name} must be in [0, 1]")


@dataclass(frozen=True)
class WaveAssignment:
    """Tee-time wave + wind sensitivity for one player.

    ``wind_sg_baseline`` is the player's strokes-gained-vs-field advantage in
    wind (positive = handles wind better, i.e. loses fewer strokes). It directly
    offsets the wave's wind penalty.
    """

    player_id: str
    wave: str
    wind_sg_baseline: float = 0.0
    market_id: str | None = None

    def __post_init__(self) -> None:
        require_non_empty(self.player_id, "player_id")
        if self.wave not in _WAVES:
            raise ValueError("wave must be 'am' or 'pm'")
        if self.market_id is not None:
            require_non_empty(self.market_id, "market_id")


@dataclass(frozen=True)
class TeeTimeWaveForecast:
    """Final tee-time waves + wind forecast, aligned (typically ~05:00 Thursday).

    ``penalty_for`` returns the round-stroke delta a player's wave imposes:
    ``own_wave_wind * base_strokes_per_mph - wind_sg_baseline``. Positive means
    the wave costs strokes (worse score); the AM/PM gap is the tradable edge.
    """

    am_wave_wind_mph: float
    pm_wave_wind_mph: float
    assignments: tuple[WaveAssignment, ...]
    base_wind_penalty_strokes_per_mph: float = 0.06

    def __post_init__(self) -> None:
        if self.am_wave_wind_mph < 0 or self.pm_wave_wind_mph < 0:
            raise ValueError("wind speeds must be >= 0")
        if not self.assignments:
            raise ValueError("assignments must not be empty")
        if self.base_wind_penalty_strokes_per_mph < 0:
            raise ValueError("base_wind_penalty_strokes_per_mph must be >= 0")
        seen: set[str] = set()
        for assignment in self.assignments:
            if assignment.player_id in seen:
                raise ValueError(f"duplicate wave assignment for {assignment.player_id}")
            seen.add(assignment.player_id)

    def wind_for_wave(self, wave: str) -> float:
        return self.am_wave_wind_mph if wave == "am" else self.pm_wave_wind_mph

    def assignment_for(self, player_id: str) -> WaveAssignment | None:
        for assignment in self.assignments:
            if assignment.player_id == player_id:
                return assignment
        return None

    def penalty_for(self, player_id: str) -> float:
        assignment = self.assignment_for(player_id)
        if assignment is None:
            return 0.0
        own_wind = self.wind_for_wave(assignment.wave)
        return own_wind * self.base_wind_penalty_strokes_per_mph - assignment.wind_sg_baseline


@dataclass(frozen=True)
class FirstRoundPlayerProbability:
    """Round-1 outcome distribution for one player."""

    player_id: str
    frl_probability: float
    top_n_probability: float
    projected_round_score_to_par: float
    round_score_p10: float
    round_score_p50: float
    round_score_p90: float
    wave: str
    market_id: str | None = None

    def __post_init__(self) -> None:
        require_non_empty(self.player_id, "player_id")
        _require_prob(self.frl_probability, "frl_probability")
        _require_prob(self.top_n_probability, "top_n_probability")
        if self.wave not in _WAVES:
            raise ValueError("wave must be 'am' or 'pm'")
        if self.market_id is not None:
            require_non_empty(self.market_id, "market_id")


@dataclass(frozen=True)
class GolfFirstRoundPrediction:
    """Field-wide first-round leader / top-N / score distribution."""

    tournament_id: str
    as_of: datetime
    round_number: int
    top_n: int
    field_size: int
    round_score_sd: float
    am_wave_wind_mph: float
    pm_wave_wind_mph: float
    simulations: int
    player_probabilities: tuple[FirstRoundPlayerProbability, ...]

    def __post_init__(self) -> None:
        require_non_empty(self.tournament_id, "tournament_id")
        require_aware_datetime(self.as_of, "as_of")
        if self.round_number <= 0:
            raise ValueError("round_number must be > 0")
        if self.simulations <= 0:
            raise ValueError("simulations must be > 0")
        if not self.player_probabilities:
            raise ValueError("player_probabilities must not be empty")

    @property
    def effective_frl_field(self) -> float:
        """1 / sum(p^2) over FRL probs; how many players realistically contend."""

        herfindahl = sum(p.frl_probability * p.frl_probability for p in self.player_probabilities)
        return 1.0 / herfindahl if herfindahl > 0 else 0.0

    def frl_probability_for(self, player_id: str) -> float:
        for prediction in self.player_probabilities:
            if prediction.player_id == player_id:
                return prediction.frl_probability
        return 0.0

    def round_score_probability_at_most(self, player_id: str, threshold_to_par: float) -> float:
        """P(round-1 score-to-par <= ``threshold``) for a round over/under market."""

        for prediction in self.player_probabilities:
            if prediction.player_id == player_id:
                return NormalDist(prediction.projected_round_score_to_par, self.round_score_sd).cdf(
                    threshold_to_par
                )
        return 0.0

    def to_frl_signal(
        self,
        forecast: TeeTimeWaveForecast,
        *,
        source: str = "weather_tee_combined",
        schema_version: str = "sports-golf-frl-prediction-v1",
        received_at: datetime | None = None,
    ) -> ExternalSignalEvent:
        """Emit the ``weather_tee_combined`` signal ``sports_frl_weather_arb`` reads.

        The strategy's rules-mode uses ``am/pm_wave_wind_mph`` + per-player
        ``wave`` / ``wind_sg_baseline``; ``model_frl_probability`` is carried
        alongside for a model-mode sleeve that prefers the simulated fair value.
        """

        event_time = received_at or self.as_of
        players: list[dict[str, object]] = []
        for prediction in self.player_probabilities:
            assignment = forecast.assignment_for(prediction.player_id)
            players.append(
                {
                    "player_id": prediction.player_id,
                    "market_id": prediction.market_id,
                    "wave": prediction.wave,
                    "wind_sg_baseline": assignment.wind_sg_baseline if assignment else 0.0,
                    "model_frl_probability": prediction.frl_probability,
                    "model_top_n_probability": prediction.top_n_probability,
                    "projected_round_score_to_par": prediction.projected_round_score_to_par,
                }
            )
        return ExternalSignalEvent(
            event_id=EventId(f"sports-golf-frl-{self.tournament_id}-r{self.round_number}-{_event_ts(self.as_of)}"),
            source=source,
            exchange_ts=self.as_of,
            received_at=event_time,
            schema_version=schema_version,
            payload={
                "tournament_id": self.tournament_id,
                "round_number": self.round_number,
                "field_size": self.field_size,
                "simulations": self.simulations,
                "am_wave_wind_mph": forecast.am_wave_wind_mph,
                "pm_wave_wind_mph": forecast.pm_wave_wind_mph,
                "players": players,
            },
            provenance=_frl_provenance(source=source, schema_version=schema_version),
        )


class GolfFirstRoundMonteCarloModel:
    """Monte Carlo first-round leader / top-N / round-score model.

    Each player's round-1 score-to-par is Gaussian with mean
    ``round_holes * baseline_per_hole - skill_strokes + wave_penalty`` and SD
    ``round_score_sd`` (a single PGA round is ~2.8-3.2 strokes). ``skill_strokes``
    treats ``sg_approach + sg_putting`` as strokes-gained per round (scaled by
    ``sg_to_round_strokes``); positive skill lowers the score.
    """

    def __init__(
        self,
        *,
        simulations: int = 20000,
        seed: int = 0,
        round_holes: int = 18,
        round_score_sd: float = 2.9,
        sg_to_round_strokes: float = 1.0,
        top_n: int = 5,
    ) -> None:
        if simulations <= 0:
            raise ValueError("simulations must be > 0")
        if round_holes <= 0:
            raise ValueError("round_holes must be > 0")
        if round_score_sd <= 0:
            raise ValueError("round_score_sd must be > 0")
        if top_n <= 0:
            raise ValueError("top_n must be > 0")
        self.simulations = simulations
        self.seed = seed
        self.round_holes = round_holes
        self.round_score_sd = round_score_sd
        self.sg_to_round_strokes = sg_to_round_strokes
        self.top_n = top_n

    def predict(
        self,
        state: GolfTournamentState,
        *,
        forecast: TeeTimeWaveForecast | None = None,
        round_number: int = 1,
        field_size: int | None = None,
    ) -> GolfFirstRoundPrediction:
        players = state.players
        n = len(players)
        means = [self._round_mean(player, forecast) for player in players]
        waves = [self._wave_for(player, forecast) for player in players]

        rng = random.Random(self.seed)
        frl = [0.0] * n
        top_n_counts = [0] * n
        top_n = min(self.top_n, n)
        for _ in range(self.simulations):
            scores = [round(rng.gauss(means[i], self.round_score_sd)) for i in range(n)]
            best = min(scores)
            leaders = [i for i in range(n) if scores[i] == best]
            share = 1.0 / len(leaders)
            for i in leaders:
                frl[i] += share
            cutoff = sorted(scores)[top_n - 1]
            for i in range(n):
                if scores[i] <= cutoff:
                    top_n_counts[i] += 1

        predictions: list[FirstRoundPlayerProbability] = []
        for i, player in enumerate(players):
            dist = NormalDist(means[i], self.round_score_sd)
            predictions.append(
                FirstRoundPlayerProbability(
                    player_id=player.player_id,
                    frl_probability=frl[i] / self.simulations,
                    top_n_probability=top_n_counts[i] / self.simulations,
                    projected_round_score_to_par=means[i],
                    round_score_p10=dist.inv_cdf(0.10),
                    round_score_p50=means[i],
                    round_score_p90=dist.inv_cdf(0.90),
                    wave=waves[i],
                    market_id=player.market_id,
                )
            )
        predictions.sort(key=lambda item: item.player_id)
        return GolfFirstRoundPrediction(
            tournament_id=state.tournament_id,
            as_of=state.as_of,
            round_number=round_number,
            top_n=self.top_n,
            field_size=field_size if field_size is not None else n,
            round_score_sd=self.round_score_sd,
            am_wave_wind_mph=forecast.am_wave_wind_mph if forecast else 0.0,
            pm_wave_wind_mph=forecast.pm_wave_wind_mph if forecast else 0.0,
            simulations=self.simulations,
            player_probabilities=tuple(predictions),
        )

    def _round_mean(self, player: GolfPlayerSnapshot, forecast: TeeTimeWaveForecast | None) -> float:
        penalty = forecast.penalty_for(player.player_id) if forecast else player.wave_weather_delta_per_round
        skill_strokes = (player.sg_approach + player.sg_putting) * self.sg_to_round_strokes
        context = (
            player.injury_strokes_per_round
            + player.rest_fatigue_strokes_per_round
            + player.caddie_absence_strokes_per_round
        )
        return self.round_holes * player.baseline_score_to_par_per_hole - skill_strokes + penalty + context

    def _wave_for(self, player: GolfPlayerSnapshot, forecast: TeeTimeWaveForecast | None) -> str:
        if forecast is not None:
            assignment = forecast.assignment_for(player.player_id)
            if assignment is not None:
                return assignment.wave
        return "am"


def wave_forecast_from_payload(payload: Mapping[str, object]) -> TeeTimeWaveForecast:
    """Build a forecast from a captured/operator ``weather_tee_combined`` payload."""

    raw_players = payload.get("players")
    entries = raw_players if isinstance(raw_players, (list, tuple)) else ()
    assignments: list[WaveAssignment] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        player_id = entry.get("player_id")
        wave = entry.get("wave")
        if not isinstance(player_id, str) or not isinstance(wave, str):
            continue
        market_id = entry.get("market_id")
        assignments.append(
            WaveAssignment(
                player_id=player_id,
                wave=wave.lower(),
                wind_sg_baseline=_as_float(entry.get("wind_sg_baseline")),
                market_id=market_id if isinstance(market_id, str) and market_id else None,
            )
        )
    return TeeTimeWaveForecast(
        am_wave_wind_mph=_as_float(payload.get("am_wave_wind_mph")),
        pm_wave_wind_mph=_as_float(payload.get("pm_wave_wind_mph")),
        assignments=tuple(assignments),
    )


def _as_float(value: object, default: float = 0.0) -> float:
    if isinstance(value, (int, float, str)):
        try:
            return float(value)
        except ValueError:
            return default
    return default


def _frl_provenance(*, source: str, schema_version: str) -> EventProvenance:
    return EventProvenance(
        source=source,
        channel="predictive_model",
        schema_version=schema_version,
        metadata={"model_family": "sports_golf_first_round"},
    )


def _event_ts(value: datetime) -> str:
    return value.isoformat().replace("+", "p").replace(":", "").replace("-", "").replace(".", "")
