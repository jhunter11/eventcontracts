"""Read-only MLB run-in-first-inning level-edge validator.

This module tests a simple but decisive question before a bigger baseball model:
does Kalshi's RFI price sit far enough away from the leak-free historical base
rate to survive executable touch and the canonical Kalshi taker fee?

It consumes settled public market tapes and never submits, cancels, replaces, or
live-submits orders. A positive result is still research evidence only; promotion
requires current live quote capture, liquidity, and larger OOS persistence.
"""

from __future__ import annotations

import csv
import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from eventcontracts.research.ledger import to_jsonable, write_jsonl
from eventcontracts.research.tennis_market_residual import kalshi_taker_fee_per_contract

KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"
_BookState = dict[str, dict[float, float]]


@dataclass(frozen=True)
class RfiMarketResult:
    """Settled RFI market metadata."""

    market_id: str
    result_yes: bool
    close_time: datetime | None = None
    title: str = ""

    def __post_init__(self) -> None:
        if not self.market_id:
            raise ValueError("market_id must not be empty")
        if self.close_time is not None and self.close_time.tzinfo is None:
            raise ValueError("close_time must be timezone-aware")


@dataclass(frozen=True)
class RfiTapeTrade:
    """One public trade-tape row."""

    market_id: str
    created_at: datetime
    yes_price: float
    quantity: float = 1.0

    def __post_init__(self) -> None:
        if not self.market_id:
            raise ValueError("market_id must not be empty")
        if self.created_at.tzinfo is None:
            raise ValueError("created_at must be timezone-aware")
        _require_probability(self.yes_price, "yes_price")
        if self.quantity < 0.0:
            raise ValueError("quantity must be non-negative")


@dataclass(frozen=True)
class RfiEarlyPriceSample:
    """Early-tape price proxy paired with the settled result."""

    market_id: str
    observed_at: datetime
    yes_price: float
    result_yes: bool
    sample_trade_count: int
    sample_quantity: float


@dataclass(frozen=True)
class RfiContextFeature:
    """Point-in-time MLB availability/context deltas for one RFI market.

    Deltas are expressed as adjustments to YES probability. Positive values
    increase YRFI probability; negative values favor NRFI. They should come from
    an upstream, timestamped injury/rest/lineup model or manual research note.
    """

    market_id: str
    feature_as_of: datetime | None = None
    rfi_probability_delta: float = 0.0
    injury_probability_delta: float = 0.0
    rest_probability_delta: float = 0.0
    roster_absence_probability_delta: float = 0.0
    lineup_probability_delta: float = 0.0
    bullpen_rest_probability_delta: float = 0.0
    starting_pitcher_probability_delta: float = 0.0
    source: str = ""

    def __post_init__(self) -> None:
        if not self.market_id:
            raise ValueError("market_id must not be empty")
        if self.feature_as_of is not None and self.feature_as_of.tzinfo is None:
            raise ValueError("feature_as_of must be timezone-aware")
        for name, value in (
            ("rfi_probability_delta", self.rfi_probability_delta),
            ("injury_probability_delta", self.injury_probability_delta),
            ("rest_probability_delta", self.rest_probability_delta),
            ("roster_absence_probability_delta", self.roster_absence_probability_delta),
            ("lineup_probability_delta", self.lineup_probability_delta),
            ("bullpen_rest_probability_delta", self.bullpen_rest_probability_delta),
            ("starting_pitcher_probability_delta", self.starting_pitcher_probability_delta),
        ):
            if not math.isfinite(value):
                raise ValueError(f"{name} must be finite")

    @property
    def total_probability_delta(self) -> float:
        return (
            self.rfi_probability_delta
            + self.injury_probability_delta
            + self.rest_probability_delta
            + self.roster_absence_probability_delta
            + self.lineup_probability_delta
            + self.bullpen_rest_probability_delta
            + self.starting_pitcher_probability_delta
        )

    @property
    def feature_count(self) -> int:
        return sum(
            1
            for value in (
                self.rfi_probability_delta,
                self.injury_probability_delta,
                self.rest_probability_delta,
                self.roster_absence_probability_delta,
                self.lineup_probability_delta,
                self.bullpen_rest_probability_delta,
                self.starting_pitcher_probability_delta,
            )
            if abs(value) > 1e-12
        )


@dataclass(frozen=True)
class RfiLiveQuoteSnapshot:
    """One read-only live/top-of-book RFI quote snapshot."""

    market_id: str
    received_at: datetime
    yes_bid: float
    yes_ask: float
    yes_bid_size: float = 0.0
    yes_ask_size: float = 0.0
    close_time: datetime | None = None
    status: str = ""
    title: str = ""
    source: str = "kalshi_rest_top_of_book"
    book_verified: bool = False

    def __post_init__(self) -> None:
        if not self.market_id:
            raise ValueError("market_id must not be empty")
        if self.received_at.tzinfo is None:
            raise ValueError("received_at must be timezone-aware")
        if self.close_time is not None and self.close_time.tzinfo is None:
            raise ValueError("close_time must be timezone-aware")
        _require_probability(self.yes_bid, "yes_bid")
        _require_probability(self.yes_ask, "yes_ask")
        if self.yes_ask < self.yes_bid:
            raise ValueError("yes_ask must be >= yes_bid")
        if self.yes_bid_size < 0.0 or self.yes_ask_size < 0.0:
            raise ValueError("quote sizes must be non-negative")

    @property
    def spread(self) -> float:
        return self.yes_ask - self.yes_bid


@dataclass(frozen=True)
class RfiEvaluationConfig:
    """Settings for leak-free base-rate and executable EV checks."""

    min_train: int = 20
    min_net_edge: float = 0.01
    early_trade_count: int = 20
    early_window_seconds: int = 900
    crosses: tuple[float, ...] = (0.0, 0.01, 0.02)
    fee_rate_bps: int = 700
    max_context_probability_delta: float = 0.15
    max_quote_age_seconds: int = 600

    def __post_init__(self) -> None:
        if self.min_train <= 0:
            raise ValueError("min_train must be positive")
        if self.min_net_edge < 0.0:
            raise ValueError("min_net_edge must be non-negative")
        if self.early_trade_count <= 0:
            raise ValueError("early_trade_count must be positive")
        if self.early_window_seconds <= 0:
            raise ValueError("early_window_seconds must be positive")
        if not self.crosses:
            raise ValueError("crosses must not be empty")
        for cross in self.crosses:
            if cross < 0.0:
                raise ValueError("crosses must be non-negative")
        if self.max_context_probability_delta < 0.0 or self.max_context_probability_delta > 0.5:
            raise ValueError("max_context_probability_delta must be in [0, 0.5]")
        if self.max_quote_age_seconds <= 0:
            raise ValueError("max_quote_age_seconds must be positive")


@dataclass(frozen=True)
class RfiBetRow:
    """One walk-forward executable decision."""

    market_id: str
    observed_at: datetime
    cross: float
    base_yes_probability: float
    model_yes_probability: float
    context_probability_delta: float
    context_feature_count: int
    context_source: str
    market_yes_price: float
    side: str
    executable_price: float
    fee: float
    expected_net_edge: float
    realized_net: float
    result_yes: bool


@dataclass(frozen=True)
class RfiCrossSummary:
    """Aggregate result for one assumed crossed spread."""

    cross: float
    samples: int
    bets: int
    mean_market_yes_price: float | None
    mean_model_yes_probability: float | None
    realized_yes_rate: float | None
    ev_per_contract: float | None
    ev_ci_low: float | None
    ev_ci_high: float | None
    total_ev: float | None
    positive: bool

    def as_dict(self) -> dict[str, object]:
        return to_jsonable(self)


@dataclass(frozen=True)
class RfiLiveTouchCandidate:
    """One live quote touch check against the settled-tape model prior."""

    market_id: str
    received_at: datetime
    base_yes_probability: float
    model_yes_probability: float
    context_probability_delta: float
    context_feature_count: int
    context_source: str
    yes_bid: float
    yes_ask: float
    yes_bid_size: float
    yes_ask_size: float
    side: str
    executable_price: float
    fee: float
    expected_net_edge: float
    spread: float
    quote_age_seconds: float
    stale_quote: bool
    book_verified: bool
    source: str
    candidate: bool
    reason: str

    def as_dict(self) -> dict[str, object]:
        return to_jsonable(self)


@dataclass(frozen=True)
class RfiLevelReport:
    """Complete RFI level-edge report."""

    as_of: datetime
    series_ticker: str
    market_count: int
    trade_count: int
    sample_count: int
    context_market_count: int
    context_sample_count: int
    context_coverage: float
    mean_context_probability_delta: float
    summaries: tuple[RfiCrossSummary, ...]
    decision_gate: str

    def as_dict(self) -> dict[str, object]:
        return {
            "as_of": self.as_of.isoformat(),
            "series_ticker": self.series_ticker,
            "market_count": self.market_count,
            "trade_count": self.trade_count,
            "sample_count": self.sample_count,
            "context_market_count": self.context_market_count,
            "context_sample_count": self.context_sample_count,
            "context_coverage": self.context_coverage,
            "mean_context_probability_delta": self.mean_context_probability_delta,
            "summaries": [item.as_dict() for item in self.summaries],
            "decision_gate": self.decision_gate,
        }


@dataclass(frozen=True)
class RfiLiveTouchReport:
    """Live executable-touch report for upcoming RFI markets."""

    as_of: datetime
    series_ticker: str
    training_sample_count: int
    quote_count: int
    context_market_count: int
    context_quote_count: int
    context_coverage: float
    candidate_count: int
    max_expected_net_edge: float | None
    decision_gate: str
    candidates: tuple[RfiLiveTouchCandidate, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "as_of": self.as_of.isoformat(),
            "series_ticker": self.series_ticker,
            "training_sample_count": self.training_sample_count,
            "quote_count": self.quote_count,
            "context_market_count": self.context_market_count,
            "context_quote_count": self.context_quote_count,
            "context_coverage": self.context_coverage,
            "candidate_count": self.candidate_count,
            "max_expected_net_edge": self.max_expected_net_edge,
            "decision_gate": self.decision_gate,
            "candidates": [item.as_dict() for item in self.candidates],
        }


