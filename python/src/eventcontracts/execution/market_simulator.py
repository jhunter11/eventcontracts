"""Market-realistic paper execution simulator.

This is the production-style simulator used by replay backtests and the
paper-trading sleeve. It models:

* venue-specific fees (passed in as a :class:`FeeModel`)
* per-order queue position (passed in as a :class:`QueuePositionEstimator`)
* submit / cancel / replace latency (passed in as a :class:`LatencyModel`)
* taker slippage by walking the opposite side of the book
* passive resting orders that fill as subsequent trades sweep their level
* venue lifecycle states (orders blocked when the market is paused)

The simulator is fed market data via :meth:`on_event`. Strategies submit
orders via :meth:`submit`. Cancels and replaces are first-class.

State lives in this object only — callers should not mutate the
:class:`PendingOrder` or :class:`MarketState` records directly. The
runner stays unaware that this simulator exists; it sees the same
``EventSource`` / ``IntentSink`` ports as everything else.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from eventcontracts.domain.decisions import (
    CancelOrder,
    IntentEnvelope,
    PlaceOrder,
    ReplaceOrder,
)
from eventcontracts.domain.events import (
    LifecycleEvent,
    NormalizedEvent,
    OrderBookEvent,
    QuoteEvent,
    TradeEvent,
)
from eventcontracts.domain.fees import FeeModel, FillContext
from eventcontracts.domain.fills import Fill
from eventcontracts.domain.ids import (
    ClientOrderId,
    CorrelationId,
    FillId,
    SleeveId,
    StrategyId,
    VenueOrderId,
)
from eventcontracts.domain.lifecycle import MarketLifecycleKind
from eventcontracts.domain.models import (
    InstrumentId,
    OrderBook,
    OrderBookLevel,
    OutcomeSide,
    Trade,
)
from eventcontracts.domain.orders import Liquidity, OrderSide, OrderStatus, TimeInForce
from eventcontracts.execution.latency import LatencyModel
from eventcontracts.execution.queue import QueueEstimate, QueuePositionEstimator
from eventcontracts.execution.simulator import OrderIntent, intent_to_order


class FillSink(Protocol):
    """Receives fills produced by the simulator.

    Typical implementations: a PnL accountant, the position keeper, or a
    runner-side queue that wraps fills as ``OwnFillEvent`` to feed back
    into the strategy.
    """

    def on_fill(self, fill: Fill) -> None: ...


@dataclass
class MarketState:
    """Current best book + lifecycle status for one instrument."""

    instrument_id: InstrumentId
    book: OrderBook | None = None
    status: MarketLifecycleKind = MarketLifecycleKind.OPENED
    last_trade_price: Decimal | None = None
    last_trade_at: datetime | None = None


@dataclass
class PendingReplace:
    effective_at: datetime
    new_price: Decimal | None
    new_quantity: Decimal | None


@dataclass
class PendingOrder:
    """A resting passive order inside the simulator's order book."""

    intent: OrderIntent
    submitted_at: datetime
    effective_at: datetime
    venue_order_id: VenueOrderId
    queue_ahead: Decimal
    filled_quantity: Decimal = Decimal("0")
    status: OrderStatus = OrderStatus.OPEN
    active: bool = True
    cancel_effective_at: datetime | None = None
    replace_request: PendingReplace | None = None

    @property
    def remaining(self) -> Decimal:
        return self.intent.quantity - self.filled_quantity


def _opposite_levels(book: OrderBook, side: OutcomeSide, order_side: OrderSide) -> tuple[OrderBookLevel, ...]:
    """Levels a marketable order would consume."""

    if side is OutcomeSide.YES:
        return book.yes_asks if order_side is OrderSide.BUY else book.yes_bids
    return book.no_asks if order_side is OrderSide.BUY else book.no_bids


def _same_side_levels(book: OrderBook, side: OutcomeSide, order_side: OrderSide) -> tuple[OrderBookLevel, ...]:
    """Levels a passive order rests on."""

    if side is OutcomeSide.YES:
        return book.yes_bids if order_side is OrderSide.BUY else book.yes_asks
    return book.no_bids if order_side is OrderSide.BUY else book.no_asks


