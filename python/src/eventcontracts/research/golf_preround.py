"""Pre-round golf research helpers for Kalshi top-N markets.

This module is deliberately research-only. It helps choose a golf market
structure from public Kalshi snapshots, then evaluates leak-free pre-round
top-N models on chronological data. A model-vs-market gap remains a candidate
until executable touch, fees, spread, liquidity, freshness, markout, and
settlement checks clear outside this module.
"""

from __future__ import annotations

import csv
import json
import math
import random
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from eventcontracts.research.calibration import brier_score, kalshi_fee, log_loss

_EPS = 1e-9

MARKET_STRUCTURE_SERIES: dict[str, tuple[str, ...]] = {
    "pre_round_winner_outright": (
        "KXPGA",
        "KXUSOPEN",
        "KXTHEOPEN",
        "KXMASTERS",
        "KXGENESISINVITATIONAL",
        "KXPGAMAJORWIN",
    ),
    "first_round_leader": ("KXPGAR1LEAD", "KXLIVR1LEAD"),
    "first_round_top_n": ("KXPGAR1TOP5", "KXPGAR1TOP10", "KXPGAR1TOP20"),
    "make_miss_cut": ("KXPGAMAKECUT", "KXMASTERSCUT"),
    "tournament_top_n": (
        "KXPGATOP5",
        "KXPGATOP10",
        "KXPGATOP20",
        "KXPGATOP40",
        "KXLIVTOP5",
        "KXLIVTOP10",
    ),
    "matchup": ("KXPGAH2H", "KXGOLFH2H", "KXLIVH2H", "KXPGA3BALL", "KXPGA5BALL"),
    "round_score_threshold": (
        "KXPGAROUNDSCORE",
        "KXPGAGOLFERSCORE",
        "KXPGALOWSCORE",
        "KXPGACUTLINE",
        "KXPGAROUNDBIRDIES",
        "KXPGABIRDIES",
    ),
    "other_golf": ("KXPGAPLAYOFF", "KXPGASTROKEMARGIN", "KXPGAWINMARGIN", "KXPGAHOLEINONE"),
}

FEATURE_GROUPS: dict[str, tuple[str, ...]] = {
    "baseline_skill": ("baseline_skill_z", "skill_rank_pct"),
    "strokes_gained": ("sg_off_tee_z", "sg_approach_z", "sg_putting_z"),
    "recent_form": ("recent_form_z",),
    "course_fit": ("course_fit_z", "prior_course_history_z"),
    "field_strength": ("field_strength_z",),
    "tee_weather": ("tee_wave_pm", "wind_mph", "wave_wind_delta"),
    "scoring_volatility": ("scoring_volatility",),
    "driving": ("driving_distance_z", "driving_accuracy_z"),
    "availability_context": (
        "injury_strokes_per_round",
        "rest_fatigue_strokes_per_round",
        "caddie_absence_strokes_per_round",
        "withdrawal_risk",
    ),
    "market_liquidity": ("market_mid", "time_to_start_hours", "liquidity", "spread"),
}

MODEL_FEATURES: tuple[str, ...] = (
    "baseline_skill_z",
    "sg_off_tee_z",
    "sg_approach_z",
    "sg_putting_z",
    "recent_form_z",
    "course_fit_z",
    "field_strength_z",
    "tee_wave_pm",
    "wind_mph",
    "wave_wind_delta",
    "scoring_volatility",
    "driving_distance_z",
    "driving_accuracy_z",
    "prior_course_history_z",
    "injury_strokes_per_round",
    "rest_fatigue_strokes_per_round",
    "caddie_absence_strokes_per_round",
    "withdrawal_risk",
    "market_mid",
    "time_to_start_hours",
    "liquidity",
    "spread",
)

PREROUND_CSV_COLUMNS: tuple[str, ...] = (
    "event_date",
    "tournament_id",
    "player_id",
    "player_name",
    "target",
    "top_n",
    "field_size",
    "course_archetype",
    "tee_wave",
    "round_number",
    "market_bid",
    "market_ask",
    "odds_probability",
    "reference_price_source",
    *MODEL_FEATURES,
)


@dataclass(frozen=True)
class DeviggedOdds:
    """Normalized probabilities from a mutually exclusive decimal-odds board."""

    probabilities: dict[str, float]
    overround: float


@dataclass(frozen=True)
class MarketStructureSummary:
    """Tradability summary for one golf market structure."""

    structure: str
    active_markets: int
    quoted_markets: int
    median_spread: float | None
    min_spread: float | None
    total_volume: float
    total_volume_24h: float
    total_open_interest: float
    example_tickers: tuple[str, ...]

    @property
    def score(self) -> float:
        spread_penalty = 1.0 if self.median_spread is None else max(self.median_spread, 0.005)
        quoted_share = self.quoted_markets / max(self.active_markets, 1)
        depth = math.log1p(self.total_volume_24h + self.total_open_interest)
        clarity = {
            "tournament_top_n": 1.25,
            "make_miss_cut": 1.15,
            "matchup": 1.05,
            "first_round_top_n": 1.0,
            "first_round_leader": 0.85,
            "pre_round_winner_outright": 0.75,
            "round_score_threshold": 0.8,
            "other_golf": 0.45,
        }.get(self.structure, 0.5)
        return quoted_share * depth * clarity / spread_penalty

    def as_dict(self) -> dict[str, object]:
        return {
            "structure": self.structure,
            "active_markets": self.active_markets,
            "quoted_markets": self.quoted_markets,
            "median_spread": self.median_spread,
            "min_spread": self.min_spread,
            "total_volume": self.total_volume,
            "total_volume_24h": self.total_volume_24h,
            "total_open_interest": self.total_open_interest,
            "example_tickers": list(self.example_tickers),
            "score": self.score,
        }


@dataclass(frozen=True)
class MarketSelection:
    """Chosen market structure plus rejected alternatives."""

    chosen: MarketStructureSummary
    rejected: tuple[MarketStructureSummary, ...]
    rationale: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "chosen": self.chosen.as_dict(),
            "rejected": [item.as_dict() for item in self.rejected],
            "rationale": list(self.rationale),
        }


@dataclass(frozen=True)
class GolfPreRoundRow:
    """One point-in-time player/market row for pre-round top-N modeling."""

    event_date: date
    tournament_id: str
    player_id: str
    player_name: str
    target: int
    top_n: int
    field_size: int
    course_archetype: str
    tee_wave: str
    round_number: int
    numeric: dict[str, float]
    market_bid: float | None = None
    market_ask: float | None = None
    odds_probability: float | None = None
    reference_price_source: str | None = None

    @property
    def group_key(self) -> tuple[date, str]:
        return (self.event_date, self.tournament_id)

    @property
    def market_mid(self) -> float | None:
        if self.market_bid is None or self.market_ask is None:
            return None
        return (self.market_bid + self.market_ask) / 2.0

    @property
    def spread(self) -> float | None:
        if self.market_bid is None or self.market_ask is None:
            return None
        return max(0.0, self.market_ask - self.market_bid)

    def feature_value(self, name: str) -> float:
        if name == "tee_wave_pm":
            return 1.0 if self.tee_wave.lower() == "pm" else 0.0
        if name == "market_mid":
            return self.market_mid if self.market_mid is not None else self.odds_probability or 0.5
        if name == "spread":
            return self.spread if self.spread is not None else 0.05
        if name == "skill_rank_pct":
            return self.numeric.get(name, 0.5)
        return self.numeric.get(name, 0.0)


@dataclass(frozen=True)
class ModelMetrics:
    """Proper-score metrics for one model."""

    n: int
    brier: float
    log_loss: float
    ece: float

    def as_dict(self) -> dict[str, float | int]:
        return {"n": self.n, "brier": self.brier, "log_loss": self.log_loss, "ece": self.ece}


