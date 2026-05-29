"""Pre-match tennis value strategy driven by an ONNX XGBoost probability.

The inference boundary is explicit: an upstream feature/model adapter emits an
``ExternalSignalEvent`` containing a player-1 win probability and market ID.
This strategy stores that fair value, observes the current YES quote, and
submits at most one paper entry per market when either side clears the edge
threshold. Rust can reproduce the same decision logic after loading the ONNX
artifact and constructing the feature vector from the shared schema.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from uuid import uuid4

from eventcontracts.domain.decisions import NoAction, PlaceOrder, StrategyDecision
from eventcontracts.domain.events import (
    ExternalSignalEvent,
    NormalizedEvent,
    OwnFillEvent,
    OwnOrderRejectEvent,
    OwnOrderUpdateEvent,
    QuoteEvent,
    market_snapshot_from_quote_event,
)
from eventcontracts.domain.ids import ClientOrderId
from eventcontracts.domain.models import MarketSnapshot, OrderBookLevel, OutcomeSide
from eventcontracts.domain.orders import OrderSide, OrderStatus, OrderType, TimeInForce
from eventcontracts.domain.spec import StrategySpec
from eventcontracts.strategy.base import StrategyBase, StrategyFeedback
from eventcontracts.strategy.context import StrategyContext
from eventcontracts.strategy.registry import register


@dataclass
class _MarketState:
    probability: Decimal | None = None
    yes_bid: Decimal | None = None
    yes_ask: Decimal | None = None
    latest_quote_snapshot: MarketSnapshot | None = None
    pending_client_order_id: ClientOrderId | None = None
    active_client_order_id: ClientOrderId | None = None
    completed_until: datetime | None = None


class SportsTennisXgboostStrategy(StrategyBase):
    """Trade a pre-match tennis winner market when model edge clears a floor."""

    def __init__(self, spec: StrategySpec) -> None:
        super().__init__(spec)
        self.prediction_source = str(spec.parameters.get("prediction_source", "tennis_xgboost_onnx"))
        self.min_edge_bps = Decimal(str(spec.parameters.get("min_edge_bps", "150")))
        self.size = Decimal(str(spec.parameters.get("size", "5")))
        self.order_ttl_ms = int(spec.parameters.get("order_ttl_ms", 5000))
        self.completed_cooldown_secs = int(
            spec.parameters.get("completed_cooldown_secs", 3600)
        )
        self._markets: dict[str, _MarketState] = {}

    def on_event(self, event: NormalizedEvent, ctx: StrategyContext) -> Sequence[StrategyDecision]:
        if isinstance(event, ExternalSignalEvent):
            if event.source != self.prediction_source:
                return (NoAction(reason="ignored_prediction_source"),)
            market_id = event.payload.get("market_id")
            probability = _probability(event.payload.get("player_1_win_probability"))
            if not isinstance(market_id, str) or probability is None:
                return (NoAction(reason="invalid_prediction_signal"),)
            state = self._markets.setdefault(market_id, _MarketState())
            state.probability = probability
            return self._decision(market_id, state, now=_event_now(ctx, event.received_at))
        if isinstance(event, OwnOrderUpdateEvent):
            self._apply_order_update(event)
            return (NoAction(reason="own_order_update_applied"),)
        if isinstance(event, OwnOrderRejectEvent):
            self._apply_order_reject(event)
            return (NoAction(reason="own_order_reject_applied"),)
        if isinstance(event, OwnFillEvent):
            self._apply_fill(event)
            return (NoAction(reason="own_fill_applied"),)
        if isinstance(event, QuoteEvent) and event.quote.side is OutcomeSide.YES:
            if event.quote.bid is None or event.quote.ask is None:
                return (NoAction(reason="incomplete_quote"),)
            market_id = event.quote.instrument_id.market_id
            state = self._markets.setdefault(market_id, _MarketState())
            state.yes_bid = event.quote.bid.price
            state.yes_ask = event.quote.ask.price
            state.latest_quote_snapshot = market_snapshot_from_quote_event(event)
            return self._decision(
                market_id,
                state,
                instrument_id=event.quote.instrument_id,
                now=_event_now(ctx, event.quote.received_at),
            )
        return (NoAction(reason=f"ignored:{type(event).__name__}"),)

    def _decision(
        self,
        market_id: str,
        state: _MarketState,
        *,
        instrument_id: object | None = None,
        now: datetime,
    ) -> Sequence[StrategyDecision]:
        self._unlock_completed_if_ready(state, now)
        if state.completed_until is not None:
            return (NoAction(reason="market_fill_cooldown"),)
        if state.active_client_order_id is not None:
            return (NoAction(reason="market_has_confirmed_open_order"),)
        if state.pending_client_order_id is not None:
            return (NoAction(reason="market_has_pending_intent"),)
        if state.probability is None or state.yes_bid is None or state.yes_ask is None:
            return (NoAction(reason="awaiting_probability_or_quote"),)
        from eventcontracts.domain.models import InstrumentId, Venue

        instrument = (
            instrument_id
            if isinstance(instrument_id, InstrumentId)
            else InstrumentId(venue=Venue.KALSHI, market_id=market_id)
        )
        yes_edge = state.probability - state.yes_ask
        no_price = Decimal("1") - state.yes_bid
        no_edge = (Decimal("1") - state.probability) - no_price
        minimum = self.min_edge_bps / Decimal("10000")
        if max(yes_edge, no_edge) < minimum:
            return (NoAction(reason="edge_below_threshold"),)
        side = OutcomeSide.YES if yes_edge >= no_edge else OutcomeSide.NO
        price = state.yes_ask if side is OutcomeSide.YES else no_price
        edge = yes_edge if side is OutcomeSide.YES else no_edge
        fair_price = state.probability if side is OutcomeSide.YES else Decimal("1") - state.probability
        client_order_id = ClientOrderId(uuid4().hex)
        snapshot = _snapshot_for_side(state.latest_quote_snapshot, side)
        return (
            PlaceOrder(
                client_order_id=client_order_id,
                instrument_id=instrument,
                outcome_side=side,
                order_side=OrderSide.BUY,
                order_type=OrderType.LIMIT,
                time_in_force=TimeInForce.GTD,
                quantity=self.size,
                price=price,
                expires_at=now + timedelta(milliseconds=self.order_ttl_ms),
                market_snapshot=snapshot,
                reason=f"tennis_xgboost:{side.value}:p1={state.probability}:edge={edge}",
                expected_edge_bps=edge * Decimal("10000"),
                metadata={
                    "fair_price": str(fair_price),
                    "min_executable_edge_ticks": str(int(self.min_edge_bps)),
                    "fee_rate_bps": "700",
                },
            ),
        )

    def _apply_order_update(self, event: OwnOrderUpdateEvent) -> None:
        order = event.order
        state = self._markets.get(order.instrument_id.market_id)
        if state is None:
            return
        if order.status in (OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED):
            state.pending_client_order_id = None
            state.active_client_order_id = order.client_order_id
            return
        if order.status is OrderStatus.FILLED:
            state.pending_client_order_id = None
            state.active_client_order_id = None
            state.completed_until = order.updated_at + timedelta(
                seconds=self.completed_cooldown_secs
            )
            return
        if order.status in (OrderStatus.CANCELED, OrderStatus.REJECTED, OrderStatus.EXPIRED):
            if state.pending_client_order_id == order.client_order_id:
                state.pending_client_order_id = None
            if state.active_client_order_id == order.client_order_id:
                state.active_client_order_id = None

    def _apply_order_reject(self, event: OwnOrderRejectEvent) -> None:
        client_order_id = event.reject.client_order_id
        self._clear_order_state(client_order_id)

    def on_feedback(self, feedback: StrategyFeedback, ctx: StrategyContext) -> None:
        del ctx
        if feedback.client_order_id is None:
            return
        if feedback.kind == "IntentAccepted" and isinstance(
            feedback.envelope.decision, PlaceOrder
        ):
            market_id = feedback.envelope.decision.instrument_id.market_id
            state = self._markets.setdefault(market_id, _MarketState())
            state.pending_client_order_id = feedback.client_order_id
            return
        if feedback.kind in {"IntentRejected", "VenueRejected", "VenueTerminal"}:
            self._clear_order_state(feedback.client_order_id)

    def _clear_order_state(self, client_order_id: ClientOrderId) -> None:
        for state in self._markets.values():
            if state.pending_client_order_id == client_order_id:
                state.pending_client_order_id = None
            if state.active_client_order_id == client_order_id:
                state.active_client_order_id = None

    def _apply_fill(self, event: OwnFillEvent) -> None:
        state = self._markets.get(event.fill.instrument_id.market_id)
        if state is None:
            return
        if event.fill.quantity > 0:
            state.pending_client_order_id = None
            state.active_client_order_id = event.fill.client_order_id
            state.completed_until = event.fill.filled_at + timedelta(
                seconds=self.completed_cooldown_secs
            )

    @staticmethod
    def _unlock_completed_if_ready(state: _MarketState, now: datetime) -> None:
        if state.completed_until is not None and now >= state.completed_until:
            state.completed_until = None


def _snapshot_for_side(
    snapshot: MarketSnapshot | None,
    side: OutcomeSide,
) -> MarketSnapshot | None:
    if snapshot is None or snapshot.side is side:
        return snapshot
    return MarketSnapshot(
        instrument_id=snapshot.instrument_id,
        side=side,
        bid=(
            OrderBookLevel(price=Decimal("1") - snapshot.ask.price, quantity=snapshot.ask.quantity)
            if snapshot.ask is not None
            else None
        ),
        ask=(
            OrderBookLevel(price=Decimal("1") - snapshot.bid.price, quantity=snapshot.bid.quantity)
            if snapshot.bid is not None
            else None
        ),
        exchange_ts=snapshot.exchange_ts,
        received_at=snapshot.received_at,
        source=snapshot.source,
        source_sequence=snapshot.source_sequence,
        sequence_gap=snapshot.sequence_gap,
        metadata=snapshot.metadata,
    )


def _probability(value: object) -> Decimal | None:
    try:
        probability = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return probability if Decimal("0") <= probability <= Decimal("1") else None


def _event_now(ctx: StrategyContext, fallback: datetime) -> datetime:
    value = getattr(ctx, "now", None)
    return value if isinstance(value, datetime) else fallback


@register("sports_tennis_xgboost")
def factory(spec: StrategySpec) -> SportsTennisXgboostStrategy:
    return SportsTennisXgboostStrategy(spec)