def _with_opposite_levels_debited(
    book: OrderBook,
    *,
    side: OutcomeSide,
    order_side: OrderSide,
    consumed_by_index: dict[int, Decimal],
) -> OrderBook:
    levels = list(_opposite_levels(book, side, order_side))
    debited: list[OrderBookLevel] = []
    for index, level in enumerate(levels):
        remaining = level.quantity - consumed_by_index.get(index, Decimal("0"))
        if remaining > 0:
            debited.append(OrderBookLevel(price=level.price, quantity=remaining))
    new_levels = tuple(debited)
    if side is OutcomeSide.YES:
        return replace(
            book,
            yes_asks=new_levels if order_side is OrderSide.BUY else book.yes_asks,
            yes_bids=new_levels if order_side is OrderSide.SELL else book.yes_bids,
        )
    return replace(
        book,
        no_asks=new_levels if order_side is OrderSide.BUY else book.no_asks,
        no_bids=new_levels if order_side is OrderSide.SELL else book.no_bids,
    )


def _client_order_id(intent: OrderIntent, venue_order_id: VenueOrderId) -> ClientOrderId:
    return ClientOrderId(intent.metadata.get("client_order_id") or f"sim-{venue_order_id}")


def _is_marketable(
    intent: OrderIntent,
    book: OrderBook | None,
) -> bool:
    """True if the order can immediately match against the opposite book."""

    if book is None or intent.post_only:
        return False
    if intent.order_type == "market":
        return True
    opposite = _opposite_levels(book, intent.side, intent.order_side)
    if not opposite:
        return False
    best = opposite[0]
    if intent.order_side is OrderSide.BUY:
        return intent.price >= best.price
    return intent.price <= best.price


def _passive_fill_price(trade: Trade, intent: OrderIntent) -> Decimal | None:
    """Return the resting order's fill price for a binary-market trade."""

    if trade.side is intent.side and intent.price == trade.price:
        if trade.aggressor_side is intent.side:
            return intent.price if intent.order_side is OrderSide.SELL else None
        if trade.aggressor_side is not None:
            return intent.price if intent.order_side is OrderSide.BUY else None
        return intent.price
    if trade.side is not None and trade.side is not intent.side:
        complement = Decimal("1") - trade.price
        if intent.price != complement:
            return None
        if trade.aggressor_side is trade.side:
            return intent.price if intent.order_side is OrderSide.BUY else None
        if trade.aggressor_side is intent.side:
            return intent.price if intent.order_side is OrderSide.SELL else None
        return intent.price
    if intent.price == trade.price or intent.price == Decimal("1") - trade.price:
        return intent.price
    return None


