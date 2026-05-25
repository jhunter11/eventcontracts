#!/usr/bin/env python3
"""Run the crypto signal ensemble against synthetic Kalshi quotes
seeded with **live Deribit ATM IV**.

Usage::

    EVENTCONTRACTS_INSECURE_TLS=1 python3 scripts/run_ensemble_demo.py [--seed N]

The script:

1. Calls Deribit REST to pull the ATM IV at the soonest BTC expiry.
2. Generates a deterministic 15-min synthetic Kalshi event stream
   sized around the live spot using that vol.
3. Injects optional mispricings (parity bump, skew bump) via CLI args.
4. Runs the ``crypto_signal_ensemble`` strategy through every event
   and prints the resulting decision counts.

This is a research-oriented demo — the strategy runs in pure paper
mode without touching the real Kalshi venue. The Deribit IV is the
single piece of live data; everything else is synthetic.

Set ``EVENTCONTRACTS_INSECURE_TLS=1`` if the host clock is far
enough in the future for Deribit's TLS cert to fail validation
(common in research VMs).
"""

from __future__ import annotations

import argparse
import dataclasses
import json
from collections import Counter
from datetime import timezone
from decimal import Decimal
from pathlib import Path

# Allow running directly from a checkout without installing.
import sys
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "python" / "src"))

from eventcontracts.config import load_strategy_spec  # noqa: E402
from eventcontracts.crypto.deribit import fetch_atm_snapshot  # noqa: E402
from eventcontracts.crypto.synthetic import (  # noqa: E402
    SyntheticConfig,
    generate_scenario,
    replace_deribit_iv,
)
from eventcontracts.domain import (  # noqa: E402
    Alert,
    NoAction,
    PlaceOrder,
    SleeveId,
)
from eventcontracts.strategy import create_from_spec  # noqa: E402
from eventcontracts.testing.doubles import InMemoryClock, InMemoryContext  # noqa: E402


CONFIGS = HERE.parent / "configs"