@dataclass(frozen=True)
class RfiMarkoutSummary:
    """Aggregate CLV/markout result for one future horizon."""

    horizon_seconds: int
    rows: int
    missing_rows: int
    mean_clv: float | None
    mean_markout_net: float | None
    positive_clv_rate: float | None
    positive_net_rate: float | None

    def as_dict(self) -> dict[str, object]:
        return to_jsonable(self)


@dataclass(frozen=True)
class RfiLiveMarkoutRow:
    """One read-only markout of a live-touch candidate against later WS book state."""

    market_id: str
    candidate_received_at: datetime
    horizon_seconds: int
    side: str
    candidate_model_yes_probability: float
    candidate_quote_age_seconds: float
    candidate_spread: float
    candidate_yes_bid_size: float
    candidate_yes_ask_size: float
    candidate_source: str
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
        return to_jsonable(self)


@dataclass(frozen=True)
class RfiLiveMarkoutReport:
    """Read-only CLV report for live-touch candidates."""

    as_of: datetime
    candidate_count: int
    quote_count: int
    markout_count: int
    horizons_seconds: tuple[int, ...]
    summaries: tuple[RfiMarkoutSummary, ...]
    decision_gate: str
    markouts: tuple[RfiLiveMarkoutRow, ...]

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
class RfiExecutionFilterRule:
    """Predeclared execution filter for RFI markout analysis."""

    name: str
    description: str
    min_expected_net_edge: float | None = None
    max_quote_age_seconds: float | None = None
    max_spread: float | None = None
    min_touch_size: float | None = None
    source_contains: str | None = None


@dataclass(frozen=True)
class RfiExecutionFilterSummary:
    """CLV outcome for one predeclared execution filter."""

    rule_name: str
    description: str
    horizon_seconds: int
    rows: int
    mean_clv: float | None
    mean_markout_net: float | None
    net_ci_low: float | None
    net_ci_high: float | None
    positive_clv_rate: float | None
    positive_net_rate: float | None
    positive: bool

    def as_dict(self) -> dict[str, object]:
        return to_jsonable(self)


@dataclass(frozen=True)
class RfiExecutionFilterReport:
    """Execution-filter analysis over realized RFI markout rows."""

    as_of: datetime
    input_rows: int
    evaluated_rows: int
    horizon_seconds: int
    min_rows: int
    decision_gate: str
    summaries: tuple[RfiExecutionFilterSummary, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "as_of": self.as_of.isoformat(),
            "input_rows": self.input_rows,
            "evaluated_rows": self.evaluated_rows,
            "horizon_seconds": self.horizon_seconds,
            "min_rows": self.min_rows,
            "decision_gate": self.decision_gate,
            "summaries": [item.as_dict() for item in self.summaries],
        }


def build_early_price_samples(
    markets: Sequence[RfiMarketResult],
    trades: Sequence[RfiTapeTrade],
    *,
    config: RfiEvaluationConfig | None = None,
) -> tuple[RfiEarlyPriceSample, ...]:
    """Build early-tape samples without using later trade prices."""

    cfg = config or RfiEvaluationConfig()
    result_by_market = {market.market_id: market for market in markets}
    trades_by_market: dict[str, list[RfiTapeTrade]] = {}
    for trade in trades:
        if trade.market_id in result_by_market:
            trades_by_market.setdefault(trade.market_id, []).append(trade)
    samples: list[RfiEarlyPriceSample] = []
    for market_id, raw_trades in trades_by_market.items():
        ordered = sorted(raw_trades, key=lambda item: item.created_at)
        if not ordered:
            continue
        start = ordered[0].created_at
        early = [
            trade
            for trade in ordered
            if (trade.created_at - start).total_seconds() <= cfg.early_window_seconds
        ][: cfg.early_trade_count]
        if not early:
            continue
        weights = [max(trade.quantity, 0.0) for trade in early]
        weight_sum = sum(weights)
        if weight_sum <= 0.0:
            weights = [1.0 for _trade in early]
            weight_sum = float(len(early))
        yes_price = sum(trade.yes_price * weight for trade, weight in zip(early, weights, strict=True)) / weight_sum
        samples.append(
            RfiEarlyPriceSample(
                market_id=market_id,
                observed_at=start,
                yes_price=yes_price,
                result_yes=result_by_market[market_id].result_yes,
                sample_trade_count=len(early),
                sample_quantity=weight_sum,
            )
        )
    return tuple(sorted(samples, key=lambda item: item.observed_at))


def evaluate_rfi_level_edge(
    markets: Sequence[RfiMarketResult],
    trades: Sequence[RfiTapeTrade],
    *,
    series_ticker: str = "KXMLBRFI",
    config: RfiEvaluationConfig | None = None,
    context_features: Sequence[RfiContextFeature] = (),
    as_of: datetime | None = None,
) -> tuple[RfiLevelReport, tuple[RfiBetRow, ...], tuple[RfiEarlyPriceSample, ...]]:
    """Evaluate leak-free base-rate RFI/NRFI EV from early public tape."""

    cfg = config or RfiEvaluationConfig()
    samples = build_early_price_samples(markets, trades, config=cfg)
    context_by_market = _context_feature_by_market(context_features)
    bets_by_cross: dict[float, list[RfiBetRow]] = {cross: [] for cross in cfg.crosses}
    context_sample_count = 0
    context_deltas: list[float] = []
    for idx, sample in enumerate(samples):
        if idx < cfg.min_train:
            continue
        prior = samples[:idx]
        base_model_yes = sum(1.0 for item in prior if item.result_yes) / len(prior)
        context_delta, context_feature = _context_delta_for_sample(sample, context_by_market, config=cfg)
        if context_feature is not None:
            context_sample_count += 1
        context_deltas.append(context_delta)
        model_yes = _clip_probability(base_model_yes + context_delta)
        for cross in cfg.crosses:
            row = _bet_row_from_sample(
                sample,
                base_yes_probability=base_model_yes,
                model_yes_probability=model_yes,
                context_probability_delta=context_delta,
                context_feature=context_feature,
                cross=cross,
                config=cfg,
            )
            if row is not None:
                bets_by_cross[cross].append(row)

    evaluated_sample_count = max(0, len(samples) - cfg.min_train)
    summaries = tuple(
        _summary_for_cross(
            cross,
            samples=samples[cfg.min_train :],
            bets=bets_by_cross[cross],
        )
        for cross in cfg.crosses
    )
    report = RfiLevelReport(
        as_of=as_of or datetime.now(UTC),
        series_ticker=series_ticker,
        market_count=len(markets),
        trade_count=len(trades),
        sample_count=len(samples),
        context_market_count=len(context_by_market),
        context_sample_count=context_sample_count,
        context_coverage=context_sample_count / evaluated_sample_count if evaluated_sample_count else 0.0,
        mean_context_probability_delta=_mean(context_deltas) or 0.0,
        summaries=summaries,
        decision_gate=_decision_gate(summaries=summaries, config=cfg),
    )
    all_bets = tuple(row for rows in bets_by_cross.values() for row in rows)
    return report, all_bets, samples


def evaluate_rfi_live_touch(
    settled_markets: Sequence[RfiMarketResult],
    settled_trades: Sequence[RfiTapeTrade],
    live_quotes: Sequence[RfiLiveQuoteSnapshot],
    *,
    series_ticker: str = "KXMLBRFI",
    config: RfiEvaluationConfig | None = None,
    context_features: Sequence[RfiContextFeature] = (),
    as_of: datetime | None = None,
) -> RfiLiveTouchReport:
    """Evaluate current quote touch against the settled-tape walk-forward prior."""

    cfg = config or RfiEvaluationConfig()
    now = as_of or datetime.now(UTC)
    samples = build_early_price_samples(settled_markets, settled_trades, config=cfg)
    if len(samples) < cfg.min_train:
        return RfiLiveTouchReport(
            as_of=now,
            series_ticker=series_ticker,
            training_sample_count=len(samples),
            quote_count=len(live_quotes),
            context_market_count=0,
            context_quote_count=0,
            context_coverage=0.0,
            candidate_count=0,
            max_expected_net_edge=None,
            decision_gate="continue research: not enough settled RFI samples for live-touch prior",
            candidates=(),
        )
    base_yes_probability = sum(1.0 for sample in samples if sample.result_yes) / len(samples)
    context_by_market = _context_feature_by_market(context_features)
    rows: list[RfiLiveTouchCandidate] = []
    context_quote_count = 0
    for quote in sorted(live_quotes, key=lambda item: (item.received_at, item.market_id)):
        context_delta, context_feature = _context_delta_for_quote(quote, context_by_market, config=cfg)
        if context_feature is not None:
            context_quote_count += 1
        model_yes = _clip_probability(base_yes_probability + context_delta)
        rows.append(
            _live_touch_candidate_from_quote(
                quote,
                base_yes_probability=base_yes_probability,
                model_yes_probability=model_yes,
                context_probability_delta=context_delta,
                context_feature=context_feature,
                config=cfg,
                as_of=now,
            )
        )
    candidate_count = sum(1 for row in rows if row.candidate)
    max_edge = max((row.expected_net_edge for row in rows), default=None)
    return RfiLiveTouchReport(
        as_of=now,
        series_ticker=series_ticker,
        training_sample_count=len(samples),
        quote_count=len(live_quotes),
        context_market_count=len(context_by_market),
        context_quote_count=context_quote_count,
        context_coverage=context_quote_count / len(live_quotes) if live_quotes else 0.0,
        candidate_count=candidate_count,
        max_expected_net_edge=max_edge,
        decision_gate=_live_touch_decision_gate(rows),
        candidates=tuple(sorted(rows, key=lambda item: item.expected_net_edge, reverse=True)),
    )


