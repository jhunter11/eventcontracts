"""Pre-round golf top-N research runner.

Read-only. Live mode uses public Kalshi market snapshots and optional odds
provider reads. It never calls order, cancel, or live-submit paths. Use
``--no-network`` for a deterministic fixture self-test.
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
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "python" / "src"))

from eventcontracts.research.golf_preround import (  # noqa: E402
    MARKET_STRUCTURE_SERIES,
    fixture_kalshi_market_snapshots,
    flatten,
    load_preround_rows_csv,
    render_markdown_report,
    run_preround_research,
    select_best_market_structure,
    summarize_kalshi_golf_markets,
    synthetic_preround_fixture,
)

KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"
ODDS_API = "https://api.the-odds-api.com/v4"
DEFAULT_REPORT = ROOT / "live-test" / "golf-preround-research.md"


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    provider_status = _provider_status(ROOT / ".env")

    rows = synthetic_preround_fixture() if args.data_csv is None else load_preround_rows_csv(args.data_csv)
    report = run_preround_research(
        rows,
        target_top_n=args.top_n,
        simulations=args.simulations,
        seed=args.seed,
        provider_status=provider_status,
        fixture_mode=args.data_csv is None,
    )

    if args.no_network:
        market_snapshots = fixture_kalshi_market_snapshots()
        odds_note = (
            "no-network fixture: direct top-N odds_probability fields are used; "
            "live bookmaker odds were not requested"
        )
    else:
        market_snapshots = _fetch_kalshi_golf_markets(max_pages_per_series=args.max_pages_per_series)
        odds_note = _odds_note(provider_status)

    summaries = summarize_kalshi_golf_markets(market_snapshots)
    selection = select_best_market_structure(summaries)
    payload = {
        "market_selection": selection.as_dict(),
        "research_report": report.as_dict(),
        "odds_note": odds_note,
    }

    if args.json_out is not None:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        markdown = render_markdown_report(report, market_selection=selection, odds_note=odds_note)
        args.report.write_text(markdown, encoding="utf-8")

    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def _fetch_kalshi_golf_markets(*, max_pages_per_series: int) -> list[dict[str, Any]]:
    series = sorted(set(flatten(MARKET_STRUCTURE_SERIES.values())))
    all_markets: list[dict[str, Any]] = []
    for ticker in series:
        cursor: str | None = None
        for _page in range(max_pages_per_series):
            params = {"limit": "200", "series_ticker": ticker}
            if cursor:
                params["cursor"] = cursor
            data = _get_json(KALSHI_API + "/markets", params=params)
            batch = [market for market in data.get("markets", []) if isinstance(market, dict)]
            all_markets.extend(batch)
            raw_cursor = data.get("cursor")
            cursor = raw_cursor if isinstance(raw_cursor, str) and raw_cursor else None
            time.sleep(0.35)
            if cursor is None:
                break
    return [market for market in all_markets if market.get("status") in {"active", "open"}]


def _odds_note(provider_status: dict[str, bool]) -> str:
    if not provider_status.get("THE_ODDS_API_KEY", False):
        return "THE_ODDS_API_KEY absent; no live bookmaker odds requested"
    key = _env_value("THE_ODDS_API_KEY", ROOT / ".env")
    if not key:
        return "THE_ODDS_API_KEY absent from process/.env; no live bookmaker odds requested"
    try:
        sports = _get_json(ODDS_API + "/sports/", params={"apiKey": key})
        golf_sports = [
            item
            for item in sports
            if isinstance(item, dict)
            and "golf" in f"{item.get('key', '')} {item.get('title', '')} {item.get('group', '')}".lower()
        ]
        keys = ", ".join(str(item.get("key")) for item in golf_sports[:4])
        us_open = _get_json(
            ODDS_API + "/sports/golf_us_open_winner/odds/",
            params={
                "apiKey": key,
                "regions": "us,uk,eu",
                "markets": "outrights",
                "oddsFormat": "decimal",
            },
        )
        books = _book_overrounds(us_open)
        if books:
            best = min(books, key=lambda item: item["overround"])
            return (
                f"The Odds API exposes golf outright feeds ({keys}); no direct Memorial top-20 feed was found. "
                f"Best sampled US Open outright book={best['book']} overround={best['overround']:.4f}; "
                "use only as a player-skill/reference prior for top-N research."
            )
        return f"The Odds API exposes golf feeds ({keys}); no direct Memorial top-20 odds were found."
    except Exception as exc:
        return f"odds provider read failed: {exc}"


def _book_overrounds(events: object) -> list[dict[str, float | str]]:
    books: list[dict[str, float | str]] = []
    if not isinstance(events, list):
        return books
    for event in events:
        if not isinstance(event, dict):
            continue
        for book in event.get("bookmakers", []):
            if not isinstance(book, dict):
                continue
            outcomes: list[dict[str, Any]] = []
            for market in book.get("markets", []):
                if isinstance(market, dict) and market.get("key") == "outrights":
                    raw_outcomes = market.get("outcomes")
                    if isinstance(raw_outcomes, list):
                        outcomes = [item for item in raw_outcomes if isinstance(item, dict)]
            implied = 0.0
            for outcome in outcomes:
                raw_price = outcome.get("price")
                if raw_price is None:
                    continue
                try:
                    price = float(raw_price)
                except (TypeError, ValueError):
                    continue
                if price > 1.0:
                    implied += 1.0 / price
            if implied > 0.0:
                books.append({"book": str(book.get("key") or book.get("title") or "unknown"), "overround": implied})
    return books


def _get_json(url: str, *, params: dict[str, str]) -> Any:
    query = urllib.parse.urlencode(params)
    full_url = f"{url}?{query}" if query else url
    last_error: Exception | None = None
    for attempt in range(5):
        try:
            request = urllib.request.Request(full_url, headers={"User-Agent": "eventcontracts-golf-preround/0.1"})
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


def _provider_status(env_path: Path) -> dict[str, bool]:
    keys = ("DATAGOLF_API_KEY", "PGA_TOUR_API_KEY", "SHOTLINK_API_KEY", "THE_ODDS_API_KEY")
    return {key: bool(os.getenv(key) or _env_value(key, env_path)) for key in keys}


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


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-network", action="store_true", help="Use fixture market/data/odds inputs only.")
    parser.add_argument("--data-csv", type=Path, default=None, help="Point-in-time golf top-N rows CSV.")
    parser.add_argument("--top-n", type=int, default=20)
    parser.add_argument("--simulations", type=int, default=1500)
    parser.add_argument("--seed", type=int, default=11)
    parser.add_argument("--max-pages-per-series", type=int, default=2)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--json-out", type=Path, default=None)
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
