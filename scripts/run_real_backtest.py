#!/usr/bin/env python3
"""Walk-forward backtest of the crypto signal ensemble on real Kalshi + Deribit data.

No synthetic data is used anywhere in this pipeline:

* Kalshi market roster and 1-minute candlesticks come from
  ``https://api.elections.kalshi.com/trade-api/v2/...``.
* Deribit BTC-PERPETUAL 1m OHLC and BTC DVOL come from
  ``https://www.deribit.com/api/v2/public/...``.
* Settlement is the venue's own ``result`` field on each bracket
  market; PnL = ``payout - fill_price - fee`` with fills priced at the
  observed Kalshi bid/ask at decision time and Kalshi's published
  taker fee.

Usage::

    EVENTCONTRACTS_INSECURE_TLS=1 \\
      python3 scripts/run_real_backtest.py \\
        --expiries 26MAY2508 26MAY2509 26MAY2510 \\
        --enabled-sources parity,bracket_vol \\
        --min-confluence 1 --min-edge-bps 100
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
from eventcontracts.crypto import (  # noqa: E402
    SizingPolicy,
    WalkForwardReport,
    build_historical_stream,
    fetch_cohort_settlement,
    run_cohort_backtest,
)
from eventcontracts.domain import SleeveId  # noqa: E402
from eventcontracts.strategy import create_from_spec  # noqa: E402
from eventcontracts.testing.doubles import InMemoryClock, InMemoryContext  # noqa: E402

CONFIGS = HERE.parent / "configs"


def _spec_strings_for(stream) -> dict[str, str]:
    """Build the operator-supplied roster strings from the real markets.

    * ``bracket_market_ids`` is the parity partition. Used by KXBTC
      (between-strike) cohorts; KXBTCD has only above-K markets so
      this comes out empty and parity stays silent.
    * ``strike_market_map`` is the strike grid for vol_surface and
      skew. KXBTCD provides this directly via every above-K ticker;
      KXBTC has only the two unbounded T-tails (far OTM, useless).
    """

    between = [m for m in stream.kalshi_markets if m.kind == "between"]
    above = [m for m in stream.kalshi_markets if m.kind == "above"]
    below = [m for m in stream.kalshi_markets if m.kind == "below"]

    # Parity partition: low-tail + every between-bracket + high-tail.
    parity_parts: list[str] = []
    if below:
        m = below[0]
        parity_parts.append(f"{m.ticker}:-inf:{m.upper}")
    for m in sorted(between, key=lambda x: x.lower or Decimal("0")):
        parity_parts.append(f"{m.ticker}:{m.lower}:{m.upper}")
    if above and not between:
        # KXBTCD: many above-K markets exist; treat them as a
        # rough parity layer too so the parity gate has something
        # to chew on. P(S>=K) decreasing in K, but each is a
        # standalone bet — parity won't sum to 1 here.
        pass
    elif above:
        m = above[0]
        parity_parts.append(f"{m.ticker}:{m.lower}:inf")

    # Strike map for vol_surface and skew. Every above-K market is
    # one (ticker, strike) entry; KXBTCD provides plenty, KXBTC
    # provides at most one (the high tail).
    strike_parts = [
        f"{m.ticker}:{m.lower}"
        for m in sorted(above, key=lambda x: x.lower or Decimal("0"))
        if m.lower is not None
    ]

    return {
        "bracket_market_ids": ";".join(parity_parts),
        "strike_market_map": ";".join(strike_parts),
    }


def _build_ensemble(stream, args):
    spec = load_strategy_spec(CONFIGS / "strategies" / "crypto-signal-ensemble.toml")
    merged = dict(spec.parameters)
    merged.update(_spec_strings_for(stream))
    merged.update(
        {
            "enabled_sources": args.enabled_sources,
            "min_confluence": args.min_confluence,
            "min_combined_edge_bps": args.min_edge_bps,
            "max_spread_bps": args.max_spread_bps,
            "size": args.size,
        }
    )
    spec = dataclasses.replace(spec, parameters=merged)
    return create_from_spec(spec)


def _build_sizing(args) -> SizingPolicy:
    if args.sizing == "flat-contracts":
        return SizingPolicy(mode="flat_contracts", params={})
    if args.sizing == "fixed-premium":
        return SizingPolicy(
            mode="fixed_premium",
            params={"dollars": Decimal(args.sizing_dollars)},
        )
    if args.sizing == "fixed-payout":
        return SizingPolicy(
            mode="fixed_payout",
            params={"dollars": Decimal(args.sizing_dollars)},
        )
    raise SystemExit(f"unknown --sizing: {args.sizing}")


def _run_one(expiry: str, args, *, atm_radius: Decimal | None, sizing: SizingPolicy):
    print(f"loading {expiry}...", file=sys.stderr)
    stream = build_historical_stream(
        expiry_hour_token=expiry,
        series_ticker=args.series_ticker,
        atm_radius_dollars=atm_radius,
    )
    settlement = fetch_cohort_settlement(
        expiry_hour_token=expiry,
        series_ticker=args.series_ticker,
    )
    if not stream.events:
        return None, stream, settlement
    print(
        f"  events={len(stream.events)} markets={len(stream.kalshi_markets)}"
        f" yes_market={settlement.yes_market_ticker} settle≈${settlement.settlement_price}",
        file=sys.stderr,
    )
    strategy = _build_ensemble(stream, args)
    ctx = InMemoryContext(
        strategy_id_value=strategy.spec.strategy_id,
        sleeve_id_value=SleeveId("backtest-sleeve"),
        clock_now=stream.events[0].received_at
        if hasattr(stream.events[0], "received_at")
        else stream.events[0].quote.received_at,
    )
    result = run_cohort_backtest(
        stream=stream,
        settlement=settlement,
        strategy=strategy,
        ctx=ctx,
        sizing=sizing,
        one_fill_per_market=args.one_fill_per_market,
    )
    return result, stream, settlement


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expiries", nargs="+", required=True,
                        help="One or more Kalshi expiry tokens, e.g. 26MAY2508 26MAY2509.")
    parser.add_argument("--series-ticker", default="KXBTC",
                        help="Kalshi series to backtest (KXBTC=bracket, KXBTCD=above-K).")
    parser.add_argument("--atm-radius", type=int, default=500,
                        help="Only fetch Kalshi brackets within this many $ of spot.")
    parser.add_argument("--enabled-sources", default="parity,bracket_vol")
    parser.add_argument("--min-confluence", default="1")
    parser.add_argument("--min-edge-bps", default="100")
    parser.add_argument("--max-spread-bps", default="2000")
    parser.add_argument("--size", default="5",
                        help="Default contract count for flat sizing.")
    parser.add_argument(
        "--sizing",
        choices=("flat-contracts", "fixed-premium", "fixed-payout"),
        default="fixed-premium",
        help="Position sizing rule. 'fixed-premium' equalizes max loss across trades.",
    )
    parser.add_argument(
        "--sizing-dollars",
        default="1",
        help="Per-trade dollar budget for fixed-premium / fixed-payout modes.",
    )
    parser.add_argument(
        "--one-fill-per-market",
        action="store_true",
        help="Cap each cohort at the first PlaceOrder per market — avoids "
             "signal-stacking inflation when the strategy re-fires the same "
             "view every minute.",
    )
    args = parser.parse_args(argv)

    radius = Decimal(args.atm_radius) if args.atm_radius > 0 else None
    sizing = _build_sizing(args)
    report = WalkForwardReport()
    overall_decisions: Counter[str] = Counter()
    per_market_fills: Counter[str] = Counter()
    sample_fills: list[dict] = []

    for expiry in args.expiries:
        cohort, _stream, _settlement = _run_one(expiry, args, atm_radius=radius, sizing=sizing)
        if cohort is None:
            print(f"  (skipped: no events)", file=sys.stderr)
            continue
        report.cohorts.append(cohort)
        overall_decisions.update(cohort.decisions_counter)
        for fill in cohort.fills:
            per_market_fills[fill.market_id] += 1
            if len(sample_fills) < 8 and fill.settled:
                sample_fills.append(
                    {
                        "market": fill.market_id,
                        "side": fill.outcome_side.value,
                        "fill_price": str(fill.fill_price),
                        "payout": str(fill.payout_per_contract),
                        "fee": str(fill.fee_amount),
                        "pnl": str(fill.pnl_total),
                        "sources": list(fill.sources),
                    }
                )
        print(
            f"  cohort {expiry}: fills={len(cohort.fills)}"
            f" settled={len(cohort.settled_fills)}"
            f" pnl=${cohort.total_pnl:.2f}"
            f" fees=${cohort.total_fees:.2f}"
            f" win_rate={cohort.win_rate:.2%}",
            file=sys.stderr,
        )

    summary = {
        "sizing": {"mode": args.sizing, "dollars": args.sizing_dollars},
        "cohorts_run": len(report.cohorts),
        "total_fills": report.total_fills,
        "total_settled_fills": report.total_settled_fills,
        "total_pnl": str(report.total_pnl),
        "total_fees": str(report.total_fees),
        "win_rate": report.win_rate,
        "per_source_pnl": {k: str(v) for k, v in report.per_source_pnl.items()},
        "decisions": dict(overall_decisions),
        "top_markets_by_fill_count": dict(per_market_fills.most_common(10)),
        "sample_fills": sample_fills,
    }
    print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
