"""Research-only MLB outright residual edge validator.

The production sleeve for ``mlb-outright-residual-v1`` is the generic
``external_edge`` strategy. This module is the missing proof harness around that
producer: it checks whether an external MLB outright probability is better than
a sportsbook-futures reference and whether the difference survives executable
Kalshi touch, fees, quote freshness, capital duration, and correlation caps.

It never submits, cancels, replaces, or live-submits orders. Candidate rows are
shadow/tick-logging prompts until CLV/markout and settlement evidence clear.
"""

from __future__ import annotations

import csv
import json
import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from eventcontracts.research.ledger import to_jsonable, write_jsonl
from eventcontracts.research.tennis_market_residual import kalshi_taker_fee_per_contract

_EPS = 1e-12
_FORBIDDEN_SIGNAL_FIELDS = {
    "actual_outcome",
    "final",
    "final_rank",
    "label",
    "result",
    "settled_at",
    "settled_yes",
    "settlement",
    "target",
    "winner",
}


@dataclass(frozen=True)
class MlbOutrightValidationConfig:
    """Gate settings for long-duration MLB outright residuals."""

    min_net_edge: float = 0.03
    min_reference_residual: float = 0.01
    max_signal_age_ms: int = 24 * 60 * 60 * 1000
    max_quote_age_ms: int = 60 * 60 * 1000
    min_confidence: float = 0.0
    quantity: int = 1
    fee_rate_bps: int = 700
    slippage: float = 0.0
    capital_annual_rate: float = 0.05
    max_group_candidates: int = 2
    min_settlement_evidence: int = 20

    def __post_init__(self) -> None:
        if self.min_net_edge < 0.0:
            raise ValueError("min_net_edge must be non-negative")
        if self.min_reference_residual < 0.0:
            raise ValueError("min_reference_residual must be non-negative")
        if self.max_signal_age_ms < 0 or self.max_quote_age_ms < 0:
            raise ValueError("stale-age gates must be non-negative")
        if self.quantity <= 0:
            raise ValueError("quantity must be positive")
        if self.fee_rate_bps < 0:
            raise ValueError("fee_rate_bps must be non-negative")
        if self.slippage < 0.0:
            raise ValueError("slippage must be non-negative")
        if self.capital_annual_rate < 0.0:
            raise ValueError("capital_annual_rate must be non-negative")
        if self.max_group_candidates <= 0:
            raise ValueError("max_group_candidates must be positive")
        if self.min_settlement_evidence <= 0:
            raise ValueError("min_settlement_evidence must be positive")


@dataclass(frozen=True)
class MlbModelSignal:
    """One point-in-time MLB outright probability from an external producer."""

    market_id: str
    outcome_id: str
    group_id: str
    as_of: datetime
    yes_probability: float
    confidence: float
    source: str
    days_to_settlement: float
    correlation_group: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.market_id, "market_id")
        _require_text(self.outcome_id, "outcome_id")
        _require_text(self.group_id, "group_id")
        _require_text(self.source, "source")
        _require_probability(self.yes_probability, "yes_probability")
        _require_probability(self.confidence, "confidence")
        if self.as_of.tzinfo is None:
            raise ValueError("as_of must be timezone-aware")
        if self.days_to_settlement < 0.0:
            raise ValueError("days_to_settlement must be non-negative")

    @property
    def effective_correlation_group(self) -> str:
        return self.correlation_group or self.group_id


@dataclass(frozen=True)
class FuturesReferencePrice:
    """Sportsbook futures price for one mutually exclusive outcome."""

    group_id: str
    outcome_id: str
    decimal_odds: float
    source: str
    as_of: datetime

    def __post_init__(self) -> None:
        _require_text(self.group_id, "group_id")
        _require_text(self.outcome_id, "outcome_id")
        _require_text(self.source, "source")
        if not math.isfinite(self.decimal_odds) or self.decimal_odds <= 1.0:
            raise ValueError("decimal_odds must be finite and > 1")
        if self.as_of.tzinfo is None:
            raise ValueError("as_of must be timezone-aware")


