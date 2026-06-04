"""Golf market mapping, historical import, and no-trade shadow fills.

This module is research-only. It never submits, cancels, replaces, or live-submits
orders. The live-paper bridge records hypothetical decisions against public
market data so fill probability, fees, spread, stale quotes, CLV/markout, and
settlement can be measured before any stronger promotion decision.
"""

from __future__ import annotations

import csv
import json
import math
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from eventcontracts.research.calibration import kalshi_fee

GOLF_MARKET_MAP_COLUMNS: tuple[str, ...] = (
    "market_ticker",
    "series_ticker",
    "event_ticker",
    "tournament_id",
    "market_family",
    "subject_id",
    "subject_name",
    "top_n",
    "cut_line",
    "cut_line_relation",
    "status",
    "close_time",
    "expected_expiration_time",
    "title",
)

GOLF_HISTORICAL_COLUMNS: tuple[str, ...] = (
    "event_date",
    "tournament_id",
    "market_family",
    "market_ticker",
    "subject_id",
    "subject_name",
    "feature_as_of",
    "decision_time",
    "target",
    "settlement_status",
    "market_bid",
    "market_ask",
    "yes_bid_size",
    "yes_ask_size",
    "volume",
    "volume_24h",
    "open_interest",
    "close_time",
    "expected_expiration_time",
    "odds_probability",
    "reference_price_source",
    "baseline_skill_z",
    "sg_approach_z",
    "sg_putting_z",
    "strokes_to_cut",
    "wave_weather_delta",
    "field_scoring_avg_delta_vs_par",
    "top_65_current_score",
    "cut_line",
    "cut_line_relation",
    "market_mid",
    "spread",
    "liquidity",
    "time_to_decision_hours",
)

SHADOW_FILL_COLUMNS: tuple[str, ...] = (
    "intent_id",
    "decision_time",
    "market_ticker",
    "market_family",
    "side",
    "quantity",
    "fair_yes_probability",
    "limit_price",
    "yes_bid",
    "yes_ask",
    "yes_bid_size",
    "yes_ask_size",
    "spread",
    "quote_received_at",
    "quote_age_ms",
    "stale_quote",
    "executable_touch",
    "fee",
    "gross_edge",
    "net_edge",
    "queue_model",
    "queue_ahead",
    "would_fill_now",
    "would_fill_later",
    "fill_price",
    "fill_time",
    "fill_reason",
    "clv_close",
    "markout_1m",
    "markout_5m",
    "markout_30m",
    "settlement_value",
    "settlement_source",
    "candidate",
    "reject_reason",
)

_TOP_N_RE = re.compile(r"(?:TOP|TOPN)(\d+)")
_PLAYER_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"will\s+(.+?)\s+finish\s+top\s+\d+", re.IGNORECASE),
    re.compile(r"will\s+(.+?)\s+make\s+the\s+cut", re.IGNORECASE),
    re.compile(r"will\s+(.+?)\s+miss\s+the\s+cut", re.IGNORECASE),
)


@dataclass(frozen=True)
class GolfMarketMapRow:
    """Deterministic mapping from Kalshi market payload to golf subject ids."""

    market_ticker: str
    series_ticker: str
    event_ticker: str
    tournament_id: str
    market_family: str
    subject_id: str
    subject_name: str
    top_n: int | None
    cut_line: int | None
    cut_line_relation: str | None
    status: str
    close_time: str | None
    expected_expiration_time: str | None
    title: str

    def as_dict(self) -> dict[str, object]:
        return {
            "market_ticker": self.market_ticker,
            "series_ticker": self.series_ticker,
            "event_ticker": self.event_ticker,
            "tournament_id": self.tournament_id,
            "market_family": self.market_family,
            "subject_id": self.subject_id,
            "subject_name": self.subject_name,
            "top_n": "" if self.top_n is None else self.top_n,
            "cut_line": "" if self.cut_line is None else self.cut_line,
            "cut_line_relation": self.cut_line_relation or "",
            "status": self.status,
            "close_time": self.close_time or "",
            "expected_expiration_time": self.expected_expiration_time or "",
            "title": self.title,
        }


@dataclass(frozen=True)
class HistoricalBuildReport:
    """Summary of a historical golf outcome CSV build."""

    output_path: str
    market_family: str
    rows_written: int
    features_read: int
    labels_read: int
    snapshots_read: int
    rows_missing_market: int
    decision_gate: str

    def as_dict(self) -> dict[str, object]:
        return {
            "output_path": self.output_path,
            "market_family": self.market_family,
            "rows_written": self.rows_written,
            "features_read": self.features_read,
            "labels_read": self.labels_read,
            "snapshots_read": self.snapshots_read,
            "rows_missing_market": self.rows_missing_market,
            "decision_gate": self.decision_gate,
        }


@dataclass(frozen=True)
class ShadowFillReport:
    """Summary of a no-trade shadow-fill ledger run."""

    output_path: str
    intents_read: int
    quotes_read: int
    trades_read: int
    rows_written: int
    would_fill_now: int
    would_fill_later: int
    stale_rejections: int
    candidate_rows: int
    decision_gate: str

    def as_dict(self) -> dict[str, object]:
        return {
            "output_path": self.output_path,
            "intents_read": self.intents_read,
            "quotes_read": self.quotes_read,
            "trades_read": self.trades_read,
            "rows_written": self.rows_written,
            "would_fill_now": self.would_fill_now,
            "would_fill_later": self.would_fill_later,
            "stale_rejections": self.stale_rejections,
            "candidate_rows": self.candidate_rows,
            "decision_gate": self.decision_gate,
        }


