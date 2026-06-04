"""Run the read-only NBA spread sharp-reference validator.

The live path fetches public ESPN odds/scoreboard data and public Kalshi market
data only. It never submits, cancels, replaces, or live-submits orders. Candidate
rows are paper/shadow signals for CLV and settlement validation.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "python" / "src"))

from eventcontracts.research.nba_spread import (  # noqa: E402
    NbaSpreadValidationConfig,
    evaluate_spread_ladder,
    fixture_anchor,
    fixture_game_state,
    fixture_markets,
    markout_report_from_entry_report,
    parse_espn_live_odds_anchor,
    parse_kalshi_spread_market,
    parse_nba_game_state,
    settlement_report_from_entry_report,
    write_markout_outputs,
    write_report_outputs,
    write_settlement_outputs,
)

KALSHI_MARKETS_URL = "https://api.elections.kalshi.com/trade-api/v2/markets"
ESPN_SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
ESPN_CORE_EVENT_URL = "https://sports.core.api.espn.com/v2/sports/basketball/leagues/nba/events"


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    return int(args.handler(args))


def _handle_validate_once(args: argparse.Namespace) -> int:
    config = _config_from_args(args)
    as_of = datetime.now(UTC)
    if args.no_network:
        game = fixture_game_state()
        anchor = fixture_anchor()
        markets = fixture_markets()
    else:
        scoreboard = _fetch_scoreboard(date=args.espn_date)
        event = _select_event(scoreboard, event_id=args.espn_event_id)
        game = parse_nba_game_state(event, received_at=as_of)
        odds_payload = _fetch_espn_odds(game.event_id)
        anchor = parse_espn_live_odds_anchor(
            odds_payload,
            received_at=as_of,
            prefer_provider_contains=args.prefer_provider_contains,
        )
        markets_payload = _fetch_kalshi_markets(args.series_ticker)
        orderbooks = (
            _fetch_orderbooks(
                [str(row.get("ticker") or "") for row in _market_rows(markets_payload)],
                pause_seconds=args.orderbook_pause_seconds,
            )
            if not args.skip_orderbooks
            else {}
        )
        markets = tuple(
            quote
            for quote in (
                parse_kalshi_spread_market(
                    row,
                    game=game,
                    received_at=as_of,
                    orderbook=orderbooks.get(str(row.get("ticker") or "")),
                )
                for row in _market_rows(markets_payload)
            )
            if quote is not None
        )

    report = evaluate_spread_ladder(
        game=game,
        anchor=anchor,
        markets=markets,
        config=config,
        as_of=as_of,
    )
    write_report_outputs(
        report,
        report_json=args.report_json,
        report_md=args.report_md,
        signals_jsonl=args.signals_jsonl_out,
    )
    print(json.dumps(report.as_dict()["summary"], indent=2, sort_keys=True))
    return 0


def _handle_render_latest(args: argparse.Namespace) -> int:
    payload = json.loads(args.report_json.read_text(encoding="utf-8"))
    print(json.dumps(payload["summary"], indent=2, sort_keys=True))
    print(args.report_md.read_text(encoding="utf-8") if args.report_md.exists() else "")
    return 0


def _handle_markout(args: argparse.Namespace) -> int:
    as_of = datetime.now(UTC)
    entry_report = json.loads(args.entry_report_json.read_text(encoding="utf-8"))
    if args.no_network:
        game = fixture_game_state()
        current_quotes = {quote.ticker: quote for quote in fixture_markets()}
    else:
        scoreboard = _fetch_scoreboard(date=args.espn_date)
        event = _select_event(scoreboard, event_id=args.espn_event_id)
        game = parse_nba_game_state(event, received_at=as_of)
        markets_payload = _fetch_kalshi_markets(args.series_ticker)
        orderbooks = (
            _fetch_orderbooks(
                [str(row.get("ticker") or "") for row in _market_rows(markets_payload)],
                pause_seconds=args.orderbook_pause_seconds,
            )
            if not args.skip_orderbooks
            else {}
        )
        quotes = (
            parse_kalshi_spread_market(
                row,
                game=game,
                received_at=as_of,
                orderbook=orderbooks.get(str(row.get("ticker") or "")),
            )
            for row in _market_rows(markets_payload)
        )
        current_quotes = {quote.ticker: quote for quote in quotes if quote is not None}
    report = markout_report_from_entry_report(
        entry_report,
        current_quotes=current_quotes,
        as_of=as_of,
        entry_report_name=str(args.entry_report_json),
    )
    write_markout_outputs(report, report_json=args.report_json, report_md=args.report_md)
    print(json.dumps(report.as_dict()["summary"], indent=2, sort_keys=True))
    return 0


def _handle_settle(args: argparse.Namespace) -> int:
    as_of = datetime.now(UTC)
    entry_report = json.loads(args.entry_report_json.read_text(encoding="utf-8"))
    if args.no_network:
        game = fixture_game_state()
    else:
        scoreboard = _fetch_scoreboard(date=args.espn_date)
        event = _select_event(scoreboard, event_id=args.espn_event_id)
        game = parse_nba_game_state(event, received_at=as_of)
    report = settlement_report_from_entry_report(
        entry_report,
        game=game,
        as_of=as_of,
        entry_report_name=str(args.entry_report_json),
    )
    write_settlement_outputs(report, report_json=args.report_json, report_md=args.report_md)
    print(json.dumps(report.as_dict()["summary"], indent=2, sort_keys=True))
    return 0


def _config_from_args(args: argparse.Namespace) -> NbaSpreadValidationConfig:
    return NbaSpreadValidationConfig(
        min_net_edge=args.min_net_edge,
        min_executable_size=args.min_executable_size,
        max_source_age_seconds=args.max_source_age_seconds,
        max_scoreboard_win_probability_disagreement=args.max_scoreboard_win_probability_disagreement,
        paper_contracts=args.paper_contracts,
        fee_coeff=args.fee_coeff,
        slippage=args.slippage,
    )


def _fetch_scoreboard(*, date: str) -> Mapping[str, object]:
    url = f"{ESPN_SCOREBOARD_URL}?dates={urllib.parse.quote(date)}"
    return _fetch_json(url)


def _fetch_espn_odds(event_id: str) -> Mapping[str, object]:
    quoted = urllib.parse.quote(event_id)
    url = f"{ESPN_CORE_EVENT_URL}/{quoted}/competitions/{quoted}/odds?lang=en&region=us"
    return _fetch_json(url)


def _fetch_kalshi_markets(series_ticker: str) -> Mapping[str, object]:
    params = urllib.parse.urlencode({"series_ticker": series_ticker, "status": "open", "limit": "1000"})
    return _fetch_json(f"{KALSHI_MARKETS_URL}?{params}")


def _fetch_orderbooks(tickers: Sequence[str], *, pause_seconds: float) -> dict[str, Mapping[str, object]]:
    out: dict[str, Mapping[str, object]] = {}
    for ticker in tickers:
        if not ticker:
            continue
        quoted = urllib.parse.quote(ticker, safe="")
        url = f"{KALSHI_MARKETS_URL}/{quoted}/orderbook"
        try:
            out[ticker] = _fetch_json(url)
        except Exception as exc:  # noqa: BLE001 - missing depth should not kill a read-only valuation pass
            print(f"warning: orderbook fetch failed for {ticker}: {type(exc).__name__}", file=sys.stderr)
        if pause_seconds > 0.0:
            time.sleep(pause_seconds)
    return out


def _fetch_json(url: str, *, timeout: float = 30.0, tries: int = 4) -> Mapping[str, object]:
    last_exc: Exception | None = None
    for idx in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                payload = json.loads(response.read())
            if isinstance(payload, Mapping):
                return payload
            raise ValueError("response was not a JSON object")
        except Exception as exc:  # noqa: BLE001 - retry public endpoints conservatively
            last_exc = exc
            if idx < tries - 1:
                time.sleep(1.0 + idx)
    assert last_exc is not None
    raise last_exc


def _select_event(payload: Mapping[str, object], *, event_id: str | None) -> Mapping[str, object]:
    events = payload.get("events")
    if not isinstance(events, Sequence) or isinstance(events, str) or not events:
        raise ValueError("scoreboard payload has no events")
    for event in events:
        if not isinstance(event, Mapping):
            continue
        if event_id is None or str(event.get("id") or "") == event_id:
            return event
    raise ValueError(f"event_id not found in ESPN scoreboard: {event_id}")


def _market_rows(payload: Mapping[str, object]) -> Sequence[Mapping[str, object]]:
    rows = payload.get("markets")
    if not isinstance(rows, Sequence) or isinstance(rows, str):
        raise ValueError("Kalshi markets payload has no markets list")
    out: list[Mapping[str, object]] = []
    for row in rows:
        if isinstance(row, Mapping):
            out.append(row)
    return out


def _add_common(subparser: argparse.ArgumentParser) -> None:
    subparser.add_argument("--no-network", action="store_true")
    subparser.add_argument("--min-net-edge", type=float, default=0.015)
    subparser.add_argument("--min-executable-size", type=float, default=1.0)
    subparser.add_argument("--max-source-age-seconds", type=float, default=180.0)
    subparser.add_argument("--max-scoreboard-win-probability-disagreement", type=float, default=0.12)
    subparser.add_argument("--paper-contracts", type=int, default=5)
    subparser.add_argument("--fee-coeff", type=float, default=0.07)
    subparser.add_argument("--slippage", type=float, default=0.0)


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    subparsers = parser.add_subparsers(dest="command", required=True)

    once = subparsers.add_parser("validate-once", help="Run one read-only NBA spread validation pass.")
    _add_common(once)
    once.add_argument("--espn-date", default="20260603")
    once.add_argument("--espn-event-id", default=None)
    once.add_argument("--series-ticker", default="KXNBASPREAD")
    once.add_argument("--prefer-provider-contains", default="live")
    once.add_argument("--skip-orderbooks", action="store_true")
    once.add_argument("--orderbook-pause-seconds", type=float, default=0.05)
    once.add_argument("--report-json", type=Path, default=ROOT / "live-test" / "nba-spread-live-edge.json")
    once.add_argument("--report-md", type=Path, default=ROOT / "live-test" / "nba-spread-live-edge.md")
    once.add_argument(
        "--signals-jsonl-out",
        type=Path,
        default=ROOT / "live-test" / "nba-spread-live-edge-signals.jsonl",
    )
    once.set_defaults(handler=_handle_validate_once)

    markout = subparsers.add_parser("markout", help="Mark prior candidate entries to fresh public Kalshi bids.")
    markout.add_argument("--no-network", action="store_true")
    markout.add_argument("--entry-report-json", type=Path, required=True)
    markout.add_argument("--espn-date", default="20260603")
    markout.add_argument("--espn-event-id", default=None)
    markout.add_argument("--series-ticker", default="KXNBASPREAD")
    markout.add_argument("--skip-orderbooks", action="store_true")
    markout.add_argument("--orderbook-pause-seconds", type=float, default=0.05)
    markout.add_argument("--report-json", type=Path, default=ROOT / "live-test" / "nba-spread-markout.json")
    markout.add_argument("--report-md", type=Path, default=ROOT / "live-test" / "nba-spread-markout.md")
    markout.set_defaults(handler=_handle_markout)

    settle = subparsers.add_parser("settle", help="Settle prior candidate entries from ESPN final score.")
    settle.add_argument("--no-network", action="store_true")
    settle.add_argument("--entry-report-json", type=Path, required=True)
    settle.add_argument("--espn-date", default="20260603")
    settle.add_argument("--espn-event-id", default=None)
    settle.add_argument("--report-json", type=Path, default=ROOT / "live-test" / "nba-spread-settlement.json")
    settle.add_argument("--report-md", type=Path, default=ROOT / "live-test" / "nba-spread-settlement.md")
    settle.set_defaults(handler=_handle_settle)

    latest = subparsers.add_parser("render-latest", help="Print the latest report summary and markdown.")
    latest.add_argument("--report-json", type=Path, default=ROOT / "live-test" / "nba-spread-live-edge.json")
    latest.add_argument("--report-md", type=Path, default=ROOT / "live-test" / "nba-spread-live-edge.md")
    latest.set_defaults(handler=_handle_render_latest)
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
