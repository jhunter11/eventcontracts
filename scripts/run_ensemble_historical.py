#!/usr/bin/env python3
"""Run the crypto signal ensemble on **real historical 1-minute data**.

Pulls one hour of Kalshi BTC bracket candlesticks plus Deribit
``BTC-PERPETUAL`` 1m close and BTC DVOL from the free public REST
endpoints (no API keys), assembles them into a ``NormalizedEvent``
stream, and runs the ensemble strategy through the entire stream.

Example::

    EVENTCONTRACTS_INSECURE_TLS=1 \\
      python3 scripts/run_ensemble_historical.py \\
        --expiry 26MAY2508 \\
        --atm-radius 1000 \\
        --min-confluence 1 \\
        --min-edge-bps 100

The script picks the bracket cluster within ``atm_radius`` dollars of
the BTC price at the start of the expiry. Strategy parameters are
auto-built from the discovered markets — the operator just supplies
the cohort token.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from collections import Counter
from decimal import Decimal
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "python" / "src"))

from eventcontracts.config import load_strategy_spec  # noqa: E402
from eventcontracts.crypto import HistoricalStream, build_historical_stream  # noqa: E402
from eventcontracts.domain import (  # noqa: E402
    Alert,
    NoAction,
    PlaceOrder,
    SleeveId,
)
from eventcontracts.strategy import create_from_spec  # noqa: E402
from eventcontracts.testing.doubles import InMemoryClock, InMemoryContext  # noqa: E402

CONFIGS = HERE.parent / "configs"


def _spec_strings_for(stream: HistoricalStream) -> dict[str, str]:
    """Derive the four operator-supplied roster strings from the real markets."""

    between_markets = [m for m in stream.kalshi_markets if m.kind == "between"]
    above_markets = [m for m in stream.kalshi_markets if m.kind == "above"]
    below_markets = [m for m in stream.kalshi_markets if m.kind == "below"]
    # Kalshi BTC partition: one "below" tail + N "between" interior + one "above" tail.
    parts: list[str] = []
    if below_markets:
        m = below_markets[0]
        parts.append(f"{m.ticker}:-inf:{m.upper}")
    for m in sorted(between_markets, key=lambda x: x.lower or Decimal("0")):
        parts.append(f"{m.ticker}:{m.lower}:{m.upper}")
    if above_markets:
        m = above_markets[0]
        parts.append(f"{m.ticker}:{m.lower}:inf")
    bracket_market_ids = ";".join(parts)

    # For vol_surface + skew (which need monotone P(S>=K)): use the
    # *derived* above-K markets the loader synthesized from the
    # cumulative bracket sum. These tickers (``DERIVED-A-<K>``) carry
    # well-defined ``P(S >= K)`` quotes and the real "T..." tails
    # exist far OTM where vol_surface is least useful.
    strike_map_parts = [
        f"{m.ticker}:{m.lower}" for m in stream.derived_above_markets
    ]
    strike_market_map = ";".join(strike_map_parts)

    return {
        "bracket_market_ids": bracket_market_ids,
        "strike_market_map": strike_market_map,
    }


def _build_ensemble(stream: HistoricalStream, args: argparse.Namespace):
    spec = load_strategy_spec(CONFIGS / "strategies" / "crypto-signal-ensemble.toml")
    merged = dict(spec.parameters)
    merged.update(_spec_strings_for(stream))
    merged.update(
        {
            "enabled_sources": args.enabled_sources,
            "min_confluence": args.min_confluence,
            "min_combined_edge_bps": args.min_edge_bps,
            "max_spread_bps": args.max_spread_bps,
            # Use only the data we actually have. Above markets are
            # rare on Kalshi crypto so default to disabling regime
            # (which needs a tail map) unless caller explicitly
            # configures it.
            "rv_window_samples": "30",
            "terminal_min_realized_samples": "10",
        }
    )
    spec = dataclasses.replace(spec, parameters=merged)
    return create_from_spec(spec)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--expiry",
        required=True,
        help="Kalshi expiry-hour token, e.g. '26MAY2508' for May 25 2026 12:00 UTC.",
    )
    parser.add_argument(
        "--atm-radius",
        type=int,
        default=500,
        help="Only fetch Kalshi brackets within this many $ of opening spot.",
    )
    parser.add_argument("--enabled-sources", default="parity,vol_surface,skew")
    parser.add_argument("--min-confluence", default="1")
    parser.add_argument("--min-edge-bps", default="50")
    parser.add_argument("--max-spread-bps", default="2000")
    args = parser.parse_args(argv)

    radius = Decimal(args.atm_radius) if args.atm_radius > 0 else None
    print(f"loading expiry {args.expiry} (atm_radius=${radius})...", file=sys.stderr)
    stream = build_historical_stream(
        expiry_hour_token=args.expiry,
        atm_radius_dollars=radius,
    )
    print(
        f"  events={len(stream.events)} markets={len(stream.kalshi_markets)} expiry={stream.expiry_at}",
        file=sys.stderr,
    )

    strategy = _build_ensemble(stream, args)
    clock = InMemoryClock()
    ctx = InMemoryContext(
        strategy_id_value=strategy.spec.strategy_id,
        sleeve_id_value=SleeveId("historical-sleeve"),
        clock_now=clock.current,
    )

    counter: Counter[str] = Counter()
    yes_edge_bps = Decimal("0")
    no_edge_bps = Decimal("0")
    market_decision_counts: Counter[str] = Counter()
    sample_alerts: list[str] = []
    for event in stream.events:
        ts = getattr(event, "received_at", None)
        if ts is None and hasattr(event, "quote"):
            ts = event.quote.received_at
        if ts is not None:
            clock.current = ts
            ctx.clock_now = ts
        for d in strategy.on_event(event, ctx):
            if isinstance(d, PlaceOrder):
                counter["place"] += 1
                counter[f"place_{d.outcome_side.value}"] += 1
                market_decision_counts[d.instrument_id.market_id] += 1
                if d.outcome_side.value == "yes":
                    yes_edge_bps += d.expected_edge_bps or Decimal("0")
                else:
                    no_edge_bps += d.expected_edge_bps or Decimal("0")
            elif isinstance(d, Alert):
                counter["alert"] += 1
                if len(sample_alerts) < 5:
                    sample_alerts.append(d.message)
            elif isinstance(d, NoAction):
                counter["no_action"] += 1

    report = {
        "expiry": stream.expiry_at.isoformat() if stream.expiry_at else None,
        "kalshi_markets": [
            {
                "ticker": m.ticker,
                "kind": m.kind,
                "lower": str(m.lower) if m.lower is not None else None,
                "upper": str(m.upper) if m.upper is not None else None,
            }
            for m in stream.kalshi_markets[:20]
        ],
        "kalshi_market_count": len(stream.kalshi_markets),
        "events_processed": len(stream.events),
        "decisions": dict(counter),
        "per_market_orders_top_10": dict(market_decision_counts.most_common(10)),
        "edge_bps_summed": {
            "buy_yes": str(yes_edge_bps),
            "buy_no": str(no_edge_bps),
        },
        "sample_alerts": sample_alerts,
    }
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
