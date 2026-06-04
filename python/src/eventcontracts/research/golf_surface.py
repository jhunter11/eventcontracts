"""Coherent multi-outcome golf surface and async no-trade inference loop.

This module is research-only. It prices several Kalshi golf market families from
one shared tournament simulation so top-N, make-cut, and cut-line probabilities
remain internally coherent. It never submits, cancels, replaces, or live-submits
orders; downstream outputs are hypothetical intents for shadow-fill measurement.
"""

from __future__ import annotations

import asyncio
import csv
import json
import math
import random
from collections import Counter, defaultdict
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from eventcontracts.research.calibration import kalshi_fee
from eventcontracts.sports import GolfPlayerSnapshot, GolfTournamentState

_EPS = 1e-9
_BookState = dict[str, dict[float, float]]


@dataclass(frozen=True)
class GolfSurfaceConfig:
    """Simulation and candidate-gate settings for the surface."""

    simulations: int = 4000
    seed: int = 17
    top_n_values: tuple[int, ...] = (5, 10, 20)
    cut_line_values: tuple[int, ...] = (-2, -1, 0, 1, 2, 3, 4)
    min_net_edge: float = 0.03
    max_quote_age_ms: int = 60_000
    quantity: float = 1.0
    remaining_hole_volatility: float = 0.34
    approach_weight_per_sg: float = 0.018
    putting_weight_per_sg: float = 0.006
    weather_weight_per_round: float = 1.0

    def __post_init__(self) -> None:
        if self.simulations <= 0:
            raise ValueError("simulations must be > 0")
        if not self.top_n_values:
            raise ValueError("top_n_values must not be empty")
        if min(self.top_n_values) <= 0:
            raise ValueError("top_n_values must be positive")
        if not self.cut_line_values:
            raise ValueError("cut_line_values must not be empty")
        if self.remaining_hole_volatility <= 0:
            raise ValueError("remaining_hole_volatility must be > 0")


@dataclass(frozen=True)
class SurfaceMarket:
    """Tradable market target to price from the shared surface."""

    market_ticker: str
    market_family: str
    subject_id: str
    top_n: int | None = None
    cut_line: int | None = None
    cut_line_relation: str = "exact"

    def __post_init__(self) -> None:
        if not self.market_ticker:
            raise ValueError("market_ticker must not be empty")
        if self.market_family not in {"top_n", "make_cut", "cut_line"}:
            raise ValueError("market_family must be top_n, make_cut, or cut_line")
        if not self.subject_id:
            raise ValueError("subject_id must not be empty")
        if self.market_family == "top_n" and self.top_n is None:
            raise ValueError("top_n markets need top_n")
        if self.market_family == "cut_line" and self.cut_line is None:
            raise ValueError("cut_line markets need cut_line")


@dataclass(frozen=True)
class SurfaceQuote:
    """Public quote used for executable-touch candidate checks."""

    market_ticker: str
    received_at: datetime
    yes_bid: float
    yes_ask: float
    yes_bid_size: float = 0.0
    yes_ask_size: float = 0.0
    source: str = "csv"

    def __post_init__(self) -> None:
        if not self.market_ticker:
            raise ValueError("market_ticker must not be empty")
        _require_probability(self.yes_bid, "yes_bid")
        _require_probability(self.yes_ask, "yes_ask")
        if self.yes_ask < self.yes_bid:
            raise ValueError("yes_ask must be >= yes_bid")
        if self.received_at.tzinfo is None:
            raise ValueError("received_at must be timezone-aware")

    @property
    def spread(self) -> float:
        return self.yes_ask - self.yes_bid


@dataclass(frozen=True)
class SurfaceProbability:
    """One priced market probability from the shared tournament surface."""

    market_ticker: str
    market_family: str
    subject_id: str
    fair_yes_probability: float
    description: str

    def __post_init__(self) -> None:
        _require_probability(self.fair_yes_probability, "fair_yes_probability")