def evaluate_rfi_live_markouts(
    candidates: Sequence[RfiLiveTouchCandidate],
    quote_timeline: Sequence[RfiLiveQuoteSnapshot],
    *,
    horizons_seconds: Sequence[int] = (300, 900, 1800),
    min_markout_rows: int = 10,
    as_of: datetime | None = None,
) -> RfiLiveMarkoutReport:
    """Mark out live-touch candidates against later read-only WS book snapshots.

    The markout is CLV evidence only. It does not prove settlement edge.
    """

    if min_markout_rows <= 0:
        raise ValueError("min_markout_rows must be positive")
    horizons = tuple(int(item) for item in horizons_seconds)
    if not horizons or any(item <= 0 for item in horizons):
        raise ValueError("horizons_seconds must contain positive integers")
    now = as_of or datetime.now(UTC)
    active_candidates = tuple(row for row in candidates if row.candidate and row.book_verified)
    quotes_by_market: dict[str, list[RfiLiveQuoteSnapshot]] = {}
    for quote in sorted(quote_timeline, key=lambda item: (item.market_id, item.received_at)):
        quotes_by_market.setdefault(quote.market_id, []).append(quote)

    rows: list[RfiLiveMarkoutRow] = []
    for candidate in sorted(active_candidates, key=lambda item: (item.received_at, item.market_id)):
        market_quotes = quotes_by_market.get(candidate.market_id, [])
        for horizon in horizons:
            target = candidate.received_at + timedelta(seconds=horizon)
            markout_quote = _first_quote_at_or_after(market_quotes, target)
            rows.append(_markout_row(candidate, horizon_seconds=horizon, markout_quote=markout_quote))

    summaries = tuple(_markout_summary(horizon, rows) for horizon in horizons)
    markout_count = sum(1 for row in rows if row.markout_price is not None)
    return RfiLiveMarkoutReport(
        as_of=now,
        candidate_count=len(active_candidates),
        quote_count=len(quote_timeline),
        markout_count=markout_count,
        horizons_seconds=horizons,
        summaries=summaries,
        decision_gate=_markout_decision_gate(
            candidate_count=len(active_candidates),
            summaries=summaries,
            min_markout_rows=min_markout_rows,
        ),
        markouts=tuple(rows),
    )


def evaluate_rfi_execution_filters(
    markouts: Sequence[RfiLiveMarkoutRow],
    *,
    horizon_seconds: int = 60,
    min_rows: int = 10,
    as_of: datetime | None = None,
) -> RfiExecutionFilterReport:
    """Evaluate predeclared execution filters against realized markout rows."""

    if horizon_seconds <= 0:
        raise ValueError("horizon_seconds must be positive")
    if min_rows <= 0:
        raise ValueError("min_rows must be positive")
    evaluated = tuple(
        row
        for row in markouts
        if row.horizon_seconds == horizon_seconds
        and row.clv is not None
        and row.markout_net_after_entry_fee is not None
    )
    summaries = tuple(
        _execution_filter_summary(rule, evaluated, horizon_seconds=horizon_seconds, min_rows=min_rows)
        for rule in _rfi_execution_filter_rules()
    )
    return RfiExecutionFilterReport(
        as_of=as_of or datetime.now(UTC),
        input_rows=len(markouts),
        evaluated_rows=len(evaluated),
        horizon_seconds=horizon_seconds,
        min_rows=min_rows,
        decision_gate=_execution_filter_decision_gate(summaries=summaries, min_rows=min_rows),
        summaries=summaries,
    )


