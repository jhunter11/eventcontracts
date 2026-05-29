"""V6-C3: live-promotable fair-value strategies must discretise their per-side
BUY price to the venue cent, identically to the Rust runtime.

These tests pin the *Python* side of the cross-language pricing contract that
``contracts/parity/flu_hospitalization_surge/04_signal_halfcent.json`` pins on
the Rust side. A half-cent mid (an odd tick-sum book, e.g. bid 0.58 / ask 0.61
-> mid 0.595) must **floor** to the cent: never emit a sub-cent price Kalshi
would reject, and never pay above fair. Together with the Rust parity case this
proves both gates agree on discretisation rather than diverging silently.
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
from eventcontracts.strategy import create_from_spec
from eventcontracts.testing.doubles import InMemoryContext
from tests.conftest import REPO_ROOT

NOW = datetime(2026, 1, 2, 14, 30, tzinfo=UTC)
CONFIGS = REPO_ROOT / "configs"


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


def _only_order(decisions: list[StrategyDecision]) -> PlaceOrder:
    orders = [d for d in decisions if isinstance(d, PlaceOrder)]
    assert len(orders) == 1, f"expected exactly one PlaceOrder, got {decisions}"
    return orders[0]


def test_flu_floors_half_cent_no_price_to_the_cent() -> None:
    # Mirrors contracts/parity/flu_hospitalization_surge/04_signal_halfcent.json:
    # mid 0.595, prob 0.30 -> NO, raw price 1-0.595 = 0.405 -> floor -> 0.40.
    spec = load_strategy_spec(CONFIGS / "strategies" / "flu-hospitalization-surge.toml")
    strategy = create_from_spec(spec)
    ctx = _ctx(spec)
    strategy.on_event(_quote("KXFLU-DEMO", "0.58", "0.61"), ctx)
    decisions = list(
        strategy.on_event(
            ExternalSignalEvent(
                event_id=EventId("flu-sig-no"),
                source="public-health-nowcast",
                exchange_ts=NOW,
                received_at=NOW,
                schema_version="public-health-nowcast-v1",
                payload={"market_id": "KXFLU-DEMO", "surge_probability": "0.30"},
            ),
            ctx,
        )
    )
    order = _only_order(decisions)
    assert order.outcome_side is OutcomeSide.NO
    assert order.price == Decimal("0.40")  # never 0.405 (sub-cent) or 0.41 (above fair)
    assert Decimal(order.metadata["fair_price"]) == Decimal("0.70")


def test_flu_floors_half_cent_yes_price_to_the_cent() -> None:
    # mid 0.595, prob 0.90 -> YES, raw price 0.595 -> floor -> 0.59.
    spec = load_strategy_spec(CONFIGS / "strategies" / "flu-hospitalization-surge.toml")
    strategy = create_from_spec(spec)
    ctx = _ctx(spec)
    strategy.on_event(_quote("KXFLU-DEMO", "0.58", "0.61"), ctx)
    decisions = list(
        strategy.on_event(
            ExternalSignalEvent(
                event_id=EventId("flu-sig-yes"),
                source="public-health-nowcast",
                exchange_ts=NOW,
                received_at=NOW,
                schema_version="public-health-nowcast-v1",
                payload={"market_id": "KXFLU-DEMO", "surge_probability": "0.90"},
            ),
            ctx,
        )
    )
    order = _only_order(decisions)
    assert order.outcome_side is OutcomeSide.YES
    assert order.price == Decimal("0.59")  # never 0.595 (sub-cent)


def test_crop_floors_half_cent_no_price_to_the_cent() -> None:
    spec = load_strategy_spec(CONFIGS / "strategies" / "crop-drought-yield-reversion.toml")
    strategy = create_from_spec(spec)
    ctx = _ctx(spec)
    strategy.on_event(_quote("KXCROP-DEMO", "0.58", "0.61"), ctx)
    decisions = list(
        strategy.on_event(
            ExternalSignalEvent(
                event_id=EventId("crop-sig-no"),
                source="crop-water-balance",
                exchange_ts=NOW,
                received_at=NOW,
                schema_version="crop-water-balance-v1",
                payload={
                    "market_id": "KXCROP-DEMO",
                    "yield_reversion_probability": "0.30",
                    "confidence": "0.90",
                },
            ),
            ctx,
        )
    )
    order = _only_order(decisions)
    assert order.outcome_side is OutcomeSide.NO
    assert order.price == Decimal("0.40")
