"""Golf no-trade live-paper bridge and shadow-fill tools.

Research-only. This script reads public market data or fixture inputs and writes
mapping, historical CSV, and hypothetical shadow-fill ledgers. It never submits,
cancels, replaces, or live-submits orders.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "python" / "src"))

from eventcontracts.research.golf_live_paper import (  # noqa: E402
    build_historical_golf_dataset,
    fixture_historical_inputs,
    fixture_market_payloads,
    fixture_shadow_inputs,
    map_kalshi_golf_markets,
    read_csv_rows,
    render_shadow_fill_summary_markdown,
    summarize_shadow_fill_ledger,
    write_json_report,
    write_market_map_csv,
    write_shadow_fill_ledger,
)

KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"
DEFAULT_SERIES = ("KXPGATOP20", "KXPGATOP10", "KXPGATOP5", "KXPGAMAKECUT", "KXPGACUTLINE")


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    return int(args.handler(args))


def _handle_map_markets(args: argparse.Namespace) -> int:
    markets = (
        fixture_market_payloads()
        if args.no_network
        else _fetch_markets(_split_csv(args.series_tickers), args.max_pages_per_series)
    )
    rows = map_kalshi_golf_markets(markets)
    write_market_map_csv(args.out, rows)
    payload = {
        "rows_written": len(rows),
        "out": str(args.out),
        "families": _family_counts(rows),
        "no_network": bool(args.no_network),
        "decision_gate": (
            "mapping only; validate settlement rules and historical OOS before tick logging or paper promotion"
        ),
    }
    if args.report_json is not None:
        write_json_report(args.report_json, payload)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def _handle_build_historical(args: argparse.Namespace) -> int:
    features: Sequence[Mapping[str, object]]
    labels: Sequence[Mapping[str, object]]
    snapshots: Sequence[Mapping[str, object]]
    if args.no_network:
        features, labels, snapshots = fixture_historical_inputs(args.family)
    else:
        if args.features_csv is None or args.labels_csv is None:
            raise SystemExit("--features-csv and --labels-csv are required unless --no-network is set")
        features = read_csv_rows(args.features_csv)
        labels = read_csv_rows(args.labels_csv)
        snapshots = read_csv_rows(args.kalshi_snapshots_csv) if args.kalshi_snapshots_csv is not None else []
    report = build_historical_golf_dataset(
        feature_rows=features,
        label_rows=labels,
        snapshot_rows=snapshots,
        out=args.out,
        market_family=args.family,
    )
    if args.report_json is not None:
        write_json_report(args.report_json, report.as_dict())
    print(json.dumps(report.as_dict(), indent=2, sort_keys=True))
    return 0


def _handle_shadow_fill(args: argparse.Namespace) -> int:
    intents: Sequence[Mapping[str, object]]
    quotes: Sequence[Mapping[str, object]]
    trades: Sequence[Mapping[str, object]]
    settlements: Sequence[Mapping[str, object]]
    if args.no_network:
        intents, quotes, trades, settlements = fixture_shadow_inputs()
    else:
        if args.intents_csv is None or args.quotes_csv is None:
            raise SystemExit("--intents-csv and --quotes-csv are required unless --no-network is set")
        intents = read_csv_rows(args.intents_csv)
        quotes = read_csv_rows(args.quotes_csv)
        trades = read_csv_rows(args.trades_csv) if args.trades_csv is not None else []
        settlements = read_csv_rows(args.settlements_csv) if args.settlements_csv is not None else []
    report = write_shadow_fill_ledger(
        intents=intents,
        quotes=quotes,
        trades=trades,
        settlements=settlements,
        out=args.out,
        max_quote_age_ms=args.max_quote_age_ms,
        min_net_edge=args.min_net_edge,
    )
    if args.report_json is not None:
        write_json_report(args.report_json, report.as_dict())
    print(json.dumps(report.as_dict(), indent=2, sort_keys=True))
    return 0


def _handle_bridge_once(args: argparse.Namespace) -> int:
    """Run a deterministic no-trade bridge pass from CSVs or fixtures."""

    run_dir = args.out
    run_dir.mkdir(parents=True, exist_ok=True)
    intents: Sequence[Mapping[str, object]]
    quotes: Sequence[Mapping[str, object]]
    trades: Sequence[Mapping[str, object]]
    settlements: Sequence[Mapping[str, object]]
    if args.no_network:
        markets = fixture_market_payloads()
        map_rows = map_kalshi_golf_markets(markets)
        write_market_map_csv(run_dir / "market_map.csv", map_rows)
        intents, quotes, trades, settlements = fixture_shadow_inputs()
    else:
        if args.intents_csv is None or args.quotes_csv is None:
            raise SystemExit("--intents-csv and --quotes-csv are required unless --no-network is set")
        if args.market_map_csv is not None:
            # Preserve the operator-supplied map inside the run directory for auditability.
            (run_dir / "market_map.csv").write_text(args.market_map_csv.read_text(encoding="utf-8"), encoding="utf-8")
        intents = read_csv_rows(args.intents_csv)
        quotes = read_csv_rows(args.quotes_csv)
        trades = read_csv_rows(args.trades_csv) if args.trades_csv is not None else []
        settlements = read_csv_rows(args.settlements_csv) if args.settlements_csv is not None else []
    shadow_report = write_shadow_fill_ledger(
        intents=intents,
        quotes=quotes,
        trades=trades,
        settlements=settlements,
        out=run_dir / "shadow_fills.jsonl",
        max_quote_age_ms=args.max_quote_age_ms,
        min_net_edge=args.min_net_edge,
    )
    manifest = {
        "kind": "golf-live-paper-bridge",
        "no_trade": True,
        "no_network": bool(args.no_network),
        "shadow_fill_report": shadow_report.as_dict(),
        "limits": [
            "hypothetical fills only",
            "no order submit/cancel/replace/live-submit path",
            "public quote/trade evidence is incomplete until WS depth and settlement are reconciled",
        ],
    }
    write_json_report(run_dir / "manifest.json", manifest)
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


def _handle_summarize_shadow_fill(args: argparse.Namespace) -> int:
    summary = summarize_shadow_fill_ledger(ledger_path=args.ledger, fixture_mode=args.fixture_mode)
    payload = summary.as_dict()
    if args.report_json is not None:
        write_json_report(args.report_json, payload)
    if args.report_md is not None:
        args.report_md.parent.mkdir(parents=True, exist_ok=True)
        args.report_md.write_text(render_shadow_fill_summary_markdown(summary), encoding="utf-8")
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def _fetch_markets(series_tickers: Sequence[str], max_pages_per_series: int) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for series in series_tickers:
        cursor: str | None = None
        for _page in range(max_pages_per_series):
            params = {"limit": "200", "series_ticker": series}
            if cursor:
                params["cursor"] = cursor
            data = _get_json(KALSHI_API + "/markets", params=params)
            rows.extend(market for market in data.get("markets", []) if isinstance(market, dict))
            raw_cursor = data.get("cursor")
            cursor = raw_cursor if isinstance(raw_cursor, str) and raw_cursor else None
            time.sleep(0.35)
            if cursor is None:
                break
    return rows


def _get_json(url: str, *, params: Mapping[str, str]) -> Any:
    query = urllib.parse.urlencode(params)
    full_url = f"{url}?{query}" if query else url
    last_error: Exception | None = None
    for attempt in range(5):
        try:
            request = urllib.request.Request(full_url, headers={"User-Agent": "eventcontracts-golf-live-paper/0.1"})
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code == 429:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise
        except Exception as exc:
            last_error = exc
            time.sleep(0.5 * (attempt + 1))
    raise RuntimeError(f"GET failed for {url}: {last_error}")


def _family_counts(rows: Sequence[Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        family = str(row.market_family)
        counts[family] = counts.get(family, 0) + 1
    return counts


def _split_csv(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in value.split(",") if part.strip())


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    mapper = subparsers.add_parser("map-markets", help="Map public Kalshi golf tickers to deterministic subject ids.")
    mapper.add_argument("--no-network", action="store_true")
    mapper.add_argument("--series-tickers", default=",".join(DEFAULT_SERIES))
    mapper.add_argument("--max-pages-per-series", type=int, default=2)
    mapper.add_argument("--out", type=Path, default=ROOT / "data" / "golf" / "market_map.csv")
    mapper.add_argument("--report-json", type=Path, default=None)
    mapper.set_defaults(handler=_handle_map_markets)

    historical = subparsers.add_parser("build-historical", help="Build top-N, make-cut, or cut-line historical CSV.")
    historical.add_argument("--no-network", action="store_true")
    historical.add_argument("--family", choices=("top_n", "make_cut", "cut_line"), required=True)
    historical.add_argument("--features-csv", type=Path, default=None)
    historical.add_argument("--labels-csv", type=Path, default=None)
    historical.add_argument("--kalshi-snapshots-csv", type=Path, default=None)
    historical.add_argument("--out", type=Path, required=True)
    historical.add_argument("--report-json", type=Path, default=None)
    historical.set_defaults(handler=_handle_build_historical)

    shadow = subparsers.add_parser("shadow-fill", help="Write a no-trade hypothetical fill ledger.")
    shadow.add_argument("--no-network", action="store_true")
    shadow.add_argument("--intents-csv", type=Path, default=None)
    shadow.add_argument("--quotes-csv", type=Path, default=None)
    shadow.add_argument("--trades-csv", type=Path, default=None)
    shadow.add_argument("--settlements-csv", type=Path, default=None)
    shadow.add_argument("--out", type=Path, required=True)
    shadow.add_argument("--report-json", type=Path, default=None)
    shadow.add_argument("--max-quote-age-ms", type=int, default=60_000)
    shadow.add_argument("--min-net-edge", type=float, default=0.05)
    shadow.set_defaults(handler=_handle_shadow_fill)

    bridge = subparsers.add_parser("bridge-once", help="Run one no-trade golf live-paper bridge pass.")
    bridge.add_argument("--no-network", action="store_true")
    bridge.add_argument("--market-map-csv", type=Path, default=None)
    bridge.add_argument("--intents-csv", type=Path, default=None)
    bridge.add_argument("--quotes-csv", type=Path, default=None)
    bridge.add_argument("--trades-csv", type=Path, default=None)
    bridge.add_argument("--settlements-csv", type=Path, default=None)
    bridge.add_argument("--out", type=Path, required=True)
    bridge.add_argument("--max-quote-age-ms", type=int, default=60_000)
    bridge.add_argument("--min-net-edge", type=float, default=0.05)
    bridge.set_defaults(handler=_handle_bridge_once)

    summary = subparsers.add_parser(
        "summarize-shadow-fill",
        help="Summarize a no-trade shadow-fill JSONL ledger into decision-gate evidence.",
    )
    summary.add_argument("--ledger", type=Path, required=True)
    summary.add_argument("--report-json", type=Path, default=None)
    summary.add_argument("--report-md", type=Path, default=None)
    summary.add_argument(
        "--fixture-mode",
        action="store_true",
        help="Label the summary as fixture/no-network evidence that cannot promote logging or paper.",
    )
    summary.set_defaults(handler=_handle_summarize_shadow_fill)

    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