@dataclass(frozen=True)
class CandidateCheck:
    """Cost/liquidity check for a model candidate against executable quotes."""

    player_id: str
    tournament_id: str
    model_probability: float
    market_bid: float
    market_ask: float
    best_side: str
    executable_price: float
    fee: float
    gross_probability_gap: float
    net_after_fee_and_spread: float
    liquidity: float
    candidate: bool
    reason: str

    def as_dict(self) -> dict[str, object]:
        return {
            "player_id": self.player_id,
            "tournament_id": self.tournament_id,
            "model_probability": self.model_probability,
            "market_bid": self.market_bid,
            "market_ask": self.market_ask,
            "best_side": self.best_side,
            "executable_price": self.executable_price,
            "fee": self.fee,
            "gross_probability_gap": self.gross_probability_gap,
            "net_after_fee_and_spread": self.net_after_fee_and_spread,
            "liquidity": self.liquidity,
            "candidate": self.candidate,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class GolfPreRoundResearchReport:
    """Full no-network/CSV research result."""

    target_market: str
    target_top_n: int
    data_source: str
    train_rows: int
    test_rows: int
    train_events: int
    test_events: int
    provider_status: dict[str, bool]
    metrics: dict[str, ModelMetrics]
    calibration_by_group: dict[str, dict[str, float]]
    mutual_information: dict[str, float]
    permutation_importance: dict[str, float]
    grouped_residuals: dict[str, float]
    cluster_residuals: dict[str, float]
    interaction_residuals: dict[str, float]
    top_candidate_checks: tuple[CandidateCheck, ...]
    decision: str
    leakage_controls: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "target_market": self.target_market,
            "target_top_n": self.target_top_n,
            "data_source": self.data_source,
            "train_rows": self.train_rows,
            "test_rows": self.test_rows,
            "train_events": self.train_events,
            "test_events": self.test_events,
            "provider_status": self.provider_status,
            "metrics": {name: metrics.as_dict() for name, metrics in self.metrics.items()},
            "calibration_by_group": self.calibration_by_group,
            "mutual_information": self.mutual_information,
            "permutation_importance": self.permutation_importance,
            "grouped_residuals": self.grouped_residuals,
            "cluster_residuals": self.cluster_residuals,
            "interaction_residuals": self.interaction_residuals,
            "top_candidate_checks": [item.as_dict() for item in self.top_candidate_checks],
            "decision": self.decision,
            "leakage_controls": list(self.leakage_controls),
        }


@dataclass(frozen=True)
class ReferenceTopNConfig:
    """Settings for bookmaker-outright to Kalshi top-N inference."""

    simulations: int = 5000
    seed: int = 23
    min_net_edge: float = 0.03
    min_executable_size: float = 100.0
    kalshi_event_token: str = "USO26"
    title_contains: str = "U.S. Open"
    max_book_overround: float = 1.8

    def __post_init__(self) -> None:
        if self.simulations <= 0:
            raise ValueError("simulations must be positive")
        if self.min_net_edge < 0.0:
            raise ValueError("min_net_edge must be non-negative")
        if self.min_executable_size < 0.0:
            raise ValueError("min_executable_size must be non-negative")
        if self.max_book_overround <= 1.0:
            raise ValueError("max_book_overround must be > 1")