def _build_ensemble(scenario, *, min_combined_edge_bps: str, min_confluence: str):
    spec = load_strategy_spec(CONFIGS / "strategies" / "crypto-signal-ensemble.toml")
    bracket_ids = ";".join(
        f"{b.market_id}:{'-inf' if b.strike == 0 else b.strike}:"
        f"{'inf' if b.upper is None else b.upper}"
        for b in scenario.bracket_partition
    )
    strike_map = ";".join(f"{m}:{s}" for m, s in scenario.strike_market_map.items())
    merged = dict(spec.parameters)
    merged.update(
        {
            "bracket_market_ids": bracket_ids,
            "strike_market_map": strike_map,
            "enabled_sources": "parity,vol_surface,skew",
            "min_confluence": min_confluence,
            "min_combined_edge_bps": min_combined_edge_bps,
            "max_spread_bps": "5000",
            "size": "5",
        }
    )
    spec = dataclasses.replace(spec, parameters=merged)
    return create_from_spec(spec), spec


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--parity-bump",
        type=str,
        default="0",
        help="Add this constant to every bracket mid (probability units).",
    )
    parser.add_argument(
        "--skew-bump",
        type=str,
        default="0",
        help="Add this constant to one above-market mid to break monotonicity.",
    )
    parser.add_argument(
        "--skew-bump-market",
        type=str,
        default="BTCD-A100K5",
        help="Which above-market gets the skew bump.",
    )
    parser.add_argument("--min-confluence", type=str, default="1")
    parser.add_argument("--min-edge-bps", type=str, default="50")
    parser.add_argument(
        "--no-deribit",
        action="store_true",
        help="Skip the live Deribit IV pull and use a fixed 0.55 vol.",
    )
    args = parser.parse_args(argv)

    if args.no_deribit:
        sigma_annual = Decimal("0.55")
        deribit_summary = {"source": "synthetic", "atm_iv": str(sigma_annual)}
    else:
        snap = fetch_atm_snapshot("BTC")
        sigma_annual = snap.atm_iv
        deribit_summary = {
            "source": "deribit",
            "underlying": snap.underlying,
            "spot": str(snap.spot),
            "atm_iv": str(snap.atm_iv),
            "expiry_at": snap.expiry_at.isoformat(),
            "instrument": snap.instrument_name,
        }
        # Center the synthetic strike grid on the live spot.
        spot_start = snap.spot

    base_config_kwargs = {
        "seed": args.seed,
        "sigma_annual": sigma_annual,
        "parity_bump": Decimal(args.parity_bump),
        "skew_bump_market_id": args.skew_bump_market,
        "skew_bump": Decimal(args.skew_bump),
    }
    if not args.no_deribit:
        # Round spot to nearest $500 so strikes line up cleanly.
        step = Decimal("500")
        rounded_spot = (spot_start // step) * step
        base_config_kwargs["spot_start"] = rounded_spot
        base_config_kwargs["strikes"] = (
            rounded_spot - step,
            rounded_spot,
            rounded_spot + step,
        )
        base_config_kwargs["market_ids"] = (
            "BTCD-LO",
            "BTCD-MIDLOW",
            "BTCD-MID",
            "BTCD-HIMID",
        )
        base_config_kwargs["above_market_ids"] = (
            "BTCD-A-LO",
            "BTCD-A-MID",
            "BTCD-A-HI",
        )
        # Skew bump default name won't exist with the new ids — pick the highest above-market.
        if args.skew_bump_market == "BTCD-A100K5":
            base_config_kwargs["skew_bump_market_id"] = "BTCD-A-HI"

    config = SyntheticConfig(**base_config_kwargs)
    scenario = generate_scenario(config)
    # Lock the Deribit IV in every published event to the configured
    # ``sigma_annual``. Without this the generator publishes a noisy
    # empirical IV that diverges from the IV used to compute the
    # bracket mids — which makes the vol_surface source fire even in
    # the absence of real mispricing.
    scenario = replace_deribit_iv(scenario, sigma_annual)

    strategy, _spec = _build_ensemble(
        scenario,
        min_combined_edge_bps=args.min_edge_bps,
        min_confluence=args.min_confluence,
    )
    clock = InMemoryClock()
    ctx = InMemoryContext(
        strategy_id_value=strategy.spec.strategy_id,
        sleeve_id_value=SleeveId("demo-sleeve"),
        clock_now=clock.current,
    )
    counter: Counter[str] = Counter()
    yes_edge_bps = Decimal("0")
    no_edge_bps = Decimal("0")
    for event in scenario.events:
        ts = (
            getattr(event, "received_at", None)
            or getattr(getattr(event, "quote", None), "received_at", None)
        )
        if ts is not None:
            clock.current = ts
            ctx.clock_now = ts
        for d in strategy.on_event(event, ctx):
            if isinstance(d, PlaceOrder):
                counter["place"] += 1
                counter[f"place_{d.outcome_side.value}"] += 1
                if d.outcome_side.value == "yes":
                    yes_edge_bps += d.expected_edge_bps or Decimal("0")
                else:
                    no_edge_bps += d.expected_edge_bps or Decimal("0")
            elif isinstance(d, Alert):
                counter["alert"] += 1
            elif isinstance(d, NoAction):
                counter["no_action"] += 1

    print(
        json.dumps(
            {
                "deribit": deribit_summary,
                "config": {
                    "seed": args.seed,
                    "parity_bump": args.parity_bump,
                    "skew_bump": args.skew_bump,
                    "skew_bump_market": base_config_kwargs["skew_bump_market_id"],
                    "min_confluence": args.min_confluence,
                    "min_combined_edge_bps": args.min_edge_bps,
                },
                "decisions": dict(counter),
                "edge_bps": {
                    "buy_yes_total": str(yes_edge_bps),
                    "buy_no_total": str(no_edge_bps),
                },
            },
            indent=2,
            default=str,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
