"""Audit F2 regression: the generic external_edge confidence gate must fail-closed.

Before the fix, a missing/unparseable ``confidence`` *passed* the gate (the check
was ``confidence is not None and confidence < floor``), and the strategy only read
``confidence_floor`` while the Rust archetype reads ``min_confidence`` — so a spec
written for one runtime silently ran ungated on the other. These tests pin the
Python side: with a floor configured, no confidence => no trade; and the
``min_confidence`` alias is honoured.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from eventcontracts.config import load_strategy_spec
from eventcontracts.domain import (
    EventId,
    ExternalSignalEvent,
    InstrumentId,
    OrderBookLevel,
    OutcomeSide,
    PlaceOrder,
    Quote,
    QuoteEvent,
    SleeveId,
    StrategyDecision,
    Venue,
)
from eventcontracts.domain.decisions import NoAction
from eventcontracts.strategy import create_from_spec
from eventcontracts.testing.doubles import InMemoryContext
from tests.conftest import REPO_ROOT

NOW = datetime(2026, 1, 2, 14, 30, tzinfo=UTC)
CONFIGS = REPO_ROOT / "configs"
# entertainment-awards uses name="external_edge" with confidence_floor=0.70.
AWARDS = CONFIGS / "strategies" / "entertainment-awards.toml"
MARKET = "KXOSCARS-DEMO"


def _ctx(spec: object) -> InMemoryContext:
    return InMemoryContext(
        strategy_id_value=spec.strategy_id,  # type: ignore[attr-defined]
        sleeve_id_value=SleeveId("test-sleeve"),
        clock_now=NOW,
    )


def _quote(market_id: str, bid: str, ask: str) -> QuoteEvent:
    return QuoteEvent(
        event_id=EventId("q"),
        quote=Quote(
            instrument_id=InstrumentId(venue=Venue.KALSHI, market_id=market_id),
            side=OutcomeSide.YES,
            bid=OrderBookLevel(price=Decimal(bid), quantity=Decimal("100")),
            ask=OrderBookLevel(price=Decimal(ask), quantity=Decimal("100")),
            exchange_ts=NOW,
            received_at=NOW,
        ),
    )


def _signal(payload: dict[str, str]) -> ExternalSignalEvent:
    return ExternalSignalEvent(
        event_id=EventId("awards-sig"),
        source="awards-model",
        exchange_ts=NOW,
        received_at=NOW,
        schema_version="awards-model-v1",
        payload={"market_id": MARKET, **payload},
    )


def _decide(payload: dict[str, str]) -> list[StrategyDecision]:
    spec = load_strategy_spec(AWARDS)
    strategy = create_from_spec(spec)
    ctx = _ctx(spec)
    strategy.on_event(_quote(MARKET, "0.50", "0.52"), ctx)  # mid 0.51
    return list(strategy.on_event(_signal(payload), ctx))


def test_missing_confidence_is_fail_closed() -> None:
    # prob 0.70 vs mid 0.51 is a big edge, but no confidence => must NOT trade.
    decisions = _decide({"probability": "0.70"})
    assert all(isinstance(d, NoAction) for d in decisions)
    assert any("confidence_missing_fail_closed" in d.reason for d in decisions)


def test_confidence_below_floor_blocks() -> None:
    decisions = _decide({"probability": "0.70", "confidence": "0.60"})  # < 0.70
    assert all(isinstance(d, NoAction) for d in decisions)
    assert any("confidence_below_floor" in d.reason for d in decisions)


def test_sufficient_confidence_trades() -> None:
    decisions = _decide({"probability": "0.70", "confidence": "0.80"})  # >= 0.70
    orders = [d for d in decisions if isinstance(d, PlaceOrder)]
    assert len(orders) == 1
    assert orders[0].outcome_side is OutcomeSide.YES


def test_min_confidence_alias_is_honoured() -> None:
    # A spec carrying only the Rust key (min_confidence) must still gate in Python.
    spec = load_strategy_spec(AWARDS)
    params = dict(spec.parameters)
    params.pop("confidence_floor", None)
    params["min_confidence"] = "0.70"
    import dataclasses

    aliased = dataclasses.replace(spec, parameters=params)
    strategy = create_from_spec(aliased)
    ctx = _ctx(aliased)
    strategy.on_event(_quote(MARKET, "0.50", "0.52"), ctx)
    decisions = list(strategy.on_event(_signal({"probability": "0.70"}), ctx))
    assert any(
        isinstance(d, NoAction) and "confidence_missing_fail_closed" in d.reason
        for d in decisions
    )
