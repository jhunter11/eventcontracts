"""Paper decision coverage for the ONNX-backed tennis strategy boundary."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import cast

from eventcontracts.config import load_sleeve_spec, load_strategy_spec
from eventcontracts.domain.decisions import IntentEnvelope, NoAction, PlaceOrder
from eventcontracts.domain.events import (
    EventProvenance,
    ExternalSignalEvent,
    OwnOrderRejectEvent,
    QuoteEvent,
)
from eventcontracts.domain.ids import ClientOrderId, CorrelationId, EventId, SleeveId
from eventcontracts.domain.models import InstrumentId, OrderBookLevel, OutcomeSide, Quote, Venue
from eventcontracts.domain.orders import OrderReject
from eventcontracts.plugins.strategies.sports_tennis_xgboost import SportsTennisXgboostStrategy
from eventcontracts.strategy.base import StrategyFeedback
from eventcontracts.strategy.context import StrategyContext
from tests.conftest import REPO_ROOT

NOW = datetime(2026, 5, 27, 14, 0, tzinfo=UTC)
INSTRUMENT = InstrumentId(venue=Venue.KALSHI, market_id="KX-ATP-DEMO")
CONTEXT = cast(StrategyContext, object())


def test_tennis_strategy_configs_load_for_paper_bundle() -> None:
    spec = load_strategy_spec(REPO_ROOT / "configs/strategies/sports-tennis-xgboost.toml")
    sleeve = load_sleeve_spec(REPO_ROOT / "configs/sleeves/sports-tennis-kalshi-paper-a.toml")

    assert spec.name == "sports_tennis_xgboost"
    assert sleeve.strategy_id == spec.strategy_id


def test_tennis_strategy_buys_yes_once_when_onnx_probability_beats_ask() -> None:
    strategy = _strategy()

    waiting = strategy.on_event(_signal("0.70"), CONTEXT)
    decision = strategy.on_event(_quote("0.55", "0.57"), CONTEXT)
    assert isinstance(decision[0], PlaceOrder)
    _feedback(strategy, "IntentAccepted", decision[0])
    repeat = strategy.on_event(_quote("0.54", "0.56"), CONTEXT)

    assert isinstance(waiting[0], NoAction)
    assert decision[0].outcome_side is OutcomeSide.YES
    assert decision[0].price == Decimal("0.57")
    assert decision[0].market_snapshot is not None
    assert isinstance(repeat[0], NoAction)


def test_tennis_strategy_buys_no_when_model_probability_is_below_yes_bid() -> None:
    strategy = _strategy()
    strategy.on_event(_quote("0.60", "0.62"), CONTEXT)

    decision = strategy.on_event(_signal("0.38"), CONTEXT)

    assert isinstance(decision[0], PlaceOrder)
    assert decision[0].outcome_side is OutcomeSide.NO
    assert decision[0].price == Decimal("0.40")
    assert decision[0].market_snapshot is not None
    assert decision[0].market_snapshot.side is OutcomeSide.NO


def test_tennis_strategy_clears_pending_intent_on_order_reject() -> None:
    strategy = _strategy()
    strategy.on_event(_signal("0.70"), CONTEXT)
    first = strategy.on_event(_quote("0.55", "0.57"), CONTEXT)
    assert isinstance(first[0], PlaceOrder)

    rejected = strategy.on_event(_reject(first[0].client_order_id), CONTEXT)
    retry = strategy.on_event(_quote("0.55", "0.57"), CONTEXT)

    assert isinstance(rejected[0], NoAction)
    assert isinstance(retry[0], PlaceOrder)


def test_tennis_strategy_clears_pending_on_intent_reject_feedback() -> None:
    strategy = _strategy()
    strategy.on_event(_signal("0.70"), CONTEXT)
    first = strategy.on_event(_quote("0.55", "0.57"), CONTEXT)
    assert isinstance(first[0], PlaceOrder)

    strategy.on_feedback(
        StrategyFeedback(
            kind="IntentRejected",
            envelope=IntentEnvelope(
                decision=first[0],
                strategy_id=strategy.spec.strategy_id,
                sleeve_id=SleeveId("tennis-test-sleeve"),
                correlation_id=CorrelationId("corr-tennis-reject"),
                emitted_at=NOW,
            ),
            client_order_id=first[0].client_order_id,
            reasons=("risk_reject",),
        ),
        CONTEXT,
    )
    retry = strategy.on_event(_quote("0.55", "0.57"), CONTEXT)

    assert isinstance(retry[0], PlaceOrder)


def _strategy() -> SportsTennisXgboostStrategy:
    spec = load_strategy_spec(REPO_ROOT / "configs/strategies/sports-tennis-xgboost.toml")
    return SportsTennisXgboostStrategy(spec)


def _feedback(
    strategy: SportsTennisXgboostStrategy,
    kind: str,
    decision: PlaceOrder,
) -> None:
    strategy.on_feedback(
        StrategyFeedback(
            kind=kind,
            envelope=IntentEnvelope(
                decision=decision,
                strategy_id=strategy.spec.strategy_id,
                sleeve_id=SleeveId("tennis-test-sleeve"),
                correlation_id=CorrelationId(f"corr-{kind}"),
                emitted_at=NOW,
            ),
            client_order_id=decision.client_order_id,
        ),
        CONTEXT,
    )


def _signal(probability: str) -> ExternalSignalEvent:
    return ExternalSignalEvent(
        event_id=EventId(f"prediction-{probability}"),
        source="tennis_xgboost_onnx",
        exchange_ts=NOW,
        received_at=NOW,
        schema_version="tennis-prediction-v1",
        payload={
            "market_id": INSTRUMENT.market_id,
            "player_1_win_probability": probability,
        },
        provenance=EventProvenance(source="fixture", channel="external"),
    )


def _quote(bid: str, ask: str) -> QuoteEvent:
    return QuoteEvent(
        event_id=EventId(f"quote-{bid}-{ask}"),
        quote=Quote(
            instrument_id=INSTRUMENT,
            side=OutcomeSide.YES,
            bid=OrderBookLevel(price=Decimal(bid), quantity=Decimal("50")),
            ask=OrderBookLevel(price=Decimal(ask), quantity=Decimal("50")),
            exchange_ts=NOW,
            received_at=NOW,
        ),
        provenance=EventProvenance(source="fixture", channel="quote", source_sequence="1"),
    )


def _reject(client_order_id: ClientOrderId) -> OwnOrderRejectEvent:
    return OwnOrderRejectEvent(
        event_id=EventId(f"reject-{client_order_id}"),
        reject=OrderReject(
            client_order_id=client_order_id,
            reason="risk_reject",
            rejected_at=NOW,
        ),
        provenance=EventProvenance(source="fixture", channel="orders"),
    )