@dataclass(frozen=True)
class ReferenceTopNCandidate:
    """One executable-touch check from a reference top-N model."""

    market_ticker: str
    tournament_id: str
    player_id: str
    player_name: str
    top_n: int
    decision_time: datetime
    fair_yes_probability: float
    yes_bid: float
    yes_ask: float
    yes_bid_size: float
    yes_ask_size: float
    side: str
    executable_price: float
    executable_size: float
    fee: float
    gross_edge: float
    net_edge: float
    candidate: bool
    reason: str
    odds_sources: int

    def as_dict(self) -> dict[str, object]:
        return {
            "market_ticker": self.market_ticker,
            "tournament_id": self.tournament_id,
            "player_id": self.player_id,
            "player_name": self.player_name,
            "top_n": self.top_n,
            "decision_time": self.decision_time.isoformat(),
            "fair_yes_probability": self.fair_yes_probability,
            "yes_bid": self.yes_bid,
            "yes_ask": self.yes_ask,
            "yes_bid_size": self.yes_bid_size,
            "yes_ask_size": self.yes_ask_size,
            "side": self.side,
            "executable_price": self.executable_price,
            "executable_size": self.executable_size,
            "fee": self.fee,
            "gross_edge": self.gross_edge,
            "net_edge": self.net_edge,
            "candidate": self.candidate,
            "reason": self.reason,
            "odds_sources": self.odds_sources,
        }

    def as_intent(self) -> dict[str, object]:
        return {
            "intent_id": f"reference-topn-{self.market_ticker}-{_event_ts(self.decision_time)}",
            "decision_time": self.decision_time.isoformat(),
            "market_ticker": self.market_ticker,
            "market_family": "top_n",
            "side": self.side,
            "quantity": 1.0,
            "fair_yes_probability": self.fair_yes_probability,
            "executable_price": self.executable_price,
            "limit_price": self.executable_price,
            "fee": self.fee,
            "expected_net_edge": self.net_edge,
            "source_model": "golf_reference_outright_plackett_luce_topn_v1",
            "candidate": self.candidate,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ReferenceTopNReport:
    """Inference-only golf top-N reference-pricing report."""

    as_of: datetime
    market_rows: int
    filtered_market_rows: int
    matched_market_rows: int
    odds_rows: int
    odds_player_count: int
    tournament_filter: str
    candidate_count: int
    max_net_edge: float | None
    decision_gate: str
    candidates: tuple[ReferenceTopNCandidate, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "as_of": self.as_of.isoformat(),
            "market_rows": self.market_rows,
            "filtered_market_rows": self.filtered_market_rows,
            "matched_market_rows": self.matched_market_rows,
            "odds_rows": self.odds_rows,
            "odds_player_count": self.odds_player_count,
            "tournament_filter": self.tournament_filter,
            "candidate_count": self.candidate_count,
            "max_net_edge": self.max_net_edge,
            "decision_gate": self.decision_gate,
            "candidates": [item.as_dict() for item in self.candidates],
        }


def devig_decimal_odds(decimal_odds: Mapping[str, float]) -> DeviggedOdds:
    """Normalize a mutually exclusive decimal-odds board."""

    raw: dict[str, float] = {}
    for name, odds in decimal_odds.items():
        if odds <= 1.0 or not math.isfinite(odds):
            raise ValueError(f"decimal odds for {name!r} must be finite and > 1")
        raw[name] = 1.0 / odds
    overround = sum(raw.values())
    if overround <= 0:
        raise ValueError("empty odds board")
    return DeviggedOdds(probabilities={name: value / overround for name, value in raw.items()}, overround=overround)


def evaluate_reference_topn(
    *,
    snapshot_rows: Sequence[Mapping[str, object]],
    odds_rows: Sequence[Mapping[str, object]],
    config: ReferenceTopNConfig | None = None,
    as_of: datetime | None = None,
) -> ReferenceTopNReport:
    """Price Kalshi top-N markets from a bookmaker outright reference board.

    This is inference-only. It is useful for creating shadow intents for CLV
    measurement, not for claiming edge without markout and settlement.
    """

    cfg = config or ReferenceTopNConfig()
    odds_by_player, source_count_by_player = _reference_odds_strengths(
        odds_rows,
        max_book_overround=cfg.max_book_overround,
    )
    filtered = [
        row
        for row in snapshot_rows
        if _snapshot_matches_reference_filter(row, config=cfg)
    ]
    top_n_values = sorted({top_n for row in filtered if (top_n := _infer_top_n(row)) is not None})
    probabilities = _plackett_luce_topn_probabilities(
        odds_by_player,
        top_n_values=top_n_values,
        simulations=cfg.simulations,
        seed=cfg.seed,
    )
    candidates: list[ReferenceTopNCandidate] = []
    for row in filtered:
        player_key = _player_key(str(row.get("player_name") or row.get("player_id") or ""))
        top_n = _infer_top_n(row)
        if top_n is None or player_key not in odds_by_player:
            continue
        try:
            candidates.append(
                _reference_candidate_from_snapshot(
                    row,
                    fair_yes_probability=probabilities.get(top_n, {}).get(player_key, 0.0),
                    top_n=top_n,
                    odds_sources=source_count_by_player.get(player_key, 0),
                    config=cfg,
                )
            )
        except ValueError:
            continue
    ordered = tuple(sorted(candidates, key=lambda item: item.net_edge, reverse=True))
    candidate_count = sum(1 for item in ordered if item.candidate)
    return ReferenceTopNReport(
        as_of=as_of or datetime.now(UTC),
        market_rows=len(snapshot_rows),
        filtered_market_rows=len(filtered),
        matched_market_rows=len(ordered),
        odds_rows=len(odds_rows),
        odds_player_count=len(odds_by_player),
        tournament_filter=_reference_filter_label(cfg),
        candidate_count=candidate_count,
        max_net_edge=max((item.net_edge for item in ordered), default=None),
        decision_gate=_reference_topn_decision_gate(
            filtered_market_rows=len(filtered),
            matched_market_rows=len(ordered),
            candidate_count=candidate_count,
        ),
        candidates=ordered[:25],
    )


def write_reference_topn_outputs(
    report: ReferenceTopNReport,
    *,
    report_json: Path,
    report_md: Path | None = None,
    intents_jsonl: Path | None = None,
) -> None:
    """Write reference top-N report and optional shadow-intent ledger."""

    report_json.parent.mkdir(parents=True, exist_ok=True)
    report_json.write_text(json_dumps(report.as_dict()) + "\n", encoding="utf-8")
    if report_md is not None:
        report_md.parent.mkdir(parents=True, exist_ok=True)
        report_md.write_text(render_reference_topn_markdown(report), encoding="utf-8")
    if intents_jsonl is not None:
        intents_jsonl.parent.mkdir(parents=True, exist_ok=True)
        with intents_jsonl.open("w", encoding="utf-8") as handle:
            for candidate in report.candidates:
                if candidate.candidate:
                    handle.write(json_dumps(candidate.as_intent()) + "\n")


def render_reference_topn_markdown(report: ReferenceTopNReport) -> str:
    """Render a compact reference top-N inference report."""

    lines = [
        "# Golf Reference Top-N Inference",
        "",
        f"- As of: `{report.as_of.isoformat()}`",
        f"- Tournament filter: `{report.tournament_filter}`",
        f"- Market rows: `{report.market_rows}`",
        f"- Filtered market rows: `{report.filtered_market_rows}`",
        f"- Matched market rows: `{report.matched_market_rows}`",
        f"- Odds rows / players: `{report.odds_rows}` / `{report.odds_player_count}`",
        f"- Candidate rows: `{report.candidate_count}`",
        f"- Max net edge: `{_fmt_optional_signed(report.max_net_edge)}`",
        f"- Decision: **{report.decision_gate}**",
        "",
        "| Market | Player | Top-N | Side | Fair | Bid | Ask | Net | Size | Reason |",
        "| --- | --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for candidate in report.candidates[:15]:
        lines.append(
            f"| {candidate.market_ticker} | {candidate.player_name} | {candidate.top_n} | "
            f"{candidate.side} | {candidate.fair_yes_probability:.4f} | {candidate.yes_bid:.4f} | "
            f"{candidate.yes_ask:.4f} | {candidate.net_edge:+.4f} | "
            f"{candidate.executable_size:.2f} | {candidate.reason} |"
        )
    lines.extend(
        [
            "",
            "## Boundary",
            "",
            "This uses bookmaker outright probabilities as a reference prior and simulates top-N placement with a "
            "Plackett-Luce ranking model. It is not edge until executable touch, freshness, CLV/markout, and "
            "settlement evidence clear.",
            "",
        ]
    )
    return "\n".join(lines)


def fixture_reference_topn_inputs() -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Small deterministic reference top-N fixture."""

    captured_at = datetime(2026, 6, 3, 18, 0, tzinfo=UTC).isoformat()
    odds_as_of = datetime(2026, 6, 3, 17, 30, tzinfo=UTC).isoformat()
    players = [
        ("Scottie Scheffler", 0.25, 0.22, 0.24, 5000.0),
        ("Rory McIlroy", 0.16, 0.16, 0.18, 3500.0),
        ("Xander Schauffele", 0.12, 0.12, 0.14, 3000.0),
        ("Long Shot", 0.01, 0.18, 0.22, 1500.0),
    ]
    snapshots: list[dict[str, object]] = []
    odds: list[dict[str, object]] = []
    for name, win_prob, bid, ask, size in players:
        player_id = _player_key(name).upper()[:5]
        snapshots.append(
            {
                "captured_at": captured_at,
                "tournament_id": "KXPGATOP5-USO26",
                "player_id": player_id,
                "player_name": name,
                "market_ticker": f"KXPGATOP5-USO26-{player_id}",
                "title": f"U.S. Open: Will {name} finish top 5?",
                "yes_bid": bid,
                "yes_ask": ask,
                "yes_bid_size": size,
                "yes_ask_size": size,
                "open_interest": size,
            }
        )
        for source in ("fixture-book-a", "fixture-book-b"):
            odds.append(
                {
                    "source": source,
                    "tournament_id": "fixture-us-open",
                    "player_id": _player_key(name),
                    "player_name": name,
                    "odds_as_of": odds_as_of,
                    "market_type": "outright_reference",
                    "odds_probability": win_prob,
                    "reference_price_source": f"{source}:outright_reference",
                }
            )
    return snapshots, odds


def _reference_odds_strengths(
    odds_rows: Sequence[Mapping[str, object]],
    *,
    max_book_overround: float,
) -> tuple[dict[str, float], dict[str, int]]:
    by_player: defaultdict[str, list[float]] = defaultdict(list)
    sources: defaultdict[str, set[str]] = defaultdict(set)
    for row in odds_rows:
        player_key = _player_key(str(row.get("player_name") or row.get("player_id") or ""))
        probability = _optional_float(row.get("odds_probability") or row.get("probability"))
        overround = _optional_float(row.get("overround"))
        if overround is not None and overround > max_book_overround:
            continue
        if not player_key or probability is None or probability <= 0.0:
            continue
        by_player[player_key].append(probability)
        sources[player_key].add(str(row.get("source") or row.get("reference_price_source") or "unknown"))
    raw = {player: sum(values) / len(values) for player, values in by_player.items() if values}
    total = sum(raw.values())
    if total <= 0.0:
        return {}, {}
    return (
        {player: value / total for player, value in raw.items()},
        {player: len(sources[player]) for player in raw},
    )


def _plackett_luce_topn_probabilities(
    strengths: Mapping[str, float],
    *,
    top_n_values: Sequence[int],
    simulations: int,
    seed: int,
) -> dict[int, dict[str, float]]:
    top_ns = tuple(sorted(set(top_n_values)))
    if not strengths or not top_ns:
        return {}
    players = list(strengths)
    weights = [max(strengths[player], 1e-12) for player in players]
    counts: dict[int, dict[str, int]] = {top_n: {player: 0 for player in players} for top_n in top_ns}
    rng = random.Random(seed)
    for _ in range(simulations):
        ranked = sorted(
            (rng.expovariate(weight), player)
            for player, weight in zip(players, weights, strict=True)
        )
        for top_n in top_ns:
            for _score, player in ranked[: min(top_n, len(ranked))]:
                counts[top_n][player] += 1
    return {
        top_n: {player: count / simulations for player, count in player_counts.items()}
        for top_n, player_counts in counts.items()
    }


def _reference_candidate_from_snapshot(
    row: Mapping[str, object],
    *,
    fair_yes_probability: float,
    top_n: int,
    odds_sources: int,
    config: ReferenceTopNConfig,
) -> ReferenceTopNCandidate:
    fair = _clip_probability(fair_yes_probability)
    yes_bid = _probability_price(row.get("yes_bid_dollars") or row.get("yes_bid"))
    yes_ask = _probability_price(row.get("yes_ask_dollars") or row.get("yes_ask"))
    if yes_ask < yes_bid:
        raise ValueError("yes_ask must be >= yes_bid")
    yes_bid_size = _float_value(row.get("yes_bid_size"), 0.0)
    yes_ask_size = _float_value(row.get("yes_ask_size"), 0.0)
    yes_fee = kalshi_fee(yes_ask)
    no_price = 1.0 - yes_bid
    no_fee = kalshi_fee(no_price)
    yes_edge = fair - yes_ask - yes_fee
    no_edge = (1.0 - fair) - no_price - no_fee
    if yes_edge >= no_edge:
        side = "YES"
        executable = yes_ask
        executable_size = yes_ask_size
        fee = yes_fee
        gross = fair - executable
        net = yes_edge
    else:
        side = "NO"
        executable = no_price
        executable_size = yes_bid_size
        fee = no_fee
        gross = (1.0 - fair) - executable
        net = no_edge
    if executable_size < config.min_executable_size:
        reason = "insufficient_touch_size"
    elif odds_sources < 2:
        reason = "insufficient_reference_sources"
    elif net < config.min_net_edge:
        reason = "fails_fee_spread_gate"
    else:
        reason = "fee_net_reference_candidate_needs_ws_markout_settlement"
    return ReferenceTopNCandidate(
        market_ticker=_required(row, "market_ticker"),
        tournament_id=str(row.get("tournament_id") or ""),
        player_id=str(row.get("player_id") or ""),
        player_name=str(row.get("player_name") or row.get("player_id") or ""),
        top_n=top_n,
        decision_time=_parse_datetime(str(row.get("captured_at") or datetime.now(UTC).isoformat())),
        fair_yes_probability=fair,
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        yes_bid_size=yes_bid_size,
        yes_ask_size=yes_ask_size,
        side=side,
        executable_price=executable,
        executable_size=executable_size,
        fee=fee,
        gross_edge=gross,
        net_edge=net,
        candidate=reason == "fee_net_reference_candidate_needs_ws_markout_settlement",
        reason=reason,
        odds_sources=odds_sources,
    )


def _snapshot_matches_reference_filter(row: Mapping[str, object], *, config: ReferenceTopNConfig) -> bool:
    ticker = str(row.get("market_ticker") or "").upper()
    tournament_id = str(row.get("tournament_id") or "").upper()
    title = str(row.get("title") or "")
    if config.kalshi_event_token:
        token = config.kalshi_event_token.upper()
        if token not in ticker and token not in tournament_id:
            return False
    if config.title_contains and config.title_contains.lower() not in title.lower():
        return False
    return _infer_top_n(row) is not None


def _reference_filter_label(config: ReferenceTopNConfig) -> str:
    return (
        f"event_token={config.kalshi_event_token or '*'}, "
        f"title_contains={config.title_contains or '*'}, "
        f"max_book_overround={config.max_book_overround:.3f}"
    )


def _reference_topn_decision_gate(
    *,
    filtered_market_rows: int,
    matched_market_rows: int,
    candidate_count: int,
) -> str:
    if filtered_market_rows == 0:
        return "continue research: no Kalshi top-N rows matched the reference tournament filter"
    if matched_market_rows == 0:
        return "continue research: reference odds could not be joined to filtered Kalshi player names"
    if candidate_count == 0:
        return "continue research: reference top-N surface has no fee-net executable candidates"
    return "start read-only markout: reference top-N candidates need WS CLV and settlement before edge"


def _infer_top_n(row: Mapping[str, object]) -> int | None:
    raw = row.get("top_n")
    if raw is not None and str(raw).strip():
        return int(_float_value(raw))
    text = f"{row.get('market_ticker') or ''} {row.get('title') or ''}".upper()
    for top_n in (5, 10, 20, 40):
        if f"TOP{top_n}" in text or f"TOP {top_n}" in text:
            return top_n
    return None


def _player_key(name: str) -> str:
    return "".join(ch for ch in name.lower() if ch.isalnum())


def _probability_price(value: object) -> float:
    parsed = _float_value(value)
    if parsed > 1.0 and parsed <= 100.0:
        parsed /= 100.0
    return _clip_probability(parsed)


def json_dumps(payload: Mapping[str, object]) -> str:
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True)


def summarize_kalshi_golf_markets(markets: Sequence[Mapping[str, object]]) -> tuple[MarketStructureSummary, ...]:
    """Categorize public Kalshi market snapshots into golf structures."""

    grouped: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    for market in markets:
        structure = categorize_market(market)
        if structure is not None:
            grouped[structure].append(market)
    summaries: list[MarketStructureSummary] = []
    for structure in MARKET_STRUCTURE_SERIES:
        items = grouped.get(structure, [])
        quoted: list[Mapping[str, object]] = []
        spreads: list[float] = []
        for item in items:
            bid_ask = _market_bid_ask(item)
            if bid_ask is None:
                continue
            quoted.append(item)
            spreads.append(bid_ask[1] - bid_ask[0])
        summaries.append(
            MarketStructureSummary(
                structure=structure,
                active_markets=len(items),
                quoted_markets=len(quoted),
                median_spread=_median(spreads) if spreads else None,
                min_spread=min(spreads) if spreads else None,
                total_volume=sum(_float_field(item, "volume_fp", "volume") for item in items),
                total_volume_24h=sum(_float_field(item, "volume_24h_fp", "volume_24h") for item in items),
                total_open_interest=sum(_float_field(item, "open_interest_fp", "open_interest") for item in items),
                example_tickers=tuple(
                    str(item.get("ticker", ""))
                    for item in sorted(quoted, key=_tradability_sort_key)[:5]
                    if item.get("ticker")
                ),
            )
        )
    return tuple(summaries)


def select_best_market_structure(summaries: Sequence[MarketStructureSummary]) -> MarketSelection:
    """Select the best pre-round golf target from structure summaries."""

    viable = [item for item in summaries if item.quoted_markets > 0]
    if not viable:
        raise ValueError("no quoted golf market structures available")
    ordered = sorted(viable, key=lambda item: item.score, reverse=True)
    chosen = ordered[0]
    rejected = tuple(
        item
        for item in sorted(summaries, key=lambda summary: summary.score, reverse=True)
        if item.structure != chosen.structure
    )
    rationale = (
        "ranked by quoted coverage, spread, 24h volume/open interest, settlement clarity, "
        "and pre-round modelability",
        "tournament top-N is preferred when liquidity is competitive because it is binary, "
        "broad, and historically labelable",
        "model output is a candidate only until executable touch, fees, spread, liquidity, "
        "freshness, CLV/markout, and settlement are checked",
    )
    return MarketSelection(chosen=chosen, rejected=rejected, rationale=rationale)


def categorize_market(market: Mapping[str, object]) -> str | None:
    series = str(market.get("series_ticker") or "").upper()
    ticker = str(market.get("ticker") or "").upper()
    title = str(market.get("title") or "").lower()
    if not series and "-" in ticker:
        series = ticker.split("-", 1)[0]
    for structure, series_tickers in MARKET_STRUCTURE_SERIES.items():
        if series in series_tickers:
            return structure
    if "finish top" in title and "round 1" not in title:
        return "tournament_top_n"
    if "make the cut" in title:
        return "make_miss_cut"
    if "round 1" in title and "top" in title:
        return "first_round_top_n"
    if "lead at the end of round 1" in title:
        return "first_round_leader"
    if "head-to-head" in title or " beat " in title:
        return "matchup"
    if "round score" in title or "strokes" in title:
        return "round_score_threshold"
    if "winner" in title or " win " in title:
        return "pre_round_winner_outright"
    return None


def load_preround_rows_csv(path: Path) -> list[GolfPreRoundRow]:
    """Load point-in-time pre-round top-N rows from CSV."""

    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"empty CSV: {path}")
        return [row_from_mapping(row) for row in reader]


def write_preround_rows_csv(path: Path, rows: Sequence[GolfPreRoundRow]) -> None:
    """Write rows in the schema consumed by :func:`load_preround_rows_csv`."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(PREROUND_CSV_COLUMNS), lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow(preround_row_to_mapping(row))


def preround_row_to_mapping(row: GolfPreRoundRow) -> dict[str, object]:
    """Serialize one model row to a stable CSV mapping."""

    payload: dict[str, object] = {
        "event_date": row.event_date.isoformat(),
        "tournament_id": row.tournament_id,
        "player_id": row.player_id,
        "player_name": row.player_name,
        "target": row.target,
        "top_n": row.top_n,
        "field_size": row.field_size,
        "course_archetype": row.course_archetype,
        "tee_wave": row.tee_wave,
        "round_number": row.round_number,
        "market_bid": "" if row.market_bid is None else row.market_bid,
        "market_ask": "" if row.market_ask is None else row.market_ask,
        "odds_probability": "" if row.odds_probability is None else row.odds_probability,
        "reference_price_source": row.reference_price_source or "",
    }
    for feature in MODEL_FEATURES:
        payload[feature] = row.feature_value(feature)
    return payload


def row_from_mapping(row: Mapping[str, object]) -> GolfPreRoundRow:
    """Build a row from a CSV/JSON mapping."""

    event_date = _parse_date(_required(row, "event_date"))
    target = int(_float_value(row.get("target") or row.get("topn_result")))
    top_n = int(_float_value(row.get("top_n"), 20.0))
    field_size = int(_float_value(row.get("field_size"), 100.0))
    market_bid = _optional_float(row.get("market_bid"))
    market_ask = _optional_float(row.get("market_ask"))
    odds_probability = _optional_float(row.get("odds_probability") or row.get("odds_topn_probability"))
    numeric = {feature: _float_value(row.get(feature), 0.0) for feature in MODEL_FEATURES}
    if "market_mid" not in row and market_bid is not None and market_ask is not None:
        numeric["market_mid"] = (market_bid + market_ask) / 2.0
    if "spread" not in row and market_bid is not None and market_ask is not None:
        numeric["spread"] = market_ask - market_bid
    return GolfPreRoundRow(
        event_date=event_date,
        tournament_id=_required(row, "tournament_id"),
        player_id=_required(row, "player_id"),
        player_name=str(row.get("player_name") or row.get("player_id") or ""),
        target=target,
        top_n=top_n,
        field_size=field_size,
        course_archetype=str(row.get("course_archetype") or "unknown"),
        tee_wave=str(row.get("tee_wave") or "unknown"),
        round_number=int(_float_value(row.get("round_number"), 1.0)),
        numeric=numeric,
        market_bid=market_bid,
        market_ask=market_ask,
        odds_probability=odds_probability,
        reference_price_source=str(row.get("reference_price_source") or "") or None,
    )


def run_preround_research(
    rows: Sequence[GolfPreRoundRow],
    *,
    target_top_n: int = 20,
    simulations: int = 1500,
    seed: int = 11,
    provider_status: Mapping[str, bool] | None = None,
    fixture_mode: bool = False,
) -> GolfPreRoundResearchReport:
    """Evaluate baselines and three candidate models on chronological OOS splits."""

    filtered = [row for row in rows if row.top_n == target_top_n]
    if len(filtered) < 8:
        raise ValueError("need at least 8 rows for chronological OOS research")
    train, test = chronological_split(filtered)
    if not train or not test:
        raise ValueError("chronological split produced empty train or test")
    prepared = _prepare_feature_matrix(train, MODEL_FEATURES)
    train_y = [float(row.target) for row in train]

    field_rate = sum(train_y) / len(train_y)
    logistic = _fit_logistic(train, train_y, prepared)
    residual = _fit_anchor_residual(train, train_y, prepared)

    predictions: dict[str, list[float]] = {
        "field_rate": [field_rate for _ in test],
        "market_implied": [_anchor_probability(row, field_rate, prefer_market=True) for row in test],
        "odds_implied": [_odds_probability(row, field_rate) for row in test],
        "skill_rank": [_skill_rank_probability(row, field_rate) for row in test],
        "calibrated_logistic": logistic.predict_many(test),
        "score_distribution_simulation": _score_distribution_topn_probabilities(
            filtered,
            evaluation_rows=test,
            top_n=target_top_n,
            simulations=simulations,
            seed=seed,
        ),
        "market_odds_residual": residual.predict_many(test),
    }
    outcomes = [float(row.target) for row in test]
    metrics = {name: _metrics(probs, outcomes) for name, probs in predictions.items()}
    best_name = min(metrics, key=lambda name: metrics[name].brier)
    best_probs = predictions[best_name]
    fixture_data = fixture_mode or _looks_like_fixture_data(filtered)

    report = GolfPreRoundResearchReport(
        target_market="Kalshi PGA tournament top-N, selected structure: top-20",
        target_top_n=target_top_n,
        data_source="fixture" if fixture_data else "point_in_time_csv",
        train_rows=len(train),
        test_rows=len(test),
        train_events=len({row.group_key for row in train}),
        test_events=len({row.group_key for row in test}),
        provider_status=dict(provider_status or {}),
        metrics=metrics,
        calibration_by_group=_calibration_groups(test, best_probs),
        mutual_information=_feature_group_mi(train),
        permutation_importance=_permutation_importance(test, outcomes, logistic, metrics["calibrated_logistic"].brier),
        grouped_residuals=_grouped_residuals(test, best_probs),
        cluster_residuals=_cluster_residuals(test, best_probs),
        interaction_residuals=_interaction_residuals(test, best_probs),
        top_candidate_checks=tuple(
            sorted(
                _candidate_checks(test, best_probs, actionable=not fixture_data),
                key=lambda item: item.net_after_fee_and_spread,
                reverse=True,
            )[:8]
        ),
        decision=_decision(metrics, best_name, test, best_probs, fixture_mode=fixture_data),
        leakage_controls=(
            "chronological split by tournament date; no row from a held-out event appears in train",
            "features are pre-round only: baseline skill, trailing SG/form, course fit, "
            "tee wave/weather, odds/market state",
            "final leaderboard rank/result appears only in the target column",
            "course history must be joined with as-of dates before the event start or omitted",
            "market/odds features are treated as reference prices, not proof of edge",
        ),
    )
    return report


def chronological_split(
    rows: Sequence[GolfPreRoundRow],
    *,
    test_fraction: float = 0.34,
) -> tuple[list[GolfPreRoundRow], list[GolfPreRoundRow]]:
    """Split by event date/tournament, never by random row."""

    if not 0.0 < test_fraction < 1.0:
        raise ValueError("test_fraction must be in (0, 1)")
    groups = sorted({row.group_key for row in rows})
    split_at = max(1, int(round(len(groups) * (1.0 - test_fraction))))
    split_at = min(split_at, len(groups) - 1)
    train_groups = set(groups[:split_at])
    train = [row for row in rows if row.group_key in train_groups]
    test = [row for row in rows if row.group_key not in train_groups]
    return train, test


def synthetic_preround_fixture() -> list[GolfPreRoundRow]:
    """Deterministic fixture with point-in-time features and settled top-20 labels."""

    rows: list[GolfPreRoundRow] = []
    archetypes = ("parkland", "coastal", "major", "resort")
    names = (
        ("scheffler", "Scottie Scheffler", 2.25, 2.0, 1.7, 0.3, 1.4, 1.2, 0.6, 0.8),
        ("rory", "Rory McIlroy", 1.65, 1.7, 1.2, 0.0, 0.9, 0.7, 1.2, -0.1),
        ("xander", "Xander Schauffele", 1.45, 0.9, 1.4, 0.4, 0.8, 0.9, 0.2, 0.7),
        ("morikawa", "Collin Morikawa", 1.1, 0.1, 1.6, -0.3, 0.5, 1.0, -0.2, 1.0),
        ("spieth", "Jordan Spieth", 0.35, 0.0, 0.2, 0.9, 0.2, 0.5, -0.3, -0.2),
        ("griffin", "Ben Griffin", 0.05, -0.1, 0.4, 0.2, 0.7, 0.0, -0.4, 0.4),
        ("kuchar", "Matt Kuchar", -0.55, -1.0, -0.1, 0.6, -0.4, -0.2, -1.1, 0.5),
        ("longshot", "Long Shot", -1.2, -0.8, -0.9, -0.4, -0.9, -0.6, 0.0, -1.1),
    )
    for event_idx in range(8):
        event_date = date(2024 + event_idx // 4, 2 + event_idx, 3 + event_idx)
        archetype = archetypes[event_idx % len(archetypes)]
        wind = 8.0 + (event_idx % 4) * 3.5
        field_strength = 0.4 + 0.1 * event_idx
        pm_penalty = 0.04 * max(0.0, wind - 12.0)
        scored: list[tuple[float, tuple[str, str, float, float, float, float, float, float, float, float], str]] = []
        for player_idx, player in enumerate(names):
            wave = "pm" if (player_idx + event_idx) % 2 else "am"
            (
                _pid,
                _pname,
                baseline,
                off_tee,
                approach,
                putting,
                form,
                course_fit,
                distance,
                accuracy,
            ) = player
            deterministic_noise = ((event_idx * 17 + player_idx * 13) % 11 - 5) / 10.0
            score = (
                baseline
                + 0.35 * approach
                + 0.25 * form
                + 0.22 * course_fit
                + 0.10 * accuracy
                - (pm_penalty if wave == "pm" else 0.0)
                + deterministic_noise
            )
            scored.append((score, player, wave))
        rank = {player[0]: idx + 1 for idx, (_score, player, _wave) in enumerate(sorted(scored, reverse=True))}
        for score, player, wave in scored:
            pid, pname, baseline, off_tee, approach, putting, form, course_fit, distance, accuracy = player
            rank_pct = (9 - rank[pid]) / 8.0
            fair = _clip_probability(0.24 + 0.11 * score + 0.05 * rank_pct)
            market_mid = _clip_probability(fair - 0.03 + ((event_idx % 3) - 1) * 0.01)
            spread = 0.01 if rank[pid] <= 5 else 0.03
            sampled_top_n = min(len(names), max(1, round(len(names) * 20 / 72)))
            rows.append(
                GolfPreRoundRow(
                    event_date=event_date,
                    tournament_id=f"fixture-{event_idx:02d}",
                    player_id=pid,
                    player_name=pname,
                    target=1 if rank[pid] <= sampled_top_n else 0,
                    top_n=20,
                    field_size=72,
                    course_archetype=archetype,
                    tee_wave=wave,
                    round_number=1,
                    numeric={
                        "baseline_skill_z": baseline,
                        "skill_rank_pct": rank_pct,
                        "sg_off_tee_z": off_tee,
                        "sg_approach_z": approach,
                        "sg_putting_z": putting,
                        "recent_form_z": form,
                        "course_fit_z": course_fit,
                        "field_strength_z": field_strength,
                        "tee_wave_pm": 1.0 if wave == "pm" else 0.0,
                        "wind_mph": wind,
                        "wave_wind_delta": pm_penalty if wave == "pm" else 0.0,
                        "scoring_volatility": 2.7 + (event_idx % 3) * 0.15,
                        "driving_distance_z": distance,
                        "driving_accuracy_z": accuracy,
                        "prior_course_history_z": course_fit * 0.7,
                        "market_mid": market_mid,
                        "time_to_start_hours": 18.0 - event_idx,
                        "liquidity": 5000.0 + 1000.0 * max(score, 0.0),
                        "spread": spread,
                    },
                    market_bid=max(0.01, market_mid - spread / 2.0),
                    market_ask=min(0.99, market_mid + spread / 2.0),
                    odds_probability=_clip_probability(fair + 0.01),
                    reference_price_source="fixture-direct-top20",
                )
            )
    return rows


def render_markdown_report(
    report: GolfPreRoundResearchReport,
    *,
    market_selection: MarketSelection | None = None,
    odds_note: str | None = None,
) -> str:
    """Render a compact markdown report suitable for live-test/docs."""

    lines = [
        "# Golf Pre-Round Top-N Research",
        "",
        "## Decision",
        "",
        f"- Target: {report.target_market}",
        f"- Result: **{report.decision}**",
        "- This is research/paper only. No orders, cancels, or live-submit paths are used.",
        "",
    ]
    if market_selection is not None:
        lines.extend(
            [
                "## Market Discovery",
                "",
                "| Structure | Active | Quoted | Median spread | 24h volume | OI | Score |",
                "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
            ]
        )
        for item in (market_selection.chosen, *market_selection.rejected[:7]):
            lines.append(
                f"| {item.structure} | {item.active_markets} | {item.quoted_markets} | "
                f"{_fmt_optional(item.median_spread)} | {item.total_volume_24h:.2f} | "
                f"{item.total_open_interest:.2f} | {item.score:.2f} |"
            )
        lines.extend(["", "Chosen structure rationale:"])
        lines.extend(f"- {item}" for item in market_selection.rationale)
        lines.append("")
    lines.extend(
        [
            "## Data And Leakage Controls",
            "",
            f"- Data source: {report.data_source}",
            f"- Train rows/events: {report.train_rows}/{report.train_events}",
            f"- OOS rows/events: {report.test_rows}/{report.test_events}",
            f"- Provider status: {report.provider_status}",
        ]
    )
    if odds_note:
        lines.append(f"- Odds note: {odds_note}")
    lines.extend(f"- {control}" for control in report.leakage_controls)
    lines.extend(["", "## OOS Metrics", "", "| Model | Brier | Log loss | ECE |", "| --- | ---: | ---: | ---: |"])
    for name, metrics in sorted(report.metrics.items(), key=lambda item: item[1].brier):
        lines.append(f"| {name} | {metrics.brier:.4f} | {metrics.log_loss:.4f} | {metrics.ece:.4f} |")
    lines.extend(["", "## Feature Group Findings", ""])
    lines.append(f"- Mutual information: {_top_items(report.mutual_information)}")
    lines.append(f"- Permutation importance: {_top_items(report.permutation_importance)}")
    lines.append(f"- Grouped residuals: {_top_items_abs(report.grouped_residuals)}")
    lines.append(f"- Cluster residuals: {_top_items_abs(report.cluster_residuals)}")
    lines.append(f"- Interaction residuals: {_top_items_abs(report.interaction_residuals)}")
    lines.extend(["", "## Candidate Economics", ""])
    if report.top_candidate_checks:
        lines.extend(
            [
                "| Player | Side | Fair | Bid | Ask | Fee | Net | Candidate | Reason |",
                "| --- | --- | ---: | ---: | ---: | ---: | ---: | --- | --- |",
            ]
        )
        for check in report.top_candidate_checks[:8]:
            lines.append(
                f"| {check.player_id} | {check.best_side} | {check.model_probability:.3f} | "
                f"{check.market_bid:.3f} | {check.market_ask:.3f} | {check.fee:.3f} | "
                f"{check.net_after_fee_and_spread:.3f} | {check.candidate} | {check.reason} |"
            )
    else:
        lines.append("- No rows had executable bid/ask fields, so no candidate economics were computed.")
    lines.extend(
        [
            "",
            "A positive candidate check is not an edge. It still needs live executable touch, stale-source gates, "
            "capacity, CLV/markout, and settlement reconciliation.",
            "",
        ]
    )
    return "\n".join(lines)


def _fit_logistic(
    rows: Sequence[GolfPreRoundRow],
    outcomes: Sequence[float],
    prepared: _PreparedFeatures,
) -> _LinearProbabilityModel:
    weights = [0.0] * (len(prepared.features) + 1)
    lr = 0.08
    l2 = 0.003
    matrix = [_feature_vector(row, prepared) for row in rows]
    for _ in range(600):
        grads = [0.0] * len(weights)
        for x, y in zip(matrix, outcomes, strict=True):
            pred = _sigmoid(weights[0] + sum(w * xv for w, xv in zip(weights[1:], x, strict=True)))
            err = pred - y
            grads[0] += err
            for idx, value in enumerate(x, start=1):
                grads[idx] += err * value
        n = float(len(matrix))
        weights[0] -= lr * grads[0] / n
        for idx in range(1, len(weights)):
            weights[idx] -= lr * ((grads[idx] / n) + l2 * weights[idx])
    return _LinearProbabilityModel(
        features=prepared.features,
        means=prepared.means,
        scales=prepared.scales,
        weights=tuple(weights),
    )


def _fit_anchor_residual(
    rows: Sequence[GolfPreRoundRow],
    outcomes: Sequence[float],
    prepared: _PreparedFeatures,
) -> _AnchorResidualModel:
    weights = [0.0] * (len(prepared.features) + 1)
    lr = 0.04
    matrix = [_feature_vector(row, prepared) for row in rows]
    anchors = [_anchor_probability(row, sum(outcomes) / len(outcomes), prefer_market=True) for row in rows]
    for _ in range(500):
        grads = [0.0] * len(weights)
        for x, y, anchor in zip(matrix, outcomes, anchors, strict=True):
            pred = anchor + weights[0] + sum(w * xv for w, xv in zip(weights[1:], x, strict=True))
            err = pred - y
            grads[0] += err
            for idx, value in enumerate(x, start=1):
                grads[idx] += err * value
        n = float(len(matrix))
        for idx in range(len(weights)):
            weights[idx] -= lr * grads[idx] / n
    return _AnchorResidualModel(
        features=prepared.features,
        means=prepared.means,
        scales=prepared.scales,
        weights=tuple(weights),
        fallback=sum(outcomes) / len(outcomes),
    )


@dataclass(frozen=True)
class _PreparedFeatures:
    features: tuple[str, ...]
    means: dict[str, float]
    scales: dict[str, float]


@dataclass(frozen=True)
class _LinearProbabilityModel:
    features: tuple[str, ...]
    means: dict[str, float]
    scales: dict[str, float]
    weights: tuple[float, ...]

    def predict_many(self, rows: Sequence[GolfPreRoundRow]) -> list[float]:
        prepared = _PreparedFeatures(self.features, self.means, self.scales)
        out: list[float] = []
        for row in rows:
            x = _feature_vector(row, prepared)
            linear = self.weights[0] + sum(w * xv for w, xv in zip(self.weights[1:], x, strict=True))
            out.append(_clip_probability(_sigmoid(linear)))
        return out


@dataclass(frozen=True)
class _AnchorResidualModel:
    features: tuple[str, ...]
    means: dict[str, float]
    scales: dict[str, float]
    weights: tuple[float, ...]
    fallback: float

    def predict_many(self, rows: Sequence[GolfPreRoundRow]) -> list[float]:
        prepared = _PreparedFeatures(self.features, self.means, self.scales)
        out: list[float] = []
        for row in rows:
            x = _feature_vector(row, prepared)
            anchor = _anchor_probability(row, self.fallback, prefer_market=True)
            residual = self.weights[0] + sum(w * xv for w, xv in zip(self.weights[1:], x, strict=True))
            out.append(_clip_probability(anchor + residual))
        return out


def _prepare_feature_matrix(rows: Sequence[GolfPreRoundRow], features: Sequence[str]) -> _PreparedFeatures:
    means: dict[str, float] = {}
    scales: dict[str, float] = {}
    for feature in features:
        values = [row.feature_value(feature) for row in rows]
        mean = sum(values) / len(values)
        var = sum((value - mean) ** 2 for value in values) / len(values)
        means[feature] = mean
        scales[feature] = math.sqrt(var) if var > 1e-12 else 1.0
    return _PreparedFeatures(features=tuple(features), means=means, scales=scales)


def _feature_vector(row: GolfPreRoundRow, prepared: _PreparedFeatures) -> list[float]:
    return [
        (row.feature_value(feature) - prepared.means[feature]) / prepared.scales[feature]
        for feature in prepared.features
    ]


def _score_distribution_topn_probabilities(
    rows: Sequence[GolfPreRoundRow],
    *,
    evaluation_rows: Sequence[GolfPreRoundRow],
    top_n: int,
    simulations: int,
    seed: int,
) -> list[float]:
    event_rows: dict[tuple[date, str], list[GolfPreRoundRow]] = defaultdict(list)
    for row in rows:
        event_rows[row.group_key].append(row)
    rng = random.Random(seed)
    probabilities: dict[tuple[tuple[date, str], str], float] = {}
    for group_key, players in event_rows.items():
        counts = {row.player_id: 0 for row in players}
        field_size = max(max(row.field_size for row in players), len(players))
        cutoff_n = min(len(players), max(1, round(len(players) * top_n / field_size)))
        for _ in range(simulations):
            common = rng.gauss(0.0, 1.0)
            scores: list[tuple[float, str]] = []
            for row in players:
                mean = _expected_score_to_par(row)
                sd = max(1.8, row.feature_value("scoring_volatility"))
                score = mean + 0.25 * common + rng.gauss(0.0, sd)
                scores.append((score, row.player_id))
            cutoff = sorted(score for score, _pid in scores)[cutoff_n - 1]
            for score, player_id in scores:
                if score <= cutoff:
                    counts[player_id] += 1
        for row in players:
            probabilities[(group_key, row.player_id)] = counts[row.player_id] / simulations
    return [probabilities.get((row.group_key, row.player_id), 0.5) for row in evaluation_rows]


def _expected_score_to_par(row: GolfPreRoundRow) -> float:
    return -(
        0.9 * row.feature_value("baseline_skill_z")
        + 0.35 * row.feature_value("sg_off_tee_z")
        + 0.55 * row.feature_value("sg_approach_z")
        + 0.15 * row.feature_value("sg_putting_z")
        + 0.30 * row.feature_value("recent_form_z")
        + 0.25 * row.feature_value("course_fit_z")
        + 0.10 * row.feature_value("driving_accuracy_z")
    ) + (
        0.08 * row.feature_value("field_strength_z")
        + row.feature_value("wave_wind_delta")
        + row.feature_value("injury_strokes_per_round")
        + row.feature_value("rest_fatigue_strokes_per_round")
        + row.feature_value("caddie_absence_strokes_per_round")
        + 1.5 * row.feature_value("withdrawal_risk")
    )


def _metrics(probs: Sequence[float], outcomes: Sequence[float]) -> ModelMetrics:
    clipped = [_clip_probability(p) for p in probs]
    return ModelMetrics(
        n=len(clipped),
        brier=brier_score(clipped, outcomes),
        log_loss=log_loss(clipped, outcomes),
        ece=_ece(clipped, outcomes),
    )


def _ece(probs: Sequence[float], outcomes: Sequence[float], *, bins: int = 5) -> float:
    total = 0.0
    for idx in range(bins):
        lo = idx / bins
        hi = (idx + 1) / bins
        members = [
            (p, y)
            for p, y in zip(probs, outcomes, strict=True)
            if lo <= p < hi or (idx == bins - 1 and math.isclose(p, hi))
        ]
        if not members:
            continue
        mean_pred = sum(p for p, _y in members) / len(members)
        obs = sum(y for _p, y in members) / len(members)
        total += len(members) / len(probs) * abs(mean_pred - obs)
    return total


def _calibration_groups(rows: Sequence[GolfPreRoundRow], probs: Sequence[float]) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    groups = {
        "skill_top_half": [idx for idx, row in enumerate(rows) if row.feature_value("skill_rank_pct") >= 0.5],
        "skill_bottom_half": [idx for idx, row in enumerate(rows) if row.feature_value("skill_rank_pct") < 0.5],
        "am_wave": [idx for idx, row in enumerate(rows) if row.tee_wave.lower() == "am"],
        "pm_wave": [idx for idx, row in enumerate(rows) if row.tee_wave.lower() == "pm"],
    }
    for name, indices in groups.items():
        if not indices:
            continue
        mean_pred = sum(probs[idx] for idx in indices) / len(indices)
        obs = sum(rows[idx].target for idx in indices) / len(indices)
        out[name] = {"count": float(len(indices)), "mean_pred": mean_pred, "obs_rate": obs, "residual": obs - mean_pred}
    return out


def _feature_group_mi(rows: Sequence[GolfPreRoundRow]) -> dict[str, float]:
    return {group: _mutual_information(rows, features) for group, features in FEATURE_GROUPS.items()}


def _mutual_information(rows: Sequence[GolfPreRoundRow], features: Sequence[str]) -> float:
    labels = [row.target for row in rows]
    if len(set(labels)) < 2:
        return 0.0
    bins: defaultdict[str, list[int]] = defaultdict(list)
    for idx, row in enumerate(rows):
        key_parts: list[str] = []
        for feature in features:
            value = row.feature_value(feature)
            key_parts.append("hi" if value >= _median([r.feature_value(feature) for r in rows]) else "lo")
        bins["|".join(key_parts)].append(idx)
    base_pos = sum(labels) / len(labels)
    entropy_y = _binary_entropy(base_pos)
    conditional = 0.0
    for indices in bins.values():
        pos = sum(labels[idx] for idx in indices) / len(indices)
        conditional += len(indices) / len(labels) * _binary_entropy(pos)
    return max(0.0, entropy_y - conditional)


def _permutation_importance(
    rows: Sequence[GolfPreRoundRow],
    outcomes: Sequence[float],
    model: _LinearProbabilityModel,
    base_brier: float,
) -> dict[str, float]:
    rng = random.Random(5)
    out: dict[str, float] = {}
    for group, features in FEATURE_GROUPS.items():
        mutated = [_replace_features(row, features, rows[(idx + 3) % len(rows)]) for idx, row in enumerate(rows)]
        rng.shuffle(mutated)
        probs = model.predict_many(mutated)
        out[group] = max(0.0, brier_score(probs, outcomes) - base_brier)
    return out


def _replace_features(row: GolfPreRoundRow, features: Sequence[str], donor: GolfPreRoundRow) -> GolfPreRoundRow:
    numeric = dict(row.numeric)
    for feature in features:
        if feature in numeric:
            numeric[feature] = donor.feature_value(feature)
    return GolfPreRoundRow(
        event_date=row.event_date,
        tournament_id=row.tournament_id,
        player_id=row.player_id,
        player_name=row.player_name,
        target=row.target,
        top_n=row.top_n,
        field_size=row.field_size,
        course_archetype=donor.course_archetype if "course_fit_z" in features else row.course_archetype,
        tee_wave=donor.tee_wave if "tee_wave_pm" in features else row.tee_wave,
        round_number=row.round_number,
        numeric=numeric,
        market_bid=row.market_bid,
        market_ask=row.market_ask,
        odds_probability=row.odds_probability,
        reference_price_source=row.reference_price_source,
    )


def _grouped_residuals(rows: Sequence[GolfPreRoundRow], probs: Sequence[float]) -> dict[str, float]:
    groups: defaultdict[str, list[float]] = defaultdict(list)
    for row, prob in zip(rows, probs, strict=True):
        groups[f"course:{row.course_archetype}"].append(row.target - prob)
        groups[f"wave:{row.tee_wave.lower()}"].append(row.target - prob)
    return {name: sum(values) / len(values) for name, values in groups.items()}


def _cluster_residuals(rows: Sequence[GolfPreRoundRow], probs: Sequence[float]) -> dict[str, float]:
    groups: defaultdict[str, list[float]] = defaultdict(list)
    for row, prob in zip(rows, probs, strict=True):
        bomber = row.feature_value("driving_distance_z") >= 0.5
        accurate = row.feature_value("driving_accuracy_z") >= 0.5
        approach = row.feature_value("sg_approach_z") >= 0.5
        cluster = "bomber" if bomber and not accurate else "precision" if accurate and approach else "balanced"
        groups[cluster].append(row.target - prob)
    return {name: sum(values) / len(values) for name, values in groups.items()}


def _interaction_residuals(rows: Sequence[GolfPreRoundRow], probs: Sequence[float]) -> dict[str, float]:
    groups: defaultdict[str, list[float]] = defaultdict(list)
    for row, prob in zip(rows, probs, strict=True):
        wind = "windy" if row.feature_value("wind_mph") >= 14.0 else "calm"
        fit = "fit_hi" if row.feature_value("course_fit_z") >= 0.5 else "fit_lo"
        groups[f"{row.tee_wave.lower()}_{wind}"].append(row.target - prob)
        groups[f"{row.course_archetype}_{fit}"].append(row.target - prob)
    return {name: sum(values) / len(values) for name, values in groups.items()}


def _candidate_checks(
    rows: Sequence[GolfPreRoundRow],
    probs: Sequence[float],
    *,
    actionable: bool,
) -> list[CandidateCheck]:
    checks: list[CandidateCheck] = []
    for row, fair in zip(rows, probs, strict=True):
        if row.market_bid is None or row.market_ask is None:
            continue
        yes_gap = fair - row.market_ask
        no_price = 1.0 - row.market_bid
        no_gap = (1.0 - fair) - no_price
        if yes_gap >= no_gap:
            side = "YES"
            executable = row.market_ask
            fee = kalshi_fee(executable)
            net = yes_gap - fee
            gross_gap = yes_gap
        else:
            side = "NO"
            executable = no_price
            fee = kalshi_fee(executable)
            net = no_gap - fee
            gross_gap = no_gap
        liquidity = row.feature_value("liquidity")
        raw_candidate = net > 0.015 and liquidity >= 1000.0
        candidate = raw_candidate and actionable
        if raw_candidate and not actionable:
            reason = "fixture_only_not_actionable"
        else:
            reason = "fee_net_candidate_needs_tick_logging" if candidate else "fails_fee_spread_liquidity_gate"
        checks.append(
            CandidateCheck(
                player_id=row.player_id,
                tournament_id=row.tournament_id,
                model_probability=fair,
                market_bid=row.market_bid,
                market_ask=row.market_ask,
                best_side=side,
                executable_price=executable,
                fee=fee,
                gross_probability_gap=gross_gap,
                net_after_fee_and_spread=net,
                liquidity=liquidity,
                candidate=candidate,
                reason=reason,
            )
        )
    return checks


def _decision(
    metrics: Mapping[str, ModelMetrics],
    best_name: str,
    rows: Sequence[GolfPreRoundRow],
    probs: Sequence[float],
    *,
    fixture_mode: bool,
) -> str:
    if fixture_mode:
        return "continue: fixture/no-network path works; needs point-in-time historical data before tick logging"
    market = metrics.get("market_implied")
    best = metrics[best_name]
    candidates = [check for check in _candidate_checks(rows, probs, actionable=True) if check.candidate]
    if market is not None and best.brier >= market.brier:
        return "kill: best model does not beat market-implied OOS Brier"
    if not candidates:
        return "paper only: model beats baseline but no fee/spread/liquidity candidate cleared"
    return "start tick logging: fee-net candidates exist, but no edge until CLV/markout/settlement evidence"


def _looks_like_fixture_data(rows: Sequence[GolfPreRoundRow]) -> bool:
    if not rows:
        return False
    fixture_like = 0
    for row in rows:
        source = row.reference_price_source or ""
        if row.tournament_id.startswith("fixture-") or source.startswith("fixture"):
            fixture_like += 1
    return fixture_like == len(rows)


def _anchor_probability(row: GolfPreRoundRow, fallback: float, *, prefer_market: bool) -> float:
    if prefer_market and row.market_mid is not None:
        return _clip_probability(row.market_mid)
    if row.odds_probability is not None:
        return _clip_probability(row.odds_probability)
    return _clip_probability(fallback)


def _odds_probability(row: GolfPreRoundRow, fallback: float) -> float:
    return _clip_probability(row.odds_probability if row.odds_probability is not None else fallback)


def _skill_rank_probability(row: GolfPreRoundRow, field_rate: float) -> float:
    base_logit = _logit(_clip_probability(field_rate))
    return _clip_probability(_sigmoid(base_logit + 2.2 * (row.feature_value("skill_rank_pct") - 0.5)))


def _market_bid_ask(market: Mapping[str, object]) -> tuple[float, float] | None:
    bid = _optional_float(market.get("yes_bid_dollars") or market.get("yes_bid"))
    ask = _optional_float(market.get("yes_ask_dollars") or market.get("yes_ask"))
    if bid is None or ask is None:
        return None
    return bid, ask


def _tradability_sort_key(market: Mapping[str, object]) -> tuple[float, float]:
    bid_ask = _market_bid_ask(market)
    spread = 1.0 if bid_ask is None else bid_ask[1] - bid_ask[0]
    volume = _float_field(market, "volume_24h_fp", "volume_24h")
    return (spread, -volume)


def _float_field(market: Mapping[str, object], *names: str) -> float:
    for name in names:
        parsed = _optional_float(market.get(name))
        if parsed is not None:
            return parsed
    return 0.0


def _required(row: Mapping[str, object], field: str) -> str:
    value = row.get(field)
    if value is None or str(value).strip() == "":
        raise ValueError(f"missing required field {field!r}")
    return str(value).strip()


def _optional_float(value: object) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    return _float_value(value)


def _float_value(value: object, default: float | None = None) -> float:
    if value is None or str(value).strip() == "":
        if default is not None:
            return default
        raise ValueError("missing numeric value")
    parsed = float(str(value))
    if not math.isfinite(parsed):
        raise ValueError("numeric value must be finite")
    return parsed


def _parse_date(value: str) -> date:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).date()


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _median(values: Sequence[float]) -> float:
    if not values:
        raise ValueError("cannot median empty sequence")
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _binary_entropy(p: float) -> float:
    pc = _clip_probability(p)
    return -(pc * math.log(pc) + (1.0 - pc) * math.log(1.0 - pc))


def _clip_probability(value: float) -> float:
    return min(1.0 - _EPS, max(_EPS, value))


def _sigmoid(value: float) -> float:
    if value >= 0:
        return 1.0 / (1.0 + math.exp(-value))
    exp_value = math.exp(value)
    return exp_value / (1.0 + exp_value)


def _logit(value: float) -> float:
    p = _clip_probability(value)
    return math.log(p / (1.0 - p))


def _fmt_optional(value: float | None) -> str:
    return "" if value is None else f"{value:.4f}"


def _fmt_optional_signed(value: float | None) -> str:
    return "n/a" if value is None else f"{value:+.4f}"


def _event_ts(value: datetime) -> str:
    return value.isoformat().replace("+", "p").replace(":", "").replace("-", "").replace(".", "")


def _top_items(values: Mapping[str, float], *, limit: int = 4) -> str:
    items = sorted(values.items(), key=lambda item: item[1], reverse=True)[:limit]
    return ", ".join(f"{name}={value:.4f}" for name, value in items)


def _top_items_abs(values: Mapping[str, float], *, limit: int = 4) -> str:
    items = sorted(values.items(), key=lambda item: abs(item[1]), reverse=True)[:limit]
    return ", ".join(f"{name}={value:+.4f}" for name, value in items)


def fixture_kalshi_market_snapshots() -> list[dict[str, object]]:
    """Small public-market-shape fixture for no-network tests and script runs."""

    return [
        _market(
            "KXPGATOP20-THMTPBW26-WKIM",
            "The Memorial Tournament: Will Si Woo Kim finish top 20?",
            0.56,
            0.57,
            4949.69,
            3179.47,
            4343.69,
        ),
        _market(
            "KXPGATOP20-THMTPBW26-BGRI",
            "The Memorial Tournament: Will Ben Griffin finish top 20?",
            0.40,
            0.41,
            18835.54,
            9649.36,
            18835.54,
        ),
        _market(
            "KXPGAMAKECUT-THMTPBW26-JSPI",
            "The Memorial Tournament: Will Jordan Spieth make the cut?",
            0.78,
            0.79,
            2496.70,
            2485.70,
            2496.70,
        ),
        _market(
            "KXPGAR1TOP10-THMTPBW26-XSCH",
            "The Memorial Tournament: Will Xander Schauffele finish top 10 in Round 1?",
            0.21,
            0.36,
            20.0,
            5.0,
            20.0,
        ),
        _market(
            "KXPGAH2H-THMT26R1ARAISSCH-SSCH",
            "Will Scottie Scheffler beat Aaron Rai in the 1st round?",
            0.74,
            0.75,
            30.0,
            0.0,
            30.0,
        ),
    ]


def _market(
    ticker: str,
    title: str,
    bid: float,
    ask: float,
    volume: float,
    volume_24h: float,
    open_interest: float,
) -> dict[str, object]:
    return {
        "ticker": ticker,
        "title": title,
        "status": "active",
        "yes_bid_dollars": f"{bid:.4f}",
        "yes_ask_dollars": f"{ask:.4f}",
        "volume_fp": f"{volume:.2f}",
        "volume_24h_fp": f"{volume_24h:.2f}",
        "open_interest_fp": f"{open_interest:.2f}",
    }


def flatten(items: Iterable[Iterable[Any]]) -> list[Any]:
    """Tiny helper used by scripts when merging paginated API batches."""

    return [value for group in items for value in group]
