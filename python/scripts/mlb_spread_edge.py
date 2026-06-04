"""Run the read-only MLB spread sharp-reference validator.

This script reuses the spread-ladder evaluator for Kalshi MLB run-margin
markets. It fetches public ESPN scoreboard/odds data and public Kalshi market
data only. It never submits, cancels, replaces, or live-submits orders.
Candidate rows are paper/shadow signals until CLV, fill, and settlement evidence
prove them.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "python" / "src"))

from eventcontracts.research.ledger import append_jsonl, to_jsonable, write_jsonl  # noqa: E402
from eventcontracts.research.nba_spread import (  # noqa: E402
    NbaSpreadMarkoutReport,
    NbaSpreadMarkoutRow,
    NbaSpreadSettlementRow,
    NbaSpreadValidationConfig,
    NbaSpreadValidationReport,
    evaluate_spread_ladder,
    fixture_anchor,
    fixture_game_state,
    fixture_markets,
    markout_report_from_entry_report,
    parse_espn_live_odds_anchor,
    parse_kalshi_spread_market,
    parse_nba_game_state,
    settlement_report_from_entry_report,
)

KALSHI_MARKETS_URL = "https://api.elections.kalshi.com/trade-api/v2/markets"
ESPN_SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/scoreboard"
ESPN_CORE_EVENT_URL = "https://sports.core.api.espn.com/v2/sports/baseball/leagues/mlb/events"


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    return int(args.handler(args))


def _handle_validate_once(args: argparse.Namespace) -> int:
    config = _config_from_args(args)
    as_of = datetime.now(UTC)
    reports: tuple[NbaSpreadValidationReport, ...]
    skipped: list[dict[str, str]]
    if args.no_network:
        reports = (
            evaluate_spread_ladder(
                game=fixture_game_state(),
                anchor=fixture_anchor(),
                markets=fixture_markets(),
                config=config,
                as_of=as_of,
            ),
        )
        skipped = []
    else:
        reports, skipped = _build_live_reports(args, as_of=as_of, config=config)

    payload = _combined_validation_payload(
        reports,
        skipped_events=skipped,
        as_of=as_of,
        series_ticker=args.series_ticker,
        config=config,
    )
    _write_json(args.report_json, payload)
    args.report_md.parent.mkdir(parents=True, exist_ok=True)
    args.report_md.write_text(_render_validation_markdown(payload, reports), encoding="utf-8")
    write_jsonl(args.signals_jsonl_out, _candidate_signals(reports))
    print(json.dumps(payload["summary"], indent=2, sort_keys=True))
    return 0


def _handle_render_latest(args: argparse.Namespace) -> int:
    payload = _read_json(args.report_json)
    print(json.dumps(payload["summary"], indent=2, sort_keys=True))
    print(args.report_md.read_text(encoding="utf-8") if args.report_md.exists() else "")
    return 0


def _handle_markout(args: argparse.Namespace) -> int:
    as_of = datetime.now(UTC)
    entry_payload = _read_json(args.entry_report_json)
    current_quotes = (
        {quote.ticker: quote for quote in fixture_markets()}
        if args.no_network
        else _build_current_quotes(args, as_of=as_of, target_tickers=_entry_candidate_tickers(entry_payload))
    )
    payload = _build_markout_payload(
        entry_payload=entry_payload,
        current_quotes=current_quotes,
        markout_as_of=as_of,
        entry_report=str(args.entry_report_json),
        target_horizon_seconds=args.horizon_seconds,
        label=args.markout_label,
        source_markout_report=None,
    )
    _write_json(args.report_json, payload)
    args.report_md.parent.mkdir(parents=True, exist_ok=True)
    args.report_md.write_text(_render_markout_payload_markdown(payload), encoding="utf-8")
    _append_markout_ledger(args.markout_ledger_jsonl_out, payload, report_path=args.report_json)
    print(json.dumps(_summary_mapping(payload), indent=2, sort_keys=True))
    return 0


def _handle_markout_horizons(args: argparse.Namespace) -> int:
    entry_payload = _read_json(args.entry_report_json)
    horizons = _parse_horizons(args.horizons_seconds)
    reports: list[dict[str, object]] = []
    args.report_dir.mkdir(parents=True, exist_ok=True)
    for horizon_seconds in horizons:
        label = _horizon_label(horizon_seconds)
        wait_result = _wait_for_horizon(
            entry_payload,
            horizon_seconds=horizon_seconds,
            max_wait_seconds=args.max_wait_seconds,
        )
        if wait_result["status"] == "skipped_future_horizon":
            reports.append(
                {
                    "horizon_seconds": horizon_seconds,
                    "label": label,
                    **wait_result,
                }
            )
            continue
        as_of = datetime.now(UTC)
        current_quotes = (
            {quote.ticker: quote for quote in fixture_markets()}
            if args.no_network
            else _build_current_quotes(args, as_of=as_of, target_tickers=_entry_candidate_tickers(entry_payload))
        )
        payload = _build_markout_payload(
            entry_payload=entry_payload,
            current_quotes=current_quotes,
            markout_as_of=as_of,
            entry_report=str(args.entry_report_json),
            target_horizon_seconds=horizon_seconds,
            label=label,
            source_markout_report=None,
        )
        report_json = args.report_dir / f"{args.report_prefix}-{_horizon_slug(horizon_seconds)}.json"
        report_md = args.report_dir / f"{args.report_prefix}-{_horizon_slug(horizon_seconds)}.md"
        _write_json(report_json, payload)
        report_md.write_text(_render_markout_payload_markdown(payload), encoding="utf-8")
        _append_markout_ledger(args.markout_ledger_jsonl_out, payload, report_path=report_json)
        reports.append(
            {
                **wait_result,
                "horizon_seconds": horizon_seconds,
                "label": label,
                "status": "collected",
                "report_json": str(report_json),
                "report_md": str(report_md),
                "summary": _summary_mapping(payload),
            }
        )
    bundle = _markout_horizons_bundle(
        entry_report=str(args.entry_report_json),
        horizons=horizons,
        reports=reports,
        ledger_path=args.markout_ledger_jsonl_out,
    )
    _write_json(args.report_json, bundle)
    args.report_md.parent.mkdir(parents=True, exist_ok=True)
    args.report_md.write_text(_render_markout_horizons_markdown(bundle), encoding="utf-8")
    print(json.dumps(bundle["summary"], indent=2, sort_keys=True))
    return 0


def _handle_annotate_markout(args: argparse.Namespace) -> int:
    markout_payload = _read_json(args.markout_report_json)
    entry_payload = _read_json(args.entry_report_json)
    markout_as_of = _parse_datetime_or_none(markout_payload.get("as_of")) or datetime.now(UTC)
    payload = _augment_markout_payload(
        markout_payload,
        entry_payload=entry_payload,
        markout_as_of=markout_as_of,
        entry_report=str(args.entry_report_json),
        target_horizon_seconds=args.horizon_seconds,
        label=args.markout_label,
        source_markout_report=str(args.markout_report_json),
    )
    _write_json(args.report_json, payload)
    args.report_md.parent.mkdir(parents=True, exist_ok=True)
    args.report_md.write_text(_render_markout_payload_markdown(payload), encoding="utf-8")
    _append_markout_ledger(args.markout_ledger_jsonl_out, payload, report_path=args.report_json)
    print(json.dumps(_summary_mapping(payload), indent=2, sort_keys=True))
    return 0


def _handle_settle(args: argparse.Namespace) -> int:
    as_of = datetime.now(UTC)
    entry_payload = _read_json(args.entry_report_json)
    games = (
        {fixture_game_state().event_id: fixture_game_state()}
        if args.no_network
        else _game_states_by_event_id(date=args.espn_date)
    )
    rows: list[NbaSpreadSettlementRow] = []
    missing_games: list[str] = []
    for report_payload in _entry_reports(entry_payload):
        game_payload = report_payload.get("game")
        if not isinstance(game_payload, Mapping):
            continue
        event_id = str(game_payload.get("event_id") or "")
        game = games.get(event_id)
        if game is None:
            missing_games.append(event_id)
            continue
        report = settlement_report_from_entry_report(
            report_payload,
            game=game,
            as_of=as_of,
            entry_report_name=str(args.entry_report_json),
        )
        rows.extend(report.rows)

    payload = _settlement_payload(
        rows,
        missing_games=missing_games,
        as_of=as_of,
        entry_report=str(args.entry_report_json),
        paper_contracts=_paper_contracts(entry_payload),
    )
    _write_json(args.report_json, payload)
    args.report_md.parent.mkdir(parents=True, exist_ok=True)
    args.report_md.write_text(_render_settlement_markdown(payload), encoding="utf-8")
    print(json.dumps(payload["summary"], indent=2, sort_keys=True))
    return 0


def _handle_timestamp_audit(args: argparse.Namespace) -> int:
    as_of = datetime.now(UTC)
    if args.no_network:
        event_id = "fixture"
        payload: Mapping[str, object] = {
            "items": [
                {
                    "provider": {"name": "DraftKings - Live Odds"},
                    "homeTeamOdds": {"moneyLine": -120, "spreadOdds": -110},
                    "awayTeamOdds": {"moneyLine": 100, "spreadOdds": -110},
                    "spread": -1.5,
                    "overUnder": 8.5,
                }
            ]
        }
        headers: Mapping[str, str] = {"Date": as_of.strftime("%a, %d %b %Y %H:%M:%S GMT")}
    else:
        scoreboard = _fetch_scoreboard(date=args.espn_date)
        events = _select_events(scoreboard, event_id=args.espn_event_id)
        event_id = ""
        for event in events:
            game = parse_nba_game_state(event, received_at=as_of)
            if game.completed or game.status_state == "post":
                continue
            event_id = game.event_id
            break
        if not event_id:
            raise ValueError("no active ESPN event available for timestamp audit")
        payload, headers = _fetch_espn_odds_with_headers(event_id)
    audit = _timestamp_audit_payload(
        odds_payload=payload,
        headers=headers,
        as_of=as_of,
        event_id=event_id,
    )
    _write_json(args.report_json, audit)
    args.report_md.parent.mkdir(parents=True, exist_ok=True)
    args.report_md.write_text(_render_timestamp_audit_markdown(audit), encoding="utf-8")
    print(json.dumps(audit["summary"], indent=2, sort_keys=True))
    return 0


def _handle_bench(args: argparse.Namespace) -> int:
    config = _config_from_args(args)
    as_of = datetime.now(UTC)
    if args.no_network:
        compute_ms = []
        game = fixture_game_state()
        anchor = fixture_anchor()
        markets = fixture_markets()
        for _idx in range(args.compute_iterations):
            elapsed, _report = _timed_call(
                evaluate_spread_ladder,
                game=game,
                anchor=anchor,
                markets=markets,
                config=config,
                as_of=as_of,
            )
            compute_ms.append(elapsed)
        payload = _bench_payload(
            as_of=as_of,
            mode="no_network",
            compute_ms=compute_ms,
            network_ms={},
            counts={"reports": 1, "markets": len(markets), "events": 1, "orderbooks": 0},
        )
    else:
        network_ms: dict[str, float] = {}
        total_start = time.perf_counter()
        network_ms["espn_scoreboard_ms"], scoreboard = _timed_call(_fetch_scoreboard, date=args.espn_date)
        events = _select_events(scoreboard, event_id=args.espn_event_id)
        odds_payloads: dict[str, Mapping[str, object]] = {}
        odds_total = 0.0
        active_events: list[Mapping[str, object]] = []
        for event in events:
            game = parse_nba_game_state(event, received_at=as_of)
            if game.completed or game.status_state == "post":
                continue
            elapsed, odds_payload = _timed_call(_fetch_espn_odds, game.event_id)
            odds_total += elapsed
            odds_payloads[game.event_id] = odds_payload
            active_events.append(event)
        network_ms["espn_odds_total_ms"] = odds_total
        network_ms["kalshi_markets_ms"], markets_payload = _timed_call(
            _fetch_kalshi_markets,
            args.series_ticker,
        )
        rows = _market_rows(markets_payload)
        if args.skip_orderbooks:
            orderbooks: dict[str, Mapping[str, object]] = {}
            orderbooks_requested_count = 0
            network_ms["kalshi_orderbooks_total_ms"] = 0.0
            preliminary_reports: list[NbaSpreadValidationReport] = []
            preliminary_compute_ms: list[float] = []
        elif args.selective_orderbooks:
            preliminary_compute_ms, preliminary_reports = _timed_evaluate_events(
                active_events,
                odds_payloads=odds_payloads,
                rows=rows,
                orderbooks={},
                as_of=as_of,
                config=config,
                prefer_provider_contains=args.prefer_provider_contains,
            )
            depth_tickers = _candidate_tickers(preliminary_reports)
            orderbooks_requested_count = len(depth_tickers)
            network_ms["kalshi_orderbooks_total_ms"], orderbooks = _timed_call(
                _fetch_orderbooks,
                depth_tickers,
                pause_seconds=args.orderbook_pause_seconds,
                concurrency=args.orderbook_concurrency,
            )
        else:
            preliminary_reports = []
            preliminary_compute_ms = []
            orderbooks_requested_count = len(rows)
            network_ms["kalshi_orderbooks_total_ms"], orderbooks = _timed_call(
                _fetch_orderbooks,
                [str(row.get("ticker") or "") for row in rows],
                pause_seconds=args.orderbook_pause_seconds,
                concurrency=args.orderbook_concurrency,
            )

        final_compute_ms, reports = _timed_evaluate_events(
            active_events,
            odds_payloads=odds_payloads,
            rows=rows,
            orderbooks=orderbooks,
            as_of=as_of,
            config=config,
            prefer_provider_contains=args.prefer_provider_contains,
        )
        compute_ms = preliminary_compute_ms + final_compute_ms
        network_ms["end_to_end_ms"] = (time.perf_counter() - total_start) * 1000.0
        payload = _bench_payload(
            as_of=as_of,
            mode="live_public_read_only",
            compute_ms=compute_ms,
            network_ms=network_ms,
            counts={
                "reports": len(reports),
                "markets": sum(len(report.markets) for report in reports),
                "events": len(events),
                "active_events": len(active_events),
                "orderbooks_requested": orderbooks_requested_count,
                "orderbooks": len(orderbooks),
                "orderbook_concurrency": args.orderbook_concurrency,
                "selective_orderbooks": int(bool(args.selective_orderbooks)),
                "preliminary_reports": len(preliminary_reports),
                "preliminary_candidates": len(_candidate_tickers(preliminary_reports)),
            },
        )
    _write_json(args.report_json, payload)
    args.report_md.parent.mkdir(parents=True, exist_ok=True)
    args.report_md.write_text(_render_bench_markdown(payload), encoding="utf-8")
    print(json.dumps(payload["summary"], indent=2, sort_keys=True))
    return 0


def _handle_readiness(args: argparse.Namespace) -> int:
    validation = _read_json(args.validation_report_json)
    markout = _read_optional_json(args.markout_report_json)
    settlement = _read_optional_json(args.settlement_report_json)
    bench = _read_optional_json(args.bench_report_json)
    payload = _readiness_payload(
        validation,
        markout=markout,
        settlement=settlement,
        bench=bench,
        signals_jsonl=args.signals_jsonl,
        min_markout_rows=args.min_markout_rows,
        min_positive_markout_rate=args.min_positive_markout_rate,
        min_mean_markout_after_fee=args.min_mean_markout_after_fee,
        min_markout_entry_age_seconds=args.min_markout_entry_age_seconds,
        min_settled_rows=args.min_settled_rows,
        min_mean_settlement_pnl=args.min_mean_settlement_pnl,
        max_end_to_end_ms=args.max_end_to_end_ms,
    )
    _write_json(args.report_json, payload)
    args.report_md.parent.mkdir(parents=True, exist_ok=True)
    args.report_md.write_text(_render_readiness_markdown(payload), encoding="utf-8")
    summary = _summary_mapping(payload)
    print(json.dumps(summary, indent=2, sort_keys=True))
    if args.allow_not_ready:
        return 0
    return 0 if bool(summary.get("production_ready")) else 2


def _build_live_reports(
    args: argparse.Namespace,
    *,
    as_of: datetime,
    config: NbaSpreadValidationConfig,
) -> tuple[tuple[NbaSpreadValidationReport, ...], list[dict[str, str]]]:
    scoreboard = _fetch_scoreboard(date=args.espn_date)
    events = _select_events(scoreboard, event_id=args.espn_event_id)
    markets_payload = _fetch_kalshi_markets(args.series_ticker)
    rows = _market_rows(markets_payload)
    skipped: list[dict[str, str]] = []
    active_events: list[Mapping[str, object]] = []
    odds_payloads: dict[str, Mapping[str, object]] = {}
    for event in events:
        event_id = str(event.get("id") or "")
        try:
            game = parse_nba_game_state(event, received_at=as_of)
            if game.completed or game.status_state == "post":
                skipped.append({"event_id": game.event_id, "reason": "completed_game_not_entry_eligible"})
                continue
            odds_payload = _fetch_espn_odds(game.event_id)
            odds_payloads[game.event_id] = odds_payload
            active_events.append(event)
        except Exception as exc:  # noqa: BLE001 - one bad public odds row should not kill the full scan
            skipped.append({"event_id": event_id, "reason": f"{type(exc).__name__}: {exc}"})

    if args.skip_orderbooks:
        orderbooks: dict[str, Mapping[str, object]] = {}
    elif args.selective_orderbooks:
        preliminary_reports = _evaluate_events(
            active_events,
            odds_payloads=odds_payloads,
            rows=rows,
            orderbooks={},
            as_of=as_of,
            config=config,
            prefer_provider_contains=args.prefer_provider_contains,
        )
        orderbooks = _fetch_orderbooks(
            _candidate_tickers(preliminary_reports),
            pause_seconds=args.orderbook_pause_seconds,
            concurrency=args.orderbook_concurrency,
        )
    else:
        orderbooks = _fetch_orderbooks(
            [str(row.get("ticker") or "") for row in rows],
            pause_seconds=args.orderbook_pause_seconds,
            concurrency=args.orderbook_concurrency,
        )
    reports = _evaluate_events(
        active_events,
        odds_payloads=odds_payloads,
        rows=rows,
        orderbooks=orderbooks,
        as_of=as_of,
        config=config,
        prefer_provider_contains=args.prefer_provider_contains,
    )
    return tuple(reports), skipped


def _evaluate_events(
    events: Sequence[Mapping[str, object]],
    *,
    odds_payloads: Mapping[str, Mapping[str, object]],
    rows: Sequence[Mapping[str, object]],
    orderbooks: Mapping[str, Mapping[str, object]],
    as_of: datetime,
    config: NbaSpreadValidationConfig,
    prefer_provider_contains: str,
) -> list[NbaSpreadValidationReport]:
    reports: list[NbaSpreadValidationReport] = []
    for event in events:
        game = parse_nba_game_state(event, received_at=as_of)
        try:
            anchor = parse_espn_live_odds_anchor(
                odds_payloads[game.event_id],
                received_at=as_of,
                prefer_provider_contains=prefer_provider_contains,
            )
        except Exception:
            continue
        markets = _parse_markets_for_game(
            rows,
            game=game,
            as_of=as_of,
            orderbooks=orderbooks,
        )
        if not markets:
            continue
        reports.append(
            evaluate_spread_ladder(
                game=game,
                anchor=anchor,
                markets=markets,
                config=config,
                as_of=as_of,
            )
        )
    return reports


def _timed_evaluate_events(
    events: Sequence[Mapping[str, object]],
    *,
    odds_payloads: Mapping[str, Mapping[str, object]],
    rows: Sequence[Mapping[str, object]],
    orderbooks: Mapping[str, Mapping[str, object]],
    as_of: datetime,
    config: NbaSpreadValidationConfig,
    prefer_provider_contains: str,
) -> tuple[list[float], list[NbaSpreadValidationReport]]:
    reports: list[NbaSpreadValidationReport] = []
    compute_ms: list[float] = []
    for event in events:
        elapsed, event_reports = _timed_call(
            _evaluate_events,
            [event],
            odds_payloads=odds_payloads,
            rows=rows,
            orderbooks=orderbooks,
            as_of=as_of,
            config=config,
            prefer_provider_contains=prefer_provider_contains,
        )
        if event_reports:
            compute_ms.append(elapsed)
            reports.extend(event_reports)
    return compute_ms, reports


def _parse_markets_for_game(
    rows: Sequence[Mapping[str, object]],
    *,
    game: Any,
    as_of: datetime,
    orderbooks: Mapping[str, Mapping[str, object]],
) -> tuple[Any, ...]:
    return tuple(
        quote
        for quote in (
            parse_kalshi_spread_market(
                row,
                game=game,
                received_at=as_of,
                orderbook=orderbooks.get(str(row.get("ticker") or "")),
            )
            for row in rows
        )
        if quote is not None
    )


def _candidate_tickers(reports: Sequence[NbaSpreadValidationReport]) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            decision.ticker
            for report in reports
            for decision in report.decisions
            if decision.candidate
        )
    )


def _entry_candidate_tickers(payload: Mapping[str, object]) -> tuple[str, ...]:
    tickers: list[str] = []
    for report in _entry_reports(payload):
        decisions = report.get("decisions")
        if not isinstance(decisions, Sequence) or isinstance(decisions, str):
            continue
        for row in decisions:
            if not isinstance(row, Mapping) or not row.get("candidate"):
                continue
            ticker = str(row.get("ticker") or "")
            if ticker:
                tickers.append(ticker)
    return tuple(dict.fromkeys(tickers))


def _build_current_quotes(
    args: argparse.Namespace,
    *,
    as_of: datetime,
    target_tickers: Sequence[str] | None = None,
) -> dict[str, Any]:
    scoreboard = _fetch_scoreboard(date=args.espn_date)
    events = _select_events(scoreboard, event_id=args.espn_event_id)
    markets_payload = _fetch_kalshi_markets(args.series_ticker)
    rows = _market_rows(markets_payload)
    target_set = set(target_tickers or ())
    depth_rows = [row for row in rows if not target_set or str(row.get("ticker") or "") in target_set]
    orderbooks = (
        _fetch_orderbooks(
            [str(row.get("ticker") or "") for row in depth_rows],
            pause_seconds=args.orderbook_pause_seconds,
            concurrency=args.orderbook_concurrency,
        )
        if not args.skip_orderbooks
        else {}
    )
    quotes: dict[str, Any] = {}
    for event in events:
        try:
            game = parse_nba_game_state(event, received_at=as_of)
        except Exception:
            continue
        for row in depth_rows:
            quote = parse_kalshi_spread_market(
                row,
                game=game,
                received_at=as_of,
                orderbook=orderbooks.get(str(row.get("ticker") or "")),
            )
            if quote is not None:
                quotes[quote.ticker] = quote
    return quotes


def _combined_validation_payload(
    reports: Sequence[NbaSpreadValidationReport],
    *,
    skipped_events: Sequence[Mapping[str, str]],
    as_of: datetime,
    series_ticker: str,
    config: NbaSpreadValidationConfig,
) -> dict[str, object]:
    decisions = [decision for report in reports for decision in report.decisions]
    candidates = [decision for decision in decisions if decision.candidate]
    best = max(
        (decision for decision in decisions if decision.net_edge is not None),
        key=lambda decision: float(decision.net_edge or -999.0),
        default=None,
    )
    candidate_expected_profit = sum(float(decision.expected_profit_dollars or 0.0) for decision in candidates)
    decision_gate = (
        "paper_candidate:live_mlb_spread_reference_edge_needs_markout_and_settlement"
        if candidates
        else "kill_or_defer:no_fee_net_executable_candidate"
    )
    return {
        "schema_version": "mlb-spread-validation-v1",
        "as_of": as_of.isoformat(),
        "series_ticker": series_ticker,
        "summary": {
            "reports": len(reports),
            "markets": sum(len(report.markets) for report in reports),
            "candidates": len(candidates),
            "best_ticker": best.ticker if best else None,
            "best_side": best.side if best else None,
            "best_net_edge": best.net_edge if best else None,
            "paper_contracts": config.paper_contracts,
            "candidate_expected_profit_dollars": candidate_expected_profit,
            "skipped_events": len(skipped_events),
            "decision": decision_gate,
        },
        "reports": [report.as_dict() for report in reports],
        "skipped_events": list(skipped_events),
        "caveat": (
            "Research/paper only. ESPN/DraftKings live odds are used as a sharp reference, "
            "but the public odds payload may not carry an upstream update timestamp. Positive rows "
            "require quote persistence, CLV, fill, and settlement evidence before edge exists."
        ),
    }


def _candidate_signals(reports: Sequence[NbaSpreadValidationReport]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for report in reports:
        for decision in report.decisions:
            if not decision.candidate:
                continue
            payload = decision.as_signal_payload()
            payload["as_of"] = decision.as_of.isoformat()
            payload["instrument"] = f"kalshi:{decision.ticker}"
            payload["strategy_family"] = "mlb_spread_sharp_reference"
            payload["source"] = "sharp-consensus"
            payload["odds_provider"] = "espn_draftkings_live_odds"
            payload["sport"] = "mlb"
            rows.append(payload)
    return rows


def _bench_payload(
    *,
    as_of: datetime,
    mode: str,
    compute_ms: Sequence[float],
    network_ms: Mapping[str, float],
    counts: Mapping[str, int],
) -> dict[str, object]:
    compute_total = sum(compute_ms)
    end_to_end = network_ms.get("end_to_end_ms")
    network_total = sum(value for key, value in network_ms.items() if key != "end_to_end_ms")
    if end_to_end is not None and compute_total > 0.0:
        bottleneck_ratio = end_to_end / max(compute_total, 1e-9)
    else:
        bottleneck_ratio = None
    if not compute_ms:
        conclusion = "insufficient_compute_measurement"
    elif bottleneck_ratio is not None and bottleneck_ratio > 10.0:
        conclusion = "network_bound"
    elif compute_ms:
        conclusion = "compute_measured_no_network"
    else:
        conclusion = "insufficient_measurement"
    return {
        "schema_version": "mlb-spread-bench-v1",
        "as_of": as_of.isoformat(),
        "mode": mode,
        "counts": dict(counts),
        "summary": {
            "compute_iterations": len(compute_ms),
            "compute_eval_total_ms": compute_total,
            "compute_eval_min_ms": min(compute_ms) if compute_ms else None,
            "compute_eval_median_ms": statistics.median(compute_ms) if compute_ms else None,
            "network_total_ms": network_total if network_ms else None,
            "end_to_end_ms": end_to_end,
            "network_to_compute_ratio": bottleneck_ratio,
            "conclusion": conclusion,
        },
        "network_ms": dict(network_ms),
        "compute_eval_ms": list(compute_ms),
    }


def _readiness_payload(
    validation: Mapping[str, object],
    *,
    markout: Mapping[str, object] | None,
    settlement: Mapping[str, object] | None,
    bench: Mapping[str, object] | None,
    signals_jsonl: Path | None,
    min_markout_rows: int,
    min_positive_markout_rate: float,
    min_mean_markout_after_fee: float,
    min_markout_entry_age_seconds: float,
    min_settled_rows: int,
    min_mean_settlement_pnl: float,
    max_end_to_end_ms: float | None,
) -> dict[str, object]:
    checks: list[dict[str, object]] = []

    validation_summary = _summary_mapping(validation)
    candidate_count = _int_value(validation_summary.get("candidates"))
    expected_profit = _float_or_none(validation_summary.get("candidate_expected_profit_dollars"))
    _add_check(
        checks,
        "fee_net_candidates_present",
        candidate_count > 0 and expected_profit is not None and expected_profit > 0.0,
        f"candidates={candidate_count}, expected_profit={expected_profit}",
    )
    leakage_detected, leakage_detail = _entry_generation_leakage_detail(validation)
    _add_check(
        checks,
        "entry_generation_point_in_time",
        not leakage_detected,
        leakage_detail,
    )
    source_timestamp_ok, source_detail = _candidate_source_timestamp_detail(validation)
    strict_source_gate_ok, strict_source_gate_detail = _validation_source_timestamp_gate_detail(validation)
    _add_check(
        checks,
        "source_timestamp_required_by_validation",
        strict_source_gate_ok,
        strict_source_gate_detail,
    )
    _add_check(
        checks,
        "upstream_source_timestamps_present",
        source_timestamp_ok,
        source_detail,
    )
    signal_ok, signal_detail = _signals_jsonl_compatible(signals_jsonl, expected_rows=candidate_count)
    _add_check(
        checks,
        "external_signal_payload_compatible",
        signal_ok,
        signal_detail,
    )

    if markout is None:
        _add_check(checks, "positive_markout", False, "missing markout report")
    else:
        markout_summary = _summary_mapping(markout)
        rows = _int_value(markout_summary.get("markout_rows"))
        positives = _int_value(markout_summary.get("positive_markouts"))
        positive_rate = positives / rows if rows > 0 else 0.0
        mean = _float_or_none(markout_summary.get("mean_markout_after_entry_fee"))
        age = _float_or_none(markout_summary.get("entry_age_seconds"))
        _add_check(
            checks,
            "markout_horizon_covered",
            age is not None and age >= min_markout_entry_age_seconds,
            f"entry_age_seconds={age}, min={min_markout_entry_age_seconds}",
        )
        _add_check(
            checks,
            "positive_markout",
            rows >= min_markout_rows
            and mean is not None
            and mean > min_mean_markout_after_fee
            and positive_rate >= min_positive_markout_rate,
            (
                f"rows={rows}/{min_markout_rows}, mean={mean}, "
                f"positive_rate={positive_rate:.3f}/{min_positive_markout_rate:.3f}"
            ),
        )

    if settlement is None:
        _add_check(checks, "positive_settlement", False, "missing settlement report")
    else:
        settlement_summary = _summary_mapping(settlement)
        rows = _int_value(settlement_summary.get("settled_rows"))
        mean = _float_or_none(settlement_summary.get("mean_pnl_after_entry_fee"))
        _add_check(
            checks,
            "positive_settlement",
            rows >= min_settled_rows and mean is not None and mean > min_mean_settlement_pnl,
            f"settled_rows={rows}/{min_settled_rows}, mean_pnl={mean}",
        )

    if bench is None:
        _add_check(checks, "executable_depth_bench_present", False, "missing bench report")
        _add_check(checks, "latency_budget", False, "missing bench report")
    else:
        bench_summary = _summary_mapping(bench)
        bench_counts = bench.get("counts")
        count_rows = bench_counts if isinstance(bench_counts, Mapping) else {}
        orderbooks = _int_value(count_rows.get("orderbooks"))
        orderbooks_requested = _int_value(count_rows.get("orderbooks_requested"))
        preliminary_candidates = _int_value(count_rows.get("preliminary_candidates"))
        depth_ok = orderbooks > 0 or (candidate_count == 0 and preliminary_candidates == 0)
        _add_check(
            checks,
            "executable_depth_bench_present",
            depth_ok,
            (
                f"orderbooks={orderbooks}, orderbooks_requested={orderbooks_requested}, "
                f"preliminary_candidates={preliminary_candidates}"
            ),
        )
        end_to_end = _float_or_none(bench_summary.get("end_to_end_ms"))
        compute = _float_or_none(bench_summary.get("compute_eval_median_ms"))
        latency_ok = end_to_end is not None if max_end_to_end_ms is None else (
            end_to_end is not None and end_to_end <= max_end_to_end_ms
        )
        _add_check(
            checks,
            "latency_budget",
            latency_ok,
            f"end_to_end_ms={end_to_end}, compute_median_ms={compute}, max_end_to_end_ms={max_end_to_end_ms}",
        )

    production_ready = all(bool(check["passed"]) for check in checks)
    blockers = [str(check["name"]) for check in checks if not check["passed"]]
    return {
        "schema_version": "mlb-spread-production-readiness-v1",
        "as_of": datetime.now(UTC).isoformat(),
        "summary": {
            "production_ready": production_ready,
            "decision": "production_ready" if production_ready else "not_ready",
            "blockers": blockers,
        },
        "checks": checks,
        "boundary": (
            "Read-only production-readiness gate. This does not authorize orders or live submission. "
            "Current repo policy still forbids trading."
        ),
    }


def _entry_reports(payload: Mapping[str, object]) -> tuple[Mapping[str, object], ...]:
    reports = payload.get("reports")
    if isinstance(reports, Sequence) and not isinstance(reports, str):
        return tuple(report for report in reports if isinstance(report, Mapping))
    return (payload,)


def _paper_contracts(payload: Mapping[str, object]) -> int:
    summary = payload.get("summary")
    if isinstance(summary, Mapping):
        try:
            return int(float(str(summary.get("paper_contracts") or "1")))
        except ValueError:
            return 1
    return 1


def _summary_mapping(payload: Mapping[str, object]) -> Mapping[str, object]:
    summary = payload.get("summary")
    return summary if isinstance(summary, Mapping) else {}


def _entry_generation_leakage_detail(validation: Mapping[str, object]) -> tuple[bool, str]:
    candidate_reports = 0
    completed = 0
    post = 0
    for report in _entry_reports(validation):
        decisions = report.get("decisions")
        if not isinstance(decisions, Sequence) or isinstance(decisions, str):
            continue
        if not any(isinstance(raw, Mapping) and raw.get("candidate") for raw in decisions):
            continue
        candidate_reports += 1
        game = report.get("game")
        if not isinstance(game, Mapping):
            continue
        if bool(game.get("completed")):
            completed += 1
        if str(game.get("status_state") or "").lower() == "post":
            post += 1
    detail = f"candidate_reports={candidate_reports}, completed={completed}, post={post}"
    return completed > 0 or post > 0, detail


def _candidate_source_timestamp_detail(validation: Mapping[str, object]) -> tuple[bool, str]:
    candidate_count = 0
    missing = 0
    proxy_only = 0
    for report in _entry_reports(validation):
        decisions = report.get("decisions")
        if not isinstance(decisions, Sequence) or isinstance(decisions, str):
            continue
        for raw in decisions:
            if not isinstance(raw, Mapping) or not raw.get("candidate"):
                continue
            candidate_count += 1
            basis = str(raw.get("source_timestamp_basis") or "").lower()
            if raw.get("source_age_seconds") is None:
                missing += 1
            if "received_at" in basis or "no_odds_last_modified" in basis or "proxy" in basis:
                proxy_only += 1
    ok = candidate_count > 0 and missing == 0 and proxy_only == 0
    detail = f"candidates={candidate_count}, missing_age={missing}, proxy_only={proxy_only}"
    return ok, detail


def _validation_source_timestamp_gate_detail(validation: Mapping[str, object]) -> tuple[bool, str]:
    reports = _entry_reports(validation)
    if not reports:
        return False, "reports=0, require_source_timestamp=false"
    configured = 0
    missing = 0
    for report in reports:
        config = report.get("config")
        if not isinstance(config, Mapping):
            missing += 1
            continue
        if bool(config.get("require_source_timestamp")):
            configured += 1
    ok = configured == len(reports) and missing == 0
    return ok, f"reports={len(reports)}, require_source_timestamp={configured}, missing_config={missing}"


def _signals_jsonl_compatible(path: Path | None, *, expected_rows: int) -> tuple[bool, str]:
    if path is None:
        return False, "signal path not provided"
    if not path.exists():
        return False, f"missing signal file: {path}"
    rows = 0
    invalid = 0
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip():
            continue
        rows += 1
        try:
            payload = json.loads(raw_line)
        except json.JSONDecodeError:
            invalid += 1
            continue
        if not isinstance(payload, Mapping):
            invalid += 1
            continue
        has_time = bool(payload.get("as_of"))
        has_market = bool(payload.get("market_id") or payload.get("instrument"))
        probability = _float_or_none(payload.get("probability"))
        if not has_time or not has_market or probability is None:
            invalid += 1
    if expected_rows == 0:
        return rows == 0 and invalid == 0, f"rows={rows}, expected_rows=0, invalid={invalid}, path={path}"
    return rows > 0 and invalid == 0, f"rows={rows}, expected_rows={expected_rows}, invalid={invalid}, path={path}"


def _add_check(checks: list[dict[str, object]], name: str, passed: bool, detail: str) -> None:
    checks.append({"name": name, "passed": passed, "detail": detail})


def _augment_markout_payload(
    markout_payload: Mapping[str, object],
    *,
    entry_payload: Mapping[str, object],
    markout_as_of: datetime,
    entry_report: str,
    target_horizon_seconds: float | None,
    label: str,
    source_markout_report: str | None,
) -> dict[str, object]:
    payload = dict(markout_payload)
    payload["schema_version"] = "mlb-spread-markout-v2"
    payload["as_of"] = markout_as_of.isoformat()
    payload["entry_report"] = entry_report
    summary = dict(_summary_mapping(markout_payload))
    entry_as_of = _parse_datetime_or_none(entry_payload.get("as_of"))
    entry_age_seconds = (markout_as_of - entry_as_of).total_seconds() if entry_as_of is not None else None
    horizon_lag_seconds = (
        entry_age_seconds - target_horizon_seconds
        if entry_age_seconds is not None and target_horizon_seconds is not None
        else None
    )
    context: dict[str, object] = {
        "label": label,
        "entry_as_of": entry_as_of.isoformat() if entry_as_of is not None else None,
        "markout_as_of": markout_as_of.isoformat(),
        "entry_age_seconds": entry_age_seconds,
        "target_horizon_seconds": target_horizon_seconds,
        "horizon_lag_seconds": horizon_lag_seconds,
        "horizon_status": _horizon_status(entry_age_seconds, target_horizon_seconds),
        "source_markout_report": source_markout_report,
    }
    summary.update(context)
    payload["summary"] = summary
    payload["markout_context"] = context
    return payload


def _build_markout_payload(
    *,
    entry_payload: Mapping[str, object],
    current_quotes: Mapping[str, Any],
    markout_as_of: datetime,
    entry_report: str,
    target_horizon_seconds: float | None,
    label: str,
    source_markout_report: str | None,
) -> dict[str, object]:
    rows: list[NbaSpreadMarkoutRow] = []
    for report_payload in _entry_reports(entry_payload):
        report = markout_report_from_entry_report(
            report_payload,
            current_quotes=current_quotes,
            as_of=markout_as_of,
            entry_report_name=entry_report,
        )
        rows.extend(report.rows)
    combined = NbaSpreadMarkoutReport(
        as_of=markout_as_of,
        entry_report=entry_report,
        paper_contracts=_paper_contracts(entry_payload),
        rows=tuple(rows),
        decision=_markout_decision(rows),
        schema_version="mlb-spread-markout-v1",
    )
    return _augment_markout_payload(
        combined.as_dict(),
        entry_payload=entry_payload,
        markout_as_of=markout_as_of,
        entry_report=entry_report,
        target_horizon_seconds=target_horizon_seconds,
        label=label,
        source_markout_report=source_markout_report,
    )


def _horizon_status(entry_age_seconds: float | None, target_horizon_seconds: float | None) -> str:
    if target_horizon_seconds is None:
        return "observed:no_target_horizon"
    if entry_age_seconds is None:
        return "unknown:entry_timestamp_missing"
    if entry_age_seconds + 1e-9 < target_horizon_seconds:
        return "early:before_target_horizon"
    return "observed:at_or_after_target_horizon"


def _wait_for_horizon(
    entry_payload: Mapping[str, object],
    *,
    horizon_seconds: float,
    max_wait_seconds: float,
) -> dict[str, object]:
    entry_as_of = _parse_datetime_or_none(entry_payload.get("as_of"))
    if entry_as_of is None:
        return {
            "entry_as_of": None,
            "target_as_of": None,
            "wait_seconds": None,
            "status": "entry_timestamp_missing",
        }
    target_as_of = entry_as_of.timestamp() + horizon_seconds
    now = datetime.now(UTC).timestamp()
    wait_seconds = max(0.0, target_as_of - now)
    if wait_seconds > max_wait_seconds:
        return {
            "entry_as_of": entry_as_of.isoformat(),
            "target_as_of": datetime.fromtimestamp(target_as_of, tz=UTC).isoformat(),
            "wait_seconds": wait_seconds,
            "status": "skipped_future_horizon",
        }
    if wait_seconds > 0.0:
        time.sleep(wait_seconds)
    return {
        "entry_as_of": entry_as_of.isoformat(),
        "target_as_of": datetime.fromtimestamp(target_as_of, tz=UTC).isoformat(),
        "wait_seconds": wait_seconds,
        "status": "ready",
    }


def _markout_horizons_bundle(
    *,
    entry_report: str,
    horizons: Sequence[float],
    reports: Sequence[Mapping[str, object]],
    ledger_path: Path | None,
) -> dict[str, object]:
    collected = [report for report in reports if report.get("status") == "collected"]
    skipped = [report for report in reports if report.get("status") == "skipped_future_horizon"]
    return {
        "schema_version": "mlb-spread-markout-horizons-v1",
        "as_of": datetime.now(UTC).isoformat(),
        "entry_report": entry_report,
        "ledger_path": str(ledger_path) if ledger_path is not None else None,
        "summary": {
            "horizons_requested": len(horizons),
            "horizons_collected": len(collected),
            "horizons_skipped": len(skipped),
            "decision": (
                "continue_capture:markout_horizons_collected"
                if len(collected) == len(horizons)
                else "continue_capture:future_horizons_skipped_by_wait_cap"
            ),
        },
        "reports": list(reports),
    }


def _horizon_label(horizon_seconds: float) -> str:
    return f"plus_{_horizon_slug(horizon_seconds).replace('-', '_')}"


def _horizon_slug(horizon_seconds: float) -> str:
    if float(horizon_seconds).is_integer():
        return f"{int(horizon_seconds)}s"
    text = f"{horizon_seconds:.3f}".rstrip("0").rstrip(".")
    return f"{text.replace('.', 'p')}s"


def _parse_horizons(raw: str) -> tuple[float, ...]:
    values: list[float] = []
    for item in raw.split(","):
        text = item.strip()
        if not text:
            continue
        value = float(text)
        if value < 0.0:
            raise ValueError("horizons must be non-negative")
        values.append(value)
    if not values:
        raise ValueError("at least one horizon is required")
    return tuple(dict.fromkeys(values))


def _append_markout_ledger(path: Path | None, payload: Mapping[str, object], *, report_path: Path) -> None:
    if path is None:
        return
    context = payload.get("markout_context")
    context_payload = context if isinstance(context, Mapping) else {}
    rows = payload.get("rows")
    if not isinstance(rows, Sequence) or isinstance(rows, str):
        return
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        ledger_row = {
            **dict(context_payload),
            **dict(row),
            "markout_report": str(report_path),
            "entry_report": str(payload.get("entry_report") or ""),
            "paper_contracts": payload.get("paper_contracts"),
            "schema_version": "mlb-spread-markout-ledger-v1",
        }
        append_jsonl(path, ledger_row)


def _markout_decision(rows: Sequence[NbaSpreadMarkoutRow]) -> str:
    values = [row.markout_after_entry_fee for row in rows if row.markout_after_entry_fee is not None]
    if not rows:
        return "continue_capture:no_candidate_entries"
    if not values:
        return "continue_capture:no_markout_rows"
    mean = sum(float(value) for value in values) / len(values)
    if mean > 0.0:
        return "continue_capture:positive_short_markout_needs_more_rows_and_settlement"
    return "kill_or_defer:short_markout_negative"


def _settlement_payload(
    rows: Sequence[NbaSpreadSettlementRow],
    *,
    missing_games: Sequence[str],
    as_of: datetime,
    entry_report: str,
    paper_contracts: int,
) -> dict[str, object]:
    settled = [row for row in rows if row.pnl_after_entry_fee is not None]
    values = [float(row.pnl_after_entry_fee) for row in settled if row.pnl_after_entry_fee is not None]
    mean = sum(values) / len(values) if values else None
    total = sum(value * paper_contracts for value in values)
    pending = any(row.reason == "game_not_completed" for row in rows)
    if pending:
        decision = "pending:games_not_completed"
    elif missing_games:
        decision = "pending:missing_games"
    elif mean is not None and mean > 0.0:
        decision = "paper_edge_supported:settlement_positive"
    else:
        decision = "kill:settlement_negative_or_empty"
    return {
        "schema_version": "mlb-spread-settlement-v1",
        "as_of": as_of.isoformat(),
        "entry_report": entry_report,
        "paper_contracts": paper_contracts,
        "summary": {
            "entries": len(rows),
            "settled_rows": len(settled),
            "winning_rows": sum(1 for row in settled if (row.pnl_after_entry_fee or 0.0) > 0.0),
            "mean_pnl_after_entry_fee": mean,
            "total_pnl_dollars": total,
            "missing_games": len(missing_games),
            "decision": decision,
        },
        "missing_games": list(missing_games),
        "rows": [to_jsonable(row) for row in rows],
    }


def _render_validation_markdown(
    payload: Mapping[str, object],
    reports: Sequence[NbaSpreadValidationReport],
) -> str:
    summary = payload["summary"]
    assert isinstance(summary, Mapping)
    decisions = [decision for report in reports for decision in report.decisions]
    top = sorted(
        (decision for decision in decisions if decision.net_edge is not None),
        key=lambda decision: float(decision.net_edge or -999.0),
        reverse=True,
    )[:25]
    candidates = [decision for decision in decisions if decision.candidate]
    lines = [
        "# MLB spread sharp-reference validation",
        "",
        f"- Generated: {payload.get('as_of')}",
        f"- Series: `{payload.get('series_ticker')}`",
        f"- Matched games: `{summary.get('reports')}`",
        f"- Markets checked: `{summary.get('markets')}`",
        f"- Candidates: `{summary.get('candidates')}`",
        f"- Candidate expected profit at {summary.get('paper_contracts')} contracts each: "
        f"{_fmt(summary.get('candidate_expected_profit_dollars'))}",
        f"- Skipped ESPN events: `{summary.get('skipped_events')}`",
        f"- Decision: **{summary.get('decision')}**",
        "",
        "## Top fee-net rows",
        "",
        "| ticker | side | fair_yes | px | fee | net | size | reason |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in top:
        lines.append(
            "| "
            f"{row.ticker} | {row.side} | {row.fair_yes:.4f} | {_fmt(row.executable_price)} | "
            f"{_fmt(row.fee)} | {_fmt(row.net_edge)} | {_fmt(row.executable_size, digits=2)} | "
            f"{row.reason} |"
        )
    if not top:
        lines.append("| _none_ |  |  |  |  |  |  |  |")
    lines.extend(
        [
            "",
            "## Candidate signals",
            "",
            "| ticker | side | fair_yes | net | expected_profit |",
            "| --- | --- | ---: | ---: | ---: |",
        ]
    )
    for row in candidates[:30]:
        lines.append(
            "| "
            f"{row.ticker} | {row.side} | {row.fair_yes:.4f} | {_fmt(row.net_edge)} | "
            f"{_fmt(row.expected_profit_dollars)} |"
        )
    if not candidates:
        lines.append("| _none_ |  |  |  |  |")
    lines.extend(["", "## Caveat", "", str(payload.get("caveat") or ""), ""])
    return "\n".join(lines)


def _render_markout_markdown(report: NbaSpreadMarkoutReport) -> str:
    data = report.as_dict()
    summary = data["summary"]
    assert isinstance(summary, Mapping)
    rows = sorted(
        report.rows,
        key=lambda row: row.markout_after_entry_fee if row.markout_after_entry_fee is not None else -999.0,
        reverse=True,
    )
    lines = [
        "# MLB spread markout",
        "",
        f"- Generated: {report.as_of.isoformat()}",
        f"- Entry report: {report.entry_report}",
        f"- Entries: `{summary.get('entries')}`",
        f"- Markout rows: `{summary.get('markout_rows')}`",
        f"- Positive markouts: `{summary.get('positive_markouts')}`",
        f"- Mean markout after entry fee: {_fmt(summary.get('mean_markout_after_entry_fee'))}",
        f"- Total at {report.paper_contracts} contracts: {_fmt(summary.get('total_markout_dollars'))}",
        f"- Decision: **{summary.get('decision')}**",
        "",
        "| ticker | side | entry | bid | fee | markout | size | reason |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in rows[:30]:
        lines.append(
            "| "
            f"{row.ticker} | {row.side} | {_fmt(row.entry_price)} | {_fmt(row.markout_bid)} | "
            f"{_fmt(row.entry_fee)} | {_fmt(row.markout_after_entry_fee)} | "
            f"{_fmt(row.bid_size, digits=2)} | {row.reason} |"
        )
    if not rows:
        lines.append("| _none_ |  |  |  |  |  |  |  |")
    lines.append("")
    return "\n".join(lines)


def _render_markout_payload_markdown(payload: Mapping[str, object]) -> str:
    summary = _summary_mapping(payload)
    rows = payload.get("rows")
    row_items = rows if isinstance(rows, Sequence) and not isinstance(rows, str) else []
    ordered_rows = sorted(
        (row for row in row_items if isinstance(row, Mapping)),
        key=lambda row: _float_or_none(row.get("markout_after_entry_fee")) or -999.0,
        reverse=True,
    )
    lines = [
        "# MLB spread markout",
        "",
        f"- Generated: {payload.get('as_of')}",
        f"- Entry report: {payload.get('entry_report')}",
        f"- Label: `{summary.get('label')}`",
        f"- Entry age seconds: `{_fmt(summary.get('entry_age_seconds'))}`",
        f"- Target horizon seconds: `{_fmt(summary.get('target_horizon_seconds'))}`",
        f"- Horizon status: `{summary.get('horizon_status')}`",
        f"- Entries: `{summary.get('entries')}`",
        f"- Markout rows: `{summary.get('markout_rows')}`",
        f"- Positive markouts: `{summary.get('positive_markouts')}`",
        f"- Mean markout after entry fee: {_fmt(summary.get('mean_markout_after_entry_fee'))}",
        f"- Total at {payload.get('paper_contracts')} contracts: {_fmt(summary.get('total_markout_dollars'))}",
        f"- Decision: **{summary.get('decision')}**",
        "",
        "| ticker | side | entry | bid | fee | markout | size | reason |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in ordered_rows[:30]:
        lines.append(
            "| "
            f"{row.get('ticker')} | {row.get('side')} | {_fmt(row.get('entry_price'))} | "
            f"{_fmt(row.get('markout_bid'))} | {_fmt(row.get('entry_fee'))} | "
            f"{_fmt(row.get('markout_after_entry_fee'))} | {_fmt(row.get('bid_size'), digits=2)} | "
            f"{row.get('reason')} |"
        )
    if not ordered_rows:
        lines.append("| _none_ |  |  |  |  |  |  |  |")
    lines.append("")
    return "\n".join(lines)


def _render_markout_horizons_markdown(payload: Mapping[str, object]) -> str:
    summary = _summary_mapping(payload)
    reports = payload.get("reports")
    report_rows = reports if isinstance(reports, Sequence) and not isinstance(reports, str) else []
    lines = [
        "# MLB spread markout horizons",
        "",
        f"- Generated: {payload.get('as_of')}",
        f"- Entry report: {payload.get('entry_report')}",
        f"- Ledger: `{payload.get('ledger_path')}`",
        f"- Horizons requested: `{summary.get('horizons_requested')}`",
        f"- Horizons collected: `{summary.get('horizons_collected')}`",
        f"- Horizons skipped: `{summary.get('horizons_skipped')}`",
        f"- Decision: **{summary.get('decision')}**",
        "",
        "| horizon | label | status | wait_seconds | report | markout_rows | mean_markout |",
        "| ---: | --- | --- | ---: | --- | ---: | ---: |",
    ]
    for item in report_rows:
        if not isinstance(item, Mapping):
            continue
        item_summary = item.get("summary")
        row_summary = item_summary if isinstance(item_summary, Mapping) else {}
        lines.append(
            "| "
            f"{_fmt(item.get('horizon_seconds'))} | {item.get('label')} | {item.get('status')} | "
            f"{_fmt(item.get('wait_seconds'))} | {item.get('report_json') or ''} | "
            f"{row_summary.get('markout_rows') or ''} | {_fmt(row_summary.get('mean_markout_after_entry_fee'))} |"
        )
    if not report_rows:
        lines.append("| _none_ |  |  |  |  |  |  |")
    lines.append("")
    return "\n".join(lines)


def _render_settlement_markdown(payload: Mapping[str, object]) -> str:
    summary = payload["summary"]
    assert isinstance(summary, Mapping)
    rows = payload.get("rows")
    row_items = rows if isinstance(rows, Sequence) and not isinstance(rows, str) else []
    lines = [
        "# MLB spread settlement",
        "",
        f"- Generated: {payload.get('as_of')}",
        f"- Entry report: {payload.get('entry_report')}",
        f"- Entries: `{summary.get('entries')}`",
        f"- Settled rows: `{summary.get('settled_rows')}`",
        f"- Mean PnL after entry fee: {_fmt(summary.get('mean_pnl_after_entry_fee'))}",
        f"- Total at {payload.get('paper_contracts')} contracts: {_fmt(summary.get('total_pnl_dollars'))}",
        f"- Missing games: `{summary.get('missing_games')}`",
        f"- Decision: **{summary.get('decision')}**",
        "",
        "| ticker | side | threshold | yes_settled | payout | pnl | reason |",
        "| --- | --- | ---: | --- | ---: | ---: | --- |",
    ]
    for item in row_items[:40]:
        if not isinstance(item, Mapping):
            continue
        lines.append(
            "| "
            f"{item.get('ticker')} | {item.get('side')} | {_fmt(item.get('threshold'), digits=1)} | "
            f"{item.get('yes_settled')} | {_fmt(item.get('payout'))} | "
            f"{_fmt(item.get('pnl_after_entry_fee'))} | {item.get('reason')} |"
        )
    if not row_items:
        lines.append("| _none_ |  |  |  |  |  |  |")
    lines.append("")
    return "\n".join(lines)


def _render_bench_markdown(payload: Mapping[str, object]) -> str:
    summary = _summary_mapping(payload)
    network = payload.get("network_ms")
    network_rows = network if isinstance(network, Mapping) else {}
    counts = payload.get("counts")
    count_rows = counts if isinstance(counts, Mapping) else {}
    lines = [
        "# MLB spread latency and compute benchmark",
        "",
        f"- Generated: {payload.get('as_of')}",
        f"- Mode: `{payload.get('mode')}`",
        f"- Counts: `{dict(count_rows)}`",
        f"- Compute eval median ms: `{_fmt(summary.get('compute_eval_median_ms'))}`",
        f"- Compute eval total ms: `{_fmt(summary.get('compute_eval_total_ms'))}`",
        f"- Network total ms: `{_fmt(summary.get('network_total_ms'))}`",
        f"- End-to-end ms: `{_fmt(summary.get('end_to_end_ms'))}`",
        f"- Network/compute ratio: `{_fmt(summary.get('network_to_compute_ratio'))}`",
        f"- Conclusion: **{summary.get('conclusion')}**",
        "",
        "## Network legs",
        "",
        "| leg | ms |",
        "| --- | ---: |",
    ]
    for key, value in network_rows.items():
        lines.append(f"| {key} | {_fmt(value)} |")
    if not network_rows:
        lines.append("| _none_ |  |")
    lines.append("")
    return "\n".join(lines)


def _render_readiness_markdown(payload: Mapping[str, object]) -> str:
    summary = _summary_mapping(payload)
    checks = payload.get("checks")
    check_rows = checks if isinstance(checks, Sequence) and not isinstance(checks, str) else []
    lines = [
        "# MLB spread production readiness gate",
        "",
        f"- Generated: {payload.get('as_of')}",
        f"- Production ready: `{summary.get('production_ready')}`",
        f"- Decision: **{summary.get('decision')}**",
        f"- Blockers: `{summary.get('blockers')}`",
        "",
        "| check | passed | detail |",
        "| --- | --- | --- |",
    ]
    for row in check_rows:
        if not isinstance(row, Mapping):
            continue
        lines.append(f"| {row.get('name')} | {row.get('passed')} | {row.get('detail')} |")
    lines.extend(["", str(payload.get("boundary") or ""), ""])
    return "\n".join(lines)


def _timestamp_audit_payload(
    *,
    odds_payload: Mapping[str, object],
    headers: Mapping[str, str],
    as_of: datetime,
    event_id: str,
) -> dict[str, object]:
    fields = _timestamp_like_fields(odds_payload)
    interesting_headers = _interesting_timestamp_headers(headers)
    decision = (
        "timestamp_source_available"
        if fields
        else "proxy_only:no_upstream_odds_timestamp_field"
    )
    return {
        "schema_version": "mlb-spread-timestamp-audit-v1",
        "as_of": as_of.isoformat(),
        "event_id": event_id,
        "summary": {
            "upstream_timestamp_fields": len(fields),
            "transport_timestamp_headers": len(interesting_headers),
            "decision": decision,
        },
        "timestamp_like_fields": fields,
        "transport_headers": interesting_headers,
        "caveat": (
            "HTTP Date/cache headers are transport/cache metadata. They do not prove "
            "bookmaker odds source freshness unless the payload also carries an upstream "
            "odds update timestamp or sequence."
        ),
    }


def _render_timestamp_audit_markdown(payload: Mapping[str, object]) -> str:
    summary = _summary_mapping(payload)
    fields = payload.get("timestamp_like_fields")
    field_rows = fields if isinstance(fields, Sequence) and not isinstance(fields, str) else []
    headers = payload.get("transport_headers")
    header_rows = headers if isinstance(headers, Mapping) else {}
    lines = [
        "# MLB spread ESPN odds timestamp audit",
        "",
        f"- Generated: {payload.get('as_of')}",
        f"- ESPN event: `{payload.get('event_id')}`",
        f"- Upstream timestamp-like payload fields: `{summary.get('upstream_timestamp_fields')}`",
        f"- Transport/cache timestamp headers: `{summary.get('transport_timestamp_headers')}`",
        f"- Decision: **{summary.get('decision')}**",
        "",
        "## Payload Fields",
        "",
        "| path | sample |",
        "| --- | --- |",
    ]
    for row in field_rows[:50]:
        if isinstance(row, Mapping):
            lines.append(f"| {row.get('path')} | {row.get('sample')} |")
    if not field_rows:
        lines.append("| _none_ |  |")
    lines.extend(["", "## Transport Headers", "", "| header | value |", "| --- | --- |"])
    for key, value in header_rows.items():
        lines.append(f"| {key} | {value} |")
    if not header_rows:
        lines.append("| _none_ |  |")
    lines.extend(["", str(payload.get("caveat") or ""), ""])
    return "\n".join(lines)


def _timestamp_like_fields(payload: Mapping[str, object]) -> list[dict[str, str]]:
    needles = ("time", "date", "update", "modified", "stamp", "last")
    fields: list[dict[str, str]] = []

    def walk(value: object, path: str) -> None:
        if len(fields) >= 200:
            return
        if isinstance(value, Mapping):
            for key, child in value.items():
                next_path = f"{path}.{key}" if path else str(key)
                if any(needle in str(key).lower() for needle in needles):
                    fields.append({"path": next_path, "sample": _timestamp_sample(child)})
                walk(child, next_path)
        elif isinstance(value, Sequence) and not isinstance(value, str):
            for idx, child in enumerate(value[:20]):
                walk(child, f"{path}[{idx}]")

    walk(payload, "")
    return fields


def _timestamp_sample(value: object) -> str:
    if isinstance(value, Mapping):
        return "object"
    if isinstance(value, Sequence) and not isinstance(value, str):
        return "array"
    text = str(value)
    return text if len(text) <= 120 else text[:117] + "..."


def _interesting_timestamp_headers(headers: Mapping[str, str]) -> dict[str, str]:
    exact = {"date", "last-modified", "etag", "cache-control", "expires", "age", "x-cache"}
    return {
        key: value
        for key, value in headers.items()
        if key.lower() in exact
        or "cache" in key.lower()
        or "date" in key.lower()
        or "modified" in key.lower()
    }


def _game_states_by_event_id(*, date: str) -> dict[str, Any]:
    scoreboard = _fetch_scoreboard(date=date)
    games: dict[str, Any] = {}
    now = datetime.now(UTC)
    for event in _select_events(scoreboard, event_id=None):
        try:
            game = parse_nba_game_state(event, received_at=now)
        except Exception:
            continue
        games[game.event_id] = game
    return games


def _config_from_args(args: argparse.Namespace) -> NbaSpreadValidationConfig:
    return NbaSpreadValidationConfig(
        min_net_edge=args.min_net_edge,
        min_executable_size=args.min_executable_size,
        max_source_age_seconds=args.max_source_age_seconds,
        max_scoreboard_win_probability_disagreement=args.max_scoreboard_win_probability_disagreement,
        require_source_timestamp=args.require_source_timestamp,
        paper_contracts=args.paper_contracts,
        fee_coeff=args.fee_coeff,
        slippage=args.slippage,
    )


def _fetch_scoreboard(*, date: str) -> Mapping[str, object]:
    url = f"{ESPN_SCOREBOARD_URL}?dates={urllib.parse.quote(date)}&limit=100"
    return _fetch_json(url)


def _fetch_espn_odds(event_id: str) -> Mapping[str, object]:
    quoted = urllib.parse.quote(event_id)
    url = f"{ESPN_CORE_EVENT_URL}/{quoted}/competitions/{quoted}/odds?lang=en&region=us"
    return _fetch_json(url)


def _fetch_espn_odds_with_headers(event_id: str) -> tuple[Mapping[str, object], Mapping[str, str]]:
    quoted = urllib.parse.quote(event_id)
    url = f"{ESPN_CORE_EVENT_URL}/{quoted}/competitions/{quoted}/odds?lang=en&region=us"
    return _fetch_json_with_headers(url)


def _fetch_kalshi_markets(series_ticker: str) -> Mapping[str, object]:
    params = urllib.parse.urlencode({"series_ticker": series_ticker, "status": "open", "limit": "1000"})
    return _fetch_json(f"{KALSHI_MARKETS_URL}?{params}")


def _fetch_orderbooks(
    tickers: Sequence[str],
    *,
    pause_seconds: float,
    concurrency: int,
) -> dict[str, Mapping[str, object]]:
    unique_tickers = tuple(dict.fromkeys(ticker for ticker in tickers if ticker))
    if not unique_tickers:
        return {}
    if concurrency <= 1:
        return _fetch_orderbooks_sequential(unique_tickers, pause_seconds=pause_seconds)

    out: dict[str, Mapping[str, object]] = {}
    with ThreadPoolExecutor(max_workers=min(concurrency, len(unique_tickers))) as executor:
        futures = {}
        for ticker in unique_tickers:
            futures[executor.submit(_fetch_orderbook, ticker)] = ticker
            if pause_seconds > 0.0:
                time.sleep(pause_seconds)
        for future in as_completed(futures):
            ticker = futures[future]
            try:
                out[ticker] = future.result()
            except Exception as exc:  # noqa: BLE001 - missing depth should not kill a read-only valuation pass
                print(f"warning: orderbook fetch failed for {ticker}: {type(exc).__name__}", file=sys.stderr)
    return out


def _fetch_orderbooks_sequential(
    tickers: Sequence[str],
    *,
    pause_seconds: float,
) -> dict[str, Mapping[str, object]]:
    out: dict[str, Mapping[str, object]] = {}
    for ticker in tickers:
        try:
            out[ticker] = _fetch_orderbook(ticker)
        except Exception as exc:  # noqa: BLE001 - missing depth should not kill a read-only valuation pass
            print(f"warning: orderbook fetch failed for {ticker}: {type(exc).__name__}", file=sys.stderr)
        if pause_seconds > 0.0:
            time.sleep(pause_seconds)
    return out


def _fetch_orderbook(ticker: str) -> Mapping[str, object]:
    quoted = urllib.parse.quote(ticker, safe="")
    url = f"{KALSHI_MARKETS_URL}/{quoted}/orderbook"
    return _fetch_json(url)


def _fetch_json(url: str, *, timeout: float = 30.0, tries: int = 4) -> Mapping[str, object]:
    payload, _headers = _fetch_json_with_headers(url, timeout=timeout, tries=tries)
    return payload


def _fetch_json_with_headers(
    url: str,
    *,
    timeout: float = 30.0,
    tries: int = 4,
) -> tuple[Mapping[str, object], Mapping[str, str]]:
    last_exc: Exception | None = None
    for idx in range(tries):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "eventcontracts-mlb-spread/0.1"})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read())
                headers = dict(response.headers.items())
            if isinstance(payload, Mapping):
                return payload, headers
            raise ValueError("response was not a JSON object")
        except Exception as exc:  # noqa: BLE001 - retry public endpoints conservatively
            last_exc = exc
            if idx < tries - 1:
                time.sleep(1.0 + idx)
    assert last_exc is not None
    raise last_exc


def _select_events(payload: Mapping[str, object], *, event_id: str | None) -> tuple[Mapping[str, object], ...]:
    events = payload.get("events")
    if not isinstance(events, Sequence) or isinstance(events, str) or not events:
        raise ValueError("scoreboard payload has no events")
    out: list[Mapping[str, object]] = []
    for event in events:
        if not isinstance(event, Mapping):
            continue
        if event_id is None or str(event.get("id") or "") == event_id:
            out.append(event)
    if not out:
        raise ValueError(f"event_id not found in ESPN scoreboard: {event_id}")
    return tuple(out)


def _market_rows(payload: Mapping[str, object]) -> tuple[Mapping[str, object], ...]:
    rows = payload.get("markets")
    if not isinstance(rows, Sequence) or isinstance(rows, str):
        raise ValueError("Kalshi markets payload has no markets list")
    return tuple(row for row in rows if isinstance(row, Mapping))


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(to_jsonable(payload), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _read_json(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON report must be an object: {path}")
    return payload


def _read_optional_json(path: Path | None) -> dict[str, object] | None:
    if path is None or not path.exists():
        return None
    return _read_json(path)


def _parse_datetime_or_none(value: object) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _timed_call(func: Any, *args: Any, **kwargs: Any) -> tuple[float, Any]:
    start = time.perf_counter()
    value = func(*args, **kwargs)
    return (time.perf_counter() - start) * 1000.0, value


def _float_or_none(value: object) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed else None


def _int_value(value: object) -> int:
    parsed = _float_or_none(value)
    return int(parsed) if parsed is not None else 0


def _fmt(value: object, *, digits: int = 4) -> str:
    if value is None:
        return ""
    try:
        parsed = float(str(value))
    except (TypeError, ValueError):
        return ""
    return f"{parsed:.{digits}f}"


def _add_common(subparser: argparse.ArgumentParser) -> None:
    subparser.add_argument("--no-network", action="store_true")
    subparser.add_argument("--min-net-edge", type=float, default=0.015)
    subparser.add_argument("--min-executable-size", type=float, default=1.0)
    subparser.add_argument("--max-source-age-seconds", type=float, default=180.0)
    subparser.add_argument("--max-scoreboard-win-probability-disagreement", type=float, default=None)
    subparser.add_argument("--require-source-timestamp", action="store_true")
    subparser.add_argument("--paper-contracts", type=int, default=5)
    subparser.add_argument("--fee-coeff", type=float, default=0.07)
    subparser.add_argument("--slippage", type=float, default=0.0)


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    subparsers = parser.add_subparsers(dest="command", required=True)

    once = subparsers.add_parser("validate-once", help="Run one read-only MLB spread validation pass.")
    _add_common(once)
    once.add_argument("--espn-date", default="20260603")
    once.add_argument("--espn-event-id", default=None)
    once.add_argument("--series-ticker", default="KXMLBSPREAD")
    once.add_argument("--prefer-provider-contains", default="live")
    once.add_argument("--skip-orderbooks", action="store_true")
    once.add_argument("--selective-orderbooks", action="store_true")
    once.add_argument("--orderbook-pause-seconds", type=float, default=0.05)
    once.add_argument("--orderbook-concurrency", type=int, default=1)
    once.add_argument("--report-json", type=Path, default=ROOT / "live-test" / "mlb-spread-live-edge.json")
    once.add_argument("--report-md", type=Path, default=ROOT / "live-test" / "mlb-spread-live-edge.md")
    once.add_argument(
        "--signals-jsonl-out",
        type=Path,
        default=ROOT / "live-test" / "mlb-spread-live-edge-signals.jsonl",
    )
    once.set_defaults(handler=_handle_validate_once)

    markout = subparsers.add_parser("markout", help="Mark candidate entries to fresh public Kalshi bids.")
    markout.add_argument("--no-network", action="store_true")
    markout.add_argument("--espn-date", default="20260603")
    markout.add_argument("--espn-event-id", default=None)
    markout.add_argument("--series-ticker", default="KXMLBSPREAD")
    markout.add_argument("--skip-orderbooks", action="store_true")
    markout.add_argument("--orderbook-pause-seconds", type=float, default=0.05)
    markout.add_argument("--orderbook-concurrency", type=int, default=1)
    markout.add_argument("--horizon-seconds", type=float, default=0.0)
    markout.add_argument("--markout-label", default="immediate")
    markout.add_argument("--markout-ledger-jsonl-out", type=Path, default=None)
    markout.add_argument(
        "--entry-report-json",
        type=Path,
        default=ROOT / "live-test" / "mlb-spread-live-edge.json",
    )
    markout.add_argument("--report-json", type=Path, default=ROOT / "live-test" / "mlb-spread-markout.json")
    markout.add_argument("--report-md", type=Path, default=ROOT / "live-test" / "mlb-spread-markout.md")
    markout.set_defaults(handler=_handle_markout)

    annotate_markout = subparsers.add_parser(
        "annotate-markout",
        help="Add horizon metadata and optional ledger rows to an existing markout report.",
    )
    annotate_markout.add_argument(
        "--entry-report-json",
        type=Path,
        default=ROOT / "live-test" / "mlb-spread-live-edge.json",
    )
    annotate_markout.add_argument(
        "--markout-report-json",
        type=Path,
        default=ROOT / "live-test" / "mlb-spread-markout.json",
    )
    annotate_markout.add_argument("--horizon-seconds", type=float, default=0.0)
    annotate_markout.add_argument("--markout-label", default="immediate")
    annotate_markout.add_argument("--markout-ledger-jsonl-out", type=Path, default=None)
    annotate_markout.add_argument("--report-json", type=Path, default=ROOT / "live-test" / "mlb-spread-markout.json")
    annotate_markout.add_argument("--report-md", type=Path, default=ROOT / "live-test" / "mlb-spread-markout.md")
    annotate_markout.set_defaults(handler=_handle_annotate_markout)

    markout_horizons = subparsers.add_parser(
        "markout-horizons",
        help="Collect one public-read markout report per requested horizon.",
    )
    markout_horizons.add_argument("--no-network", action="store_true")
    markout_horizons.add_argument("--espn-date", default="20260603")
    markout_horizons.add_argument("--espn-event-id", default=None)
    markout_horizons.add_argument("--series-ticker", default="KXMLBSPREAD")
    markout_horizons.add_argument("--skip-orderbooks", action="store_true")
    markout_horizons.add_argument("--orderbook-pause-seconds", type=float, default=0.05)
    markout_horizons.add_argument("--orderbook-concurrency", type=int, default=1)
    markout_horizons.add_argument("--horizons-seconds", default="0,60,180,300")
    markout_horizons.add_argument("--max-wait-seconds", type=float, default=0.0)
    markout_horizons.add_argument(
        "--entry-report-json",
        type=Path,
        default=ROOT / "live-test" / "mlb-spread-live-edge.json",
    )
    markout_horizons.add_argument(
        "--markout-ledger-jsonl-out",
        type=Path,
        default=ROOT / "live-test" / "mlb-spread-markout-horizons-ledger.jsonl",
    )
    markout_horizons.add_argument("--report-dir", type=Path, default=ROOT / "live-test")
    markout_horizons.add_argument("--report-prefix", default="mlb-spread-markout-horizon")
    markout_horizons.add_argument(
        "--report-json",
        type=Path,
        default=ROOT / "live-test" / "mlb-spread-markout-horizons.json",
    )
    markout_horizons.add_argument(
        "--report-md",
        type=Path,
        default=ROOT / "live-test" / "mlb-spread-markout-horizons.md",
    )
    markout_horizons.set_defaults(handler=_handle_markout_horizons)

    settle = subparsers.add_parser("settle", help="Settle candidate entries from final ESPN score.")
    settle.add_argument("--no-network", action="store_true")
    settle.add_argument("--espn-date", default="20260603")
    settle.add_argument(
        "--entry-report-json",
        type=Path,
        default=ROOT / "live-test" / "mlb-spread-live-edge.json",
    )
    settle.add_argument("--report-json", type=Path, default=ROOT / "live-test" / "mlb-spread-settlement.json")
    settle.add_argument("--report-md", type=Path, default=ROOT / "live-test" / "mlb-spread-settlement.md")
    settle.set_defaults(handler=_handle_settle)

    timestamp_audit = subparsers.add_parser(
        "timestamp-audit",
        help="Audit whether ESPN odds payloads expose upstream source timestamps.",
    )
    timestamp_audit.add_argument("--no-network", action="store_true")
    timestamp_audit.add_argument("--espn-date", default="20260604")
    timestamp_audit.add_argument("--espn-event-id", default=None)
    timestamp_audit.add_argument(
        "--report-json",
        type=Path,
        default=ROOT / "live-test" / "mlb-spread-espn-odds-timestamp-audit.json",
    )
    timestamp_audit.add_argument(
        "--report-md",
        type=Path,
        default=ROOT / "live-test" / "mlb-spread-espn-odds-timestamp-audit.md",
    )
    timestamp_audit.set_defaults(handler=_handle_timestamp_audit)

    bench = subparsers.add_parser("bench", help="Measure public endpoint latency and local valuation compute.")
    _add_common(bench)
    bench.add_argument("--espn-date", default="20260603")
    bench.add_argument("--espn-event-id", default=None)
    bench.add_argument("--series-ticker", default="KXMLBSPREAD")
    bench.add_argument("--prefer-provider-contains", default="live")
    bench.add_argument("--skip-orderbooks", action="store_true")
    bench.add_argument("--selective-orderbooks", action="store_true")
    bench.add_argument("--orderbook-pause-seconds", type=float, default=0.05)
    bench.add_argument("--orderbook-concurrency", type=int, default=1)
    bench.add_argument("--compute-iterations", type=int, default=1000)
    bench.add_argument("--report-json", type=Path, default=ROOT / "live-test" / "mlb-spread-bench.json")
    bench.add_argument("--report-md", type=Path, default=ROOT / "live-test" / "mlb-spread-bench.md")
    bench.set_defaults(handler=_handle_bench)

    readiness = subparsers.add_parser("readiness", help="Fail-closed production-readiness gate over evidence reports.")
    readiness.add_argument(
        "--validation-report-json",
        type=Path,
        default=ROOT / "live-test" / "mlb-spread-live-edge-strict-100c.json",
    )
    readiness.add_argument(
        "--markout-report-json",
        type=Path,
        default=ROOT / "live-test" / "mlb-spread-live-edge-strict-100c-markout.json",
    )
    readiness.add_argument(
        "--settlement-report-json",
        type=Path,
        default=ROOT / "live-test" / "mlb-spread-live-edge-strict-100c-settlement.json",
    )
    readiness.add_argument(
        "--bench-report-json",
        type=Path,
        default=ROOT / "live-test" / "mlb-spread-bench.json",
    )
    readiness.add_argument(
        "--signals-jsonl",
        type=Path,
        default=ROOT / "live-test" / "mlb-spread-live-edge-strict-100c-signals.jsonl",
    )
    readiness.add_argument("--min-markout-rows", type=int, default=50)
    readiness.add_argument("--min-positive-markout-rate", type=float, default=0.55)
    readiness.add_argument("--min-mean-markout-after-fee", type=float, default=0.0)
    readiness.add_argument("--min-markout-entry-age-seconds", type=float, default=60.0)
    readiness.add_argument("--min-settled-rows", type=int, default=20)
    readiness.add_argument("--min-mean-settlement-pnl", type=float, default=0.0)
    readiness.add_argument("--max-end-to-end-ms", type=float, default=1000.0)
    readiness.add_argument("--allow-not-ready", action="store_true")
    readiness.add_argument(
        "--report-json",
        type=Path,
        default=ROOT / "live-test" / "mlb-spread-production-readiness.json",
    )
    readiness.add_argument(
        "--report-md",
        type=Path,
        default=ROOT / "live-test" / "mlb-spread-production-readiness.md",
    )
    readiness.set_defaults(handler=_handle_readiness)

    latest = subparsers.add_parser("render-latest", help="Print an existing report summary and markdown.")
    latest.add_argument("--report-json", type=Path, default=ROOT / "live-test" / "mlb-spread-live-edge.json")
    latest.add_argument("--report-md", type=Path, default=ROOT / "live-test" / "mlb-spread-live-edge.md")
    latest.set_defaults(handler=_handle_render_latest)
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