@dataclass(frozen=True)
class KalshiOutrightQuote:
    """Public executable-touch quote snapshot for an MLB outright market."""

    market_id: str
    received_at: datetime
    yes_bid: float
    yes_ask: float
    yes_bid_size: float = 0.0
    yes_ask_size: float = 0.0

    def __post_init__(self) -> None:
        _require_text(self.market_id, "market_id")
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
class MlbSettlementOutcome:
    """Optional post-event evidence used only for proof reports."""

    market_id: str
    outcome_id: str
    settled_yes: bool
    settled_at: datetime | None = None
    closing_yes_mid: float | None = None

    def __post_init__(self) -> None:
        _require_text(self.market_id, "market_id")
        _require_text(self.outcome_id, "outcome_id")
        if self.settled_at is not None and self.settled_at.tzinfo is None:
            raise ValueError("settled_at must be timezone-aware")
        if self.closing_yes_mid is not None:
            _require_probability(self.closing_yes_mid, "closing_yes_mid")


@dataclass(frozen=True)
class DeviggedFuturesProbability:
    """Reference probability from a de-vigged futures board."""

    group_id: str
    outcome_id: str
    probability: float
    raw_implied_probability: float
    overround: float
    sources: tuple[str, ...]
    as_of: datetime


@dataclass(frozen=True)
class MlbOutrightCandidate:
    """Fee/spread/duration-aware candidate row."""

    market_id: str
    outcome_id: str
    group_id: str
    correlation_group: str
    source: str
    signal_as_of: datetime
    side: str
    fair_yes_probability: float
    reference_probability: float | None
    reference_residual: float | None
    executable_price: float | None
    limit_price: float | None
    fee: float | None
    duration_penalty: float | None
    gross_edge: float | None
    net_edge: float | None
    spread: float | None
    signal_age_ms: int
    quote_age_ms: int | None
    confidence: float
    candidate: bool
    reason: str

    def as_signal_payload(self) -> dict[str, object]:
        """ExternalSignalEvent-shaped payload for paper/shadow wiring."""

        return {
            "market_id": self.market_id,
            "probability": self.fair_yes_probability,
            "confidence": self.confidence,
            "source_model": self.source,
            "candidate": self.candidate,
            "reason": self.reason,
            "reference_probability": self.reference_probability,
            "net_edge": self.net_edge,
            "side": self.side,
        }