@dataclass(frozen=True)
class ShadowFillLedgerSummary:
    """Aggregate evidence from a no-trade shadow-fill ledger."""

    ledger_path: str
    rows_read: int
    candidate_rows: int
    filled_rows: int
    would_fill_now: int
    would_fill_later: int
    stale_rejections: int
    missing_quote_rejections: int
    not_filled_rejections: int
    avg_fee: float | None
    avg_spread: float | None
    avg_gross_edge: float | None
    avg_net_edge: float | None
    avg_clv_close: float | None
    avg_markout_1m: float | None
    avg_markout_5m: float | None
    avg_markout_30m: float | None
    settlement_rows: int
    avg_settlement_value: float | None
    settlement_wins: int
    fixture_mode: bool
    decision_gate: str

    def as_dict(self) -> dict[str, object]:
        return {
            "ledger_path": self.ledger_path,
            "rows_read": self.rows_read,
            "candidate_rows": self.candidate_rows,
            "candidate_rate": _rate(self.candidate_rows, self.rows_read),
            "filled_rows": self.filled_rows,
            "fill_rate": _rate(self.filled_rows, self.rows_read),
            "would_fill_now": self.would_fill_now,
            "would_fill_later": self.would_fill_later,
            "stale_rejections": self.stale_rejections,
            "missing_quote_rejections": self.missing_quote_rejections,
            "not_filled_rejections": self.not_filled_rejections,
            "avg_fee": self.avg_fee,
            "avg_spread": self.avg_spread,
            "avg_gross_edge": self.avg_gross_edge,
            "avg_net_edge": self.avg_net_edge,
            "avg_clv_close": self.avg_clv_close,
            "avg_markout_1m": self.avg_markout_1m,
            "avg_markout_5m": self.avg_markout_5m,
            "avg_markout_30m": self.avg_markout_30m,
            "settlement_rows": self.settlement_rows,
            "avg_settlement_value": self.avg_settlement_value,
            "settlement_wins": self.settlement_wins,
            "fixture_mode": self.fixture_mode,
            "decision_gate": self.decision_gate,
        }


def map_kalshi_golf_market(market: Mapping[str, object]) -> GolfMarketMapRow | None:
    """Map a public Kalshi golf market payload into deterministic ids."""

    ticker = str(market.get("ticker") or market.get("market_ticker") or "").strip()
    if not ticker:
        return None
    series = str(market.get("series_ticker") or ticker.split("-", 1)[0]).upper()
    title = str(market.get("title") or market.get("yes_sub_title") or "").strip()
    event_ticker = str(market.get("event_ticker") or _event_from_ticker(ticker)).strip()
    family = _market_family(series, title)
    if family is None:
        return None
    top_n = _top_n_from_series(series)
    cut_line, cut_relation = _cut_line_from_market(ticker, title) if family == "cut_line" else (None, None)
    subject_name = _subject_name_from_title(title) if family != "cut_line" else _cutline_subject(cut_line, cut_relation)
    subject_id = _subject_id(subject_name) if subject_name else _subject_from_ticker(ticker)
    if family == "cut_line":
        subject_id = _subject_from_ticker(ticker)
    return GolfMarketMapRow(
        market_ticker=ticker,
        series_ticker=series,
        event_ticker=event_ticker,
        tournament_id=event_ticker or _event_from_ticker(ticker),
        market_family=family,
        subject_id=subject_id,
        subject_name=subject_name or subject_id,
        top_n=top_n,
        cut_line=cut_line,
        cut_line_relation=cut_relation,
        status=str(market.get("status") or ""),
        close_time=_optional_str(market.get("close_time")),
        expected_expiration_time=_optional_str(market.get("expected_expiration_time")),
        title=title,
    )


def map_kalshi_golf_markets(markets: Sequence[Mapping[str, object]]) -> list[GolfMarketMapRow]:
    """Map all recognized top-N, make-cut, and cut-line markets."""

    mapped = [map_kalshi_golf_market(market) for market in markets]
    return sorted((row for row in mapped if row is not None), key=lambda row: row.market_ticker)


def write_market_map_csv(path: Path, rows: Sequence[GolfMarketMapRow]) -> None:
    """Write market mapping rows."""

    _write_dicts(path, GOLF_MARKET_MAP_COLUMNS, [row.as_dict() for row in rows])


def build_historical_golf_dataset(
    *,
    feature_rows: Sequence[Mapping[str, object]],
    label_rows: Sequence[Mapping[str, object]],
    snapshot_rows: Sequence[Mapping[str, object]] = (),
    out: Path,
    market_family: str,
) -> HistoricalBuildReport:
    """Build a generic historical golf outcome CSV.

    Supports ``top_n``, ``make_cut``, and ``cut_line`` rows. All feature and
    snapshot rows must be timestamped at or before ``decision_time``.
    """

    family = _normalize_family(market_family)
    labels = _labels_by_key(label_rows, family)
    snapshots = _snapshots_by_key(snapshot_rows, family)
    rows: list[dict[str, object]] = []
    missing_market = 0
    for feature in feature_rows:
        decision_time = _parse_datetime(_required(feature, "decision_time"))
        feature_as_of = _parse_datetime(_required(feature, "feature_as_of"))
        if feature_as_of > decision_time:
            raise ValueError(f"feature row for {_historical_key(feature, family)} is after decision_time")
        key = _historical_key(feature, family)
        label = labels.get(key)
        if label is None:
            continue
        snapshot = _latest_before(snapshots.get(key, ()), decision_time, "captured_at")
        if snapshot is None:
            missing_market += 1
        rows.append(_historical_row(feature, label, snapshot=snapshot, family=family, decision_time=decision_time))
    if not rows:
        raise ValueError("no historical rows built; check feature/label keys")
    _write_dicts(out, GOLF_HISTORICAL_COLUMNS, rows)
    return HistoricalBuildReport(
        output_path=str(out),
        market_family=family,
        rows_written=len(rows),
        features_read=len(feature_rows),
        labels_read=len(label_rows),
        snapshots_read=len(snapshot_rows),
        rows_missing_market=missing_market,
        decision_gate=(
            "historical outcome CSV only; require chronological OOS, executable-touch economics, "
            "shadow-fill markout, and settlement reconciliation before paper promotion"
        ),
    )


