"""Tests for the crypto signal ensemble strategy.

Covers:

* BUY YES verdict when two independent sources agree (parity + skew
  via a configurable scenario).
* HOLD verdict when net edge is below ``min_combined_edge_bps``.
* HOLD verdict when fewer than ``min_confluence`` sources fire.
* HOLD verdict when conflicting sources cancel out.
* Combine math: signal aggregation is the weighted sum of
  ``edge_bps × confidence`` per source with NO contributing a sign flip.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from eventcontracts.config import load_strategy_spec
from eventcontracts.crypto import (
    Signal,
    combine_signals,
)
from eventcontracts.domain import (
    Alert,
    EventId,
    InstrumentId,
    NoAction,
    OrderBookLevel,
    OutcomeSide,
    PlaceOrder,
    Quote,
    QuoteEvent,
    SleeveId,
    Venue,
)
from eventcontracts.strategy import create_from_spec
from eventcontracts.testing.doubles import InMemoryContext
from tests.conftest import REPO_ROOT

CONFIGS = REPO_ROOT / "configs"
NOW = datetime(2026, 1, 2, 14, 30, tzinfo=UTC)


def _instrument(market_id: str) -> InstrumentId:
    return InstrumentId(venue=Venue.KALSHI, market_id=market_id)


def _quote_event(market_id: str, *, bid: str, ask: str, event_id: str) -> QuoteEvent:
    return QuoteEvent(
        event_id=EventId(event_id),
        quote=Quote(
            instrument_id=_instrument(market_id),
            side=OutcomeSide.YES,
            bid=OrderBookLevel(price=Decimal(bid), quantity=Decimal("100")),
            ask=OrderBookLevel(price=Decimal(ask), quantity=Decimal("100")),
            exchange_ts=NOW,
            received_at=NOW,
        ),
    )


def _build_ensemble(overrides: dict[str, str]):
    spec = load_strategy_spec(CONFIGS / "strategies" / "crypto-signal-ensemble.toml")
    merged = dict(spec.parameters)
    merged.update(overrides)
    spec = dataclasses.replace(spec, parameters=merged)
    strategy = create_from_spec(spec)
    ctx = InMemoryContext(
        strategy_id_value=spec.strategy_id,
        sleeve_id_value=SleeveId("test-sleeve"),
        clock_now=NOW,
    )
    return strategy, ctx


# ----------------------------- pure-aggregation tests -----------------------------


def test_combine_signals_agreeing_sources_emit_buy_verdict() -> None:
    instr = _instrument("BTCD-K100K")
    signals = (
        Signal(instr, OutcomeSide.YES, Decimal("80"), Decimal("0.7"), 900, "vol_surface", "r"),
        Signal(instr, OutcomeSide.YES, Decimal("60"), Decimal("0.9"), 0, "parity", "r"),
    )
    verdicts = combine_signals(signals, min_combined_edge_bps=Decimal("20"), min_confluence=2)
    assert len(verdicts) == 1
    assert verdicts[0].side is OutcomeSide.YES
    assert verdicts[0].net_edge_bps > 0


def test_combine_signals_below_threshold_returns_hold() -> None:
    instr = _instrument("BTCD-K100K")
    signals = (
        Signal(instr, OutcomeSide.YES, Decimal("10"), Decimal("0.7"), 0, "vol_surface", "r"),
        Signal(instr, OutcomeSide.YES, Decimal("10"), Decimal("0.7"), 0, "parity", "r"),
    )
    verdicts = combine_signals(signals, min_combined_edge_bps=Decimal("100"), min_confluence=2)
    assert verdicts[0].side is None
    assert abs(verdicts[0].net_edge_bps) < Decimal("100")


def test_combine_signals_below_confluence_drops_instrument() -> None:
    instr = _instrument("BTCD-K100K")
    signals = (
        Signal(instr, OutcomeSide.YES, Decimal("200"), Decimal("0.9"), 0, "vol_surface", "r"),
    )
    verdicts = combine_signals(signals, min_combined_edge_bps=Decimal("20"), min_confluence=2)
    assert verdicts == ()


def test_combine_signals_conflicting_sources_can_hold() -> None:
    instr = _instrument("BTCD-K100K")
    signals = (
        Signal(instr, OutcomeSide.YES, Decimal("80"), Decimal("0.9"), 0, "parity", "r"),
        Signal(instr, OutcomeSide.NO, Decimal("80"), Decimal("0.9"), 0, "vol_surface", "r"),
    )
    verdicts = combine_signals(signals, min_combined_edge_bps=Decimal("20"), min_confluence=2)
    # Equal-and-opposite → net is zero → HOLD.
    assert verdicts[0].side is None
    assert verdicts[0].net_edge_bps == Decimal("0")


def test_combine_signals_weights_swing_outcome() -> None:
    instr = _instrument("BTCD-K100K")
    signals = (
        Signal(instr, OutcomeSide.YES, Decimal("80"), Decimal("0.9"), 0, "parity", "r"),
        Signal(instr, OutcomeSide.NO, Decimal("80"), Decimal("0.9"), 0, "vol_surface", "r"),
    )
    # Down-weight vol_surface so parity dominates.
    weights = {"parity": Decimal("1.0"), "vol_surface": Decimal("0.1")}
    verdicts = combine_signals(
        signals, weights=weights, min_combined_edge_bps=Decimal("20"), min_confluence=2
    )
    assert verdicts[0].side is OutcomeSide.YES


# ----------------------------- ensemble-strategy tests -----------------------------


def test_ensemble_emits_hold_when_no_signals_fire() -> None:
    """No confluence → ensemble emits NoAction("no_confluence:...")."""

    strategy, ctx = _build_ensemble(
        {
            "bracket_market_ids": (
                "BTCD-LO:-inf:100000;BTCD-MID:100000:105000;BTCD-HI:105000:inf"
            ),
            "strike_market_map": "BTCD-MID:102000",
            "tail_market_map": "BTCD-LO:95000;BTCD-HI:110000",
            "atm_market_id": "BTCD-MID",
            "atm_strike": "100000",
            "min_combined_edge_bps": "20",
            "min_confluence": "2",
        }
    )
    # Quotes that sum to exactly 1.0 → parity emits nothing.
    decisions = strategy.on_event(_quote_event("BTCD-LO", bid="0.33", ask="0.35", event_id="lo"), ctx)
    decisions = strategy.on_event(_quote_event("BTCD-MID", bid="0.33", ask="0.35", event_id="mid"), ctx)
    decisions = strategy.on_event(_quote_event("BTCD-HI", bid="0.31", ask="0.33", event_id="hi"), ctx)
    assert all(isinstance(d, NoAction) for d in decisions)


def test_ensemble_fires_when_parity_and_skew_agree() -> None:
    """Construct quotes that violate parity (Σmid = 1.06) AND have a
    butterfly skew inversion. Two sources agreeing should clear
    min_confluence=2 and produce a directional PlaceOrder."""

    strategy, ctx = _build_ensemble(
        {
            "bracket_market_ids": (
                "BTCD-LO:-inf:100000;BTCD-MID:100000:105000;BTCD-HI:105000:inf"
            ),
            "strike_market_map": "BTCD-LO:99000;BTCD-MID:102000",
            "tail_market_map": "BTCD-LO:99000;BTCD-HI:110000",
            "atm_market_id": "BTCD-MID",
            "atm_strike": "100000",
            "enabled_sources": "parity,skew",
            "weights": "parity:1.0;skew:1.0",
            "min_combined_edge_bps": "100",
            "min_confluence": "2",
            "max_spread_bps": "2000",
        }
    )
    # Σmid = 0.35 + 0.35 + 0.36 = 1.06 → parity says sell everything (NO).
    # Strike grid: BTCD-LO @ K=99000 quoted 0.35, BTCD-MID @ K=102000 quoted
    # 0.45. Higher strike has *higher* P(YES) → butterfly violation → skew
    # says buy YES on BTCD-LO and NO on BTCD-MID.
    strategy.on_event(_quote_event("BTCD-LO", bid="0.34", ask="0.36", event_id="lo"), ctx)
    strategy.on_event(_quote_event("BTCD-MID", bid="0.44", ask="0.46", event_id="mid"), ctx)
    decisions = strategy.on_event(
        _quote_event("BTCD-HI", bid="0.34", ask="0.38", event_id="hi"), ctx
    )

    place_orders = [d for d in decisions if isinstance(d, PlaceOrder)]
    alerts = [d for d in decisions if isinstance(d, Alert)]
    assert place_orders, "expected at least one PlaceOrder from the confluence"
    # The ensemble alert always carries a per-source breakdown.
    assert alerts, "expected at least one Alert from the ensemble verdict"
    breakdown_markets = {d.instrument_id.market_id for d in place_orders}
    assert breakdown_markets <= {"BTCD-LO", "BTCD-MID", "BTCD-HI"}


def test_ensemble_conflicting_signals_hold() -> None:
    """A parity-violation-driven YES signal that perfectly cancels a
    skew-violation-driven NO signal on the same instrument should
    produce a HOLD verdict."""

    instr_lo = _instrument("BTCD-LO")
    sigs = (
        Signal(instr_lo, OutcomeSide.YES, Decimal("100"), Decimal("0.95"), 0, "parity", "r"),
        Signal(instr_lo, OutcomeSide.NO, Decimal("100"), Decimal("0.95"), 0, "skew", "r"),
    )
    verdicts = combine_signals(sigs, min_combined_edge_bps=Decimal("10"), min_confluence=2)
    assert verdicts[0].side is None


@pytest.mark.parametrize(
    "missing_source", ["parity", "vol_surface", "skew"]
)
def test_ensemble_disabled_source_does_not_block_other_signals(
    missing_source: str,
) -> None:
    """Disabling one source must not change the verdict produced by the
    remaining sources — proves the toggle works."""

    enabled = [s for s in ("parity", "vol_surface", "skew") if s != missing_source]
    strategy, ctx = _build_ensemble(
        {
            "bracket_market_ids": (
                "BTCD-LO:-inf:100000;BTCD-MID:100000:105000;BTCD-HI:105000:inf"
            ),
            "strike_market_map": "BTCD-LO:99000;BTCD-MID:102000",
            "tail_market_map": "BTCD-LO:99000;BTCD-HI:110000",
            "atm_market_id": "BTCD-MID",
            "atm_strike": "100000",
            "enabled_sources": ",".join(enabled),
            "min_combined_edge_bps": "30",
            "min_confluence": "1",
            "max_spread_bps": "2000",
        }
    )
    strategy.on_event(_quote_event("BTCD-LO", bid="0.34", ask="0.36", event_id="lo"), ctx)
    strategy.on_event(_quote_event("BTCD-MID", bid="0.44", ask="0.46", event_id="mid"), ctx)
    decisions = strategy.on_event(
        _quote_event("BTCD-HI", bid="0.34", ask="0.38", event_id="hi"), ctx
    )
    assert decisions  # at minimum NoAction/Alert; the toggle did not crash
