"""Build and capture pre-round golf top-N research data.

Read-only. This script never calls order, cancel, or live-submit paths.

Subcommands:
* ``build-csv`` joins point-in-time feature rows, labels, public Kalshi
  snapshots, and optional odds priors into ``golf_preround.py``'s data schema.
* ``capture-kalshi`` records public Kalshi top-N market snapshots to CSV.
* ``fetch-odds`` fetches and de-vigs The Odds API golf outright boards as
  reference priors. These are not direct top-N odds unless the provider market
  type says so.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "python" / "src"))

from eventcontracts.research.golf_live_paper import (  # noqa: E402
    build_historical_golf_dataset,
    fixture_historical_inputs,
)
from eventcontracts.research.golf_preround_data import (  # noqa: E402
    build_preround_topn_dataset,
    fixture_input_rows,
    normalize_decimal_odds_rows,
    read_csv_rows,
    write_odds_rows,
    write_snapshot_rows,
)

KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"
ODDS_API = "https://api.the-odds-api.com/v4"
TOP_N_SERIES = ("KXPGATOP5", "KXPGATOP10", "KXPGATOP20", "KXPGATOP40", "KXLIVTOP5", "KXLIVTOP10")
DEFAULT_OUT = ROOT / "data" / "golf" / "preround_top20.csv"


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    return int(args.handler(args))


def _handle_build_csv(args: argparse.Namespace) -> int:
    features: Sequence[Mapping[str, object]]
    labels: Sequence[Mapping[str, object]]
    snapshots: Sequence[Mapping[str, object]]
    odds: Sequence[Mapping[str, object]]
    if args.no_network:
        features, labels, snapshots, odds = fixture_input_rows()
    else:
        if args.features_csv is None or args.labels_csv is None:
            raise SystemExit("--features-csv and --labels-csv are required unless --no-network is set")
        features = read_csv_rows(args.features_csv)
        labels = read_csv_rows(args.labels_csv)
        snapshots = read_csv_rows(args.kalshi_snapshots_csv) if args.kalshi_snapshots_csv is not None else []
        odds = read_csv_rows(args.odds_csv) if args.odds_csv is not None else []
    report = build_preround_topn_dataset(
        feature_rows=features,
        label_rows=labels,
        snapshot_rows=snapshots,
        odds_rows=odds,
        out=args.out,
        top_n=args.top_n,
    )
    if args.report_json is not None:
        args.report_json.parent.mkdir(parents=True, exist_ok=True)
        args.report_json.write_text(json.dumps(report.as_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report.as_dict(), indent=2, sort_keys=True))
    return 0


def _handle_capture_kalshi(args: argparse.Namespace) -> int:
    rows = _capture_kalshi_topn(max_pages_per_series=args.max_pages_per_series)
    write_snapshot_rows(args.out, rows)
    print(json.dumps({"rows_written": len(rows), "out": str(args.out)}, indent=2, sort_keys=True))
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
        args.report_json.parent.mkdir(parents=True, exist_ok=True)
        args.report_json.write_text(json.dumps(report.as_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report.as_dict(), indent=2, sort_keys=True))
    return 0


def _handle_fetch_odds(args: argparse.Namespace) -> int:
    if args.no_network:
        _features, _labels, _snapshots, odds = fixture_input_rows()
        rows = odds
    else:
        key = args.api_key or _env_value("THE_ODDS_API_KEY", ROOT / ".env")
        if not key:
            raise SystemExit("set THE_ODDS_API_KEY or pass --api-key, or use --no-network")
        rows = _fetch_golf_outright_odds(api_key=key, sport=args.sport, regions=args.regions)
    normalized = normalize_decimal_odds_rows(rows)
    write_odds_rows(args.out, normalized)
    print(json.dumps({"rows_written": len(normalized), "out": str(args.out)}, indent=2, sort_keys=True))
    return 0


def _capture_kalshi_topn(*, max_pages_per_series: int) -> list[dict[str, object]]:
    captured_at = datetime.now(UTC).isoformat()
    rows: list[dict[str, object]] = []
    for series in TOP_N_SERIES:
        cursor: str | None = None
        for _page in range(max_pages_per_series):
            params = {"limit": "200", "series_ticker": series}
            if cursor:
                params["cursor"] = cursor
            data = _get_json(KALSHI_API + "/markets", params=params)
            markets = [market for market in data.get("markets", []) if isinstance(market, dict)]
            for market in markets:
                if market.get("status") not in {"active", "open"}:
                    continue
                rows.append(_snapshot_from_market(market, captured_at=captured_at))
            raw_cursor = data.get("cursor")
            cursor = raw_cursor if isinstance(raw_cursor, str) and raw_cursor else None
            time.sleep(0.35)
            if cursor is None:
                break
    return rows


def _snapshot_from_market(market: dict[str, Any], *, captured_at: str) -> dict[str, object]:
    ticker = str(market.get("ticker") or "")
    event_ticker = str(market.get("event_ticker") or "")
    player_id = ticker.rsplit("-", 1)[-1] if "-" in ticker else ticker
    title = str(market.get("title") or "")
    yes_sub_title = str(market.get("yes_sub_title") or "")
    no_sub_title = str(market.get("no_sub_title") or "")
    player_name = _player_name_from_market(title=title, yes_sub_title=yes_sub_title, no_sub_title=no_sub_title)
    return {
        "captured_at": captured_at,
        "tournament_id": event_ticker,
        "player_id": player_id,
        "player_name": player_name,
        "market_ticker": ticker,
        "yes_sub_title": yes_sub_title,
        "no_sub_title": no_sub_title,
        "title": title,
        "rules_primary": market.get("rules_primary") or "",
        "yes_bid": market.get("yes_bid_dollars") or market.get("yes_bid"),
        "yes_ask": market.get("yes_ask_dollars") or market.get("yes_ask"),
        "yes_bid_size": market.get("yes_bid_size_fp") or market.get("yes_bid_size"),
        "yes_ask_size": market.get("yes_ask_size_fp") or market.get("yes_ask_size"),
        "volume": market.get("volume_fp") or market.get("volume"),
        "volume_24h": market.get("volume_24h_fp") or market.get("volume_24h"),
        "open_interest": market.get("open_interest_fp") or market.get("open_interest"),
        "market_status": market.get("status"),
        "expected_expiration_time": market.get("expected_expiration_time"),
        "close_time": market.get("close_time"),
    }


def _player_name_from_market(*, title: str, yes_sub_title: str, no_sub_title: str) -> str:
    for candidate in (yes_sub_title, no_sub_title):
        if candidate.strip():
            return candidate.strip()
    cleaned = title.strip()
    if cleaned.lower().startswith("will "):
        cleaned = cleaned[5:]
    for marker in (" finish ", " win ", " make ", " be "):
        index = cleaned.lower().find(marker)
        if index > 0:
            return cleaned[:index].strip(" ?")
    return cleaned


def _fetch_golf_outright_odds(*, api_key: str, sport: str, regions: str) -> list[dict[str, object]]:
    data = _get_json(
        f"{ODDS_API}/sports/{sport}/odds/",
        params={"apiKey": api_key, "regions": regions, "markets": "outrights", "oddsFormat": "decimal"},
    )
    rows: list[dict[str, object]] = []
    if not isinstance(data, list):
        return rows
    for event in data:
        if not isinstance(event, dict):
            continue
        tournament_id = str(event.get("id") or sport)
        for book in event.get("bookmakers", []):
            if not isinstance(book, dict):
                continue
            source = str(book.get("key") or book.get("title") or "unknown")
            odds_as_of = str(book.get("last_update") or datetime.now(UTC).isoformat())
            for market in book.get("markets", []):
                if not isinstance(market, dict) or market.get("key") != "outrights":
                    continue
                outcomes = market.get("outcomes")
                if not isinstance(outcomes, list):
                    continue
                for outcome in outcomes:
                    if not isinstance(outcome, dict):
                        continue
                    player_name = str(outcome.get("name") or "")
                    rows.append(
                        {
                            "source": source,
                            "tournament_id": tournament_id,
                            "player_id": _player_key(player_name),
                            "player_name": player_name,
                            "odds_as_of": odds_as_of,
                            "market_type": "outright_reference",
                            "decimal_odds": outcome.get("price"),
                            "odds_probability": "",
                            "reference_price_source": f"{source}:outright_reference",
                            "overround": "",
                        }
                    )
    return rows


def _get_json(url: str, *, params: dict[str, str]) -> Any:
    query = urllib.parse.urlencode(params)
    full_url = f"{url}?{query}" if query else url
    last_error: Exception | None = None
    for attempt in range(5):
        try:
            request = urllib.request.Request(full_url, headers={"User-Agent": "eventcontracts-golf-preround-data/0.1"})
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


def _env_value(key: str, env_path: Path) -> str | None:
    value = os.getenv(key)
    if value:
        return value
    if not env_path.exists():
        return None
    prefix = f"{key}="
    for line in env_path.read_text(encoding="utf-8").splitlines():
        if line.startswith(prefix):
            parsed = line.split("=", 1)[1].strip().strip('"').strip("'")
            return parsed or None
    return None


def _player_key(name: str) -> str:
    compact = "".join(ch for ch in name.lower() if ch.isalnum())
    return compact or "unknown"


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser("build-csv", help="Build research-ready top-N CSV.")
    build.add_argument("--no-network", action="store_true")
    build.add_argument("--features-csv", type=Path, default=None)
    build.add_argument("--labels-csv", type=Path, default=None)
    build.add_argument("--kalshi-snapshots-csv", type=Path, default=None)
    build.add_argument("--odds-csv", type=Path, default=None)
    build.add_argument("--out", type=Path, default=DEFAULT_OUT)
    build.add_argument("--top-n", type=int, default=20)
    build.add_argument("--report-json", type=Path, default=None)
    build.set_defaults(handler=_handle_build_csv)

    historical = subparsers.add_parser(
        "build-historical",
        help="Build research CSVs for top-N, player make-cut, or exact cut-line historical rows.",
    )
    historical.add_argument("--no-network", action="store_true")
    historical.add_argument("--family", choices=("top_n", "make_cut", "cut_line"), required=True)
    historical.add_argument("--features-csv", type=Path, default=None)
    historical.add_argument("--labels-csv", type=Path, default=None)
    historical.add_argument("--kalshi-snapshots-csv", type=Path, default=None)
    historical.add_argument("--out", type=Path, required=True)
    historical.add_argument("--report-json", type=Path, default=None)
    historical.set_defaults(handler=_handle_build_historical)

    capture = subparsers.add_parser("capture-kalshi", help="Capture public Kalshi golf top-N snapshots.")
    capture.add_argument("--out", type=Path, default=ROOT / "data" / "golf" / "kalshi_topn_snapshots.csv")
    capture.add_argument("--max-pages-per-series", type=int, default=2)
    capture.set_defaults(handler=_handle_capture_kalshi)

    odds = subparsers.add_parser("fetch-odds", help="Fetch/de-vig golf outright reference odds.")
    odds.add_argument("--no-network", action="store_true")
    odds.add_argument("--api-key", default=None)
    odds.add_argument("--sport", default="golf_us_open_winner")
    odds.add_argument("--regions", default="us,uk,eu")
    odds.add_argument("--out", type=Path, default=ROOT / "data" / "golf" / "golf_reference_odds.csv")
    odds.set_defaults(handler=_handle_fetch_odds)

    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