def render_rfi_markdown(report: RfiLevelReport) -> str:
    """Render a compact RFI evidence report."""

    lines = [
        "# MLB RFI Level Edge Validation",
        "",
        f"- As of: `{report.as_of.isoformat()}`",
        f"- Series: `{report.series_ticker}`",
        f"- Markets: `{report.market_count}`",
        f"- Trades: `{report.trade_count}`",
        f"- Early-price samples: `{report.sample_count}`",
        f"- Context markets: `{report.context_market_count}`",
        f"- Context coverage: `{report.context_coverage:.3f}`",
        f"- Mean context delta: `{report.mean_context_probability_delta:+.4f}`",
        f"- Decision: **{report.decision_gate}**",
        "",
        "| Cross | Samples | Bets | Market YES | Model YES | Realized YES | EV/Contract |",
        "| ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for summary in report.summaries:
        lines.append(
            f"| {summary.cross:.2f} | {summary.samples} | {summary.bets} | "
            f"{_fmt(summary.mean_market_yes_price)} | {_fmt(summary.mean_model_yes_probability)} | "
            f"{_fmt(summary.realized_yes_rate)} | {_fmt_signed(summary.ev_per_contract)} "
            f"({_fmt_signed(summary.ev_ci_low)}, {_fmt_signed(summary.ev_ci_high)}) |"
        )
    lines.extend(
        [
            "",
            "## Boundary",
            "",
            "This is read-only settled-tape research. A positive EV row is not enough for promotion "
            "until current live quotes, book depth, stale-source gates, and persistence across a larger "
            "chronological sample are verified.",
            "",
        ]
    )
    return "\n".join(lines)


def render_rfi_live_touch_markdown(report: RfiLiveTouchReport) -> str:
    """Render a compact live-touch report."""

    lines = [
        "# MLB RFI Live-Touch Validation",
        "",
        f"- As of: `{report.as_of.isoformat()}`",
        f"- Series: `{report.series_ticker}`",
        f"- Settled training samples: `{report.training_sample_count}`",
        f"- Live quote rows: `{report.quote_count}`",
        f"- Context coverage: `{report.context_coverage:.3f}`",
        f"- Candidate rows: `{report.candidate_count}`",
        f"- Max expected net edge: `{_fmt_signed(report.max_expected_net_edge)}`",
        f"- Decision: **{report.decision_gate}**",
        "",
        "| Market | Side | Model YES | Bid | Ask | Touch | Net Edge | Reason |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in report.candidates[:20]:
        lines.append(
            f"| {row.market_id} | {row.side} | {row.model_yes_probability:.4f} | "
            f"{row.yes_bid:.4f} | {row.yes_ask:.4f} | {row.executable_price:.4f} | "
            f"{row.expected_net_edge:+.4f} | {row.reason} |"
        )
    lines.extend(
        [
            "",
            "## Boundary",
            "",
            "This is read-only live-touch research. REST top-of-book rows are not proof of fillability. "
            "A candidate must survive WS/orderbook depth, freshness, markout, and final settlement before edge exists.",
            "",
        ]
    )
    return "\n".join(lines)


def render_rfi_live_markout_markdown(report: RfiLiveMarkoutReport) -> str:
    """Render a compact live-touch markout report."""

    lines = [
        "# MLB RFI Live-Touch Markout",
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
            "This is read-only CLV evidence from local WS book-state rows. Positive markout is not settlement edge; "
            "it only justifies continued paper/settlement capture.",
            "",
        ]
    )
    return "\n".join(lines)


def render_rfi_execution_filter_markdown(report: RfiExecutionFilterReport) -> str:
    """Render a compact execution-filter report."""

    lines = [
        "# MLB RFI Execution Filter",
        "",
        f"- As of: `{report.as_of.isoformat()}`",
        f"- Input rows: `{report.input_rows}`",
        f"- Evaluated horizon: `{report.horizon_seconds}` seconds",
        f"- Evaluated matched rows: `{report.evaluated_rows}`",
        f"- Minimum rows: `{report.min_rows}`",
        f"- Decision: **{report.decision_gate}**",
        "",
        "| Rule | Rows | Mean CLV | Mean Net | Net CI | Positive Net |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for summary in report.summaries:
        lines.append(
            f"| {summary.rule_name} | {summary.rows} | {_fmt_signed(summary.mean_clv)} | "
            f"{_fmt_signed(summary.mean_markout_net)} | "
            f"{_fmt_signed(summary.net_ci_low)} to {_fmt_signed(summary.net_ci_high)} | "
            f"{_fmt(summary.positive_net_rate)} |"
        )
    lines.extend(
        [
            "",
            "## Boundary",
            "",
            "This is a read-only execution-filter check over already-generated markout rows. "
            "A positive filter is not settlement edge; it only justifies continued paper/settlement capture.",
            "",
        ]
    )
    return "\n".join(lines)


def read_markets_csv(path: Path) -> tuple[RfiMarketResult, ...]:
    """Read settled RFI market metadata from CSV."""

    return tuple(_market_from_mapping(row) for row in _read_csv(path))


def read_trades_jsonl(path: Path) -> tuple[RfiTapeTrade, ...]:
    """Read public trade rows from JSONL."""

    out: list[RfiTapeTrade] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        if not isinstance(payload, Mapping):
            raise ValueError("trade JSONL rows must be objects")
        out.append(_trade_from_mapping(payload))
    return tuple(out)


def read_context_csv(path: Path) -> tuple[RfiContextFeature, ...]:
    """Read timestamped injury/rest/roster context rows from CSV."""

    return tuple(_context_from_mapping(row) for row in _read_csv(path))


def read_live_quotes_csv(path: Path) -> tuple[RfiLiveQuoteSnapshot, ...]:
    """Read live RFI quote snapshots from CSV."""

    return tuple(_live_quote_from_mapping(row) for row in _read_csv(path))


def read_ws_live_quotes_jsonl(path: Path) -> tuple[RfiLiveQuoteSnapshot, ...]:
    """Extract latest book-verified live quotes from raw Kalshi WS JSONL."""

    timeline = read_ws_live_quote_timeline_jsonl(path)
    latest: dict[str, RfiLiveQuoteSnapshot] = {}
    for quote in timeline:
        current = latest.get(quote.market_id)
        if current is None or quote.received_at >= current.received_at:
            latest[quote.market_id] = quote
    return tuple(sorted(latest.values(), key=lambda item: item.market_id))


def read_ws_live_quote_timeline_jsonl(path: Path) -> tuple[RfiLiveQuoteSnapshot, ...]:
    """Extract a conservative book-verified quote timeline from raw Kalshi WS JSONL."""

    book_states: dict[str, _BookState] = {}
    quotes: list[RfiLiveQuoteSnapshot] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        if not isinstance(payload, Mapping):
            continue
        quote = _live_quote_from_ws_state_row(payload, book_states)
        if quote is None:
            continue
        quotes.append(quote)
    return tuple(sorted(quotes, key=lambda item: (item.market_id, item.received_at)))


def read_live_touch_candidates_jsonl(path: Path) -> tuple[RfiLiveTouchCandidate, ...]:
    """Read live-touch candidates from JSONL."""

    out: list[RfiLiveTouchCandidate] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        if not isinstance(payload, Mapping):
            raise ValueError("candidate JSONL rows must be objects")
        out.append(_candidate_from_mapping(payload))
    return tuple(out)


def read_live_markouts_jsonl(path: Path) -> tuple[RfiLiveMarkoutRow, ...]:
    """Read live-touch markout rows from JSONL."""

    out: list[RfiLiveMarkoutRow] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        if not isinstance(payload, Mapping):
            raise ValueError("markout JSONL rows must be objects")
        out.append(_markout_from_mapping(payload))
    return tuple(out)


def write_fixture_inputs(out_dir: Path) -> dict[str, str]:
    """Write deterministic RFI fixture inputs."""

    out_dir.mkdir(parents=True, exist_ok=True)
    markets, trades = fixture_rfi_inputs()
    context = fixture_rfi_context_features(markets)
    markets_path = out_dir / "rfi_markets.csv"
    trades_path = out_dir / "rfi_trades.jsonl"
    context_path = out_dir / "rfi_context.csv"
    _write_csv(
        markets_path,
        ("market_id", "result", "close_time", "title"),
        [_market_to_mapping(row) for row in markets],
    )
    write_jsonl(trades_path, [_trade_to_mapping(row) for row in trades])
    _write_csv(
        context_path,
        _context_columns(),
        [_context_to_mapping(row) for row in context],
    )
    return {"markets_csv": str(markets_path), "trades_jsonl": str(trades_path), "context_csv": str(context_path)}


def write_fixture_live_inputs(out_dir: Path) -> dict[str, str]:
    """Write deterministic settled/context/live quote inputs for live-touch tests."""

    out_dir.mkdir(parents=True, exist_ok=True)
    paths = write_fixture_inputs(out_dir)
    quotes = fixture_rfi_live_quotes()
    quotes_path = out_dir / "rfi_live_quotes.csv"
    _write_csv(quotes_path, _live_quote_columns(), [_live_quote_to_mapping(row) for row in quotes])
    return {**paths, "live_quotes_csv": str(quotes_path)}


def write_fixture_markout_inputs(out_dir: Path) -> dict[str, str]:
    """Write deterministic candidate and WS book rows for markout tests."""

    out_dir.mkdir(parents=True, exist_ok=True)
    paths = write_fixture_live_inputs(out_dir)
    markets = read_markets_csv(Path(paths["markets_csv"]))
    trades = read_trades_jsonl(Path(paths["trades_jsonl"]))
    live_quotes = read_live_quotes_csv(Path(paths["live_quotes_csv"]))
    report = evaluate_rfi_live_touch(
        markets,
        trades,
        live_quotes,
        config=RfiEvaluationConfig(min_train=20, min_net_edge=0.01),
        as_of=live_quotes[0].received_at,
    )
    candidates_path = out_dir / "rfi_live_touch_candidates.jsonl"
    write_jsonl(candidates_path, report.candidates)
    ws_raw_path = out_dir / "rfi_ws_raw.jsonl"
    fixture_candidate = next(row for row in report.candidates if row.candidate)
    initial = fixture_candidate.received_at
    future = initial + timedelta(seconds=300)
    raw_rows = [
        _fixture_ws_orderbook_snapshot_row(
            fixture_candidate.market_id,
            received_at=initial,
            yes_bid=0.44,
            no_bid=0.54,
        ),
        _fixture_ws_orderbook_snapshot_row(
            fixture_candidate.market_id,
            received_at=future,
            yes_bid=0.49,
            no_bid=0.50,
        ),
    ]
    ws_raw_path.write_text(
        "".join(json.dumps(row, sort_keys=True, ensure_ascii=True) + "\n" for row in raw_rows),
        encoding="utf-8",
    )
    return {
        **paths,
        "candidates_jsonl": str(candidates_path),
        "ws_raw_jsonl": str(ws_raw_path),
    }


def write_rfi_outputs(
    report: RfiLevelReport,
    bets: Sequence[RfiBetRow],
    samples: Sequence[RfiEarlyPriceSample],
    *,
    report_json: Path,
    report_md: Path | None = None,
    bets_jsonl: Path | None = None,
    samples_jsonl: Path | None = None,
) -> None:
    """Write RFI report and optional evidence ledgers."""

    report_json.parent.mkdir(parents=True, exist_ok=True)
    report_json.write_text(json.dumps(to_jsonable(report.as_dict()), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if report_md is not None:
        report_md.parent.mkdir(parents=True, exist_ok=True)
        report_md.write_text(render_rfi_markdown(report), encoding="utf-8")
    if bets_jsonl is not None:
        write_jsonl(bets_jsonl, bets)
    if samples_jsonl is not None:
        write_jsonl(samples_jsonl, samples)


def write_rfi_live_touch_outputs(
    report: RfiLiveTouchReport,
    *,
    report_json: Path,
    report_md: Path | None = None,
    candidates_jsonl: Path | None = None,
) -> None:
    """Write live-touch report and candidate rows."""

    report_json.parent.mkdir(parents=True, exist_ok=True)
    report_json.write_text(json.dumps(to_jsonable(report.as_dict()), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if report_md is not None:
        report_md.parent.mkdir(parents=True, exist_ok=True)
        report_md.write_text(render_rfi_live_touch_markdown(report), encoding="utf-8")
    if candidates_jsonl is not None:
        write_jsonl(candidates_jsonl, report.candidates)


def write_rfi_live_markout_outputs(
    report: RfiLiveMarkoutReport,
    *,
    report_json: Path,
    report_md: Path | None = None,
    markouts_jsonl: Path | None = None,
) -> None:
    """Write live-touch markout report and optional markout rows."""

    report_json.parent.mkdir(parents=True, exist_ok=True)
    report_json.write_text(json.dumps(to_jsonable(report.as_dict()), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if report_md is not None:
        report_md.parent.mkdir(parents=True, exist_ok=True)
        report_md.write_text(render_rfi_live_markout_markdown(report), encoding="utf-8")
    if markouts_jsonl is not None:
        write_jsonl(markouts_jsonl, report.markouts)


def write_rfi_execution_filter_outputs(
    report: RfiExecutionFilterReport,
    *,
    report_json: Path,
    report_md: Path | None = None,
) -> None:
    """Write execution-filter report outputs."""

    report_json.parent.mkdir(parents=True, exist_ok=True)
    report_json.write_text(json.dumps(to_jsonable(report.as_dict()), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if report_md is not None:
        report_md.parent.mkdir(parents=True, exist_ok=True)
        report_md.write_text(render_rfi_execution_filter_markdown(report), encoding="utf-8")


def capture_kalshi_rfi_inputs(
    *,
    out_dir: Path,
    series_ticker: str = "KXMLBRFI",
    max_markets: int = 80,
    max_trade_pages: int = 2,
) -> dict[str, str | int]:
    """Fetch settled public RFI markets and trade tapes into local evidence files."""

    out_dir.mkdir(parents=True, exist_ok=True)
    raw_markets = _fetch_settled_markets(series_ticker=series_ticker, max_markets=max_markets)
    markets: list[RfiMarketResult] = []
    trades: list[RfiTapeTrade] = []
    for raw in raw_markets:
        result = str(raw.get("result") or "").lower()
        if result not in {"yes", "no"}:
            continue
        ticker = str(raw.get("ticker") or raw.get("market_ticker") or "")
        if not ticker:
            continue
        markets.append(
            RfiMarketResult(
                market_id=ticker,
                result_yes=result == "yes",
                close_time=_optional_datetime(raw.get("close_time")),
                title=str(raw.get("title") or ""),
            )
        )
        trades.extend(_fetch_trades(ticker=ticker, max_pages=max_trade_pages))
    markets_path = out_dir / "rfi_markets.csv"
    trades_path = out_dir / "rfi_trades.jsonl"
    context_path = out_dir / "rfi_context.csv"
    _write_csv(
        markets_path,
        ("market_id", "result", "close_time", "title"),
        [_market_to_mapping(row) for row in markets],
    )
    write_jsonl(trades_path, [_trade_to_mapping(row) for row in trades])
    _write_csv(context_path, _context_columns(), [])
    return {
        "series_ticker": series_ticker,
        "markets_csv": str(markets_path),
        "trades_jsonl": str(trades_path),
        "context_csv": str(context_path),
        "markets": len(markets),
        "trades": len(trades),
    }


def capture_kalshi_rfi_live_quotes(
    *,
    out_dir: Path,
    series_ticker: str = "KXMLBRFI",
    max_markets: int = 80,
) -> dict[str, str | int]:
    """Fetch public active RFI top-of-book rows into a local CSV."""

    out_dir.mkdir(parents=True, exist_ok=True)
    received_at = datetime.now(UTC)
    raw_markets = _fetch_live_markets(series_ticker=series_ticker, max_markets=max_markets)
    quotes: list[RfiLiveQuoteSnapshot] = []
    for raw in raw_markets:
        try:
            quotes.append(_live_quote_from_market(raw, received_at=received_at))
        except ValueError:
            continue
    quotes_path = out_dir / "rfi_live_quotes.csv"
    context_path = out_dir / "rfi_context.csv"
    _write_csv(quotes_path, _live_quote_columns(), [_live_quote_to_mapping(row) for row in quotes])
    _write_csv(context_path, _context_columns(), [])
    return {
        "series_ticker": series_ticker,
        "live_quotes_csv": str(quotes_path),
        "context_csv": str(context_path),
        "markets_seen": len(raw_markets),
        "quotes": len(quotes),
    }


def fixture_rfi_inputs(
    *,
    n_markets: int = 80,
    yes_rate: float = 0.48,
    market_yes_price: float = 0.60,
) -> tuple[tuple[RfiMarketResult, ...], tuple[RfiTapeTrade, ...]]:
    """Deterministic settled RFI tape with YES overpriced vs base rate."""

    start = datetime(2026, 4, 1, 17, 0, tzinfo=UTC)
    yes_wins = int(round(n_markets * yes_rate))
    markets: list[RfiMarketResult] = []
    trades: list[RfiTapeTrade] = []
    for idx in range(n_markets):
        market_id = f"KXMLBRFI-FIXTURE-{idx:03d}"
        result_yes = idx % 2 == 0 if math.isclose(yes_rate, 0.5, abs_tol=1e-12) else (idx * 37) % n_markets < yes_wins
        observed = start.replace(day=min(28, 1 + idx % 28))
        observed = observed.replace(month=4 + (idx // 28))
        markets.append(
            RfiMarketResult(
                market_id=market_id,
                result_yes=result_yes,
                close_time=observed,
                title=f"Fixture RFI {idx}",
            )
        )
        for trade_idx in range(8):
            trades.append(
                RfiTapeTrade(
                    market_id=market_id,
                    created_at=observed.replace(minute=trade_idx),
                    yes_price=market_yes_price + (0.002 if trade_idx % 2 else -0.002),
                    quantity=10.0 + trade_idx,
                )
            )
    return tuple(markets), tuple(trades)


def fixture_rfi_live_quotes() -> tuple[RfiLiveQuoteSnapshot, ...]:
    """Deterministic live quote rows with one obvious fee-net candidate."""

    now = datetime.now(UTC)
    close_time = now.replace(hour=min(23, now.hour + 1))
    return (
        RfiLiveQuoteSnapshot(
            market_id="KXMLBRFI-LIVE-FIXTURE-YES",
            received_at=now,
            yes_bid=0.44,
            yes_ask=0.46,
            yes_bid_size=120.0,
            yes_ask_size=140.0,
            close_time=close_time,
            status="open",
            title="Fixture RFI live YES",
            source="fixture",
            book_verified=True,
        ),
        RfiLiveQuoteSnapshot(
            market_id="KXMLBRFI-LIVE-FIXTURE-FAIR",
            received_at=now,
            yes_bid=0.53,
            yes_ask=0.55,
            yes_bid_size=80.0,
            yes_ask_size=75.0,
            close_time=close_time,
            status="open",
            title="Fixture RFI live fair",
            source="fixture",
            book_verified=True,
        ),
    )


def fixture_rfi_context_features(markets: Sequence[RfiMarketResult]) -> tuple[RfiContextFeature, ...]:
    """Deterministic context rows covering injury, rest, roster, and starter impacts."""

    out: list[RfiContextFeature] = []
    for idx, market in enumerate(markets):
        feature_as_of = (
            market.close_time.replace(hour=max(0, market.close_time.hour - 1)) if market.close_time else None
        )
        out.append(
            RfiContextFeature(
                market_id=market.market_id,
                feature_as_of=feature_as_of,
                injury_probability_delta=0.006 if idx % 5 == 0 else 0.0,
                rest_probability_delta=-0.004 if idx % 7 == 0 else 0.0,
                roster_absence_probability_delta=-0.005 if idx % 11 == 0 else 0.0,
                lineup_probability_delta=0.004 if idx % 3 == 0 else 0.0,
                bullpen_rest_probability_delta=0.003 if idx % 13 == 0 else 0.0,
                starting_pitcher_probability_delta=0.007 if idx % 17 == 0 else 0.0,
                source="fixture-availability-context",
            )
        )
    return tuple(out)


def _bet_row_from_sample(
    sample: RfiEarlyPriceSample,
    *,
    base_yes_probability: float,
    model_yes_probability: float,
    context_probability_delta: float,
    context_feature: RfiContextFeature | None,
    cross: float,
    config: RfiEvaluationConfig,
) -> RfiBetRow | None:
    yes_price = min(0.99, sample.yes_price + cross)
    no_price = min(0.99, 1.0 - sample.yes_price + cross)
    yes_fee = kalshi_taker_fee_per_contract(yes_price, 1, config.fee_rate_bps)
    no_fee = kalshi_taker_fee_per_contract(no_price, 1, config.fee_rate_bps)
    yes_edge = model_yes_probability - yes_price - yes_fee
    no_edge = (1.0 - model_yes_probability) - no_price - no_fee
    if max(yes_edge, no_edge) < config.min_net_edge:
        return None
    if yes_edge >= no_edge:
        side = "YES"
        executable = yes_price
        fee = yes_fee
        expected = yes_edge
        payoff = 1.0 if sample.result_yes else 0.0
    else:
        side = "NO"
        executable = no_price
        fee = no_fee
        expected = no_edge
        payoff = 0.0 if sample.result_yes else 1.0
    return RfiBetRow(
        market_id=sample.market_id,
        observed_at=sample.observed_at,
        cross=cross,
        base_yes_probability=base_yes_probability,
        model_yes_probability=model_yes_probability,
        context_probability_delta=context_probability_delta,
        context_feature_count=0 if context_feature is None else context_feature.feature_count,
        context_source="" if context_feature is None else context_feature.source,
        market_yes_price=sample.yes_price,
        side=side,
        executable_price=executable,
        fee=fee,
        expected_net_edge=expected,
        realized_net=payoff - executable - fee,
        result_yes=sample.result_yes,
    )


def _summary_for_cross(
    cross: float,
    *,
    samples: Sequence[RfiEarlyPriceSample],
    bets: Sequence[RfiBetRow],
) -> RfiCrossSummary:
    market_mean = _mean([sample.yes_price for sample in samples])
    realized_yes = _mean([1.0 if sample.result_yes else 0.0 for sample in samples])
    model_mean = _mean([bet.model_yes_probability for bet in bets])
    nets = [bet.realized_net for bet in bets]
    ev = _mean(nets)
    ci_low, ci_high = _normal_ci(nets)
    total_ev = sum(bet.realized_net for bet in bets) if bets else None
    return RfiCrossSummary(
        cross=cross,
        samples=len(samples),
        bets=len(bets),
        mean_market_yes_price=market_mean,
        mean_model_yes_probability=model_mean,
        realized_yes_rate=realized_yes,
        ev_per_contract=ev,
        ev_ci_low=ci_low,
        ev_ci_high=ci_high,
        total_ev=total_ev,
        positive=ev is not None and ev > 0.0,
    )


def _decision_gate(*, summaries: Sequence[RfiCrossSummary], config: RfiEvaluationConfig) -> str:
    positive_with_ci = [
        summary
        for summary in summaries
        if summary.positive
        and summary.bets >= config.min_train
        and summary.ev_ci_low is not None
        and summary.ev_ci_low > 0.0
    ]
    if positive_with_ci:
        return "start live quote capture: settled-tape EV is positive with CI; prove liquidity and freshness next"
    positive = [summary for summary in summaries if summary.positive and summary.bets >= config.min_train]
    if positive:
        return "continue research: positive mean EV but confidence interval does not yet prove edge"
    if any(summary.positive for summary in summaries):
        return "continue research: positive realized EV but bet count is below the configured sample gate"
    if any(summary.bets for summary in summaries):
        return "kill or defer: candidate bets did not realize positive EV"
    return "continue research: no fee-net RFI/NRFI candidates from early tape"


def _live_touch_candidate_from_quote(
    quote: RfiLiveQuoteSnapshot,
    *,
    base_yes_probability: float,
    model_yes_probability: float,
    context_probability_delta: float,
    context_feature: RfiContextFeature | None,
    config: RfiEvaluationConfig,
    as_of: datetime,
) -> RfiLiveTouchCandidate:
    yes_fee = kalshi_taker_fee_per_contract(quote.yes_ask, 1, config.fee_rate_bps)
    no_price = 1.0 - quote.yes_bid
    no_fee = kalshi_taker_fee_per_contract(no_price, 1, config.fee_rate_bps)
    yes_edge = model_yes_probability - quote.yes_ask - yes_fee
    no_edge = (1.0 - model_yes_probability) - no_price - no_fee
    if yes_edge >= no_edge:
        side = "YES"
        executable = quote.yes_ask
        fee = yes_fee
        edge = yes_edge
    else:
        side = "NO"
        executable = no_price
        fee = no_fee
        edge = no_edge
    quote_age = max(0.0, (as_of - quote.received_at).total_seconds())
    stale = quote_age > config.max_quote_age_seconds
    if stale:
        reason = "stale_quote"
    elif not quote.book_verified:
        reason = "rest_api_top_of_book_needs_ws_book_markout"
    elif edge < config.min_net_edge:
        reason = "fails_fee_spread_gate"
    else:
        reason = "fee_net_live_touch_candidate_needs_markout_settlement"
    return RfiLiveTouchCandidate(
        market_id=quote.market_id,
        received_at=quote.received_at,
        base_yes_probability=base_yes_probability,
        model_yes_probability=model_yes_probability,
        context_probability_delta=context_probability_delta,
        context_feature_count=0 if context_feature is None else context_feature.feature_count,
        context_source="" if context_feature is None else context_feature.source,
        yes_bid=quote.yes_bid,
        yes_ask=quote.yes_ask,
        yes_bid_size=quote.yes_bid_size,
        yes_ask_size=quote.yes_ask_size,
        side=side,
        executable_price=executable,
        fee=fee,
        expected_net_edge=edge,
        spread=quote.spread,
        quote_age_seconds=quote_age,
        stale_quote=stale,
        book_verified=quote.book_verified,
        source=quote.source,
        candidate=reason == "fee_net_live_touch_candidate_needs_markout_settlement",
        reason=reason,
    )


def _live_touch_decision_gate(candidates: Sequence[RfiLiveTouchCandidate]) -> str:
    if not candidates:
        return "continue research: no live RFI quote rows to evaluate"
    if any(row.candidate for row in candidates):
        return "start read-only markout capture: live touch has fee-net candidates; prove book depth and settlement"
    if any(row.reason == "rest_api_top_of_book_needs_ws_book_markout" for row in candidates):
        return "continue read-only capture: REST top-of-book gaps need WS/orderbook depth and markout"
    if all(row.stale_quote for row in candidates):
        return "continue research: all live RFI quotes were stale"
    return "continue research: live RFI touch has no fee-net candidates"


def _first_quote_at_or_after(
    quotes: Sequence[RfiLiveQuoteSnapshot],
    target: datetime,
) -> RfiLiveQuoteSnapshot | None:
    for quote in quotes:
        if quote.received_at >= target:
            return quote
    return None


def _markout_row(
    candidate: RfiLiveTouchCandidate,
    *,
    horizon_seconds: int,
    markout_quote: RfiLiveQuoteSnapshot | None,
) -> RfiLiveMarkoutRow:
    if markout_quote is None:
        return RfiLiveMarkoutRow(
            market_id=candidate.market_id,
            candidate_received_at=candidate.received_at,
            horizon_seconds=horizon_seconds,
            side=candidate.side,
            candidate_model_yes_probability=candidate.model_yes_probability,
            candidate_quote_age_seconds=candidate.quote_age_seconds,
            candidate_spread=candidate.spread,
            candidate_yes_bid_size=candidate.yes_bid_size,
            candidate_yes_ask_size=candidate.yes_ask_size,
            candidate_source=candidate.source,
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
    return RfiLiveMarkoutRow(
        market_id=candidate.market_id,
        candidate_received_at=candidate.received_at,
        horizon_seconds=horizon_seconds,
        side=candidate.side,
        candidate_model_yes_probability=candidate.model_yes_probability,
        candidate_quote_age_seconds=candidate.quote_age_seconds,
        candidate_spread=candidate.spread,
        candidate_yes_bid_size=candidate.yes_bid_size,
        candidate_yes_ask_size=candidate.yes_ask_size,
        candidate_source=candidate.source,
        executable_price=candidate.executable_price,
        entry_fee=candidate.fee,
        expected_net_edge=candidate.expected_net_edge,
        markout_received_at=markout_quote.received_at,
        actual_horizon_seconds=(markout_quote.received_at - candidate.received_at).total_seconds(),
        yes_bid=markout_quote.yes_bid,
        yes_ask=markout_quote.yes_ask,
        markout_price=markout_price,
        clv=clv,
        markout_net_after_entry_fee=clv - candidate.fee,
        source=markout_quote.source,
        reason="matched_ws_book_quote",
    )


def _markout_summary(
    horizon_seconds: int,
    rows: Sequence[RfiLiveMarkoutRow],
) -> RfiMarkoutSummary:
    horizon_rows = [row for row in rows if row.horizon_seconds == horizon_seconds]
    found = [row for row in horizon_rows if row.clv is not None and row.markout_net_after_entry_fee is not None]
    clvs = [row.clv for row in found if row.clv is not None]
    nets = [
        row.markout_net_after_entry_fee
        for row in found
        if row.markout_net_after_entry_fee is not None
    ]
    return RfiMarkoutSummary(
        horizon_seconds=horizon_seconds,
        rows=len(found),
        missing_rows=len(horizon_rows) - len(found),
        mean_clv=_mean(clvs),
        mean_markout_net=_mean(nets),
        positive_clv_rate=(
            sum(1 for value in clvs if value > 0.0) / len(clvs)
            if clvs
            else None
        ),
        positive_net_rate=(
            sum(1 for value in nets if value > 0.0) / len(nets)
            if nets
            else None
        ),
    )


def _markout_decision_gate(
    *,
    candidate_count: int,
    summaries: Sequence[RfiMarkoutSummary],
    min_markout_rows: int,
) -> str:
    if candidate_count == 0:
        return "continue research: no fee-net live-touch candidates to mark out"
    if not any(summary.rows for summary in summaries):
        return "continue capture: no future WS book quotes beyond markout horizons yet"
    eligible = [summary for summary in summaries if summary.rows >= min_markout_rows]
    if not eligible:
        return "continue capture: insufficient markout rows to judge CLV"
    if any(summary.mean_markout_net is not None and summary.mean_markout_net > 0.0 for summary in eligible):
        return "continue paper: positive CLV after entry fee; prove settlement before edge"
    if any(summary.mean_clv is not None and summary.mean_clv > 0.0 for summary in eligible):
        return "continue paper: positive price CLV but entry-fee net is not proven"
    return "kill or defer: live-touch candidates did not hold positive CLV"


def _rfi_execution_filter_rules() -> tuple[RfiExecutionFilterRule, ...]:
    return (
        RfiExecutionFilterRule("all", "All matched execution markouts"),
        RfiExecutionFilterRule("edge_ge_3c", "Expected net edge at least 3c", min_expected_net_edge=0.03),
        RfiExecutionFilterRule("edge_ge_5c", "Expected net edge at least 5c", min_expected_net_edge=0.05),
        RfiExecutionFilterRule(
            "quote_age_le_10s",
            "Candidate quote age at most 10 seconds",
            max_quote_age_seconds=10.0,
        ),
        RfiExecutionFilterRule(
            "quote_age_le_30s",
            "Candidate quote age at most 30 seconds",
            max_quote_age_seconds=30.0,
        ),
        RfiExecutionFilterRule("spread_le_1c", "Candidate spread at most 1c", max_spread=0.0100001),
        RfiExecutionFilterRule("spread_le_2c", "Candidate spread at most 2c", max_spread=0.0200001),
        RfiExecutionFilterRule(
            "touch_size_ge_1000",
            "Executable-side displayed size at least 1000",
            min_touch_size=1000.0,
        ),
        RfiExecutionFilterRule(
            "touch_size_ge_5000",
            "Executable-side displayed size at least 5000",
            min_touch_size=5000.0,
        ),
        RfiExecutionFilterRule(
            "edge_ge_5c_age_le_30s",
            "Expected net edge at least 5c and quote age at most 30 seconds",
            min_expected_net_edge=0.05,
            max_quote_age_seconds=30.0,
        ),
        RfiExecutionFilterRule(
            "edge_ge_5c_spread_le_1c",
            "Expected net edge at least 5c and spread at most 1c",
            min_expected_net_edge=0.05,
            max_spread=0.0100001,
        ),
        RfiExecutionFilterRule(
            "ws_delta_source",
            "Candidate was sourced from a WS orderbook delta",
            source_contains="orderbook_delta",
        ),
    )


def _execution_filter_summary(
    rule: RfiExecutionFilterRule,
    rows: Sequence[RfiLiveMarkoutRow],
    *,
    horizon_seconds: int,
    min_rows: int,
) -> RfiExecutionFilterSummary:
    matched = [row for row in rows if _execution_filter_matches(rule, row)]
    clvs = [row.clv for row in matched if row.clv is not None]
    nets = [
        row.markout_net_after_entry_fee
        for row in matched
        if row.markout_net_after_entry_fee is not None
    ]
    ci_low, ci_high = _normal_ci(nets)
    mean_net = _mean(nets)
    return RfiExecutionFilterSummary(
        rule_name=rule.name,
        description=rule.description,
        horizon_seconds=horizon_seconds,
        rows=len(matched),
        mean_clv=_mean(clvs),
        mean_markout_net=mean_net,
        net_ci_low=ci_low,
        net_ci_high=ci_high,
        positive_clv_rate=(sum(1 for value in clvs if value > 0.0) / len(clvs) if clvs else None),
        positive_net_rate=(sum(1 for value in nets if value > 0.0) / len(nets) if nets else None),
        positive=len(matched) >= min_rows and mean_net is not None and mean_net > 0.0,
    )


def _execution_filter_matches(rule: RfiExecutionFilterRule, row: RfiLiveMarkoutRow) -> bool:
    if rule.min_expected_net_edge is not None and row.expected_net_edge < rule.min_expected_net_edge:
        return False
    if rule.max_quote_age_seconds is not None and row.candidate_quote_age_seconds > rule.max_quote_age_seconds:
        return False
    if rule.max_spread is not None and row.candidate_spread > rule.max_spread:
        return False
    if rule.min_touch_size is not None and _execution_touch_size(row) < rule.min_touch_size:
        return False
    return rule.source_contains is None or rule.source_contains in row.candidate_source


def _execution_touch_size(row: RfiLiveMarkoutRow) -> float:
    if row.side == "YES":
        return row.candidate_yes_ask_size
    if row.side == "NO":
        return row.candidate_yes_bid_size
    return 0.0


def _execution_filter_decision_gate(
    *,
    summaries: Sequence[RfiExecutionFilterSummary],
    min_rows: int,
) -> str:
    if not summaries or not any(summary.rows for summary in summaries):
        return "continue capture: no matched markout rows for execution-filter analysis"
    eligible = [summary for summary in summaries if summary.rows >= min_rows]
    if not eligible:
        return "continue capture: insufficient execution-filter markout rows"
    positive_with_ci = [
        summary
        for summary in eligible
        if summary.mean_markout_net is not None
        and summary.mean_markout_net > 0.0
        and summary.net_ci_low is not None
        and summary.net_ci_low > 0.0
    ]
    if positive_with_ci:
        return "start paper candidate: execution filter has positive fee-net CLV with CI; prove settlement"
    positive_mean = [
        summary
        for summary in eligible
        if summary.mean_markout_net is not None and summary.mean_markout_net > 0.0
    ]
    if positive_mean:
        return "continue capture: execution filter has positive mean CLV but CI/settlement are unproven"
    return "kill or defer: no predeclared execution filter produced positive fee-net CLV"


def _context_delta_for_quote(
    quote: RfiLiveQuoteSnapshot,
    context_by_market: Mapping[str, RfiContextFeature],
    *,
    config: RfiEvaluationConfig,
) -> tuple[float, RfiContextFeature | None]:
    feature = context_by_market.get(quote.market_id)
    if feature is None:
        return 0.0, None
    if feature.feature_as_of is not None and feature.feature_as_of > quote.received_at:
        raise ValueError(f"context feature for {quote.market_id} is after the live quote observation")
    return _bounded_context_delta(feature.total_probability_delta, config=config), feature


def _fetch_settled_markets(*, series_ticker: str, max_markets: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    cursor: str | None = None
    while len(out) < max_markets:
        params = {"series_ticker": series_ticker, "status": "settled", "limit": "200"}
        if cursor:
            params["cursor"] = cursor
        payload = _get_json(f"{KALSHI_API}/markets", params=params)
        rows = [dict(item) for item in payload.get("markets", []) if isinstance(item, Mapping)]
        out.extend(rows)
        cursor_value = payload.get("cursor")
        cursor = str(cursor_value) if cursor_value else None
        if not cursor or not rows:
            break
        time.sleep(0.35)
    return out[:max_markets]


def _fetch_live_markets(*, series_ticker: str, max_markets: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    cursor: str | None = None
    while len(out) < max_markets:
        params = {"series_ticker": series_ticker, "limit": "200"}
        if cursor:
            params["cursor"] = cursor
        payload = _get_json(f"{KALSHI_API}/markets", params=params)
        rows = [dict(item) for item in payload.get("markets", []) if isinstance(item, Mapping)]
        out.extend(row for row in rows if _is_live_market(row))
        cursor_value = payload.get("cursor")
        cursor = str(cursor_value) if cursor_value else None
        if not cursor or not rows:
            break
        time.sleep(0.35)
    return out[:max_markets]


def _is_live_market(row: Mapping[str, object]) -> bool:
    status = str(row.get("status") or "").lower()
    return status in {"active", "open", "initialized"}


def _fetch_trades(*, ticker: str, max_pages: int) -> list[RfiTapeTrade]:
    trades: list[RfiTapeTrade] = []
    cursor: str | None = None
    for _page in range(max_pages):
        params = {"ticker": ticker, "limit": "1000"}
        if cursor:
            params["cursor"] = cursor
        payload = _get_json(f"{KALSHI_API}/markets/trades", params=params)
        for raw in payload.get("trades", []):
            if isinstance(raw, Mapping):
                trade = _trade_from_mapping(raw)
                if trade.market_id == ticker:
                    trades.append(trade)
        cursor_value = payload.get("cursor")
        cursor = str(cursor_value) if cursor_value else None
        if not cursor:
            break
        time.sleep(0.25)
    return trades


def _market_from_mapping(row: Mapping[str, object]) -> RfiMarketResult:
    result = str(row.get("result") or row.get("settled_side") or "").lower()
    if result not in {"yes", "no"}:
        raise ValueError("market row needs result yes/no")
    close_raw = row.get("close_time")
    return RfiMarketResult(
        market_id=_required(row, "market_id"),
        result_yes=result == "yes",
        close_time=_parse_datetime(str(close_raw)) if close_raw else None,
        title=str(row.get("title") or ""),
    )


def _trade_from_mapping(row: Mapping[str, object]) -> RfiTapeTrade:
    market_id = str(row.get("market_id") or row.get("ticker") or row.get("market_ticker") or "")
    created_raw = row.get("created_at") or row.get("created_time") or row.get("time")
    price_raw = row.get("yes_price") or row.get("yes_price_dollars") or row.get("price")
    quantity_raw = row.get("quantity") or row.get("count_fp") or row.get("count") or 1.0
    return RfiTapeTrade(
        market_id=market_id,
        created_at=_parse_datetime(str(created_raw)),
        yes_price=_float_value(price_raw),
        quantity=_float_value(quantity_raw, default=1.0),
    )


def _live_quote_from_mapping(row: Mapping[str, object]) -> RfiLiveQuoteSnapshot:
    return RfiLiveQuoteSnapshot(
        market_id=_required(row, "market_id"),
        received_at=_parse_datetime(_required(row, "received_at")),
        yes_bid=_probability_price(row.get("yes_bid") or row.get("yes_bid_dollars")),
        yes_ask=_probability_price(row.get("yes_ask") or row.get("yes_ask_dollars")),
        yes_bid_size=_float_value(row.get("yes_bid_size"), default=0.0),
        yes_ask_size=_float_value(row.get("yes_ask_size"), default=0.0),
        close_time=_optional_datetime(row.get("close_time")),
        status=str(row.get("status") or ""),
        title=str(row.get("title") or ""),
        source=str(row.get("source") or "csv"),
        book_verified=_bool_value(row.get("book_verified")),
    )


def _candidate_from_mapping(row: Mapping[str, object]) -> RfiLiveTouchCandidate:
    return RfiLiveTouchCandidate(
        market_id=_required(row, "market_id"),
        received_at=_parse_datetime(_required(row, "received_at")),
        base_yes_probability=_float_value(row.get("base_yes_probability")),
        model_yes_probability=_float_value(row.get("model_yes_probability")),
        context_probability_delta=_float_value(row.get("context_probability_delta"), default=0.0),
        context_feature_count=int(_float_value(row.get("context_feature_count"), default=0.0)),
        context_source=str(row.get("context_source") or ""),
        yes_bid=_float_value(row.get("yes_bid")),
        yes_ask=_float_value(row.get("yes_ask")),
        yes_bid_size=_float_value(row.get("yes_bid_size"), default=0.0),
        yes_ask_size=_float_value(row.get("yes_ask_size"), default=0.0),
        side=str(row.get("side") or ""),
        executable_price=_float_value(row.get("executable_price")),
        fee=_float_value(row.get("fee"), default=0.0),
        expected_net_edge=_float_value(row.get("expected_net_edge")),
        spread=_float_value(row.get("spread"), default=0.0),
        quote_age_seconds=_float_value(row.get("quote_age_seconds"), default=0.0),
        stale_quote=_bool_value(row.get("stale_quote")),
        book_verified=_bool_value(row.get("book_verified")),
        source=str(row.get("source") or ""),
        candidate=_bool_value(row.get("candidate")),
        reason=str(row.get("reason") or ""),
    )


def _markout_from_mapping(row: Mapping[str, object]) -> RfiLiveMarkoutRow:
    return RfiLiveMarkoutRow(
        market_id=_required(row, "market_id"),
        candidate_received_at=_parse_datetime(_required(row, "candidate_received_at")),
        horizon_seconds=int(_float_value(row.get("horizon_seconds"))),
        side=str(row.get("side") or ""),
        candidate_model_yes_probability=_float_value(row.get("candidate_model_yes_probability"), default=0.5),
        candidate_quote_age_seconds=_float_value(row.get("candidate_quote_age_seconds"), default=0.0),
        candidate_spread=_float_value(row.get("candidate_spread"), default=0.0),
        candidate_yes_bid_size=_float_value(row.get("candidate_yes_bid_size"), default=0.0),
        candidate_yes_ask_size=_float_value(row.get("candidate_yes_ask_size"), default=0.0),
        candidate_source=str(row.get("candidate_source") or ""),
        executable_price=_float_value(row.get("executable_price")),
        entry_fee=_float_value(row.get("entry_fee"), default=0.0),
        expected_net_edge=_float_value(row.get("expected_net_edge")),
        markout_received_at=_optional_datetime(row.get("markout_received_at")),
        actual_horizon_seconds=_optional_float(row.get("actual_horizon_seconds")),
        yes_bid=_optional_float(row.get("yes_bid")),
        yes_ask=_optional_float(row.get("yes_ask")),
        markout_price=_optional_float(row.get("markout_price")),
        clv=_optional_float(row.get("clv")),
        markout_net_after_entry_fee=_optional_float(row.get("markout_net_after_entry_fee")),
        source=str(row.get("source") or ""),
        reason=str(row.get("reason") or ""),
    )


def _live_quote_from_market(row: Mapping[str, object], *, received_at: datetime) -> RfiLiveQuoteSnapshot:
    ticker = str(row.get("ticker") or row.get("market_ticker") or "")
    return RfiLiveQuoteSnapshot(
        market_id=ticker,
        received_at=received_at,
        yes_bid=_probability_price(row.get("yes_bid_dollars") or row.get("yes_bid")),
        yes_ask=_probability_price(row.get("yes_ask_dollars") or row.get("yes_ask")),
        yes_bid_size=_float_value(row.get("yes_bid_size_fp") or row.get("yes_bid_size"), default=0.0),
        yes_ask_size=_float_value(row.get("yes_ask_size_fp") or row.get("yes_ask_size"), default=0.0),
        close_time=_optional_datetime(row.get("close_time")),
        status=str(row.get("status") or ""),
        title=str(row.get("title") or ""),
    )


def _live_quote_from_ws_state_row(
    row: Mapping[str, object],
    book_states: dict[str, _BookState],
) -> RfiLiveQuoteSnapshot | None:
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
        return _quote_from_book_state(
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
    return _quote_from_book_state(
        ticker,
        received_at=_parse_datetime(_required(row, "received_at")),
        state=book_states[ticker],
        source="kalshi_ws_orderbook_delta",
    )


def _live_quote_from_ws_row(row: Mapping[str, object]) -> RfiLiveQuoteSnapshot | None:
    payload = row.get("payload")
    if not isinstance(payload, Mapping):
        return None
    msg = payload.get("msg")
    if not isinstance(msg, Mapping):
        return None
    raw_type = str(payload.get("type") or msg.get("type") or row.get("channel") or "")
    if raw_type != "orderbook_snapshot":
        return None
    ticker = str(msg.get("market_ticker") or msg.get("ticker") or "")
    if not ticker:
        return None
    yes_bid, yes_bid_size = _best_level(msg.get("yes_dollars_fp"))
    no_bid, no_bid_size = _best_level(msg.get("no_dollars_fp"))
    if yes_bid is None or no_bid is None:
        return None
    return RfiLiveQuoteSnapshot(
        market_id=ticker,
        received_at=_parse_datetime(_required(row, "received_at")),
        yes_bid=yes_bid,
        yes_ask=1.0 - no_bid,
        yes_bid_size=yes_bid_size,
        yes_ask_size=no_bid_size,
        source="kalshi_ws_orderbook_snapshot",
        book_verified=True,
    )


def _quote_from_book_state(
    ticker: str,
    *,
    received_at: datetime,
    state: _BookState,
    source: str,
) -> RfiLiveQuoteSnapshot | None:
    yes_bid, yes_bid_size = _best_level_from_ladder(state.get("yes", {}))
    no_bid, no_bid_size = _best_level_from_ladder(state.get("no", {}))
    if yes_bid is None or no_bid is None:
        return None
    yes_ask = 1.0 - no_bid
    if yes_ask < yes_bid:
        return None
    return RfiLiveQuoteSnapshot(
        market_id=ticker,
        received_at=received_at,
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        yes_bid_size=yes_bid_size,
        yes_ask_size=no_bid_size,
        source=source,
        book_verified=True,
    )


def _best_level_from_ladder(ladder: Mapping[float, float]) -> tuple[float | None, float]:
    if not ladder:
        return None, 0.0
    best_price = max(ladder)
    return best_price, ladder[best_price]


def _best_level(levels: object) -> tuple[float | None, float]:
    if not isinstance(levels, Sequence) or isinstance(levels, str):
        return None, 0.0
    best_price: float | None = None
    best_size = 0.0
    for level in levels:
        if not isinstance(level, Sequence) or isinstance(level, str) or len(level) < 2:
            continue
        price = _probability_price(level[0])
        size = _float_value(level[1], default=0.0)
        if best_price is None or price > best_price:
            best_price = price
            best_size = size
    return best_price, best_size


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


def _market_to_mapping(row: RfiMarketResult) -> dict[str, object]:
    return {
        "market_id": row.market_id,
        "result": "yes" if row.result_yes else "no",
        "close_time": row.close_time.isoformat() if row.close_time else "",
        "title": row.title,
    }


def _trade_to_mapping(row: RfiTapeTrade) -> dict[str, object]:
    return {
        "market_id": row.market_id,
        "created_at": row.created_at.isoformat(),
        "yes_price": row.yes_price,
        "quantity": row.quantity,
    }


def _live_quote_to_mapping(row: RfiLiveQuoteSnapshot) -> dict[str, object]:
    return {
        "market_id": row.market_id,
        "received_at": row.received_at.isoformat(),
        "yes_bid": row.yes_bid,
        "yes_ask": row.yes_ask,
        "yes_bid_size": row.yes_bid_size,
        "yes_ask_size": row.yes_ask_size,
        "close_time": row.close_time.isoformat() if row.close_time else "",
        "status": row.status,
        "title": row.title,
        "source": row.source,
        "book_verified": row.book_verified,
    }


def _live_quote_columns() -> tuple[str, ...]:
    return (
        "market_id",
        "received_at",
        "yes_bid",
        "yes_ask",
        "yes_bid_size",
        "yes_ask_size",
        "close_time",
        "status",
        "title",
        "source",
        "book_verified",
    )


def _fixture_ws_orderbook_snapshot_row(
    market_id: str,
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
                "market_ticker": market_id,
                "yes_dollars_fp": [[f"{yes_bid:.4f}", "100.00"]],
                "no_dollars_fp": [[f"{no_bid:.4f}", "100.00"]],
            },
        },
    }


def _context_feature_by_market(context_features: Sequence[RfiContextFeature]) -> dict[str, RfiContextFeature]:
    out: dict[str, RfiContextFeature] = {}
    for feature in context_features:
        current = out.get(feature.market_id)
        if current is None:
            out[feature.market_id] = feature
            continue
        if current.feature_as_of is None or (
            feature.feature_as_of is not None and feature.feature_as_of >= current.feature_as_of
        ):
            out[feature.market_id] = feature
    return out


def _context_delta_for_sample(
    sample: RfiEarlyPriceSample,
    context_by_market: Mapping[str, RfiContextFeature],
    *,
    config: RfiEvaluationConfig,
) -> tuple[float, RfiContextFeature | None]:
    feature = context_by_market.get(sample.market_id)
    if feature is None:
        return 0.0, None
    if feature.feature_as_of is not None and feature.feature_as_of > sample.observed_at:
        raise ValueError(f"context feature for {sample.market_id} is after the early price observation")
    return _bounded_context_delta(feature.total_probability_delta, config=config), feature


def _bounded_context_delta(value: float, *, config: RfiEvaluationConfig) -> float:
    limit = config.max_context_probability_delta
    return min(limit, max(-limit, value))


def _context_from_mapping(row: Mapping[str, object]) -> RfiContextFeature:
    return RfiContextFeature(
        market_id=_required(row, "market_id"),
        feature_as_of=_optional_datetime(row.get("feature_as_of") or row.get("as_of")),
        rfi_probability_delta=_first_float(
            row,
            ("rfi_probability_delta", "context_probability_delta", "probability_delta"),
        ),
        injury_probability_delta=_first_float(row, ("injury_probability_delta", "injury_delta")),
        rest_probability_delta=_first_float(row, ("rest_probability_delta", "rest_delta")),
        roster_absence_probability_delta=_first_float(
            row,
            (
                "roster_absence_probability_delta",
                "roster_missing_probability_delta",
                "missing_player_probability_delta",
                "roster_delta",
            ),
        ),
        lineup_probability_delta=_first_float(row, ("lineup_probability_delta", "lineup_delta")),
        bullpen_rest_probability_delta=_first_float(
            row,
            ("bullpen_rest_probability_delta", "bullpen_probability_delta", "bullpen_delta"),
        ),
        starting_pitcher_probability_delta=_first_float(
            row,
            ("starting_pitcher_probability_delta", "starter_probability_delta", "starter_delta"),
        ),
        source=str(row.get("source") or ""),
    )


def _context_to_mapping(row: RfiContextFeature) -> dict[str, object]:
    return {
        "market_id": row.market_id,
        "feature_as_of": row.feature_as_of.isoformat() if row.feature_as_of else "",
        "rfi_probability_delta": row.rfi_probability_delta,
        "injury_probability_delta": row.injury_probability_delta,
        "rest_probability_delta": row.rest_probability_delta,
        "roster_absence_probability_delta": row.roster_absence_probability_delta,
        "lineup_probability_delta": row.lineup_probability_delta,
        "bullpen_rest_probability_delta": row.bullpen_rest_probability_delta,
        "starting_pitcher_probability_delta": row.starting_pitcher_probability_delta,
        "source": row.source,
    }


def _context_columns() -> tuple[str, ...]:
    return (
        "market_id",
        "feature_as_of",
        "rfi_probability_delta",
        "injury_probability_delta",
        "rest_probability_delta",
        "roster_absence_probability_delta",
        "lineup_probability_delta",
        "bullpen_rest_probability_delta",
        "starting_pitcher_probability_delta",
        "source",
    )


def _first_float(row: Mapping[str, object], fields: Sequence[str]) -> float:
    for field in fields:
        value = row.get(field)
        if value is not None and str(value).strip() != "":
            return _float_value(value)
    return 0.0


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"empty CSV: {path}")
        return [dict(row) for row in reader]


def _write_csv(path: Path, columns: Sequence[str], rows: Sequence[Mapping[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns), lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in columns})


def _get_json(url: str, *, params: Mapping[str, str]) -> dict[str, Any]:
    full_url = f"{url}?{urllib.parse.urlencode(params)}"
    last_error: Exception | None = None
    for attempt in range(5):
        try:
            request = urllib.request.Request(full_url, headers={"User-Agent": "eventcontracts-baseball-rfi/0.1"})
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if not isinstance(payload, Mapping):
                raise ValueError("Kalshi response was not an object")
            return dict(payload)
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code == 429:
                time.sleep(1.0 + attempt)
                continue
            raise
        except Exception as exc:
            last_error = exc
            time.sleep(0.5 + attempt * 0.25)
    raise RuntimeError(f"GET failed for {url}: {last_error}")


def _optional_datetime(value: object) -> datetime | None:
    if value is None or str(value).strip() == "":
        return None
    return _parse_datetime(str(value))


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


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


def _probability_price(value: object) -> float:
    parsed = _float_value(value)
    if parsed > 1.0 and parsed <= 100.0:
        parsed /= 100.0
    _require_probability(parsed, "price")
    return parsed


def _bool_value(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y"}


def _required(row: Mapping[str, object], field: str) -> str:
    value = row.get(field)
    if value is None or str(value).strip() == "":
        raise ValueError(f"missing required field {field!r}")
    return str(value).strip()


def _require_probability(value: float, name: str) -> None:
    if not math.isfinite(value) or value < 0.0 or value > 1.0:
        raise ValueError(f"{name} must be finite and in [0, 1]")


def _clip_probability(value: float) -> float:
    return min(1.0 - 1e-9, max(1e-9, value))


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _normal_ci(values: Sequence[float]) -> tuple[float | None, float | None]:
    if len(values) < 2:
        return None, None
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
    half_width = 1.96 * math.sqrt(variance / len(values))
    return mean - half_width, mean + half_width


def _fmt(value: float | None) -> str:
    return "" if value is None else f"{value:.4f}"


def _fmt_signed(value: float | None) -> str:
    return "" if value is None else f"{value:+.4f}"