@dataclass(frozen=True)
class SurfaceCandidate:
    """Fee/spread-aware hypothetical candidate from a market probability."""

    market_ticker: str
    market_family: str
    subject_id: str
    side: str
    fair_yes_probability: float
    executable_price: float
    limit_price: float
    fee: float
    gross_edge: float
    net_edge: float
    spread: float
    quote_age_ms: int
    stale_quote: bool
    candidate: bool
    reason: str

    def as_intent(self, decision_time: datetime, *, quantity: float = 1.0) -> dict[str, object]:
        return {
            "intent_id": f"surface-{self.market_ticker}-{_event_ts(decision_time)}",
            "decision_time": decision_time.isoformat(),
            "market_ticker": self.market_ticker,
            "market_family": self.market_family,
            "side": self.side,
            "quantity": quantity,
            "fair_yes_probability": self.fair_yes_probability,
            "executable_price": self.executable_price,
            "limit_price": self.limit_price,
            "fee": self.fee,
            "expected_net_edge": self.net_edge,
            "spread": self.spread,
            "quote_age_ms": self.quote_age_ms,
            "source_model": "golf_multi_outcome_surface_v1",
            "candidate": self.candidate,
            "reason": self.reason,
        }

    def as_dict(self) -> dict[str, object]:
        return {
            "market_ticker": self.market_ticker,
            "market_family": self.market_family,
            "subject_id": self.subject_id,
            "side": self.side,
            "fair_yes_probability": self.fair_yes_probability,
            "executable_price": self.executable_price,
            "limit_price": self.limit_price,
            "fee": self.fee,
            "gross_edge": self.gross_edge,
            "net_edge": self.net_edge,
            "spread": self.spread,
            "quote_age_ms": self.quote_age_ms,
            "stale_quote": self.stale_quote,
            "candidate": self.candidate,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class GolfSurfacePrediction:
    """Shared golf surface result."""

    tournament_id: str
    as_of: datetime
    simulations: int
    top_n_probabilities: dict[int, dict[str, float]]
    make_cut_probabilities: dict[str, float]
    cut_line_probabilities: dict[int, float]
    expected_cut_line: float
    expected_finish_scores: dict[str, float]
    player_context_strokes_per_round: dict[str, float]
    probabilities: tuple[SurfaceProbability, ...]
    candidates: tuple[SurfaceCandidate, ...]
    decision_gate: str

    def as_dict(self) -> dict[str, object]:
        return {
            "tournament_id": self.tournament_id,
            "as_of": self.as_of.isoformat(),
            "simulations": self.simulations,
            "top_n_probabilities": {
                str(top_n): dict(values) for top_n, values in self.top_n_probabilities.items()
            },
            "make_cut_probabilities": dict(self.make_cut_probabilities),
            "cut_line_probabilities": {str(line): prob for line, prob in self.cut_line_probabilities.items()},
            "expected_cut_line": self.expected_cut_line,
            "expected_finish_scores": dict(self.expected_finish_scores),
            "player_context_strokes_per_round": dict(self.player_context_strokes_per_round),
            "probabilities": [item.__dict__ for item in self.probabilities],
            "candidates": [item.as_dict() for item in self.candidates],
            "decision_gate": self.decision_gate,
        }


@dataclass(frozen=True)
class SurfaceShadowIntent:
    """One hypothetical golf surface candidate to mark out against later books."""

    market_ticker: str
    decision_time: datetime
    side: str
    executable_price: float
    fee: float
    expected_net_edge: float
    fair_yes_probability: float
    source_model: str = "golf_multi_outcome_surface_v1"

    def __post_init__(self) -> None:
        if not self.market_ticker:
            raise ValueError("market_ticker must not be empty")
        if self.decision_time.tzinfo is None:
            raise ValueError("decision_time must be timezone-aware")
        if self.side not in {"YES", "NO"}:
            raise ValueError("side must be YES or NO")
        _require_probability(self.executable_price, "executable_price")
        _require_probability(self.fair_yes_probability, "fair_yes_probability")
        if self.fee < 0.0:
            raise ValueError("fee must be non-negative")

    def as_dict(self) -> dict[str, object]:
        return {
            "market_ticker": self.market_ticker,
            "decision_time": self.decision_time.isoformat(),
            "side": self.side,
            "executable_price": self.executable_price,
            "fee": self.fee,
            "expected_net_edge": self.expected_net_edge,
            "fair_yes_probability": self.fair_yes_probability,
            "source_model": self.source_model,
        }


@dataclass(frozen=True)
class SurfaceMarkoutSummary:
    """Aggregate CLV/markout result for one future horizon."""

    horizon_seconds: int
    rows: int
    missing_rows: int
    mean_clv: float | None
    mean_markout_net: float | None
    positive_clv_rate: float | None
    positive_net_rate: float | None

    def as_dict(self) -> dict[str, object]:
        return _jsonable(self.__dict__)


@dataclass(frozen=True)
class SurfaceMarkoutRow:
    """One read-only markout of a golf surface intent."""

    market_ticker: str
    candidate_received_at: datetime
    horizon_seconds: int
    side: str
    executable_price: float
    entry_fee: float
    expected_net_edge: float
    markout_received_at: datetime | None
    actual_horizon_seconds: float | None
    yes_bid: float | None
    yes_ask: float | None
    markout_price: float | None
    clv: float | None
    markout_net_after_entry_fee: float | None
    source: str
    reason: str

    def as_dict(self) -> dict[str, object]:
        return _jsonable(self.__dict__)


@dataclass(frozen=True)
class SurfaceMarkoutReport:
    """Read-only CLV report for golf surface shadow intents."""

    as_of: datetime
    candidate_count: int
    quote_count: int
    markout_count: int
    horizons_seconds: tuple[int, ...]
    summaries: tuple[SurfaceMarkoutSummary, ...]
    decision_gate: str
    markouts: tuple[SurfaceMarkoutRow, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "as_of": self.as_of.isoformat(),
            "candidate_count": self.candidate_count,
            "quote_count": self.quote_count,
            "markout_count": self.markout_count,
            "horizons_seconds": list(self.horizons_seconds),
            "summaries": [item.as_dict() for item in self.summaries],
            "decision_gate": self.decision_gate,
            "markouts": [item.as_dict() for item in self.markouts],
        }


@dataclass(frozen=True)
class GolfTopNArbConfig:
    """Gates for hard top-N dominance checks."""

    min_net_edge: float = 0.0
    min_executable_size: float = 1.0
    max_quote_age_seconds: float = 600.0

    def __post_init__(self) -> None:
        if self.min_executable_size < 0.0:
            raise ValueError("min_executable_size must be non-negative")
        if self.max_quote_age_seconds < 0.0:
            raise ValueError("max_quote_age_seconds must be non-negative")


@dataclass(frozen=True)
class GolfTopNArbCandidate:
    """One read-only top-N monotonicity violation candidate."""

    subject_id: str
    event_key: str
    lower_top_n: int
    higher_top_n: int
    lower_market_ticker: str
    higher_market_ticker: str
    decision_time: datetime
    lower_yes_bid: float
    lower_yes_ask: float
    higher_yes_bid: float
    higher_yes_ask: float
    lower_no_cost: float
    higher_yes_cost: float
    lower_no_fee: float
    higher_yes_fee: float
    total_cost: float
    fee_net_floor: float
    executable_size: float
    quote_age_seconds: float
    candidate: bool
    reason: str
    lower_quote_source: str
    higher_quote_source: str

    def as_dict(self) -> dict[str, object]:
        return _jsonable(self.__dict__)


@dataclass(frozen=True)
class GolfTopNArbReport:
    """Read-only top-N dominance scan report."""

    as_of: datetime
    market_count: int
    quote_count: int
    pair_count: int
    candidate_count: int
    max_fee_net_floor: float | None
    decision_gate: str
    candidates: tuple[GolfTopNArbCandidate, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "as_of": self.as_of.isoformat(),
            "market_count": self.market_count,
            "quote_count": self.quote_count,
            "pair_count": self.pair_count,
            "candidate_count": self.candidate_count,
            "max_fee_net_floor": self.max_fee_net_floor,
            "decision_gate": self.decision_gate,
            "candidates": [item.as_dict() for item in self.candidates],
        }


class GolfMultiOutcomeSurfaceModel:
    """Unified Monte Carlo surface for top-N, make-cut, and cut-line markets."""

    def __init__(self, config: GolfSurfaceConfig | None = None) -> None:
        self.config = config or GolfSurfaceConfig()

    def predict(
        self,
        state: GolfTournamentState,
        *,
        markets: Sequence[SurfaceMarket] = (),
        quotes: Sequence[SurfaceQuote] = (),
    ) -> GolfSurfacePrediction:
        rng = random.Random(self.config.seed)
        top_counts: dict[int, Counter[str]] = {top_n: Counter() for top_n in self.config.top_n_values}
        make_counts: Counter[str] = Counter()
        cut_counts: Counter[int] = Counter()
        finish_score_sum: defaultdict[str, float] = defaultdict(float)
        player_ids = [player.player_id for player in state.players]

        for _ in range(self.config.simulations):
            cut_scores = self._simulate_scores_to_hole(state, state.cut_holes, rng)
            final_scores = self._simulate_scores_to_hole(state, state.tournament_holes, rng)
            cut_score = self._rank_cut_score(cut_scores, state.cut_rule_size)
            cut_counts[int(round(cut_score))] += 1
            for player_id, score in cut_scores:
                if score <= cut_score:
                    make_counts[player_id] += 1
            final_by_score = sorted(final_scores, key=lambda item: (item[1], item[0]))
            for player_id, score in final_scores:
                finish_score_sum[player_id] += score
            for top_n in self.config.top_n_values:
                cutoff = final_by_score[min(top_n, len(final_by_score)) - 1][1]
                for player_id, score in final_scores:
                    if score <= cutoff:
                        top_counts[top_n][player_id] += 1

        top_probs = {
            top_n: {player_id: top_counts[top_n][player_id] / self.config.simulations for player_id in player_ids}
            for top_n in self.config.top_n_values
        }
        make_probs = {player_id: make_counts[player_id] / self.config.simulations for player_id in player_ids}
        cut_probs = {
            cut_line: cut_counts[cut_line] / self.config.simulations
            for cut_line in sorted(set(cut_counts).union(self.config.cut_line_values))
        }
        finish_scores = {
            player_id: finish_score_sum[player_id] / self.config.simulations for player_id in player_ids
        }
        surface_probs = tuple(
            self._probability_for_market(
                market,
                top_probs=top_probs,
                make_probs=make_probs,
                cut_probs=cut_probs,
            )
            for market in markets
        )
        candidates = tuple(scan_surface_candidates(surface_probs, quotes, config=self.config, as_of=state.as_of))
        return GolfSurfacePrediction(
            tournament_id=state.tournament_id,
            as_of=state.as_of,
            simulations=self.config.simulations,
            top_n_probabilities=top_probs,
            make_cut_probabilities=make_probs,
            cut_line_probabilities=cut_probs,
            expected_cut_line=sum(cut_line * prob for cut_line, prob in cut_probs.items()),
            expected_finish_scores=finish_scores,
            player_context_strokes_per_round={
                player.player_id: _availability_context_strokes_per_round(player) for player in state.players
            },
            probabilities=surface_probs,
            candidates=candidates,
            decision_gate=_decision_gate(surface_probs, candidates),
        )

    def _simulate_scores_to_hole(
        self,
        state: GolfTournamentState,
        target_holes: int,
        rng: random.Random,
    ) -> list[tuple[str, float]]:
        scores: list[tuple[str, float]] = []
        for player in state.players:
            remaining = max(target_holes - player.holes_completed, 0)
            score = player.score_to_par
            if remaining:
                mean = _remaining_score_mean(player, remaining, self.config)
                stddev = self.config.remaining_hole_volatility * math.sqrt(remaining)
                score += rng.gauss(mean, stddev)
            scores.append((player.player_id, score))
        return scores

    @staticmethod
    def _rank_cut_score(scores: Sequence[tuple[str, float]], cut_rule_size: int) -> float:
        ordered = sorted(score for _player_id, score in scores)
        index = min(cut_rule_size, len(ordered)) - 1
        return ordered[index]

    @staticmethod
    def _probability_for_market(
        market: SurfaceMarket,
        *,
        top_probs: Mapping[int, Mapping[str, float]],
        make_probs: Mapping[str, float],
        cut_probs: Mapping[int, float],
    ) -> SurfaceProbability:
        if market.market_family == "top_n":
            probability = top_probs.get(market.top_n or 0, {}).get(market.subject_id, 0.0)
            description = f"{market.subject_id} top {market.top_n}"
        elif market.market_family == "make_cut":
            probability = make_probs.get(market.subject_id, 0.0)
            description = f"{market.subject_id} makes cut"
        else:
            probability = _cut_line_market_probability(
                cut_probs,
                cut_line=market.cut_line if market.cut_line is not None else 0,
                relation=market.cut_line_relation,
            )
            description = f"cut line {market.cut_line} {market.cut_line_relation}"
        return SurfaceProbability(
            market_ticker=market.market_ticker,
            market_family=market.market_family,
            subject_id=market.subject_id,
            fair_yes_probability=_clip_probability(probability),
            description=description,
        )


async def run_async_surface_fixture(
    *,
    iterations: int = 3,
    config: GolfSurfaceConfig | None = None,
) -> list[GolfSurfacePrediction]:
    """Run a deterministic async recompute loop over fixture state updates."""

    if iterations <= 0:
        raise ValueError("iterations must be > 0")
    model = GolfMultiOutcomeSurfaceModel(config)
    predictions: list[GolfSurfacePrediction] = []
    state = fixture_surface_state()
    markets = fixture_surface_markets()
    quotes = fixture_surface_quotes(state.as_of)
    async for update in fixture_surface_updates(iterations=iterations, start=state):
        state = apply_surface_update(state, update)
        quotes = fixture_surface_quotes(state.as_of)
        predictions.append(model.predict(state, markets=markets, quotes=quotes))
        await asyncio.sleep(0)
    return predictions


async def fixture_surface_updates(
    *,
    iterations: int,
    start: GolfTournamentState,
) -> AsyncIterator[dict[str, object]]:
    """Deterministic no-network state updates for the async loop."""

    for idx in range(iterations):
        await asyncio.sleep(0)
        yield {
            "as_of": (start.as_of + timedelta(minutes=idx + 1)).isoformat(),
            "players": [
                {"player_id": "scottie", "score_delta": -0.25 if idx == 0 else 0.0, "holes_delta": 1},
                {"player_id": "rory", "score_delta": 0.0, "holes_delta": 1},
                {"player_id": "jordan", "score_delta": 0.2 if idx > 0 else 0.0, "holes_delta": 1},
            ],
        }


def apply_surface_update(state: GolfTournamentState, update: Mapping[str, object]) -> GolfTournamentState:
    """Apply a point-in-time state update. No labels or settlement fields allowed."""

    forbidden = {"target", "settlement", "final_position", "made_cut", "made_top_n"}
    if forbidden.intersection(update):
        raise ValueError("surface updates must not contain label or settlement fields")
    by_player: dict[str, Mapping[str, object]] = {}
    raw_players = update.get("players")
    if isinstance(raw_players, Sequence) and not isinstance(raw_players, str):
        for raw in raw_players:
            if isinstance(raw, Mapping) and raw.get("player_id") is not None:
                by_player[str(raw["player_id"])] = raw
    players: list[GolfPlayerSnapshot] = []
    for player in state.players:
        raw = by_player.get(player.player_id)
        if raw is None:
            players.append(player)
            continue
        holes_completed = min(
            state.tournament_holes,
            player.holes_completed + int(_float_value(raw.get("holes_delta"), 0.0)),
        )
        players.append(
            replace(
                player,
                score_to_par=player.score_to_par + _float_value(raw.get("score_delta"), 0.0),
                holes_completed=holes_completed,
            )
        )
    as_of = _parse_datetime(str(update.get("as_of") or state.as_of.isoformat()))
    if as_of < state.as_of:
        raise ValueError("surface update as_of moved backwards")
    return replace(state, as_of=as_of, players=tuple(players))


def scan_surface_candidates(
    probabilities: Sequence[SurfaceProbability],
    quotes: Sequence[SurfaceQuote],
    *,
    config: GolfSurfaceConfig,
    as_of: datetime,
) -> list[SurfaceCandidate]:
    """Convert model probabilities into executable-touch hypothetical candidates."""

    quote_by_ticker = {quote.market_ticker: quote for quote in quotes}
    candidates: list[SurfaceCandidate] = []
    for probability in probabilities:
        quote = quote_by_ticker.get(probability.market_ticker)
        if quote is None:
            continue
        candidates.append(_candidate_from_probability(probability, quote, config=config, as_of=as_of))
    return sorted(candidates, key=lambda item: item.net_edge, reverse=True)


def write_surface_outputs(
    prediction: GolfSurfacePrediction,
    *,
    report_json: Path,
    report_md: Path | None = None,
    intents_jsonl: Path | None = None,
) -> None:
    """Write surface report and candidate intents."""

    report_json.parent.mkdir(parents=True, exist_ok=True)
    report_json.write_text(
        json.dumps(_jsonable(prediction.as_dict()), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if report_md is not None:
        report_md.parent.mkdir(parents=True, exist_ok=True)
        report_md.write_text(render_surface_markdown(prediction), encoding="utf-8")
    if intents_jsonl is not None:
        intents_jsonl.parent.mkdir(parents=True, exist_ok=True)
        with intents_jsonl.open("w", encoding="utf-8") as handle:
            for candidate in prediction.candidates:
                if candidate.candidate:
                    row = candidate.as_intent(prediction.as_of)
                    handle.write(json.dumps(_jsonable(row), sort_keys=True) + "\n")


def read_surface_intents_jsonl(path: Path) -> tuple[SurfaceShadowIntent, ...]:
    """Read hypothetical surface intents for CLV/markout evaluation."""

    out: list[SurfaceShadowIntent] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        if not isinstance(payload, Mapping):
            raise ValueError("surface intent JSONL rows must be objects")
        if not _bool_value(payload.get("candidate"), default=True):
            continue
        out.append(_intent_from_mapping(payload))
    return tuple(out)


def read_surface_ws_quotes_jsonl(
    path: Path,
    *,
    market_tickers: Sequence[str] = (),
) -> tuple[SurfaceQuote, ...]:
    """Extract latest book-verified quotes from raw Kalshi WS JSONL."""

    timeline = read_surface_ws_quote_timeline_jsonl(path, market_tickers=market_tickers)
    latest: dict[str, SurfaceQuote] = {}
    for quote in timeline:
        current = latest.get(quote.market_ticker)
        if current is None or quote.received_at >= current.received_at:
            latest[quote.market_ticker] = quote
    return tuple(sorted(latest.values(), key=lambda item: item.market_ticker))


def read_surface_ws_quote_timeline_jsonl(
    path: Path,
    *,
    market_tickers: Sequence[str] = (),
) -> tuple[SurfaceQuote, ...]:
    """Extract a conservative book-verified quote timeline from raw Kalshi WS JSONL."""

    allowed = set(market_tickers)
    book_states: dict[str, _BookState] = {}
    quotes: list[SurfaceQuote] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        if not isinstance(payload, Mapping):
            continue
        quote = _surface_quote_from_ws_state_row(payload, book_states)
        if quote is None:
            continue
        if allowed and quote.market_ticker not in allowed:
            continue
        quotes.append(quote)
    return tuple(sorted(quotes, key=lambda item: (item.market_ticker, item.received_at)))


def evaluate_surface_markouts(
    candidates: Sequence[SurfaceShadowIntent],
    quote_timeline: Sequence[SurfaceQuote],
    *,
    horizons_seconds: Sequence[int] = (300, 900, 1800),
    min_markout_rows: int = 10,
    as_of: datetime | None = None,
) -> SurfaceMarkoutReport:
    """Mark out hypothetical golf surface intents against later WS book state."""

    if min_markout_rows <= 0:
        raise ValueError("min_markout_rows must be positive")
    horizons = tuple(int(item) for item in horizons_seconds)
    if not horizons or any(item <= 0 for item in horizons):
        raise ValueError("horizons_seconds must contain positive integers")
    quotes_by_market: dict[str, list[SurfaceQuote]] = {}
    for quote in sorted(quote_timeline, key=lambda item: (item.market_ticker, item.received_at)):
        quotes_by_market.setdefault(quote.market_ticker, []).append(quote)

    rows: list[SurfaceMarkoutRow] = []
    for candidate in sorted(candidates, key=lambda item: (item.decision_time, item.market_ticker)):
        market_quotes = quotes_by_market.get(candidate.market_ticker, [])
        for horizon in horizons:
            target = candidate.decision_time + timedelta(seconds=horizon)
            markout_quote = _first_surface_quote_at_or_after(market_quotes, target)
            rows.append(_surface_markout_row(candidate, horizon_seconds=horizon, markout_quote=markout_quote))

    summaries = tuple(_surface_markout_summary(horizon, rows) for horizon in horizons)
    markout_count = sum(1 for row in rows if row.markout_price is not None)
    return SurfaceMarkoutReport(
        as_of=as_of or datetime.now(UTC),
        candidate_count=len(candidates),
        quote_count=len(quote_timeline),
        markout_count=markout_count,
        horizons_seconds=horizons,
        summaries=summaries,
        decision_gate=_surface_markout_decision_gate(
            candidate_count=len(candidates),
            summaries=summaries,
            min_markout_rows=min_markout_rows,
        ),
        markouts=tuple(rows),
    )


def write_surface_markout_outputs(
    report: SurfaceMarkoutReport,
    *,
    report_json: Path,
    report_md: Path | None = None,
    markouts_jsonl: Path | None = None,
) -> None:
    """Write golf surface markout report and optional row ledger."""

    report_json.parent.mkdir(parents=True, exist_ok=True)
    report_json.write_text(json.dumps(_jsonable(report.as_dict()), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if report_md is not None:
        report_md.parent.mkdir(parents=True, exist_ok=True)
        report_md.write_text(render_surface_markout_markdown(report), encoding="utf-8")
    if markouts_jsonl is not None:
        markouts_jsonl.parent.mkdir(parents=True, exist_ok=True)
        with markouts_jsonl.open("w", encoding="utf-8") as handle:
            for row in report.markouts:
                handle.write(json.dumps(_jsonable(row.as_dict()), sort_keys=True) + "\n")


def scan_golf_topn_arbitrage(
    markets: Sequence[SurfaceMarket],
    quotes: Sequence[SurfaceQuote],
    *,
    config: GolfTopNArbConfig | None = None,
    as_of: datetime | None = None,
) -> GolfTopNArbReport:
    """Scan same-player top-N books for hard monotonic no-arb violations."""

    arb_config = config or GolfTopNArbConfig()
    topn_markets = [market for market in markets if market.market_family == "top_n" and market.top_n is not None]
    quote_by_market = _latest_quote_by_market(quotes)
    decision_time = as_of or _latest_quote_time(quote_by_market.values()) or datetime.now(UTC)
    grouped: dict[tuple[str, str], list[SurfaceMarket]] = defaultdict(list)
    for market in topn_markets:
        grouped[(_topn_event_key(market.market_ticker), market.subject_id)].append(market)

    rows: list[GolfTopNArbCandidate] = []
    for (event_key, subject_id), group in sorted(grouped.items()):
        ordered = sorted(group, key=lambda item: (item.top_n or 0, item.market_ticker))
        for lower_idx, lower_market in enumerate(ordered):
            lower_quote = quote_by_market.get(lower_market.market_ticker)
            if lower_quote is None:
                continue
            for higher_market in ordered[lower_idx + 1 :]:
                higher_quote = quote_by_market.get(higher_market.market_ticker)
                if higher_quote is None:
                    continue
                rows.append(
                    _topn_arb_candidate(
                        subject_id=subject_id,
                        event_key=event_key,
                        lower_market=lower_market,
                        higher_market=higher_market,
                        lower_quote=lower_quote,
                        higher_quote=higher_quote,
                        as_of=decision_time,
                        config=arb_config,
                    )
                )

    rows.sort(key=lambda item: (item.fee_net_floor, item.executable_size), reverse=True)
    candidate_count = sum(1 for row in rows if row.candidate)
    max_floor = max((row.fee_net_floor for row in rows), default=None)
    return GolfTopNArbReport(
        as_of=decision_time,
        market_count=len(topn_markets),
        quote_count=sum(1 for market in topn_markets if market.market_ticker in quote_by_market),
        pair_count=len(rows),
        candidate_count=candidate_count,
        max_fee_net_floor=max_floor,
        decision_gate=_topn_arb_decision_gate(rows, candidate_count=candidate_count),
        candidates=tuple(rows),
    )


def write_golf_topn_arb_outputs(
    report: GolfTopNArbReport,
    *,
    report_json: Path,
    report_md: Path | None = None,
    candidates_jsonl: Path | None = None,
) -> None:
    """Write top-N dominance report and optional positive-candidate ledger."""

    report_json.parent.mkdir(parents=True, exist_ok=True)
    report_json.write_text(json.dumps(_jsonable(report.as_dict()), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if report_md is not None:
        report_md.parent.mkdir(parents=True, exist_ok=True)
        report_md.write_text(render_golf_topn_arb_markdown(report), encoding="utf-8")
    if candidates_jsonl is not None:
        candidates_jsonl.parent.mkdir(parents=True, exist_ok=True)
        with candidates_jsonl.open("w", encoding="utf-8") as handle:
            for row in report.candidates:
                if row.candidate:
                    handle.write(json.dumps(_jsonable(row.as_dict()), sort_keys=True) + "\n")


def read_surface_state_json(path: Path) -> GolfTournamentState:
    """Read a point-in-time tournament state JSON file."""

    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("state JSON must be an object")
    return surface_state_from_mapping(payload)


def surface_state_from_mapping(payload: Mapping[str, object]) -> GolfTournamentState:
    """Build a tournament state from point-in-time JSON-like data."""

    forbidden = {"target", "settlement", "final_position", "made_cut", "made_top_n"}
    if forbidden.intersection(payload):
        raise ValueError("surface state must not contain label or settlement fields")
    raw_players = payload.get("players")
    if not isinstance(raw_players, Sequence) or isinstance(raw_players, str) or not raw_players:
        raise ValueError("surface state needs a non-empty players list")
    players: list[GolfPlayerSnapshot] = []
    for raw_player in raw_players:
        if not isinstance(raw_player, Mapping):
            raise ValueError("player entries must be objects")
        if forbidden.intersection(raw_player):
            raise ValueError("player state must not contain label or settlement fields")
        players.append(
            GolfPlayerSnapshot(
                player_id=_required(raw_player, "player_id"),
                score_to_par=_float_value(raw_player.get("score_to_par"), 0.0),
                holes_completed=int(_float_value(raw_player.get("holes_completed"), 0.0)),
                sg_approach=_float_value(raw_player.get("sg_approach"), 0.0),
                sg_putting=_float_value(raw_player.get("sg_putting"), 0.0),
                baseline_score_to_par_per_hole=_float_value(
                    raw_player.get("baseline_score_to_par_per_hole"),
                    0.0,
                ),
                wave_weather_delta_per_round=_float_value(raw_player.get("wave_weather_delta_per_round"), 0.0),
                market_id=_optional_str(raw_player.get("market_id")),
                injury_strokes_per_round=_float_value(raw_player.get("injury_strokes_per_round"), 0.0),
                rest_fatigue_strokes_per_round=_float_value(
                    raw_player.get("rest_fatigue_strokes_per_round"),
                    0.0,
                ),
                caddie_absence_strokes_per_round=_float_value(
                    raw_player.get("caddie_absence_strokes_per_round"),
                    0.0,
                ),
            )
        )
    return GolfTournamentState(
        tournament_id=_required(payload, "tournament_id"),
        as_of=_parse_datetime(_required(payload, "as_of")),
        players=tuple(players),
        cut_rule_size=int(_float_value(payload.get("cut_rule_size"), 65.0)),
        cut_holes=int(_float_value(payload.get("cut_holes"), 36.0)),
        tournament_holes=int(_float_value(payload.get("tournament_holes"), 72.0)),
        wind_forecast_mph=_float_value(payload.get("wind_forecast_mph"), 0.0),
        course_baseline_cut_line=_float_value(payload.get("course_baseline_cut_line"), 0.0),
    )


def read_surface_markets_csv(path: Path) -> tuple[SurfaceMarket, ...]:
    """Read surface market targets from a CSV or golf market-map CSV."""

    return surface_markets_from_rows(read_csv_rows(path))


def surface_markets_from_rows(rows: Sequence[Mapping[str, object]]) -> tuple[SurfaceMarket, ...]:
    markets: list[SurfaceMarket] = []
    for row in rows:
        family = str(row.get("market_family") or "").strip()
        inferred_top_n = _infer_top_n_from_row(row)
        if not family and inferred_top_n is not None:
            family = "top_n"
        if family not in {"top_n", "make_cut", "cut_line"}:
            continue
        markets.append(
            SurfaceMarket(
                market_ticker=_required(row, "market_ticker"),
                market_family=family,
                subject_id=_subject_id_from_row(row),
                top_n=_optional_int(row.get("top_n")) or inferred_top_n,
                cut_line=_optional_int(row.get("cut_line")),
                cut_line_relation=str(row.get("cut_line_relation") or "exact") or "exact",
            )
        )
    return tuple(markets)


def read_surface_quotes_csv(path: Path) -> tuple[SurfaceQuote, ...]:
    """Read public quotes from CSV rows."""

    return surface_quotes_from_rows(read_csv_rows(path))


def surface_quotes_from_rows(rows: Sequence[Mapping[str, object]]) -> tuple[SurfaceQuote, ...]:
    quotes: list[SurfaceQuote] = []
    for row in rows:
        received = _parse_datetime(_required_one(row, ("received_at", "captured_at")))
        bid = _probability_price(row.get("yes_bid_dollars") or row.get("yes_bid"))
        ask = _probability_price(row.get("yes_ask_dollars") or row.get("yes_ask"))
        quotes.append(
            SurfaceQuote(
                market_ticker=_required(row, "market_ticker"),
                received_at=received,
                yes_bid=bid,
                yes_ask=ask,
                yes_bid_size=_float_value(row.get("yes_bid_size"), 0.0),
                yes_ask_size=_float_value(row.get("yes_ask_size"), 0.0),
                source=str(row.get("source") or "csv"),
            )
        )
    return tuple(quotes)


def write_fixture_surface_inputs(out_dir: Path) -> dict[str, str]:
    """Write fixture state/market/quote inputs for no-network reproducibility."""

    out_dir.mkdir(parents=True, exist_ok=True)
    state = fixture_surface_state()
    state_path = out_dir / "surface_state.json"
    markets_path = out_dir / "surface_markets.csv"
    quotes_path = out_dir / "surface_quotes.csv"
    state_path.write_text(
        json.dumps(_jsonable(_state_to_mapping(state)), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _write_csv(markets_path, _market_columns(), [_market_to_mapping(item) for item in fixture_surface_markets()])
    _write_csv(quotes_path, _quote_columns(), [_quote_to_mapping(item) for item in fixture_surface_quotes(state.as_of)])
    return {"state_json": str(state_path), "markets_csv": str(markets_path), "quotes_csv": str(quotes_path)}


def write_fixture_surface_markout_inputs(out_dir: Path) -> dict[str, str]:
    """Write deterministic surface intents and WS rows for markout tests."""

    out_dir.mkdir(parents=True, exist_ok=True)
    paths = write_fixture_surface_inputs(out_dir)
    state = read_surface_state_json(Path(paths["state_json"]))
    markets = read_surface_markets_csv(Path(paths["markets_csv"]))
    quotes = read_surface_quotes_csv(Path(paths["quotes_csv"]))
    prediction = GolfMultiOutcomeSurfaceModel(GolfSurfaceConfig(simulations=900, seed=5, min_net_edge=0.01)).predict(
        state,
        markets=markets,
        quotes=quotes,
    )
    intents_path = out_dir / "surface_intents.jsonl"
    write_surface_outputs(
        prediction,
        report_json=out_dir / "surface.json",
        intents_jsonl=intents_path,
    )
    intents = read_surface_intents_jsonl(intents_path)
    if not intents:
        raise ValueError("fixture surface did not produce a markout candidate")
    intent = intents[0]
    future = intent.decision_time + timedelta(seconds=300)
    raw_rows = [
        _fixture_ws_orderbook_snapshot_row(
            intent.market_ticker,
            received_at=intent.decision_time,
            yes_bid=max(0.01, min(0.98, intent.executable_price - 0.02)),
            no_bid=0.50,
        ),
        _fixture_ws_orderbook_snapshot_row(
            intent.market_ticker,
            received_at=future,
            yes_bid=_fixture_future_yes_bid(intent),
            no_bid=_fixture_future_no_bid(intent),
        ),
    ]
    ws_raw_path = out_dir / "surface_ws_raw.jsonl"
    ws_raw_path.write_text(
        "".join(json.dumps(row, sort_keys=True, ensure_ascii=True) + "\n" for row in raw_rows),
        encoding="utf-8",
    )
    return {**paths, "intents_jsonl": str(intents_path), "ws_raw_jsonl": str(ws_raw_path)}


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    """Read CSV rows as strings."""

    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"empty CSV: {path}")
        return [dict(row) for row in reader]


def render_surface_markdown(prediction: GolfSurfacePrediction) -> str:
    """Render a compact model-theory report."""

    candidate_count = sum(1 for item in prediction.candidates if item.candidate)
    lines = [
        "# Golf Multi-Outcome Surface",
        "",
        f"- Tournament: `{prediction.tournament_id}`",
        f"- As of: `{prediction.as_of.isoformat()}`",
        f"- Simulations: `{prediction.simulations}`",
        f"- Expected cut line: `{prediction.expected_cut_line:.3f}`",
        f"- Candidate rows: `{candidate_count}`",
        f"- Decision: **{prediction.decision_gate}**",
        "",
        "## Top-N Checks",
        "",
        "| Player | Top 5 | Top 10 | Top 20 | Make Cut | Context Strokes/Rd |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for player_id in sorted(prediction.make_cut_probabilities):
        top5 = prediction.top_n_probabilities.get(5, {}).get(player_id, 0.0)
        top10 = prediction.top_n_probabilities.get(10, {}).get(player_id, 0.0)
        top20 = prediction.top_n_probabilities.get(20, {}).get(player_id, 0.0)
        make_cut = prediction.make_cut_probabilities[player_id]
        context = prediction.player_context_strokes_per_round.get(player_id, 0.0)
        lines.append(
            f"| {player_id} | {top5:.4f} | {top10:.4f} | {top20:.4f} | {make_cut:.4f} | {context:+.3f} |"
        )
    lines.extend(
        [
            "",
            "## Candidate Checks",
            "",
            "| Market | Side | Fair | Touch | Net Edge | Reason |",
            "| --- | --- | ---: | ---: | ---: | --- |",
        ]
    )
    for candidate in prediction.candidates[:12]:
        lines.append(
            f"| {candidate.market_ticker} | {candidate.side} | {candidate.fair_yes_probability:.4f} | "
            f"{candidate.executable_price:.4f} | {candidate.net_edge:.4f} | {candidate.reason} |"
        )
    lines.extend(
        [
            "",
            "## Boundary",
            "",
            "This is a coherent pricing and shadow-intent surface only. It is not edge evidence until "
            "chronological OOS, executable-touch, shadow-fill markout, and settlement gates clear.",
            "",
        ]
    )
    return "\n".join(lines)


def render_surface_markout_markdown(report: SurfaceMarkoutReport) -> str:
    """Render a compact golf surface CLV report."""

    lines = [
        "# Golf Surface Markout",
        "",
        f"- As of: `{report.as_of.isoformat()}`",
        f"- Candidate rows: `{report.candidate_count}`",
        f"- WS quote timeline rows: `{report.quote_count}`",
        f"- Matched markouts: `{report.markout_count}`",
        f"- Decision: **{report.decision_gate}**",
        "",
        "| Horizon | Rows | Missing | Mean CLV | Mean Net After Entry Fee | Positive CLV | Positive Net |",
        "| ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for summary in report.summaries:
        lines.append(
            f"| {summary.horizon_seconds} | {summary.rows} | {summary.missing_rows} | "
            f"{_fmt_signed(summary.mean_clv)} | {_fmt_signed(summary.mean_markout_net)} | "
            f"{_fmt(summary.positive_clv_rate)} | {_fmt(summary.positive_net_rate)} |"
        )
    lines.extend(
        [
            "",
            "## Boundary",
            "",
            "This is read-only CLV evidence for hypothetical golf surface intents. Positive CLV is not settlement "
            "edge; it only justifies continued paper/settlement capture.",
            "",
        ]
    )
    return "\n".join(lines)


def render_golf_topn_arb_markdown(report: GolfTopNArbReport) -> str:
    """Render a compact top-N dominance scan report."""

    lines = [
        "# Golf Top-N Dominance Scan",
        "",
        f"- As of: `{report.as_of.isoformat()}`",
        f"- Top-N markets: `{report.market_count}`",
        f"- Markets with quotes: `{report.quote_count}`",
        f"- Same-player ordered pairs checked: `{report.pair_count}`",
        f"- Fee-net candidates: `{report.candidate_count}`",
        f"- Max fee-net floor: `{_fmt_signed(report.max_fee_net_floor)}`",
        f"- Decision: **{report.decision_gate}**",
        "",
        "| Event | Player | Pair | Higher YES Ask | Lower YES Bid | Total Cost | Fee-Net Floor | "
        "Size | Age Sec | Reason |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in report.candidates[:20]:
        pair = f"top{row.lower_top_n}->top{row.higher_top_n}"
        lines.append(
            f"| {row.event_key} | {row.subject_id} | {pair} | {row.higher_yes_ask:.4f} | "
            f"{row.lower_yes_bid:.4f} | {row.total_cost:.4f} | {row.fee_net_floor:+.4f} | "
            f"{row.executable_size:.2f} | {row.quote_age_seconds:.1f} | {row.reason} |"
        )
    lines.extend(
        [
            "",
            "## Boundary",
            "",
            "This is a read-only logical scanner. A positive floor assumes the market rules preserve "
            "top-N subset semantics, ties are handled consistently across both legs, and both legs fill "
            "at the observed touch. It justifies paper audit, not live orders.",
            "",
        ]
    )
    return "\n".join(lines)


def fixture_surface_state() -> GolfTournamentState:
    """Small deterministic tournament state for no-network model tests."""

    now = datetime(2026, 6, 18, 12, 0, tzinfo=UTC)
    players = [
        GolfPlayerSnapshot(
            player_id="scottie",
            score_to_par=-2.0,
            holes_completed=18,
            sg_approach=2.0,
            sg_putting=0.4,
            baseline_score_to_par_per_hole=-0.025,
            wave_weather_delta_per_round=-0.1,
        ),
        GolfPlayerSnapshot(
            player_id="rory",
            score_to_par=-1.0,
            holes_completed=18,
            sg_approach=1.4,
            sg_putting=0.2,
            baseline_score_to_par_per_hole=-0.018,
            wave_weather_delta_per_round=0.0,
        ),
        GolfPlayerSnapshot(
            player_id="xander",
            score_to_par=0.0,
            holes_completed=18,
            sg_approach=0.9,
            sg_putting=0.3,
            baseline_score_to_par_per_hole=-0.014,
            wave_weather_delta_per_round=0.05,
        ),
        GolfPlayerSnapshot(
            player_id="jordan",
            score_to_par=1.0,
            holes_completed=18,
            sg_approach=0.5,
            sg_putting=-0.4,
            baseline_score_to_par_per_hole=0.0,
            wave_weather_delta_per_round=0.2,
        ),
        GolfPlayerSnapshot(
            player_id="longshot",
            score_to_par=4.0,
            holes_completed=18,
            sg_approach=-0.8,
            sg_putting=0.1,
            baseline_score_to_par_per_hole=0.025,
            wave_weather_delta_per_round=0.25,
        ),
    ]
    for idx in range(25):
        skill_band = idx % 5
        players.append(
            GolfPlayerSnapshot(
                player_id=f"field_{idx:02d}",
                score_to_par=float((idx % 7) - 1),
                holes_completed=18,
                sg_approach=0.8 - 0.25 * skill_band,
                sg_putting=0.4 - 0.15 * ((idx + 2) % 5),
                baseline_score_to_par_per_hole=-0.012 + 0.004 * skill_band,
                wave_weather_delta_per_round=0.05 * (idx % 4),
            )
        )
    return GolfTournamentState(
        tournament_id="USO26",
        as_of=now,
        cut_rule_size=20,
        cut_holes=36,
        tournament_holes=72,
        wind_forecast_mph=15.0,
        course_baseline_cut_line=2.0,
        players=tuple(players),
    )


def fixture_surface_markets() -> tuple[SurfaceMarket, ...]:
    return (
        SurfaceMarket("KXPGATOP5-USO26-SCOTTIE", "top_n", "scottie", top_n=5),
        SurfaceMarket("KXPGATOP10-USO26-SCOTTIE", "top_n", "scottie", top_n=10),
        SurfaceMarket("KXPGATOP20-USO26-SCOTTIE", "top_n", "scottie", top_n=20),
        SurfaceMarket("KXPGATOP20-USO26-JORDAN", "top_n", "jordan", top_n=20),
        SurfaceMarket("KXPGAMAKECUT-USO26-RORY", "make_cut", "rory"),
        SurfaceMarket("KXPGAMAKECUT-USO26-LONGSHOT", "make_cut", "longshot"),
        SurfaceMarket("KXPGACUTLINE-USO26-2OVER", "cut_line", "2over", cut_line=2),
    )


def fixture_surface_quotes(as_of: datetime) -> tuple[SurfaceQuote, ...]:
    received_at = as_of - timedelta(seconds=2)
    return (
        SurfaceQuote("KXPGATOP5-USO26-SCOTTIE", received_at, 0.38, 0.42, 100.0, 120.0),
        SurfaceQuote("KXPGATOP10-USO26-SCOTTIE", received_at, 0.50, 0.53, 100.0, 120.0),
        SurfaceQuote("KXPGATOP20-USO26-SCOTTIE", received_at, 0.62, 0.65, 100.0, 120.0),
        SurfaceQuote("KXPGATOP20-USO26-JORDAN", received_at, 0.18, 0.22, 80.0, 60.0),
        SurfaceQuote("KXPGAMAKECUT-USO26-RORY", received_at, 0.72, 0.75, 90.0, 100.0),
        SurfaceQuote("KXPGAMAKECUT-USO26-LONGSHOT", received_at, 0.30, 0.35, 50.0, 55.0),
        SurfaceQuote("KXPGACUTLINE-USO26-2OVER", received_at, 0.20, 0.24, 40.0, 45.0),
    )


def fixture_topn_arb_inputs() -> tuple[tuple[SurfaceMarket, ...], tuple[SurfaceQuote, ...]]:
    """Small positive-control top-N dominance violation."""

    received_at = fixture_surface_state().as_of - timedelta(seconds=1)
    markets = (
        SurfaceMarket("KXPGATOP5-USO26-SCOTTIE", "top_n", "scottie", top_n=5),
        SurfaceMarket("KXPGATOP20-USO26-SCOTTIE", "top_n", "scottie", top_n=20),
    )
    quotes = (
        SurfaceQuote("KXPGATOP5-USO26-SCOTTIE", received_at, 0.80, 0.82, 90.0, 120.0),
        SurfaceQuote("KXPGATOP20-USO26-SCOTTIE", received_at, 0.55, 0.58, 100.0, 80.0),
    )
    return markets, quotes


def write_predictions_jsonl(path: Path, predictions: Sequence[GolfSurfacePrediction]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for prediction in predictions:
            handle.write(json.dumps(_jsonable(prediction.as_dict()), sort_keys=True) + "\n")


def _latest_quote_by_market(quotes: Iterable[SurfaceQuote]) -> dict[str, SurfaceQuote]:
    latest: dict[str, SurfaceQuote] = {}
    for quote in quotes:
        current = latest.get(quote.market_ticker)
        if current is None or quote.received_at >= current.received_at:
            latest[quote.market_ticker] = quote
    return latest


def _latest_quote_time(quotes: Iterable[SurfaceQuote]) -> datetime | None:
    return max((quote.received_at for quote in quotes), default=None)


def _topn_arb_candidate(
    *,
    subject_id: str,
    event_key: str,
    lower_market: SurfaceMarket,
    higher_market: SurfaceMarket,
    lower_quote: SurfaceQuote,
    higher_quote: SurfaceQuote,
    as_of: datetime,
    config: GolfTopNArbConfig,
) -> GolfTopNArbCandidate:
    if lower_market.top_n is None or higher_market.top_n is None:
        raise ValueError("top-N arbitrage requires top_n on both markets")
    lower_no_cost = 1.0 - lower_quote.yes_bid
    higher_yes_cost = higher_quote.yes_ask
    lower_no_fee = kalshi_fee(lower_no_cost)
    higher_yes_fee = kalshi_fee(higher_yes_cost)
    total_cost = lower_no_cost + higher_yes_cost + lower_no_fee + higher_yes_fee
    fee_net_floor = 1.0 - total_cost
    executable_size = min(lower_quote.yes_bid_size, higher_quote.yes_ask_size)
    quote_age_seconds = max(
        0.0,
        (as_of - lower_quote.received_at).total_seconds(),
        (as_of - higher_quote.received_at).total_seconds(),
    )
    if quote_age_seconds > config.max_quote_age_seconds:
        reason = "stale_quote"
    elif executable_size < config.min_executable_size:
        reason = "insufficient_touch_size"
    elif fee_net_floor < config.min_net_edge:
        reason = "fails_fee_net_floor_gate"
    else:
        reason = "fee_net_topn_dominance_candidate_needs_paper_rule_audit"
    return GolfTopNArbCandidate(
        subject_id=subject_id,
        event_key=event_key,
        lower_top_n=lower_market.top_n,
        higher_top_n=higher_market.top_n,
        lower_market_ticker=lower_market.market_ticker,
        higher_market_ticker=higher_market.market_ticker,
        decision_time=as_of,
        lower_yes_bid=lower_quote.yes_bid,
        lower_yes_ask=lower_quote.yes_ask,
        higher_yes_bid=higher_quote.yes_bid,
        higher_yes_ask=higher_quote.yes_ask,
        lower_no_cost=lower_no_cost,
        higher_yes_cost=higher_yes_cost,
        lower_no_fee=lower_no_fee,
        higher_yes_fee=higher_yes_fee,
        total_cost=total_cost,
        fee_net_floor=fee_net_floor,
        executable_size=executable_size,
        quote_age_seconds=quote_age_seconds,
        candidate=reason == "fee_net_topn_dominance_candidate_needs_paper_rule_audit",
        reason=reason,
        lower_quote_source=lower_quote.source,
        higher_quote_source=higher_quote.source,
    )


def _topn_event_key(market_ticker: str) -> str:
    parts = market_ticker.upper().split("-")
    if len(parts) >= 3:
        return parts[1]
    return "UNKNOWN"


def _topn_arb_decision_gate(rows: Sequence[GolfTopNArbCandidate], *, candidate_count: int) -> str:
    if not rows:
        return "continue capture: no same-player top-N pairs with executable quotes"
    if candidate_count:
        return (
            "continue paper: fee-net top-N dominance candidate found; audit rules, ties, "
            "simultaneous fill, and settlement before deployment"
        )
    if any(row.fee_net_floor > 0.0 for row in rows):
        return "continue capture: positive top-N floor exists but fails size, freshness, or configured edge gate"
    return "kill/defer: no executable fee-net top-N dominance violation in current books"


def _intent_from_mapping(row: Mapping[str, object]) -> SurfaceShadowIntent:
    executable = _float_value(row.get("executable_price") or row.get("limit_price"))
    return SurfaceShadowIntent(
        market_ticker=_required(row, "market_ticker"),
        decision_time=_parse_datetime(_required(row, "decision_time")),
        side=str(row.get("side") or "").upper(),
        executable_price=executable,
        fee=_float_value(row.get("fee"), default=kalshi_fee(executable)),
        expected_net_edge=_float_value(row.get("expected_net_edge") or row.get("net_edge"), default=0.0),
        fair_yes_probability=_float_value(row.get("fair_yes_probability"), default=0.5),
        source_model=str(row.get("source_model") or "golf_multi_outcome_surface_v1"),
    )


def _surface_quote_from_ws_state_row(
    row: Mapping[str, object],
    book_states: dict[str, _BookState],
) -> SurfaceQuote | None:
    payload = row.get("payload")
    if not isinstance(payload, Mapping):
        return None
    msg = payload.get("msg")
    if not isinstance(msg, Mapping):
        return None
    raw_type = str(payload.get("type") or msg.get("type") or row.get("channel") or "")
    if raw_type == "orderbook_snapshot":
        ticker = str(msg.get("market_ticker") or msg.get("ticker") or "")
        if not ticker:
            return None
        book_states[ticker] = {
            "yes": _ladder_from_levels(msg.get("yes_dollars_fp") or msg.get("yes_dollars") or msg.get("yes")),
            "no": _ladder_from_levels(msg.get("no_dollars_fp") or msg.get("no_dollars") or msg.get("no")),
        }
        return _surface_quote_from_book_state(
            ticker,
            received_at=_parse_datetime(_required(row, "received_at")),
            state=book_states[ticker],
            source="kalshi_ws_orderbook_snapshot",
        )
    if raw_type != "orderbook_delta":
        return None
    ticker = str(msg.get("market_ticker") or msg.get("ticker") or "")
    if not ticker or ticker not in book_states:
        return None
    side = str(msg.get("side") or "").lower()
    if side not in {"yes", "no"}:
        return None
    price = _probability_price(msg.get("price_dollars") or msg.get("price"))
    delta = _float_value(msg.get("delta_fp") or msg.get("delta"), default=0.0)
    ladder = book_states[ticker][side]
    new_size = max(0.0, ladder.get(price, 0.0) + delta)
    if new_size <= 1e-12:
        ladder.pop(price, None)
    else:
        ladder[price] = new_size
    return _surface_quote_from_book_state(
        ticker,
        received_at=_parse_datetime(_required(row, "received_at")),
        state=book_states[ticker],
        source="kalshi_ws_orderbook_delta",
    )


def _surface_quote_from_book_state(
    ticker: str,
    *,
    received_at: datetime,
    state: _BookState,
    source: str,
) -> SurfaceQuote | None:
    yes_bid, yes_bid_size = _best_level_from_ladder(state.get("yes", {}))
    no_bid, no_bid_size = _best_level_from_ladder(state.get("no", {}))
    if yes_bid is None or no_bid is None:
        return None
    yes_ask = 1.0 - no_bid
    if yes_ask < yes_bid:
        return None
    return SurfaceQuote(
        market_ticker=ticker,
        received_at=received_at,
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        yes_bid_size=yes_bid_size,
        yes_ask_size=no_bid_size,
        source=source,
    )


def _first_surface_quote_at_or_after(
    quotes: Sequence[SurfaceQuote],
    target: datetime,
) -> SurfaceQuote | None:
    for quote in quotes:
        if quote.received_at >= target:
            return quote
    return None


def _surface_markout_row(
    candidate: SurfaceShadowIntent,
    *,
    horizon_seconds: int,
    markout_quote: SurfaceQuote | None,
) -> SurfaceMarkoutRow:
    if markout_quote is None:
        return SurfaceMarkoutRow(
            market_ticker=candidate.market_ticker,
            candidate_received_at=candidate.decision_time,
            horizon_seconds=horizon_seconds,
            side=candidate.side,
            executable_price=candidate.executable_price,
            entry_fee=candidate.fee,
            expected_net_edge=candidate.expected_net_edge,
            markout_received_at=None,
            actual_horizon_seconds=None,
            yes_bid=None,
            yes_ask=None,
            markout_price=None,
            clv=None,
            markout_net_after_entry_fee=None,
            source="",
            reason="missing_future_ws_book_quote",
        )
    if candidate.side == "YES":
        markout_price = markout_quote.yes_bid
    elif candidate.side == "NO":
        markout_price = 1.0 - markout_quote.yes_ask
    else:
        raise ValueError(f"unsupported candidate side: {candidate.side}")
    clv = markout_price - candidate.executable_price
    return SurfaceMarkoutRow(
        market_ticker=candidate.market_ticker,
        candidate_received_at=candidate.decision_time,
        horizon_seconds=horizon_seconds,
        side=candidate.side,
        executable_price=candidate.executable_price,
        entry_fee=candidate.fee,
        expected_net_edge=candidate.expected_net_edge,
        markout_received_at=markout_quote.received_at,
        actual_horizon_seconds=(markout_quote.received_at - candidate.decision_time).total_seconds(),
        yes_bid=markout_quote.yes_bid,
        yes_ask=markout_quote.yes_ask,
        markout_price=markout_price,
        clv=clv,
        markout_net_after_entry_fee=clv - candidate.fee,
        source=markout_quote.source,
        reason="matched_ws_book_quote",
    )


def _surface_markout_summary(
    horizon_seconds: int,
    rows: Sequence[SurfaceMarkoutRow],
) -> SurfaceMarkoutSummary:
    horizon_rows = [row for row in rows if row.horizon_seconds == horizon_seconds]
    found = [row for row in horizon_rows if row.clv is not None and row.markout_net_after_entry_fee is not None]
    clvs = [row.clv for row in found if row.clv is not None]
    nets = [
        row.markout_net_after_entry_fee
        for row in found
        if row.markout_net_after_entry_fee is not None
    ]
    return SurfaceMarkoutSummary(
        horizon_seconds=horizon_seconds,
        rows=len(found),
        missing_rows=len(horizon_rows) - len(found),
        mean_clv=_mean(clvs),
        mean_markout_net=_mean(nets),
        positive_clv_rate=sum(1 for value in clvs if value > 0.0) / len(clvs) if clvs else None,
        positive_net_rate=sum(1 for value in nets if value > 0.0) / len(nets) if nets else None,
    )


def _surface_markout_decision_gate(
    *,
    candidate_count: int,
    summaries: Sequence[SurfaceMarkoutSummary],
    min_markout_rows: int,
) -> str:
    if candidate_count == 0:
        return "continue research: no fee-net golf surface candidates to mark out"
    if not any(summary.rows for summary in summaries):
        return "continue capture: no future WS book quotes beyond markout horizons yet"
    eligible = [summary for summary in summaries if summary.rows >= min_markout_rows]
    if not eligible:
        return "continue capture: insufficient golf markout rows to judge CLV"
    if any(summary.mean_markout_net is not None and summary.mean_markout_net > 0.0 for summary in eligible):
        return "continue paper: positive golf CLV after entry fee; prove settlement before edge"
    if any(summary.mean_clv is not None and summary.mean_clv > 0.0 for summary in eligible):
        return "continue paper: positive golf price CLV but entry-fee net is not proven"
    return "kill or defer: golf surface candidates did not hold positive CLV"


def _candidate_from_probability(
    probability: SurfaceProbability,
    quote: SurfaceQuote,
    *,
    config: GolfSurfaceConfig,
    as_of: datetime,
) -> SurfaceCandidate:
    quote_age_ms = max(0, int((as_of - quote.received_at).total_seconds() * 1000))
    yes_gross = probability.fair_yes_probability - quote.yes_ask
    no_price = 1.0 - quote.yes_bid
    no_gross = (1.0 - probability.fair_yes_probability) - no_price
    if yes_gross >= no_gross:
        side = "YES"
        executable = quote.yes_ask
        gross = yes_gross
    else:
        side = "NO"
        executable = no_price
        gross = no_gross
    fee = kalshi_fee(executable)
    net = gross - fee
    stale = quote_age_ms > config.max_quote_age_ms
    if stale:
        reason = "stale_quote"
    elif net < config.min_net_edge:
        reason = "fails_fee_spread_gate"
    else:
        reason = "fee_net_candidate_needs_shadow_fill"
    return SurfaceCandidate(
        market_ticker=probability.market_ticker,
        market_family=probability.market_family,
        subject_id=probability.subject_id,
        side=side,
        fair_yes_probability=probability.fair_yes_probability,
        executable_price=executable,
        limit_price=executable,
        fee=fee,
        gross_edge=gross,
        net_edge=net,
        spread=quote.spread,
        quote_age_ms=quote_age_ms,
        stale_quote=stale,
        candidate=reason == "fee_net_candidate_needs_shadow_fill",
        reason=reason,
    )


def _remaining_score_mean(player: GolfPlayerSnapshot, remaining_holes: int, config: GolfSurfaceConfig) -> float:
    weather_per_hole = (player.wave_weather_delta_per_round * config.weather_weight_per_round) / 18.0
    skill_per_hole = -(player.sg_approach * config.approach_weight_per_sg)
    skill_per_hole -= player.sg_putting * config.putting_weight_per_sg
    context_per_hole = _availability_context_strokes_per_round(player) / 18.0
    return remaining_holes * (
        player.baseline_score_to_par_per_hole + weather_per_hole + skill_per_hole + context_per_hole
    )


def _availability_context_strokes_per_round(player: GolfPlayerSnapshot) -> float:
    return (
        player.injury_strokes_per_round
        + player.rest_fatigue_strokes_per_round
        + player.caddie_absence_strokes_per_round
    )


def _cut_line_market_probability(
    cut_probs: Mapping[int, float],
    *,
    cut_line: int,
    relation: str,
) -> float:
    if relation == "or_better":
        return sum(prob for line, prob in cut_probs.items() if line <= cut_line)
    if relation == "or_worse":
        return sum(prob for line, prob in cut_probs.items() if line >= cut_line)
    return cut_probs.get(cut_line, 0.0)


def _decision_gate(probabilities: Sequence[SurfaceProbability], candidates: Sequence[SurfaceCandidate]) -> str:
    if not probabilities:
        return "continue research: no mapped markets to price"
    candidate_count = sum(1 for item in candidates if item.candidate)
    if candidate_count == 0:
        return "continue research: coherent surface works, no fee-net candidate in fixture quotes"
    return "continue shadow logging: fee-net candidates are hypothetical until markout and settlement clear"


def _require_probability(value: float, name: str) -> None:
    if not math.isfinite(value) or value < 0.0 or value > 1.0:
        raise ValueError(f"{name} must be finite and in [0, 1]")


def _clip_probability(value: float) -> float:
    return min(1.0 - _EPS, max(_EPS, value))


def _float_value(value: object, default: float | None = None) -> float:
    if value is None or str(value).strip() == "":
        if default is not None:
            return default
        raise ValueError("missing numeric value")
    parsed = float(str(value))
    if not math.isfinite(parsed):
        raise ValueError("numeric value must be finite")
    return parsed


def _probability_price(value: object) -> float:
    parsed = _float_value(value)
    if parsed > 1.0 and parsed <= 100.0:
        parsed /= 100.0
    _require_probability(parsed, "price")
    return parsed


def _required(row: Mapping[str, object], field: str) -> str:
    value = row.get(field)
    if value is None or str(value).strip() == "":
        raise ValueError(f"missing required field {field!r}")
    return str(value).strip()


def _required_one(row: Mapping[str, object], fields: Sequence[str]) -> str:
    for field in fields:
        value = row.get(field)
        if value is not None and str(value).strip() != "":
            return str(value).strip()
    joined = ", ".join(repr(field) for field in fields)
    raise ValueError(f"missing one of required fields {joined}")


def _subject_id_from_row(row: Mapping[str, object]) -> str:
    value = row.get("subject_id") or row.get("player_id") or row.get("participant_id")
    if value is None or str(value).strip() == "":
        raise ValueError("missing required field 'subject_id' or 'player_id'")
    return str(value).strip()


def _optional_str(value: object) -> str | None:
    if value is None or str(value).strip() == "":
        return None
    return str(value)


def _optional_int(value: object) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    return int(_float_value(value))


def _infer_top_n_from_row(row: Mapping[str, object]) -> int | None:
    explicit = _optional_int(row.get("top_n"))
    if explicit is not None:
        return explicit
    ticker = str(row.get("market_ticker") or "").upper()
    for marker in ("TOP5", "TOP10", "TOP20", "TOP40"):
        if marker in ticker:
            return int(marker.replace("TOP", ""))
    return None


def _write_csv(path: Path, columns: Sequence[str], rows: Sequence[Mapping[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns), lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in columns})


def _state_to_mapping(state: GolfTournamentState) -> dict[str, object]:
    return {
        "tournament_id": state.tournament_id,
        "as_of": state.as_of.isoformat(),
        "cut_rule_size": state.cut_rule_size,
        "cut_holes": state.cut_holes,
        "tournament_holes": state.tournament_holes,
        "wind_forecast_mph": state.wind_forecast_mph,
        "course_baseline_cut_line": state.course_baseline_cut_line,
        "players": [
            {
                "player_id": player.player_id,
                "score_to_par": player.score_to_par,
                "holes_completed": player.holes_completed,
                "sg_approach": player.sg_approach,
                "sg_putting": player.sg_putting,
                "baseline_score_to_par_per_hole": player.baseline_score_to_par_per_hole,
                "wave_weather_delta_per_round": player.wave_weather_delta_per_round,
                "market_id": player.market_id or "",
                "injury_strokes_per_round": player.injury_strokes_per_round,
                "rest_fatigue_strokes_per_round": player.rest_fatigue_strokes_per_round,
                "caddie_absence_strokes_per_round": player.caddie_absence_strokes_per_round,
            }
            for player in state.players
        ],
    }


def _market_columns() -> tuple[str, ...]:
    return ("market_ticker", "market_family", "subject_id", "top_n", "cut_line", "cut_line_relation")


def _market_to_mapping(market: SurfaceMarket) -> dict[str, object]:
    return {
        "market_ticker": market.market_ticker,
        "market_family": market.market_family,
        "subject_id": market.subject_id,
        "top_n": "" if market.top_n is None else market.top_n,
        "cut_line": "" if market.cut_line is None else market.cut_line,
        "cut_line_relation": market.cut_line_relation,
    }


def _quote_columns() -> tuple[str, ...]:
    return ("market_ticker", "received_at", "yes_bid", "yes_ask", "yes_bid_size", "yes_ask_size", "source")


def _quote_to_mapping(quote: SurfaceQuote) -> dict[str, object]:
    return {
        "market_ticker": quote.market_ticker,
        "received_at": quote.received_at.isoformat(),
        "yes_bid": quote.yes_bid,
        "yes_ask": quote.yes_ask,
        "yes_bid_size": quote.yes_bid_size,
        "yes_ask_size": quote.yes_ask_size,
        "source": quote.source,
    }


def _best_level_from_ladder(ladder: Mapping[float, float]) -> tuple[float | None, float]:
    if not ladder:
        return None, 0.0
    best_price = max(ladder)
    return best_price, ladder[best_price]


def _ladder_from_levels(levels: object) -> dict[float, float]:
    if not isinstance(levels, Sequence) or isinstance(levels, str):
        return {}
    out: dict[float, float] = {}
    for level in levels:
        if not isinstance(level, Sequence) or isinstance(level, str) or len(level) < 2:
            continue
        price = _probability_price(level[0])
        size = _float_value(level[1], default=0.0)
        if size > 0.0:
            out[price] = size
    return out


def _bool_value(value: object, *, default: bool = False) -> bool:
    if value is None or str(value).strip() == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.4f}"


def _fmt_signed(value: float | None) -> str:
    return "n/a" if value is None else f"{value:+.4f}"


def _fixture_future_yes_bid(intent: SurfaceShadowIntent) -> float:
    if intent.side == "YES":
        return min(0.97, intent.executable_price + 0.08)
    future_no_bid = _fixture_future_no_bid(intent)
    return max(0.01, 1.0 - future_no_bid - 0.02)


def _fixture_future_no_bid(intent: SurfaceShadowIntent) -> float:
    if intent.side == "NO":
        return min(0.97, intent.executable_price + 0.08)
    return max(0.01, 1.0 - (_fixture_future_yes_bid(intent) + 0.02))


def _fixture_ws_orderbook_snapshot_row(
    market_ticker: str,
    *,
    received_at: datetime,
    yes_bid: float,
    no_bid: float,
) -> dict[str, object]:
    return {
        "venue": "kalshi",
        "source": "kalshi-ws",
        "channel": "orderbook_snapshot",
        "received_at": received_at.isoformat(),
        "exchange_ts": None,
        "schema_version": "kalshi-ws-v1",
        "metadata": {"ws_type": "orderbook_snapshot"},
        "payload": {
            "type": "orderbook_snapshot",
            "msg": {
                "market_ticker": market_ticker,
                "yes_dollars_fp": [[f"{yes_bid:.4f}", "100.00"]],
                "no_dollars_fp": [[f"{no_bid:.4f}", "100.00"]],
            },
        },
    }


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _event_ts(value: datetime) -> str:
    return value.isoformat().replace("+", "p").replace(":", "").replace("-", "").replace(".", "")


def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    return value
