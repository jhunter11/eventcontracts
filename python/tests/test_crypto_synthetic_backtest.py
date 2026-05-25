"""Synthetic 15-min crypto backtest of the ensemble strategy.

These tests drive the ensemble through deterministic event streams
produced by :mod:`eventcontracts.crypto.synthetic`. The synthetic
generator produces *fair* bracket mids by construction, so a correct
ensemble strategy should emit only ``NoAction``/``Alert`` until a
mispricing is injected. Each test:

1. Picks a mispricing knob (``parity_bump`` or ``skew_bump``).
2. Builds a fair-then-mispriced scenario.
3. Runs the ensemble strategy through every event.
4. Asserts the strategy traded only on the mispriced legs (or stayed
   flat when no mispricing was injected).

The scenarios are deterministic — same seed → same events → same
decisions — so these double as regression fixtures for the ensemble.
"""

from __future__ import annotations

import dataclasses
from collections import Counter
from collections.abc import Sequence
from decimal import Decimal

from eventcontracts.config import load_strategy_spec
from eventcontracts.crypto import (
    SyntheticConfig,
    generate_scenario,
)
from eventcontracts.domain import (
    Alert,
    NoAction,
    PlaceOrder,
    SleeveId,
    StrategyDecision,
)
from eventcontracts.strategy import create_from_spec
from eventcontracts.testing.doubles import InMemoryClock, InMemoryContext
from tests.conftest import REPO_ROOT

CONFIGS = REPO_ROOT / "configs"


def _build_ensemble_for_scenario(scenario, overrides: dict[str, str]):
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
            # Disable regime + terminal since the scenarios don't drive
            # them by default; the user can enable per-test.
            "enabled_sources": overrides.get("enabled_sources", "parity,vol_surface,skew"),
            "min_confluence": overrides.get("min_confluence", "1"),
            "min_combined_edge_bps": overrides.get("min_combined_edge_bps", "30"),
            "max_spread_bps": overrides.get("max_spread_bps", "2000"),
        }
    )
    merged.update(overrides)
    spec = dataclasses.replace(spec, parameters=merged)
    return create_from_spec(spec), spec


def _drive(strategy, scenario, *, clock: InMemoryClock) -> list[StrategyDecision]:
    """Stream every event through the strategy with a moving clock."""

    ctx = InMemoryContext(
        strategy_id_value=strategy.spec.strategy_id,
        sleeve_id_value=SleeveId("test-sleeve"),
        clock_now=clock.current,
    )
    decisions: list[StrategyDecision] = []
    for event in scenario.events:
        ts = getattr(event, "received_at", None)
        if ts is None and hasattr(event, "quote"):
            ts = event.quote.received_at
        if ts is None and hasattr(event, "trade"):
            ts = event.trade.received_at
        if ts is not None:
            clock.current = ts
            ctx.clock_now = ts
        decisions.extend(strategy.on_event(event, ctx))
    return decisions


def _summary(decisions: Sequence[StrategyDecision]) -> dict[str, int]:
    counter: Counter[str] = Counter()
    for d in decisions:
        if isinstance(d, PlaceOrder):
            counter["place_order"] += 1
            counter[f"place_{d.outcome_side.value}"] += 1
        elif isinstance(d, Alert):
            counter["alert"] += 1
        elif isinstance(d, NoAction):
            counter["no_action"] += 1
    return dict(counter)


# ----------------------------- tests -----------------------------


def test_fair_market_yields_no_orders() -> None:
    """With no injected mispricing the ensemble should stay flat —
    every signal source agrees with the fair price."""

    scenario = generate_scenario(SyntheticConfig(seed=42))
    strategy, _spec = _build_ensemble_for_scenario(
        scenario,
        {
            "enabled_sources": "parity,skew",
            "min_confluence": "1",
            "min_combined_edge_bps": "20",
        },
    )
    decisions = _drive(strategy, scenario, clock=InMemoryClock())
    summary = _summary(decisions)
    # Parity is exact at 1.0 in the synthetic data by construction, so
    # no parity signal should fire; without injected skew the
    # higher-strike mid should never exceed the lower-strike mid.
    assert summary.get("place_order", 0) == 0