@dataclass
class MarketPaperSimulator:
    """Stateful simulator producing :class:`Fill` events.

    Construction parameters fully describe the trading model used:
    fee schedule, latency distribution, queue estimator, and an
    optional fill sink that downstream consumers (PnL, the runner)
    register with.
    """

    fee_model: FeeModel
    latency: LatencyModel
    queue_estimator: QueuePositionEstimator
    strategy_id: StrategyId
    sleeve_id: SleeveId
    fill_sink: FillSink | None = None
    states: dict[InstrumentId, MarketState] = field(default_factory=dict)
    pending: dict[ClientOrderId, PendingOrder] = field(default_factory=dict)
    _fill_seq: int = 0
    _order_seq: int = 0

    # ---------- internal helpers ----------

    def _state(self, instrument_id: InstrumentId) -> MarketState:
        state = self.states.get(instrument_id)
        if state is None:
            state = MarketState(instrument_id=instrument_id)
            self.states[instrument_id] = state
        return state

    def _next_fill_id(self) -> FillId:
        self._fill_seq += 1
        return FillId(f"sim-fill-{self._fill_seq:09d}")

    def _next_venue_order_id(self) -> VenueOrderId:
        self._order_seq += 1
        return VenueOrderId(f"sim-ord-{self._order_seq:09d}")

    def _fee_for(
        self,
        intent: OrderIntent,
        fill_price: Decimal,
        fill_qty: Decimal,
        liquidity: Liquidity,
    ) -> tuple[Decimal, str]:
        ctx = FillContext(
            instrument_id=intent.instrument_id,
            side=intent.side,
            price=fill_price,
            quantity=fill_qty,
            liquidity=liquidity.value,
        )
        est = self.fee_model.estimate(ctx)
        return est.amount, est.currency

    def _emit_fill(
        self,
        intent: OrderIntent,
        price: Decimal,
        qty: Decimal,
        when: datetime,
        liquidity: Liquidity,
        venue_order_id: VenueOrderId,
    ) -> Fill:
        fee_amount, fee_ccy = self._fee_for(intent, price, qty, liquidity)
        coid = ClientOrderId(intent.metadata.get("client_order_id") or f"sim-{self._fill_seq}")
        correlation = CorrelationId(intent.metadata.get("correlation_id") or "sim-corr")
        fill = Fill(
            fill_id=self._next_fill_id(),
            venue_order_id=venue_order_id,
            client_order_id=coid,
            instrument_id=intent.instrument_id,
            outcome_side=intent.side,
            order_side=intent.order_side,
            price=price,
            quantity=qty,
            liquidity=liquidity,
            fee_amount=fee_amount,
            fee_currency=fee_ccy,
            filled_at=when,
            exchange_ts=when,
            correlation_id=correlation,
            strategy_id=self.strategy_id,
            sleeve_id=self.sleeve_id,
        )
        if self.fill_sink is not None:
            self.fill_sink.on_fill(fill)
        return fill

    # ---------- market data ----------

    def on_event(self, event: NormalizedEvent) -> list[Fill]:
        """Process an inbound event. Returns fills produced as a side effect.

        Resting passive orders may fill when a trade event sweeps their
        price level. We model that by debiting the queue ahead of us by
        the trade quantity; once queue_ahead reaches zero we start
        receiving fills.
        """

        if isinstance(event, OrderBookEvent):
            state = self._state(event.book.instrument_id)
            fills = self._apply_due_controls(event.book.received_at)
            if state.book is None:
                state.book = event.book
                fills.extend(self._activate_due_orders(event.book.received_at))
                return fills
            fills.extend(self._activate_due_orders(event.book.received_at))
            state.book = event.book
            return fills

        if isinstance(event, QuoteEvent):
            # Quotes do not change resting orders, but they can advance time.
            fills = self._apply_due_controls(event.quote.received_at)
            fills.extend(self._activate_due_orders(event.quote.received_at))
            return fills

        if isinstance(event, LifecycleEvent):
            lc = event.lifecycle
            state = self._state(lc.instrument_id)
            if lc.kind is MarketLifecycleKind.METADATA_UPDATED:
                return []
            state.status = lc.kind
            if lc.kind in (
                MarketLifecycleKind.CLOSED,
                MarketLifecycleKind.DETERMINED,
                MarketLifecycleKind.FINALIZED,
            ):
                self._cancel_all_for(lc.instrument_id, reason="market_closed")
            return []

        if isinstance(event, TradeEvent):
            fills = self._apply_due_controls(event.trade.received_at)
            fills.extend(self._activate_due_orders(event.trade.received_at))
            fills.extend(self._apply_trade(event))
            return fills

        return []

    def _apply_trade(self, event: TradeEvent) -> list[Fill]:
        trade = event.trade
        state = self._state(trade.instrument_id)
        state.last_trade_price = trade.price
        state.last_trade_at = trade.received_at

        fills: list[Fill] = []
        remaining_volume = trade.quantity

        # Resting orders at this exact price can fill. We iterate in
        # submitted_at order so older orders fill first (FIFO at price).
        for pending in sorted(
            (p for p in self.pending.values() if p.intent.instrument_id == trade.instrument_id),
            key=lambda p: p.submitted_at,
        ):
            if remaining_volume <= 0:
                break
            if pending.status not in (OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED):
                continue
            if not pending.active or trade.received_at < pending.effective_at:
                continue
            fill_price = _passive_fill_price(trade, pending.intent)
            if fill_price is None:
                continue
            # Burn through queue first.
            if pending.queue_ahead > 0:
                burn = min(pending.queue_ahead, remaining_volume)
                pending.queue_ahead -= burn
                remaining_volume -= burn
                if pending.queue_ahead > 0:
                    continue

            # Now we are at the front of the queue: take as much as we can.
            take = min(pending.remaining, remaining_volume)
            if take <= 0:
                continue
            fill = self._emit_fill(
                intent=pending.intent,
                price=fill_price,
                qty=take,
                when=trade.received_at,
                liquidity=Liquidity.MAKER,
                venue_order_id=pending.venue_order_id,
            )
            fills.append(fill)
            pending.filled_quantity += take
            remaining_volume -= take
            pending.status = (
                OrderStatus.FILLED
                if pending.filled_quantity == pending.intent.quantity
                else OrderStatus.PARTIALLY_FILLED
            )

        return fills

    def _apply_due_controls(self, now: datetime) -> list[Fill]:
        """Apply cancel/replace requests whose venue latency has elapsed."""

        for client_order_id, pending in list(self.pending.items()):
            if pending.status not in (OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED):
                continue
            if (
                pending.cancel_effective_at is not None
                and pending.cancel_effective_at <= now
            ):
                pending.status = OrderStatus.CANCELED
                pending.cancel_effective_at = None
                continue
            request = pending.replace_request
            if request is not None and request.effective_at <= now:
                self._apply_replace_immediate(
                    client_order_id,
                    new_price=request.new_price,
                    new_quantity=request.new_quantity,
                    now=request.effective_at,
                )
                pending.replace_request = None
        return []

    def _activate_due_orders(self, now: datetime) -> list[Fill]:
        """Move in-flight orders onto the simulated venue after submit latency."""

        fills: list[Fill] = []
        due_ids = [
            client_order_id
            for client_order_id, pending in self.pending.items()
            if (
                not pending.active
                and pending.status in (OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED)
                and pending.effective_at <= now
            )
        ]
        for client_order_id in due_ids:
            pending = self.pending.get(client_order_id)
            if pending is None:
                continue
            state = self._state(pending.intent.instrument_id)
            if _is_marketable(pending.intent, state.book):
                self.pending.pop(client_order_id, None)
                fills.extend(
                    self._fill_marketable(
                        pending.intent,
                        state,
                        pending.effective_at,
                        pending.venue_order_id,
                    )
                )
                continue
            if pending.intent.order_type == "market" or pending.intent.time_in_force in (
                TimeInForce.IOC,
                TimeInForce.FOK,
            ):
                pending.status = OrderStatus.CANCELED
                continue
            queue_est = self.queue_estimator.estimate(
                instrument_id=pending.intent.instrument_id,
                side=pending.intent.side,
                order_side=pending.intent.order_side.value,
                price=pending.intent.price,
                quantity=pending.remaining,
                book=state.book,
            )
            pending.queue_ahead = queue_est.ahead_quantity
            pending.submitted_at = pending.effective_at
            pending.active = True
        return fills

    def flush_inflight(self) -> list[Fill]:
        """Activate all in-flight submits using the latest known market state."""

        fills: list[Fill] = []
        while True:
            due_times = [
                pending.effective_at
                for pending in self.pending.values()
                if (
                    not pending.active
                    and pending.status in (OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED)
                )
            ]
            due_times.extend(
                pending.cancel_effective_at
                for pending in self.pending.values()
                if (
                    pending.cancel_effective_at is not None
                    and pending.status in (OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED)
                )
            )
            due_times.extend(
                pending.replace_request.effective_at
                for pending in self.pending.values()
                if (
                    pending.replace_request is not None
                    and pending.status in (OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED)
                )
            )
            if not due_times:
                return fills
            due_at = min(due_times)
            fills.extend(self._apply_due_controls(due_at))
            fills.extend(self._activate_due_orders(due_at))

    # ---------- intent processing ----------

    def submit(self, intent: OrderIntent, now: datetime) -> list[Fill]:
        """Submit a new order.

        Returns any fills produced immediately (the taker leg of a
        marketable order). Passive orders return an empty list and rest
        in the simulator's pending state until a trade event fills them.

        Raises :class:`ValueError` if the market is paused, closed, or
        unknown.
        """

        state = self._state(intent.instrument_id)
        if state.status in (
            MarketLifecycleKind.PAUSED,
            MarketLifecycleKind.CLOSED,
            MarketLifecycleKind.DETERMINED,
            MarketLifecycleKind.DISPUTED,
            MarketLifecycleKind.FINALIZED,
        ):
            raise ValueError(f"market {intent.instrument_id} is {state.status.value}")

        latency = self.latency.draw("submit")
        effective_at = now + latency
        venue_order_id = self._next_venue_order_id()

        if effective_at > now:
            coid = _client_order_id(intent, venue_order_id)
            self.pending[coid] = PendingOrder(
                intent=intent,
                submitted_at=now,
                effective_at=effective_at,
                venue_order_id=venue_order_id,
                queue_ahead=Decimal("0"),
                active=False,
            )
            return []

        if _is_marketable(intent, state.book):
            return self._fill_marketable(intent, state, effective_at, venue_order_id)

        # Passive: rest with queue estimate.
        queue_est: QueueEstimate = self.queue_estimator.estimate(
            instrument_id=intent.instrument_id,
            side=intent.side,
            order_side=intent.order_side.value,
            price=intent.price,
            quantity=intent.quantity,
            book=state.book,
        )
        coid = _client_order_id(intent, venue_order_id)
        self.pending[coid] = PendingOrder(
            intent=intent,
            submitted_at=now,
            effective_at=effective_at,
            venue_order_id=venue_order_id,
            queue_ahead=queue_est.ahead_quantity,
        )
        return []

    def submit_envelope(self, envelope: IntentEnvelope) -> list[Fill]:
        """Apply an approved runner intent directly to the paper simulator.

        This is the canonical runner-facing API. The older ``submit`` method
        remains as a low-level adapter boundary for gateway tests and venue
        integration code that still materializes ``OrderIntent`` explicitly.
        """

        decision = envelope.decision
        if isinstance(decision, PlaceOrder):
            intent = intent_to_order(envelope)
            if intent is None:
                return []
            return self.submit(intent, envelope.emitted_at)
        if isinstance(decision, CancelOrder):
            self.cancel(decision.client_order_id, now=envelope.emitted_at)
            return []
        if isinstance(decision, ReplaceOrder):
            self.replace(
                decision.client_order_id,
                new_price=decision.new_price,
                new_quantity=decision.new_quantity,
                now=envelope.emitted_at,
            )
            return []
        return []

    def _fill_marketable(
        self,
        intent: OrderIntent,
        state: MarketState,
        when: datetime,
        venue_order_id: VenueOrderId,
    ) -> list[Fill]:
        """Walk the opposite side of the book and emit fills."""

        assert state.book is not None
        opposite = list(_opposite_levels(state.book, intent.side, intent.order_side))
        if intent.time_in_force is TimeInForce.FOK and (
            self._marketable_quantity(intent, tuple(opposite)) < intent.quantity
        ):
            return []
        fills: list[Fill] = []
        remaining = intent.quantity
        consumed_by_index: dict[int, Decimal] = {}
        for index, level in enumerate(opposite):
            if remaining <= 0:
                break
            if intent.order_type != "market":
                if intent.order_side is OrderSide.BUY and intent.price < level.price:
                    break
                if intent.order_side is OrderSide.SELL and intent.price > level.price:
                    break
            take = min(remaining, level.quantity)
            fill = self._emit_fill(
                intent=intent,
                price=level.price,
                qty=take,
                when=when,
                liquidity=Liquidity.TAKER,
                venue_order_id=venue_order_id,
            )
            fills.append(fill)
            remaining -= take
            consumed_by_index[index] = consumed_by_index.get(index, Decimal("0")) + take
        if consumed_by_index:
            state.book = _with_opposite_levels_debited(
                state.book,
                side=intent.side,
                order_side=intent.order_side,
                consumed_by_index=consumed_by_index,
            )
        # Any unfilled remainder of a marketable GTC/GTD limit order rests
        # passive at its original limit price. IOC/FOK remainders cancel.
        if (
            remaining > 0
            and intent.order_type != "market"
            and not intent.post_only
            and intent.time_in_force not in (TimeInForce.IOC, TimeInForce.FOK)
        ):
            queue_est = self.queue_estimator.estimate(
                instrument_id=intent.instrument_id,
                side=intent.side,
                order_side=intent.order_side.value,
                price=intent.price,
                quantity=remaining,
                book=state.book,
            )
            coid = _client_order_id(intent, venue_order_id)
            rest_intent = OrderIntent(
                instrument_id=intent.instrument_id,
                side=intent.side,
                order_side=intent.order_side,
                price=intent.price,
                quantity=remaining,
                order_type=intent.order_type,
                time_in_force=intent.time_in_force,
                post_only=intent.post_only,
                metadata=intent.metadata,
            )
            self.pending[coid] = PendingOrder(
                intent=rest_intent,
                submitted_at=when,
                effective_at=when,
                venue_order_id=venue_order_id,
                queue_ahead=queue_est.ahead_quantity,
                filled_quantity=Decimal("0"),
                status=OrderStatus.PARTIALLY_FILLED if fills else OrderStatus.OPEN,
            )
        return fills

    @staticmethod
    def _marketable_quantity(
        intent: OrderIntent,
        opposite: tuple[OrderBookLevel, ...],
    ) -> Decimal:
        quantity = Decimal("0")
        for level in opposite:
            if intent.order_type != "market":
                if intent.order_side is OrderSide.BUY and intent.price < level.price:
                    break
                if intent.order_side is OrderSide.SELL and intent.price > level.price:
                    break
            quantity += level.quantity
            if quantity >= intent.quantity:
                return quantity
        return quantity

    def cancel(self, client_order_id: ClientOrderId, *, now: datetime | None = None) -> bool:
        """Cancel a resting order. Returns True if it was open."""

        pending = self.pending.get(client_order_id)
        if pending is None or pending.status not in (
            OrderStatus.OPEN,
            OrderStatus.PARTIALLY_FILLED,
        ):
            return False
        if now is not None:
            effective_at = now + self.latency.draw("cancel")
            if effective_at > now:
                pending.cancel_effective_at = effective_at
                return True
        pending.status = OrderStatus.CANCELED
        return True

    def replace(
        self,
        client_order_id: ClientOrderId,
        *,
        new_price: Decimal | None,
        new_quantity: Decimal | None,
        now: datetime,
    ) -> bool:
        """Replace a resting order in place.

        A price change forfeits queue position (the venue treats it as a
        cancel-new). A quantity-only reduction keeps queue position; a
        quantity increase also forfeits.
        """

        pending = self.pending.get(client_order_id)
        if pending is None or pending.status not in (
            OrderStatus.OPEN,
            OrderStatus.PARTIALLY_FILLED,
        ):
            return False

        effective_at = now + self.latency.draw("replace")
        if effective_at > now:
            pending.replace_request = PendingReplace(
                effective_at=effective_at,
                new_price=new_price,
                new_quantity=new_quantity,
            )
            return True

        return self._apply_replace_immediate(
            client_order_id,
            new_price=new_price,
            new_quantity=new_quantity,
            now=effective_at,
        )

    def _apply_replace_immediate(
        self,
        client_order_id: ClientOrderId,
        *,
        new_price: Decimal | None,
        new_quantity: Decimal | None,
        now: datetime,
    ) -> bool:
        pending = self.pending.get(client_order_id)
        if pending is None or pending.status not in (
            OrderStatus.OPEN,
            OrderStatus.PARTIALLY_FILLED,
        ):
            return False

        price_changed = new_price is not None and new_price != pending.intent.price
        qty = new_quantity if new_quantity is not None else pending.intent.quantity
        if qty <= pending.filled_quantity:
            pending.status = OrderStatus.CANCELED
            return True
        qty_increased = qty > pending.intent.quantity
        if price_changed or qty_increased:
            state = self._state(pending.intent.instrument_id)
            queue_est = self.queue_estimator.estimate(
                instrument_id=pending.intent.instrument_id,
                side=pending.intent.side,
                order_side=pending.intent.order_side.value,
                price=new_price if new_price is not None else pending.intent.price,
                quantity=qty,
                book=state.book,
            )
            pending.queue_ahead = queue_est.ahead_quantity
            pending.submitted_at = now

        new_intent = OrderIntent(
            instrument_id=pending.intent.instrument_id,
            side=pending.intent.side,
            order_side=pending.intent.order_side,
            price=new_price if new_price is not None else pending.intent.price,
            quantity=qty,
            order_type=pending.intent.order_type,
            time_in_force=pending.intent.time_in_force,
            post_only=pending.intent.post_only,
            metadata=pending.intent.metadata,
        )
        pending.intent = new_intent
        pending.replace_request = None
        return True

    def _cancel_all_for(self, instrument_id: InstrumentId, reason: str) -> None:
        for pending in self.pending.values():
            if (
                pending.intent.instrument_id == instrument_id
                and pending.status in (OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED)
            ):
                pending.status = OrderStatus.CANCELED

    # ---------- introspection ----------

    def open_orders(self) -> Iterable[PendingOrder]:
        return [p for p in self.pending.values() if p.status in (OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED)]