def write_shadow_fill_ledger(
    *,
    intents: Sequence[Mapping[str, object]],
    quotes: Sequence[Mapping[str, object]],
    trades: Sequence[Mapping[str, object]] = (),
    settlements: Sequence[Mapping[str, object]] = (),
    out: Path,
    max_quote_age_ms: int = 60_000,
    min_net_edge: float = 0.05,
) -> ShadowFillReport:
    """Evaluate hypothetical intents against public quote/trade data.

    The output is a JSONL ledger. Rows are hypothetical and no-trade by design.
    """

    quote_map = _latest_quotes_by_ticker(quotes)
    trade_map = _trades_by_ticker(trades)
    settlement_map = {str(row.get("market_ticker") or ""): row for row in settlements}
    out.parent.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, object]] = []
    for intent in intents:
        ticker = _required(intent, "market_ticker")
        quote = quote_map.get(ticker)
        if quote is None:
            rows.append(_shadow_reject_row(intent, "missing_quote"))
            continue
        rows.append(
            evaluate_shadow_intent(
                intent=intent,
                quote=quote,
                trades=trade_map.get(ticker, ()),
                settlement=settlement_map.get(ticker),
                max_quote_age_ms=max_quote_age_ms,
                min_net_edge=min_net_edge,
            )
        )
    with out.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(_jsonable(row), sort_keys=True) + "\n")
    return ShadowFillReport(
        output_path=str(out),
        intents_read=len(intents),
        quotes_read=len(quotes),
        trades_read=len(trades),
        rows_written=len(rows),
        would_fill_now=sum(1 for row in rows if bool(row.get("would_fill_now"))),
        would_fill_later=sum(1 for row in rows if bool(row.get("would_fill_later"))),
        stale_rejections=sum(1 for row in rows if row.get("reject_reason") == "stale_quote"),
        candidate_rows=sum(1 for row in rows if bool(row.get("candidate"))),
        decision_gate=(
            "shadow fills are measurement only; no edge until fee/spread/liquidity, stale-source, "
            "CLV/markout, and settlement evidence survive OOS"
        ),
    )


def summarize_shadow_fill_ledger(*, ledger_path: Path, fixture_mode: bool = False) -> ShadowFillLedgerSummary:
    """Summarize a no-trade shadow-fill JSONL ledger into promotion-gate evidence."""

    rows = read_jsonl_rows(ledger_path)
    filled_rows = [row for row in rows if _truthy(row.get("would_fill_now")) or _truthy(row.get("would_fill_later"))]
    candidate_rows = [row for row in rows if _truthy(row.get("candidate"))]
    settlement_values = _numeric_values(rows, "settlement_value")
    avg_clv_close = _mean(_numeric_values(filled_rows, "clv_close"))
    avg_markout_5m = _mean(_numeric_values(filled_rows, "markout_5m"))
    summary = ShadowFillLedgerSummary(
        ledger_path=str(ledger_path),
        rows_read=len(rows),
        candidate_rows=len(candidate_rows),
        filled_rows=len(filled_rows),
        would_fill_now=sum(1 for row in rows if _truthy(row.get("would_fill_now"))),
        would_fill_later=sum(1 for row in rows if _truthy(row.get("would_fill_later"))),
        stale_rejections=sum(1 for row in rows if row.get("reject_reason") == "stale_quote"),
        missing_quote_rejections=sum(1 for row in rows if row.get("reject_reason") == "missing_quote"),
        not_filled_rejections=sum(1 for row in rows if row.get("reject_reason") == "not_filled_by_shadow_rules"),
        avg_fee=_mean(_numeric_values(filled_rows, "fee")),
        avg_spread=_mean(_numeric_values(rows, "spread")),
        avg_gross_edge=_mean(_numeric_values(rows, "gross_edge")),
        avg_net_edge=_mean(_numeric_values(rows, "net_edge")),
        avg_clv_close=avg_clv_close,
        avg_markout_1m=_mean(_numeric_values(filled_rows, "markout_1m")),
        avg_markout_5m=avg_markout_5m,
        avg_markout_30m=_mean(_numeric_values(filled_rows, "markout_30m")),
        settlement_rows=len(settlement_values),
        avg_settlement_value=_mean(settlement_values),
        settlement_wins=sum(1 for value in settlement_values if value >= 0.5),
        fixture_mode=fixture_mode,
        decision_gate=_shadow_summary_decision(
            fixture_mode=fixture_mode,
            rows_read=len(rows),
            candidate_rows=len(candidate_rows),
            filled_rows=len(filled_rows),
            settlement_rows=len(settlement_values),
            avg_clv_close=avg_clv_close,
            avg_markout_5m=avg_markout_5m,
        ),
    )
    return summary


