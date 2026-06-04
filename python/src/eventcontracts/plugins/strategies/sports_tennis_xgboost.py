"""Pre-match tennis value strategy driven by an ONNX XGBoost probability.

The inference boundary is explicit: an upstream feature/model adapter emits an
``ExternalSignalEvent`` containing a player-1 win probability and market ID.
This strategy stores that fair value, observes the current YES quote, and
submits one entry per market when either side clears the edge threshold. The
resulting position then HOLDS to settlement unless a trailing stop fires: the
peak of the held side's best bid is tracked, and once that bid falls
``trailing_stop_loss`` (probability points) below the peak the position is
liquidated as a taker sell at the bid. Rust reproduces the same entry AND exit
decisions after loading the ONNX artifact and constructing the feature vector
from the shared schema (cross-language parity covers both).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
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
    confidence: Decimal | None = None
    odds_present: bool | None = None
    yes_bid: Decimal | None = None
    yes_ask: Decimal | None = None
    latest_quote_snapshot: MarketSnapshot | None = None
    pending_client_order_id: ClientOrderId | None = None
    active_client_order_id: ClientOrderId | None = None
    completed_until: datetime | None = None
    # entry order intent (recorded at emit so the held side is known on fill
    # without depending on the fill payload carrying outcome_side — keeps the
    # Rust live twin, whose OwnFill has no side, in exact parity).
    pending_side: OutcomeSide | None = None
    pending_qty: Decimal | None = None
    # open-position / trailing-stop management
    holding: bool = False
    held_side: OutcomeSide | None = None
    held_qty: Decimal | None = None
    peak_bid: Decimal | None = None
    exit_client_order_id: ClientOrderId | None = None
    closed: bool = False


class SportsTennisXgboostStrategy(StrategyBase):
    """Trade a pre-match tennis winner market when model edge clears a floor."""

    def __init__(self, spec: StrategySpec) -> None:
        super().__init__(spec)
        self.prediction_source = str(spec.parameters.get("prediction_source", "tennis_xgboost_onnx"))
        self.min_edge_bps = Decimal(str(spec.parameters.get("min_edge_bps", "150")))
        self.min_model_confidence = Decimal(str(spec.parameters.get("min_model_confidence", "0")))
        self.require_odds_present = _bool_parameter(spec.parameters.get("require_odds_present", False))
        self.size = Decimal(str(spec.parameters.get("size", "5")))
        # Trailing stop-loss on an open position (probability points; 0.12 = 12c).
        # The position holds to settlement unless the held side's best bid falls
        # this far below its running peak, at which point it is liquidated (taker
        # sell at the bid). 0 disables the exit (pure hold-to-completion).
        self.trailing_stop_loss = Decimal(str(spec.parameters.get("trailing_stop_loss", "0.12")))
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
            state.confidence = _signal_confidence(probability, event.payload)
            state.odds_present = _optional_bool(event.payload.get("odds_present"))
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
        if state.closed:
            return (NoAction(reason="position_closed"),)
        # Manage an open position before considering any (re-)entry: a held
        # position holds to completion unless the trailing stop fires.
        if state.holding:
            return self._exit_decision(market_id, state, instrument_id=instrument_id, now=now)
        if state.completed_until is not None:
            return (NoAction(reason="market_fill_cooldown"),)
        if state.active_client_order_id is not None:
            return (NoAction(reason="market_has_confirmed_open_order"),)
        if state.pending_client_order_id is not None:
            return (NoAction(reason="market_has_pending_intent"),)
        if state.probability is None or state.yes_bid is None or state.yes_ask is None:
            return (NoAction(reason="awaiting_probability_or_quote"),)
        if self.require_odds_present and state.odds_present is not True:
            return (NoAction(reason="odds_required"),)
        confidence = state.confidence if state.confidence is not None else max(
            state.probability,
            Decimal("1") - state.probability,
        )
        if confidence < self.min_model_confidence:
            return (NoAction(reason="model_confidence_below_threshold"),)
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
        # Remember what we are trying to hold, so a fill establishes the position
        # (and its liquidation side) deterministically.
        state.pending_side = side
        state.pending_qty = self.size
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
                    "fair_price": _fair_price_4dp(fair_price),
                    "min_executable_edge_ticks": str(int(self.min_edge_bps)),
                    "model_confidence": str(confidence),
                    "odds_present": str(state.odds_present is True).lower(),
                    "fee_rate_bps": "700",
                },
            ),
        )

    def _exit_decision(
        self,
        market_id: str,
        state: _MarketState,
        *,
        instrument_id: object | None,
        now: datetime,
    ) -> Sequence[StrategyDecision]:
        """Trailing-stop liquidation of an open position.

        Tracks the peak of the HELD side's best bid (what we could sell into) and
        liquidates as a taker at the bid once it falls ``trailing_stop_loss`` below
        that peak. Otherwise the position holds (no take-profit, ride to settlement).
        """
        if self.trailing_stop_loss <= 0:
            return (NoAction(reason="trailing_stop_disabled"),)
        if state.exit_client_order_id is not None:
            return (NoAction(reason="exit_order_in_flight"),)
        if (
            state.held_side is None
            or state.held_qty is None
            or state.yes_bid is None
            or state.yes_ask is None
        ):
            return (NoAction(reason="awaiting_quote_for_exit"),)
        liquidation_bid = (
            state.yes_bid if state.held_side is OutcomeSide.YES else Decimal("1") - state.yes_ask
        )
        if state.peak_bid is None or liquidation_bid > state.peak_bid:
            state.peak_bid = liquidation_bid
        if state.peak_bid - liquidation_bid < self.trailing_stop_loss:
            return (NoAction(reason="holding_within_trailing_stop"),)

        from eventcontracts.domain.models import InstrumentId, Venue

        instrument = (
            instrument_id
            if isinstance(instrument_id, InstrumentId)
            else InstrumentId(venue=Venue.KALSHI, market_id=market_id)
        )
        client_order_id = ClientOrderId(uuid4().hex)
        state.exit_client_order_id = client_order_id
        snapshot = _snapshot_for_side(state.latest_quote_snapshot, state.held_side)
        return (
            PlaceOrder(
                client_order_id=client_order_id,
                instrument_id=instrument,
                outcome_side=state.held_side,
                order_side=OrderSide.SELL,
                order_type=OrderType.LIMIT,
                time_in_force=TimeInForce.IOC,
                quantity=state.held_qty,
                price=liquidation_bid,
                expires_at=now + timedelta(milliseconds=self.order_ttl_ms),
                market_snapshot=snapshot,
                reason=(
                    f"tennis_xgboost:trailing_stop:{state.held_side.value}:"
                    f"peak={state.peak_bid}:bid={liquidation_bid}"
                ),
                metadata={"liquidation": "true", "fee_rate_bps": "700"},
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
            # Position bookkeeping (holding/closed) is driven by the fill event;
            # here we only release the in-flight order flags. No re-entry cooldown:
            # a held position must keep seeing quotes to manage the trailing stop.
            state.pending_client_order_id = None
            state.active_client_order_id = None
            return
        if order.status in (OrderStatus.CANCELED, OrderStatus.REJECTED, OrderStatus.EXPIRED):
            if state.pending_client_order_id == order.client_order_id:
                state.pending_client_order_id = None
            if state.active_client_order_id == order.client_order_id:
                state.active_client_order_id = None
            # A failed liquidation must be retryable on the next adverse quote.
            if state.exit_client_order_id == order.client_order_id:
                state.exit_client_order_id = None

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
        if state is None or event.fill.quantity <= 0:
            return
        client_order_id = event.fill.client_order_id
        if state.exit_client_order_id is not None and client_order_id == state.exit_client_order_id:
            # Liquidation filled -> position closed for good (no re-entry).
            state.holding = False
            state.held_qty = None
            state.exit_client_order_id = None
            state.closed = True
            return
        # Entry fill -> establish the held position. The held side comes from our
        # recorded entry intent (so it matches the Rust twin, whose fill carries
        # no side); quantity comes from the fill.
        state.pending_client_order_id = None
        state.active_client_order_id = client_order_id
        state.holding = True
        state.held_side = (
            state.pending_side if state.pending_side is not None else event.fill.outcome_side
        )
        state.held_qty = (state.held_qty or Decimal("0")) + event.fill.quantity
        state.peak_bid = None

    @staticmethod
    def _unlock_completed_if_ready(state: _MarketState, now: datetime) -> None:
        if state.completed_until is not None and now >= state.completed_until:
            state.completed_until = None


def _fair_price_4dp(value: Decimal) -> str:
    """Format a fair value as a decimal string with at most 4 fractional digits.

    The risk gate (`risk/limits.py`), the Rust risk gate (`parse_fixed`) and the
    gateway (`parse_fixed_4`) all reject a price string with more than 4 decimal
    places, and Kalshi settles on a whole-cent grid. A raw model probability like
    0.967731 would otherwise stringify to "0.967731" and be rejected as
    InvalidNumeric -- the bug that blocked every live tennis intent. Round
    half-up onto the 1e4 grid (matching the Rust `format_decimal_ticks` fix) and
    strip trailing zeros so YES/NO sides agree across languages.
    """
    quantized = value.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    text = format(quantized, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


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


def _signal_confidence(probability: Decimal, payload: Mapping[str, object]) -> Decimal:
    supplied = _probability(payload.get("model_confidence"))
    if supplied is None:
        supplied = _probability(payload.get("confidence"))
    if supplied is not None:
        return supplied
    return max(probability, Decimal("1") - probability)


def _bool_parameter(value: object) -> bool:
    parsed = _optional_bool(value)
    return parsed is True


def _optional_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "y", "on"}:
            return True
        if normalized in {"false", "0", "no", "n", "off"}:
            return False
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    return None


def _event_now(ctx: StrategyContext, fallback: datetime) -> datetime:
    value = getattr(ctx, "now", None)
    return value if isinstance(value, datetime) else fallback


@register("sports_tennis_xgboost")
def factory(spec: StrategySpec) -> SportsTennisXgboostStrategy:
    return SportsTennisXgboostStrategy(spec)