@dataclass(frozen=True)
class MlbEvidenceSummary:
    """Realized settlement/CLV evidence for rows that passed the candidate gate."""

    n: int
    mean_realized_ev: float | None
    total_realized_ev: float | None
    mean_clv: float | None
    positive_settlement_ev: bool
    positive_or_missing_clv: bool
    proven: bool
    reason: str

    def as_dict(self) -> dict[str, object]:
        return {
            "n": self.n,
            "mean_realized_ev": self.mean_realized_ev,
            "total_realized_ev": self.total_realized_ev,
            "mean_clv": self.mean_clv,
            "positive_settlement_ev": self.positive_settlement_ev,
            "positive_or_missing_clv": self.positive_or_missing_clv,
            "proven": self.proven,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class MlbOutrightValidationReport:
    """Complete MLB outright residual validation result."""

    as_of: datetime
    signal_count: int
    reference_count: int
    quote_count: int
    candidates: tuple[MlbOutrightCandidate, ...]
    evidence: MlbEvidenceSummary
    decision_gate: str

    def as_dict(self) -> dict[str, object]:
        return {
            "as_of": self.as_of.isoformat(),
            "signal_count": self.signal_count,
            "reference_count": self.reference_count,
            "quote_count": self.quote_count,
            "candidate_count": sum(1 for item in self.candidates if item.candidate),
            "decision_gate": self.decision_gate,
            "evidence": self.evidence.as_dict(),
            "candidates": [to_jsonable(item) for item in self.candidates],
        }


def devig_futures_board(
    references: Sequence[FuturesReferencePrice],
) -> dict[tuple[str, str], DeviggedFuturesProbability]:
    """De-vig a futures board by group using averaged raw implied probabilities."""

    by_group: dict[str, dict[str, list[FuturesReferencePrice]]] = defaultdict(lambda: defaultdict(list))
    for ref in references:
        by_group[ref.group_id][ref.outcome_id].append(ref)
    out: dict[tuple[str, str], DeviggedFuturesProbability] = {}
    for group_id, by_outcome in by_group.items():
        raw_by_outcome: dict[str, float] = {}
        sources_by_outcome: dict[str, set[str]] = {}
        as_of_by_outcome: dict[str, datetime] = {}
        for outcome_id, rows in by_outcome.items():
            raw_values = [1.0 / row.decimal_odds for row in rows]
            raw_by_outcome[outcome_id] = sum(raw_values) / len(raw_values)
            sources_by_outcome[outcome_id] = {row.source for row in rows}
            as_of_by_outcome[outcome_id] = max(row.as_of for row in rows)
        overround = sum(raw_by_outcome.values())
        if overround <= 0.0:
            raise ValueError(f"reference board has non-positive overround: {group_id}")
        for outcome_id, raw in raw_by_outcome.items():
            out[(group_id, outcome_id)] = DeviggedFuturesProbability(
                group_id=group_id,
                outcome_id=outcome_id,
                probability=_clip_probability(raw / overround),
                raw_implied_probability=raw,
                overround=overround,
                sources=tuple(sorted(sources_by_outcome[outcome_id])),
                as_of=as_of_by_outcome[outcome_id],
            )
    return out


def evaluate_mlb_outright_residual(
    signals: Sequence[MlbModelSignal],
    references: Sequence[FuturesReferencePrice],
    quotes: Sequence[KalshiOutrightQuote],
    *,
    settlements: Sequence[MlbSettlementOutcome] = (),
    as_of: datetime | None = None,
    config: MlbOutrightValidationConfig | None = None,
) -> MlbOutrightValidationReport:
    """Evaluate MLB outright model-vs-market rows without trading."""

    cfg = config or MlbOutrightValidationConfig()
    now = as_of or _max_as_of(signals, quotes)
    reference_by_key = devig_futures_board(references)
    quote_by_market = {quote.market_id: quote for quote in quotes}
    rows = [
        _candidate_from_signal(
            signal,
            reference_by_key.get((signal.group_id, signal.outcome_id)),
            quote_by_market.get(signal.market_id),
            as_of=now,
            config=cfg,
        )
        for signal in signals
    ]
    capped = _apply_correlation_caps(rows, cfg)
    evidence = score_mlb_settlement_evidence(capped, settlements, config=cfg)
    report = MlbOutrightValidationReport(
        as_of=now,
        signal_count=len(signals),
        reference_count=len(reference_by_key),
        quote_count=len(quotes),
        candidates=tuple(sorted(capped, key=lambda item: item.net_edge or -999.0, reverse=True)),
        evidence=evidence,
        decision_gate=_decision_gate(capped, evidence),
    )
    return report


def score_mlb_settlement_evidence(
    candidates: Sequence[MlbOutrightCandidate],
    settlements: Sequence[MlbSettlementOutcome],
    *,
    config: MlbOutrightValidationConfig | None = None,
) -> MlbEvidenceSummary:
    """Score realized EV and CLV for already-gated candidate rows."""

    cfg = config or MlbOutrightValidationConfig()
    by_market = {row.market_id: row for row in settlements}
    realized: list[float] = []
    clv: list[float] = []
    for candidate in candidates:
        if not candidate.candidate:
            continue
        if candidate.executable_price is None or candidate.fee is None:
            continue
        outcome = by_market.get(candidate.market_id)
        if outcome is None:
            continue
        if candidate.side == "YES":
            payoff = 1.0 if outcome.settled_yes else 0.0
            closing_value = outcome.closing_yes_mid
        elif candidate.side == "NO":
            payoff = 0.0 if outcome.settled_yes else 1.0
            closing_value = None if outcome.closing_yes_mid is None else 1.0 - outcome.closing_yes_mid
        else:
            continue
        duration = candidate.duration_penalty or 0.0
        realized.append(payoff - candidate.executable_price - candidate.fee - duration)
        if closing_value is not None:
            clv.append(closing_value - candidate.executable_price - candidate.fee)

    n = len(realized)
    if n == 0:
        return MlbEvidenceSummary(
            n=0,
            mean_realized_ev=None,
            total_realized_ev=None,
            mean_clv=None,
            positive_settlement_ev=False,
            positive_or_missing_clv=False,
            proven=False,
            reason="no_candidate_settlement_evidence",
        )
    mean_realized = sum(realized) / n
    total = sum(realized)
    mean_clv = (sum(clv) / len(clv)) if clv else None
    positive_clv = mean_clv is None or mean_clv > 0.0
    enough = n >= cfg.min_settlement_evidence
    proven = enough and mean_realized > 0.0 and positive_clv
    if not enough:
        reason = "settlement_sample_too_small"
    elif mean_realized <= 0.0:
        reason = "negative_settlement_ev"
    elif not positive_clv:
        reason = "negative_clv"
    else:
        reason = "settlement_and_clv_positive"
    return MlbEvidenceSummary(
        n=n,
        mean_realized_ev=mean_realized,
        total_realized_ev=total,
        mean_clv=mean_clv,
        positive_settlement_ev=mean_realized > 0.0,
        positive_or_missing_clv=positive_clv,
        proven=proven,
        reason=reason,
    )


def read_model_signals_jsonl(path: Path) -> tuple[MlbModelSignal, ...]:
    """Read point-in-time model signals, rejecting label/settlement fields."""

    rows: list[MlbModelSignal] = []
    for payload in _read_jsonl(path):
        _reject_forbidden_signal_fields(payload)
        rows.append(model_signal_from_mapping(payload))
    return tuple(rows)


def model_signal_from_mapping(payload: Mapping[str, object]) -> MlbModelSignal:
    """Build a model signal from JSON-like input."""

    _reject_forbidden_signal_fields(payload)
    return MlbModelSignal(
        market_id=_required(payload, "market_id"),
        outcome_id=_required(payload, "outcome_id"),
        group_id=_required(payload, "group_id"),
        as_of=_parse_datetime(_required(payload, "as_of")),
        yes_probability=_float_value(payload.get("yes_probability") or payload.get("probability")),
        confidence=_float_value(payload.get("confidence"), 0.0),
        source=str(payload.get("source") or "mlb-outright-model"),
        days_to_settlement=_float_value(payload.get("days_to_settlement")),
        correlation_group=_optional_str(payload.get("correlation_group")),
    )


def read_references_csv(path: Path) -> tuple[FuturesReferencePrice, ...]:
    """Read sportsbook futures reference prices from CSV."""

    return tuple(reference_from_mapping(row) for row in _read_csv(path))


def reference_from_mapping(payload: Mapping[str, object]) -> FuturesReferencePrice:
    return FuturesReferencePrice(
        group_id=_required(payload, "group_id"),
        outcome_id=_required(payload, "outcome_id"),
        decimal_odds=_float_value(payload.get("decimal_odds")),
        source=str(payload.get("source") or "sportsbook_futures"),
        as_of=_parse_datetime(_required(payload, "as_of")),
    )


def read_quotes_csv(path: Path) -> tuple[KalshiOutrightQuote, ...]:
    """Read public Kalshi quote snapshots from CSV."""

    return tuple(quote_from_mapping(row) for row in _read_csv(path))


def quote_from_mapping(payload: Mapping[str, object]) -> KalshiOutrightQuote:
    return KalshiOutrightQuote(
        market_id=_required(payload, "market_id"),
        received_at=_parse_datetime(_required(payload, "received_at")),
        yes_bid=_float_value(payload.get("yes_bid")),
        yes_ask=_float_value(payload.get("yes_ask")),
        yes_bid_size=_float_value(payload.get("yes_bid_size"), 0.0),
        yes_ask_size=_float_value(payload.get("yes_ask_size"), 0.0),
    )


def read_settlements_csv(path: Path) -> tuple[MlbSettlementOutcome, ...]:
    """Read optional settlement/CLV evidence from CSV."""

    return tuple(settlement_from_mapping(row) for row in _read_csv(path))


def settlement_from_mapping(payload: Mapping[str, object]) -> MlbSettlementOutcome:
    settled_at = _optional_str(payload.get("settled_at"))
    closing = _optional_str(payload.get("closing_yes_mid"))
    return MlbSettlementOutcome(
        market_id=_required(payload, "market_id"),
        outcome_id=_required(payload, "outcome_id"),
        settled_yes=_bool_value(payload.get("settled_yes")),
        settled_at=_parse_datetime(settled_at) if settled_at else None,
        closing_yes_mid=float(closing) if closing else None,
    )


def fixture_signals(as_of: datetime | None = None) -> tuple[MlbModelSignal, ...]:
    """Deterministic no-network MLB outright model signals."""

    now = as_of or datetime(2026, 6, 3, 16, 0, tzinfo=UTC)
    return (
        MlbModelSignal(
            market_id="KXMLBSERIES-26-WS-DODGERS",
            outcome_id="dodgers",
            group_id="world_series_2026",
            as_of=now,
            yes_probability=0.21,
            confidence=0.64,
            source="mlb-outright-model-fixture",
            days_to_settlement=120.0,
            correlation_group="world_series_2026",
        ),
        MlbModelSignal(
            market_id="KXMLBSERIES-26-WS-METS",
            outcome_id="mets",
            group_id="world_series_2026",
            as_of=now,
            yes_probability=0.06,
            confidence=0.58,
            source="mlb-outright-model-fixture",
            days_to_settlement=120.0,
            correlation_group="world_series_2026",
        ),
        MlbModelSignal(
            market_id="KXMLBSERIES-26-WS-YANKEES",
            outcome_id="yankees",
            group_id="world_series_2026",
            as_of=now,
            yes_probability=0.13,
            confidence=0.55,
            source="mlb-outright-model-fixture",
            days_to_settlement=120.0,
            correlation_group="world_series_2026",
        ),
    )


def fixture_references(as_of: datetime | None = None) -> tuple[FuturesReferencePrice, ...]:
    """Deterministic futures board with a de-vigged sharp reference."""

    now = as_of or datetime(2026, 6, 3, 15, 59, tzinfo=UTC)
    rows = [
        ("dodgers", 5.0),
        ("yankees", 7.0),
        ("mets", 9.0),
        ("braves", 10.0),
        ("phillies", 11.0),
        ("orioles", 13.0),
        ("padres", 17.0),
        ("mariners", 19.0),
        ("field", 2.6),
    ]
    return tuple(
        FuturesReferencePrice(
            group_id="world_series_2026",
            outcome_id=outcome_id,
            decimal_odds=odds,
            source="fixture_consensus",
            as_of=now,
        )
        for outcome_id, odds in rows
    )


def fixture_quotes(as_of: datetime | None = None) -> tuple[KalshiOutrightQuote, ...]:
    """Deterministic public quote fixture."""

    received = (as_of or datetime(2026, 6, 3, 16, 0, tzinfo=UTC))
    return (
        KalshiOutrightQuote("KXMLBSERIES-26-WS-DODGERS", received, 0.13, 0.15, 90.0, 95.0),
        KalshiOutrightQuote("KXMLBSERIES-26-WS-METS", received, 0.13, 0.16, 80.0, 85.0),
        KalshiOutrightQuote("KXMLBSERIES-26-WS-YANKEES", received, 0.11, 0.14, 75.0, 70.0),
    )


def write_fixture_inputs(out_dir: Path) -> dict[str, str]:
    """Write reusable no-network inputs for the MLB residual validator."""

    out_dir.mkdir(parents=True, exist_ok=True)
    signals_path = out_dir / "model_signals.jsonl"
    references_path = out_dir / "futures_references.csv"
    quotes_path = out_dir / "kalshi_quotes.csv"
    write_jsonl(signals_path, [to_jsonable(item) for item in fixture_signals()])
    _write_csv(references_path, _reference_columns(), [_reference_to_mapping(item) for item in fixture_references()])
    _write_csv(quotes_path, _quote_columns(), [_quote_to_mapping(item) for item in fixture_quotes()])
    return {
        "signals_jsonl": str(signals_path),
        "references_csv": str(references_path),
        "quotes_csv": str(quotes_path),
    }


def write_report_outputs(
    report: MlbOutrightValidationReport,
    *,
    report_json: Path,
    report_md: Path | None = None,
    signals_jsonl: Path | None = None,
) -> None:
    """Write JSON, markdown, and ExternalSignal-shaped candidate payloads."""

    report_json.parent.mkdir(parents=True, exist_ok=True)
    report_json.write_text(
        json.dumps(to_jsonable(report.as_dict()), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if report_md is not None:
        report_md.parent.mkdir(parents=True, exist_ok=True)
        report_md.write_text(render_markdown(report), encoding="utf-8")
    if signals_jsonl is not None:
        rows = [candidate.as_signal_payload() for candidate in report.candidates if candidate.candidate]
        write_jsonl(signals_jsonl, rows)


def render_markdown(report: MlbOutrightValidationReport) -> str:
    """Render a compact edge-validation report."""

    candidate_count = sum(1 for item in report.candidates if item.candidate)
    lines = [
        "# MLB Outright Residual Validation",
        "",
        f"- As of: `{report.as_of.isoformat()}`",
        f"- Signals: `{report.signal_count}`",
        f"- Reference outcomes: `{report.reference_count}`",
        f"- Quote snapshots: `{report.quote_count}`",
        f"- Fee-net candidates: `{candidate_count}`",
        f"- Settlement evidence rows: `{report.evidence.n}`",
        f"- Decision: **{report.decision_gate}**",
        "",
        "## Candidate Gate",
        "",
        "| Market | Side | Fair | Ref | Touch | Net Edge | Reason |",
        "| --- | --- | ---: | ---: | ---: | ---: | --- |",
    ]
    for item in report.candidates:
        lines.append(
            f"| {item.market_id} | {item.side} | {item.fair_yes_probability:.4f} | "
            f"{_fmt_prob(item.reference_probability)} | {_fmt_prob(item.executable_price)} | "
            f"{_fmt_signed(item.net_edge)} | {item.reason} |"
        )
    lines.extend(
        [
            "",
            "## Evidence Gate",
            "",
            f"- Evidence verdict: `{report.evidence.reason}`",
            f"- Mean realized EV: `{_fmt_signed(report.evidence.mean_realized_ev)}`",
            f"- Mean CLV: `{_fmt_signed(report.evidence.mean_clv)}`",
            "",
            "## Boundary",
            "",
            "This is read-only validation for an external-edge producer. Candidate rows are not "
            "edge until chronological OOS, executable quote capture, CLV/markout, and "
            "settlement evidence are positive at sufficient sample size.",
            "",
        ]
    )
    return "\n".join(lines)


def _candidate_from_signal(
    signal: MlbModelSignal,
    reference: DeviggedFuturesProbability | None,
    quote: KalshiOutrightQuote | None,
    *,
    as_of: datetime,
    config: MlbOutrightValidationConfig,
) -> MlbOutrightCandidate:
    signal_age_ms = max(0, int((as_of - signal.as_of).total_seconds() * 1000))
    if quote is None:
        return _blocked_candidate(signal, as_of, reference, "missing_quote", signal_age_ms)
    quote_age_ms = max(0, int((as_of - quote.received_at).total_seconds() * 1000))

    yes_gross = signal.yes_probability - quote.yes_ask
    no_price = 1.0 - quote.yes_bid
    no_gross = (1.0 - signal.yes_probability) - no_price
    if yes_gross >= no_gross:
        side = "YES"
        executable = quote.yes_ask
        gross = yes_gross
    else:
        side = "NO"
        executable = no_price
        gross = no_gross
    fee = kalshi_taker_fee_per_contract(executable, config.quantity, config.fee_rate_bps)
    duration = executable * config.capital_annual_rate * signal.days_to_settlement / 365.0
    net = gross - fee - duration - config.slippage
    ref_prob = reference.probability if reference is not None else None
    ref_residual = _reference_residual_for_side(
        model_probability=signal.yes_probability,
        reference_probability=ref_prob,
        side=side,
    )

    if reference is None:
        reason = "missing_futures_reference"
    elif signal_age_ms > config.max_signal_age_ms:
        reason = "stale_model_signal"
    elif quote_age_ms > config.max_quote_age_ms:
        reason = "stale_quote"
    elif signal.confidence < config.min_confidence:
        reason = "confidence_below_floor"
    elif ref_residual is None or ref_residual < config.min_reference_residual:
        reason = "fails_reference_residual_gate"
    elif net < config.min_net_edge:
        reason = "fails_fee_spread_duration_gate"
    else:
        reason = "fee_net_candidate_needs_markout_settlement"
    return MlbOutrightCandidate(
        market_id=signal.market_id,
        outcome_id=signal.outcome_id,
        group_id=signal.group_id,
        correlation_group=signal.effective_correlation_group,
        source=signal.source,
        signal_as_of=signal.as_of,
        side=side,
        fair_yes_probability=signal.yes_probability,
        reference_probability=ref_prob,
        reference_residual=ref_residual,
        executable_price=executable,
        limit_price=executable,
        fee=fee,
        duration_penalty=duration,
        gross_edge=gross,
        net_edge=net,
        spread=quote.spread,
        signal_age_ms=signal_age_ms,
        quote_age_ms=quote_age_ms,
        confidence=signal.confidence,
        candidate=reason == "fee_net_candidate_needs_markout_settlement",
        reason=reason,
    )


def _blocked_candidate(
    signal: MlbModelSignal,
    as_of: datetime,
    reference: DeviggedFuturesProbability | None,
    reason: str,
    signal_age_ms: int | None = None,
) -> MlbOutrightCandidate:
    ref_prob = reference.probability if reference is not None else None
    age_ms = signal_age_ms
    if age_ms is None:
        age_ms = max(0, int((as_of - signal.as_of).total_seconds() * 1000))
    return MlbOutrightCandidate(
        market_id=signal.market_id,
        outcome_id=signal.outcome_id,
        group_id=signal.group_id,
        correlation_group=signal.effective_correlation_group,
        source=signal.source,
        signal_as_of=signal.as_of,
        side="NONE",
        fair_yes_probability=signal.yes_probability,
        reference_probability=ref_prob,
        reference_residual=None,
        executable_price=None,
        limit_price=None,
        fee=None,
        duration_penalty=None,
        gross_edge=None,
        net_edge=None,
        spread=None,
        signal_age_ms=age_ms,
        quote_age_ms=None,
        confidence=signal.confidence,
        candidate=False,
        reason=reason,
    )


def _apply_correlation_caps(
    rows: Sequence[MlbOutrightCandidate],
    config: MlbOutrightValidationConfig,
) -> list[MlbOutrightCandidate]:
    grouped: dict[str, int] = defaultdict(int)
    capped: list[MlbOutrightCandidate] = []
    for row in sorted(rows, key=lambda item: item.net_edge or -999.0, reverse=True):
        if not row.candidate:
            capped.append(row)
            continue
        grouped[row.correlation_group] += 1
        if grouped[row.correlation_group] > config.max_group_candidates:
            capped.append(replace(row, candidate=False, reason="correlation_group_cap"))
        else:
            capped.append(row)
    return capped


def _reference_residual_for_side(
    *,
    model_probability: float,
    reference_probability: float | None,
    side: str,
) -> float | None:
    if reference_probability is None:
        return None
    if side == "YES":
        return model_probability - reference_probability
    if side == "NO":
        return reference_probability - model_probability
    return None


def _decision_gate(
    candidates: Sequence[MlbOutrightCandidate],
    evidence: MlbEvidenceSummary,
) -> str:
    if evidence.proven:
        return "paper only: settlement and CLV evidence positive; still needs live liquidity and capacity audit"
    if any(item.candidate for item in candidates):
        return "start or continue tick logging: fee-net candidates need CLV/markout/settlement"
    if not candidates:
        return "continue research: no model signals"
    return "continue research: no fee-net executable residual candidates"


def _max_as_of(signals: Sequence[MlbModelSignal], quotes: Sequence[KalshiOutrightQuote]) -> datetime:
    values = [item.as_of for item in signals] + [item.received_at for item in quotes]
    return max(values) if values else datetime.now(UTC)


def _reject_forbidden_signal_fields(payload: Mapping[str, object]) -> None:
    forbidden = sorted(set(payload).intersection(_FORBIDDEN_SIGNAL_FIELDS))
    if forbidden:
        raise ValueError(f"model signal contains label/settlement fields: {', '.join(forbidden)}")


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        if not isinstance(payload, dict):
            raise ValueError(f"JSONL row must be an object: {path}")
        rows.append(payload)
    return rows


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


def _reference_columns() -> tuple[str, ...]:
    return ("group_id", "outcome_id", "decimal_odds", "source", "as_of")


def _quote_columns() -> tuple[str, ...]:
    return ("market_id", "received_at", "yes_bid", "yes_ask", "yes_bid_size", "yes_ask_size")


def _reference_to_mapping(row: FuturesReferencePrice) -> dict[str, object]:
    return {
        "group_id": row.group_id,
        "outcome_id": row.outcome_id,
        "decimal_odds": row.decimal_odds,
        "source": row.source,
        "as_of": row.as_of.isoformat(),
    }


def _quote_to_mapping(row: KalshiOutrightQuote) -> dict[str, object]:
    return {
        "market_id": row.market_id,
        "received_at": row.received_at.isoformat(),
        "yes_bid": row.yes_bid,
        "yes_ask": row.yes_ask,
        "yes_bid_size": row.yes_bid_size,
        "yes_ask_size": row.yes_ask_size,
    }


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _bool_value(value: object) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y"}:
        return True
    if text in {"0", "false", "no", "n"}:
        return False
    raise ValueError(f"cannot parse boolean value: {value!r}")


def _float_value(value: object, default: float | None = None) -> float:
    if value is None or str(value).strip() == "":
        if default is not None:
            return default
        raise ValueError("missing numeric value")
    parsed = float(str(value))
    if not math.isfinite(parsed):
        raise ValueError("numeric value must be finite")
    return parsed


def _optional_str(value: object) -> str | None:
    if value is None or str(value).strip() == "":
        return None
    return str(value).strip()


def _required(row: Mapping[str, object], field: str) -> str:
    value = row.get(field)
    if value is None or str(value).strip() == "":
        raise ValueError(f"missing required field {field!r}")
    return str(value).strip()


def _require_text(value: str, name: str) -> None:
    if not value:
        raise ValueError(f"{name} must not be empty")


def _require_probability(value: float, name: str) -> None:
    if not math.isfinite(value) or value < 0.0 or value > 1.0:
        raise ValueError(f"{name} must be finite and in [0, 1]")


def _clip_probability(value: float) -> float:
    return min(1.0 - _EPS, max(_EPS, value))


def _fmt_prob(value: float | None) -> str:
    return "" if value is None else f"{value:.4f}"


def _fmt_signed(value: float | None) -> str:
    return "" if value is None else f"{value:+.4f}"