def render_shadow_fill_summary_markdown(summary: ShadowFillLedgerSummary) -> str:
    """Render a compact operator-facing shadow-fill summary."""

    data = summary.as_dict()
    metrics = (
        ("rows_read", data["rows_read"]),
        ("candidate_rows", data["candidate_rows"]),
        ("candidate_rate", data["candidate_rate"]),
        ("filled_rows", data["filled_rows"]),
        ("fill_rate", data["fill_rate"]),
        ("stale_rejections", data["stale_rejections"]),
        ("missing_quote_rejections", data["missing_quote_rejections"]),
        ("not_filled_rejections", data["not_filled_rejections"]),
        ("avg_fee", data["avg_fee"]),
        ("avg_spread", data["avg_spread"]),
        ("avg_net_edge", data["avg_net_edge"]),
        ("avg_clv_close", data["avg_clv_close"]),
        ("avg_markout_1m", data["avg_markout_1m"]),
        ("avg_markout_5m", data["avg_markout_5m"]),
        ("avg_markout_30m", data["avg_markout_30m"]),
        ("settlement_rows", data["settlement_rows"]),
        ("avg_settlement_value", data["avg_settlement_value"]),
        ("settlement_wins", data["settlement_wins"]),
    )
    lines = [
        "# Golf Shadow-Fill Summary",
        "",
        f"- Ledger: `{summary.ledger_path}`",
        f"- Fixture mode: `{str(summary.fixture_mode).lower()}`",
        f"- Decision: **{summary.decision_gate}**",
        "",
        "## Metrics",
        "",
        "| Metric | Value |",
        "| --- | ---: |",
    ]
    lines.extend(f"| `{name}` | {_format_metric(value)} |" for name, value in metrics)
    lines.extend(
        [
            "",
            "## Boundary",
            "",
            "This report is no-trade evidence only. It does not submit, cancel, replace, or live-submit orders.",
            "",
        ]
    )
    return "\n".join(lines)


def evaluate_shadow_intent(
    *,
    intent: Mapping[str, object],
    quote: Mapping[str, object],
    trades: Sequence[Mapping[str, object]] = (),
    settlement: Mapping[str, object] | None = None,
    max_quote_age_ms: int = 60_000,
    min_net_edge: float = 0.05,
) -> dict[str, object]:
    """Evaluate one hypothetical buy intent against a quote/trade sequence."""

    decision_time = _parse_datetime(_required(intent, "decision_time"))
    quote_received_at = _parse_datetime(_required(quote, "received_at"))
    quote_age_ms = max(0, int((decision_time - quote_received_at).total_seconds() * 1000))
    yes_bid = _float_value(quote.get("yes_bid"))
    yes_ask = _float_value(quote.get("yes_ask"))
    if yes_ask < yes_bid:
        raise ValueError("yes_ask must be >= yes_bid")
    side = _side(intent.get("side"))
    fair_yes = _float_value(intent.get("fair_yes_probability"))
    limit_price = _float_value(intent.get("limit_price"))
    quantity = _float_value(intent.get("quantity"), 1.0)
    yes_bid_size = _float_value(quote.get("yes_bid_size"), 0.0)
    yes_ask_size = _float_value(quote.get("yes_ask_size"), 0.0)

    executable = yes_ask if side == "YES" else 1.0 - yes_bid
    fair_side = fair_yes if side == "YES" else 1.0 - fair_yes
    fee = kalshi_fee(executable)
    gross_edge = fair_side - executable
    net_edge = gross_edge - fee
    stale = quote_age_ms > max_quote_age_ms
    would_fill_now = (limit_price >= executable) and not stale
    later = _later_fill(
        side=side,
        limit_price=limit_price,
        trades=trades,
        decision_time=decision_time,
        stale=stale,
    )
    fill_price = executable if would_fill_now else later[0]
    fill_time = decision_time if would_fill_now else later[1]
    fill_reason = "taker_touch" if would_fill_now else later[2]
    queue_ahead = (
        0.0
        if would_fill_now
        else _queue_ahead(side, limit_price, yes_bid, yes_ask, yes_bid_size, yes_ask_size)
    )
    filled = fill_price is not None
    markouts = _markouts(side=side, fill_price=fill_price, intent=intent)
    settlement_value = _settlement_value(side=side, settlement=settlement)
    reject_reason = _reject_reason(
        stale=stale,
        filled=filled,
        net_edge=net_edge,
        min_net_edge=min_net_edge,
        yes_bid=yes_bid,
        yes_ask=yes_ask,
    )
    return {
        "intent_id": str(intent.get("intent_id") or f"shadow-{_event_ts(decision_time)}"),
        "decision_time": decision_time.isoformat(),
        "market_ticker": _required(intent, "market_ticker"),
        "market_family": str(intent.get("market_family") or ""),
        "side": side,
        "quantity": quantity,
        "fair_yes_probability": fair_yes,
        "limit_price": limit_price,
        "yes_bid": yes_bid,
        "yes_ask": yes_ask,
        "yes_bid_size": yes_bid_size,
        "yes_ask_size": yes_ask_size,
        "spread": yes_ask - yes_bid,
        "quote_received_at": quote_received_at.isoformat(),
        "quote_age_ms": quote_age_ms,
        "stale_quote": stale,
        "executable_touch": executable,
        "fee": fee,
        "gross_edge": gross_edge,
        "net_edge": net_edge,
        "queue_model": "touch_or_later_public_trade_queue_proxy",
        "queue_ahead": queue_ahead,
        "would_fill_now": would_fill_now,
        "would_fill_later": later[0] is not None and not would_fill_now,
        "fill_price": "" if fill_price is None else fill_price,
        "fill_time": "" if fill_time is None else fill_time.isoformat(),
        "fill_reason": fill_reason,
        "clv_close": markouts["clv_close"],
        "markout_1m": markouts["markout_1m"],
        "markout_5m": markouts["markout_5m"],
        "markout_30m": markouts["markout_30m"],
        "settlement_value": "" if settlement_value is None else settlement_value,
        "settlement_source": "" if settlement is None else str(settlement.get("settlement_source") or "unknown"),
        "candidate": reject_reason == "",
        "reject_reason": reject_reason,
    }