def test_injected_parity_bump_triggers_sell_all() -> None:
    """Bumping every bracket mid by +0.03 inflates the parity sum to
    ~1.12; the parity source must fire NO-side legs across the
    partition."""

    scenario = generate_scenario(
        SyntheticConfig(seed=42, parity_bump=Decimal("0.03"))
    )
    strategy, _spec = _build_ensemble_for_scenario(
        scenario,
        {
            "enabled_sources": "parity",
            "min_confluence": "1",
            "min_combined_edge_bps": "100",
            "max_spread_bps": "2000",
        },
    )
    decisions = _drive(strategy, scenario, clock=InMemoryClock())
    summary = _summary(decisions)
    assert summary.get("place_order", 0) > 0
    # Parity bump > 0 → buy NO on each bracket.
    assert summary.get("place_no", 0) > 0
    assert summary.get("place_yes", 0) == 0


def test_injected_skew_violation_triggers_butterfly() -> None:
    """Bumping ONE bracket's mid by +0.10 breaks monotonicity if the
    bracket sits above its neighbor in strike. Pick BTCD-K100K5 (the
    second-highest strike) and push it well above its lower
    neighbor."""

    scenario = generate_scenario(
        SyntheticConfig(
            seed=42,
            skew_bump_market_id="BTCD-A100K5",
            skew_bump=Decimal("0.50"),
        )
    )
    strategy, _spec = _build_ensemble_for_scenario(
        scenario,
        {
            "enabled_sources": "skew",
            "min_confluence": "1",
            "min_combined_edge_bps": "100",
            "max_spread_bps": "2000",
        },
    )
    decisions = _drive(strategy, scenario, clock=InMemoryClock())
    summary = _summary(decisions)
    assert summary.get("place_order", 0) > 0
    # Skew arb places a YES on the cheaper-strike leg and a NO on
    # the inflated leg.
    assert summary.get("place_yes", 0) > 0
    assert summary.get("place_no", 0) > 0


def test_combined_mispricings_fire_independently() -> None:
    """Parity and skew operate on disjoint market layers (bracket vs
    above), so they can never both fire on the same instrument. With
    both bumps injected and ``min_confluence=1`` each source should
    independently produce decisions on its own market layer."""

    scenario = generate_scenario(
        SyntheticConfig(
            seed=42,
            parity_bump=Decimal("0.03"),
            skew_bump_market_id="BTCD-A100K5",
            skew_bump=Decimal("0.30"),
        )
    )
    strategy, _spec = _build_ensemble_for_scenario(
        scenario,
        {
            "enabled_sources": "parity,skew",
            "min_confluence": "1",
            "min_combined_edge_bps": "100",
            "max_spread_bps": "2000",
        },
    )
    decisions = _drive(strategy, scenario, clock=InMemoryClock())
    summary = _summary(decisions)
    assert summary.get("place_order", 0) > 0
    # Parity bump > 0 → NO legs across the bracket layer; skew inversion
    # → YES + NO legs across the above-market layer. Both sides must
    # appear in the decision stream.
    assert summary.get("place_no", 0) > 0
    assert summary.get("place_yes", 0) > 0


def test_determinism_same_seed_same_decisions() -> None:
    """Two runs with the same seed must produce identical decision streams."""

    cfg = SyntheticConfig(seed=99, parity_bump=Decimal("0.02"))
    scen_a = generate_scenario(cfg)
    scen_b = generate_scenario(cfg)
    overrides = {
        "enabled_sources": "parity",
        "min_confluence": "1",
        "min_combined_edge_bps": "50",
        "max_spread_bps": "2000",
    }
    strat_a, _ = _build_ensemble_for_scenario(scen_a, overrides)
    strat_b, _ = _build_ensemble_for_scenario(scen_b, overrides)
    dec_a = _drive(strat_a, scen_a, clock=InMemoryClock())
    dec_b = _drive(strat_b, scen_b, clock=InMemoryClock())
    # Two scenarios with identical RNG seeds produce identical event
    # streams; the ensemble is deterministic so the decision stream
    # tags should match (client_order_id is uuid4 so we don't compare
    # PlaceOrder identity directly, just the verdict tag).
    summary_a = _summary(dec_a)
    summary_b = _summary(dec_b)
    assert summary_a == summary_b


def test_min_confluence_two_blocks_single_source_fire() -> None:
    """With ``min_confluence=2`` and only one source enabled, no
    confluence is possible → no PlaceOrders."""

    scenario = generate_scenario(
        SyntheticConfig(seed=7, parity_bump=Decimal("0.05"))
    )
    strategy, _spec = _build_ensemble_for_scenario(
        scenario,
        {
            "enabled_sources": "parity",
            "min_confluence": "2",
            "min_combined_edge_bps": "30",
        },
    )
    decisions = _drive(strategy, scenario, clock=InMemoryClock())
    summary = _summary(decisions)
    assert summary.get("place_order", 0) == 0