def fixture_market_payloads() -> list[dict[str, object]]:
    """Small public-market-shape fixture for mapping tests and no-network runs."""

    return [
        {
            "ticker": "KXPGATOP20-USO26-SSCH",
            "series_ticker": "KXPGATOP20",
            "event_ticker": "USO26",
            "status": "active",
            "title": "U.S. Open: Will Scottie Scheffler finish top 20?",
            "close_time": "2026-06-18T12:00:00+00:00",
            "expected_expiration_time": "2026-06-22T02:00:00+00:00",
        },
        {
            "ticker": "KXPGAMAKECUT-USO26-RMCI",
            "series_ticker": "KXPGAMAKECUT",
            "event_ticker": "USO26",
            "status": "active",
            "title": "U.S. Open: Will Rory McIlroy make the cut?",
            "close_time": "2026-06-19T23:00:00+00:00",
            "expected_expiration_time": "2026-06-20T03:00:00+00:00",
        },
        {
            "ticker": "KXPGACUTLINE-USO26-2OVER",
            "series_ticker": "KXPGACUTLINE",
            "event_ticker": "USO26",
            "status": "initialized",
            "title": "Will the cut line of the tournament be +2?",
            "close_time": "2026-06-19T23:00:00+00:00",
            "expected_expiration_time": "2026-06-20T03:00:00+00:00",
        },
    ]


def fixture_historical_inputs(
    market_family: str,
) -> tuple[list[dict[str, object]], list[dict[str, object]], list[dict[str, object]]]:
    """Deterministic historical raw inputs for no-network importer tests."""

    family = _normalize_family(market_family)
    decision_time = datetime(2026, 6, 18, 12, 0, tzinfo=UTC)
    common_feature = {
        "event_date": "2026-06-18T00:00:00+00:00",
        "tournament_id": "USO26",
        "feature_as_of": (decision_time - timedelta(hours=2)).isoformat(),
        "decision_time": decision_time.isoformat(),
        "baseline_skill_z": 1.2,
        "sg_approach_z": 0.8,
        "sg_putting_z": -0.3,
        "strokes_to_cut": -1.0,
        "wave_weather_delta": 0.1,
    }
    if family == "cut_line":
        features = [
            {
                **common_feature,
                "market_ticker": "KXPGACUTLINE-USO26-2OVER",
                "subject_id": "2over",
                "subject_name": "+2",
                "field_scoring_avg_delta_vs_par": 1.1,
                "top_65_current_score": 2,
                "cut_line": 2,
                "cut_line_relation": "exact",
            }
        ]
        labels = [{"tournament_id": "USO26", "subject_id": "2over", "winning_cut_line": 2}]
        snapshots = [
            {
                "captured_at": (decision_time - timedelta(minutes=15)).isoformat(),
                "tournament_id": "USO26",
                "subject_id": "2over",
                "market_ticker": "KXPGACUTLINE-USO26-2OVER",
                "yes_bid": 0.24,
                "yes_ask": 0.27,
                "yes_bid_size": 50,
                "yes_ask_size": 60,
                "volume": 1000,
                "volume_24h": 200,
                "open_interest": 800,
            }
        ]
        return features, labels, snapshots
    player_id = "rory" if family == "make_cut" else "scottie"
    market_ticker = "KXPGAMAKECUT-USO26-RMCI" if family == "make_cut" else "KXPGATOP20-USO26-SSCH"
    features = [
        {
            **common_feature,
            "market_ticker": market_ticker,
            "subject_id": player_id,
            "subject_name": "Rory McIlroy" if family == "make_cut" else "Scottie Scheffler",
            "player_id": player_id,
            "player_name": "Rory McIlroy" if family == "make_cut" else "Scottie Scheffler",
            "top_n": 20,
        }
    ]
    labels = [
        {
            "tournament_id": "USO26",
            "subject_id": player_id,
            "player_id": player_id,
            "made_cut": 1 if family == "make_cut" else "",
            "made_top_n": 1 if family == "top_n" else "",
            "final_position": 8 if family == "top_n" else "",
        }
    ]
    snapshots = [
        {
            "captured_at": (decision_time - timedelta(minutes=10)).isoformat(),
            "tournament_id": "USO26",
            "subject_id": player_id,
            "player_id": player_id,
            "market_ticker": market_ticker,
            "yes_bid": 0.41,
            "yes_ask": 0.44,
            "yes_bid_size": 120,
            "yes_ask_size": 90,
            "volume": 3000,
            "volume_24h": 700,
            "open_interest": 2300,
        }
    ]
    return features, labels, snapshots


def fixture_shadow_inputs() -> tuple[
    list[dict[str, object]],
    list[dict[str, object]],
    list[dict[str, object]],
    list[dict[str, object]],
]:
    """Deterministic no-network shadow-fill inputs."""

    now = datetime(2026, 6, 18, 12, 0, tzinfo=UTC)
    intents = [
        {
            "intent_id": "fixture-intent-1",
            "decision_time": now.isoformat(),
            "market_ticker": "KXPGATOP20-USO26-SSCH",
            "market_family": "top_n",
            "side": "YES",
            "quantity": 1,
            "fair_yes_probability": 0.55,
            "limit_price": 0.44,
            "markout_1m_mid": 0.46,
            "markout_5m_mid": 0.47,
            "markout_30m_mid": 0.48,
            "close_mid": 0.50,
        }
    ]
    quotes = [
        {
            "received_at": (now - timedelta(seconds=2)).isoformat(),
            "market_ticker": "KXPGATOP20-USO26-SSCH",
            "yes_bid": 0.41,
            "yes_ask": 0.44,
            "yes_bid_size": 120,
            "yes_ask_size": 90,
        }
    ]
    trades = [
        {
            "trade_time": (now + timedelta(minutes=3)).isoformat(),
            "market_ticker": "KXPGATOP20-USO26-SSCH",
            "side": "YES",
            "price": 0.44,
            "quantity": 10,
        }
    ]
    settlements = [
        {
            "market_ticker": "KXPGATOP20-USO26-SSCH",
            "settlement_value": 1,
            "settlement_source": "fixture-final-leaderboard",
        }
    ]
    return intents, quotes, trades, settlements


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    """Read CSV rows as strings."""

    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"empty CSV: {path}")
        return [dict(row) for row in reader]


def read_jsonl_rows(path: Path) -> list[dict[str, object]]:
    """Read JSONL rows as string-keyed mappings."""

    rows: list[dict[str, object]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()
            if not line:
                continue
            payload = json.loads(line)
            if not isinstance(payload, dict):
                raise ValueError(f"{path}:{line_number} is not a JSON object")
            rows.append({str(key): value for key, value in payload.items()})
    return rows


def write_json_report(path: Path, payload: Mapping[str, object]) -> None:
    """Write a compact JSON report."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_jsonable(dict(payload)), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _historical_row(
    feature: Mapping[str, object],
    label: Mapping[str, object],
    *,
    snapshot: Mapping[str, object] | None,
    family: str,
    decision_time: datetime,
) -> dict[str, object]:
    market_bid = _optional_float(snapshot.get("yes_bid")) if snapshot is not None else None
    market_ask = _optional_float(snapshot.get("yes_ask")) if snapshot is not None else None
    spread = "" if market_bid is None or market_ask is None else market_ask - market_bid
    market_mid = "" if market_bid is None or market_ask is None else (market_bid + market_ask) / 2.0
    liquidity = _first_float(snapshot or {}, ("liquidity", "open_interest", "volume_24h", "volume"))
    feature_as_of = _parse_datetime(_required(feature, "feature_as_of"))
    subject_id = _subject_key(feature, family)
    return {
        "event_date": _required(feature, "event_date"),
        "tournament_id": _required(feature, "tournament_id"),
        "market_family": family,
        "market_ticker": str((snapshot or feature).get("market_ticker") or ""),
        "subject_id": subject_id,
        "subject_name": str(feature.get("subject_name") or feature.get("player_name") or subject_id),
        "feature_as_of": feature_as_of.isoformat(),
        "decision_time": decision_time.isoformat(),
        "target": _target_from_label(label, family=family, feature=feature),
        "settlement_status": str(label.get("settlement_status") or "settled"),
        "market_bid": "" if market_bid is None else market_bid,
        "market_ask": "" if market_ask is None else market_ask,
        "yes_bid_size": "" if snapshot is None else snapshot.get("yes_bid_size", ""),
        "yes_ask_size": "" if snapshot is None else snapshot.get("yes_ask_size", ""),
        "volume": "" if snapshot is None else snapshot.get("volume", ""),
        "volume_24h": "" if snapshot is None else snapshot.get("volume_24h", ""),
        "open_interest": "" if snapshot is None else snapshot.get("open_interest", ""),
        "close_time": "" if snapshot is None else snapshot.get("close_time", ""),
        "expected_expiration_time": "" if snapshot is None else snapshot.get("expected_expiration_time", ""),
        "odds_probability": feature.get("odds_probability", ""),
        "reference_price_source": feature.get("reference_price_source", ""),
        "baseline_skill_z": _float_value(feature.get("baseline_skill_z"), 0.0),
        "sg_approach_z": _float_value(feature.get("sg_approach_z"), 0.0),
        "sg_putting_z": _float_value(feature.get("sg_putting_z"), 0.0),
        "strokes_to_cut": _float_value(feature.get("strokes_to_cut"), 0.0),
        "wave_weather_delta": _float_value(feature.get("wave_weather_delta"), 0.0),
        "field_scoring_avg_delta_vs_par": _float_value(feature.get("field_scoring_avg_delta_vs_par"), 0.0),
        "top_65_current_score": _float_value(feature.get("top_65_current_score"), 0.0),
        "cut_line": feature.get("cut_line", ""),
        "cut_line_relation": feature.get("cut_line_relation", ""),
        "market_mid": market_mid,
        "spread": spread,
        "liquidity": "" if liquidity is None else liquidity,
        "time_to_decision_hours": max(0.0, (decision_time - feature_as_of).total_seconds() / 3600.0),
    }


def _target_from_label(label: Mapping[str, object], *, family: str, feature: Mapping[str, object]) -> int:
    direct = _optional_float(label.get("target"))
    if direct is not None:
        return 1 if direct >= 0.5 else 0
    if family == "make_cut":
        made = _optional_float(label.get("made_cut"))
        if made is None:
            raise ValueError("make_cut label needs target or made_cut")
        return 1 if made >= 0.5 else 0
    if family == "top_n":
        made_top = _optional_float(label.get("made_top_n"))
        if made_top is not None:
            return 1 if made_top >= 0.5 else 0
        final_position = _float_value(label.get("final_position"))
        top_n = int(_float_value(feature.get("top_n"), 20.0))
        return 1 if final_position <= top_n else 0
    winning = _float_value(label.get("winning_cut_line"))
    cut_line = int(_float_value(feature.get("cut_line")))
    relation = str(feature.get("cut_line_relation") or "exact")
    if relation == "or_better":
        return 1 if winning <= cut_line else 0
    if relation == "or_worse":
        return 1 if winning >= cut_line else 0
    return 1 if int(winning) == cut_line else 0


def _labels_by_key(rows: Sequence[Mapping[str, object]], family: str) -> dict[tuple[str, str], Mapping[str, object]]:
    return {_historical_key(row, family): row for row in rows}


def _snapshots_by_key(
    rows: Sequence[Mapping[str, object]],
    family: str,
) -> dict[tuple[str, str], tuple[Mapping[str, object], ...]]:
    grouped: defaultdict[tuple[str, str], list[Mapping[str, object]]] = defaultdict(list)
    for row in rows:
        grouped[_historical_key(row, family)].append(row)
    return {
        key: tuple(sorted(items, key=lambda item: _parse_datetime(_required(item, "captured_at"))))
        for key, items in grouped.items()
    }


def _latest_before(
    rows: Sequence[Mapping[str, object]],
    cutoff: datetime,
    timestamp_field: str,
) -> Mapping[str, object] | None:
    latest: Mapping[str, object] | None = None
    latest_ts: datetime | None = None
    for row in rows:
        ts = _parse_datetime(_required(row, timestamp_field))
        if ts > cutoff:
            raise ValueError(f"{timestamp_field} for {_required(row, 'market_ticker')} is after decision_time")
        if latest_ts is None or ts > latest_ts:
            latest = row
            latest_ts = ts
    return latest


def _historical_key(row: Mapping[str, object], family: str) -> tuple[str, str]:
    return (_required(row, "tournament_id"), _subject_key(row, family))


def _subject_key(row: Mapping[str, object], family: str) -> str:
    if family == "cut_line":
        explicit = str(row.get("subject_id") or "").strip()
        if explicit:
            return explicit
        cut_line = str(row.get("cut_line") or "").strip()
        relation = str(row.get("cut_line_relation") or "exact").strip()
        return f"{cut_line}:{relation}"
    return str(row.get("subject_id") or row.get("player_id") or "").strip()


def _latest_quotes_by_ticker(rows: Sequence[Mapping[str, object]]) -> dict[str, Mapping[str, object]]:
    latest: dict[str, Mapping[str, object]] = {}
    latest_ts: dict[str, datetime] = {}
    for row in rows:
        ticker = _required(row, "market_ticker")
        ts = _parse_datetime(_required(row, "received_at"))
        if ticker not in latest_ts or ts > latest_ts[ticker]:
            latest[ticker] = row
            latest_ts[ticker] = ts
    return latest


def _trades_by_ticker(rows: Sequence[Mapping[str, object]]) -> dict[str, tuple[Mapping[str, object], ...]]:
    grouped: defaultdict[str, list[Mapping[str, object]]] = defaultdict(list)
    for row in rows:
        grouped[_required(row, "market_ticker")].append(row)
    return {
        ticker: tuple(sorted(items, key=lambda item: _parse_datetime(_required(item, "trade_time"))))
        for ticker, items in grouped.items()
    }


def _later_fill(
    *,
    side: str,
    limit_price: float,
    trades: Sequence[Mapping[str, object]],
    decision_time: datetime,
    stale: bool,
) -> tuple[float | None, datetime | None, str]:
    if stale:
        return None, None, ""
    for trade in trades:
        trade_time = _parse_datetime(_required(trade, "trade_time"))
        if trade_time <= decision_time:
            continue
        trade_side = _side(trade.get("side"))
        trade_price = _float_value(trade.get("price"))
        side_price = trade_price if trade_side == side else 1.0 - trade_price
        if side_price <= limit_price + 1e-9:
            return limit_price, trade_time, "later_public_trade_queue_proxy"
    return None, None, ""


def _queue_ahead(
    side: str,
    limit_price: float,
    yes_bid: float,
    yes_ask: float,
    yes_bid_size: float,
    yes_ask_size: float,
) -> float:
    if side == "YES":
        if limit_price <= yes_bid + 1e-9:
            return yes_bid_size
        return 0.0
    no_bid = 1.0 - yes_ask
    if limit_price <= no_bid + 1e-9:
        return yes_ask_size
    return 0.0


def _markouts(side: str, fill_price: float | None, intent: Mapping[str, object]) -> dict[str, object]:
    out: dict[str, object] = {}
    for column, target in (
        ("close_mid", "clv_close"),
        ("markout_1m_mid", "markout_1m"),
        ("markout_5m_mid", "markout_5m"),
        ("markout_30m_mid", "markout_30m"),
    ):
        mid = _optional_float(intent.get(column))
        if fill_price is None or mid is None:
            out[target] = ""
            continue
        side_mid = mid if side == "YES" else 1.0 - mid
        out[target] = side_mid - fill_price
    return out


def _settlement_value(side: str, settlement: Mapping[str, object] | None) -> float | None:
    if settlement is None:
        return None
    yes_value = _optional_float(settlement.get("settlement_value") or settlement.get("target"))
    if yes_value is None:
        return None
    return yes_value if side == "YES" else 1.0 - yes_value


def _reject_reason(
    *,
    stale: bool,
    filled: bool,
    net_edge: float,
    min_net_edge: float,
    yes_bid: float,
    yes_ask: float,
) -> str:
    if stale:
        return "stale_quote"
    if yes_ask < yes_bid:
        return "crossed_or_bad_quote"
    if not filled:
        return "not_filled_by_shadow_rules"
    if net_edge < min_net_edge:
        return "fails_min_net_edge"
    return ""


def _shadow_summary_decision(
    *,
    fixture_mode: bool,
    rows_read: int,
    candidate_rows: int,
    filled_rows: int,
    settlement_rows: int,
    avg_clv_close: float | None,
    avg_markout_5m: float | None,
) -> str:
    if fixture_mode:
        return "continue research: fixture/no-network summary only; replace with chronological shadow fills"
    if rows_read == 0:
        return "continue research: no shadow-fill rows yet"
    if candidate_rows == 0:
        return "continue research: no fee-net filled candidates in this ledger"
    if filled_rows == 0:
        return "continue research: candidates were not fillable under shadow rules"
    if avg_clv_close is None and avg_markout_5m is None:
        return "continue shadow logging: candidates exist but markout evidence is missing"
    markout_signal = avg_markout_5m if avg_markout_5m is not None else avg_clv_close
    if markout_signal is not None and markout_signal <= 0.0:
        return "kill or defer: candidate markout is non-positive after shadow fills"
    if settlement_rows == 0:
        return "continue shadow logging: positive markout candidate, settlement evidence missing"
    return "paper-only candidate: positive shadow evidence, still require larger OOS settlement sample"


def _shadow_reject_row(intent: Mapping[str, object], reason: str) -> dict[str, object]:
    decision_time = _parse_datetime(str(intent.get("decision_time") or datetime.now(UTC).isoformat()))
    return {
        "intent_id": str(intent.get("intent_id") or f"shadow-{_event_ts(decision_time)}"),
        "decision_time": decision_time.isoformat(),
        "market_ticker": str(intent.get("market_ticker") or ""),
        "market_family": str(intent.get("market_family") or ""),
        "side": str(intent.get("side") or ""),
        "quantity": intent.get("quantity", ""),
        "fair_yes_probability": intent.get("fair_yes_probability", ""),
        "limit_price": intent.get("limit_price", ""),
        "candidate": False,
        "reject_reason": reason,
    }


def _market_family(series: str, title: str) -> str | None:
    upper = series.upper()
    lower = title.lower()
    if upper.startswith("KXPGATOP") or upper.startswith("KXLIVTOP"):
        return "top_n"
    if upper in {"KXPGAMAKECUT", "KXMASTERSCUT"} or "make the cut" in lower:
        return "make_cut"
    if upper == "KXPGACUTLINE" or "cut line" in lower:
        return "cut_line"
    return None


def _numeric_values(rows: Sequence[Mapping[str, object]], field: str) -> list[float]:
    values: list[float] = []
    for row in rows:
        parsed = _optional_float(row.get(field))
        if parsed is not None:
            values.append(parsed)
    return values


def _mean(values: Sequence[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def _rate(numerator: int, denominator: int) -> float | None:
    if denominator == 0:
        return None
    return numerator / denominator


def _truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes"}


def _format_metric(value: object) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value)


def _top_n_from_series(series: str) -> int | None:
    match = _TOP_N_RE.search(series.upper())
    return int(match.group(1)) if match else None


def _cut_line_from_market(ticker: str, title: str) -> tuple[int | None, str | None]:
    suffix = ticker.rsplit("-", 1)[-1].upper()
    relation = "exact"
    lower = title.lower()
    if "or better" in lower:
        relation = "or_better"
    elif "or worse" in lower:
        relation = "or_worse"
    if suffix == "EVEN":
        return 0, relation
    if suffix.endswith("OVER") and suffix[:-4].isdigit():
        return int(suffix[:-4]), relation
    if suffix.endswith("UNDER") and suffix[:-5].isdigit():
        return -int(suffix[:-5]), relation
    return None, relation


def _cutline_subject(cut_line: int | None, relation: str | None) -> str:
    if cut_line is None:
        return "cut line"
    prefix = "+" if cut_line > 0 else ""
    suffix = {
        "or_better": " or better",
        "or_worse": " or worse",
    }.get(relation or "", "")
    return f"{prefix}{cut_line}{suffix}"


def _subject_name_from_title(title: str) -> str:
    cleaned = title.strip().rstrip("?")
    for pattern in _PLAYER_PATTERNS:
        match = pattern.search(cleaned)
        if match:
            return match.group(1).split(":", 1)[-1].strip()
    if ":" in cleaned:
        return cleaned.split(":", 1)[-1].strip()
    return cleaned


def _event_from_ticker(ticker: str) -> str:
    parts = ticker.split("-")
    return parts[1] if len(parts) > 2 else ""


def _subject_from_ticker(ticker: str) -> str:
    return ticker.rsplit("-", 1)[-1].lower() if "-" in ticker else ticker.lower()


def _subject_id(name: str) -> str:
    compact = "".join(ch for ch in name.lower() if ch.isalnum())
    return compact or "unknown"


def _normalize_family(value: str) -> str:
    normalized = value.strip().lower().replace("-", "_")
    aliases = {
        "top20": "top_n",
        "top_20": "top_n",
        "topn": "top_n",
        "makecut": "make_cut",
        "make_miss_cut": "make_cut",
        "cutline": "cut_line",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in {"top_n", "make_cut", "cut_line"}:
        raise ValueError("market_family must be top_n, make_cut, or cut_line")
    return normalized


def _side(value: object) -> str:
    side = str(value or "YES").upper()
    if side not in {"YES", "NO"}:
        raise ValueError("side must be YES or NO")
    return side


def _first_float(row: Mapping[str, object], fields: Sequence[str]) -> float | None:
    for field in fields:
        parsed = _optional_float(row.get(field))
        if parsed is not None:
            return parsed
    return None


def _write_dicts(path: Path, columns: Sequence[str], rows: Sequence[Mapping[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns), lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in columns})


def _required(row: Mapping[str, object], field: str) -> str:
    value = row.get(field)
    if value is None or str(value).strip() == "":
        raise ValueError(f"missing required field {field!r}")
    return str(value).strip()


def _optional_str(value: object) -> str | None:
    if value is None or str(value).strip() == "":
        return None
    return str(value)


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
