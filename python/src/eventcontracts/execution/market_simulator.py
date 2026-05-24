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
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Protocol

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
)
from eventcontracts.domain.orders import Liquidity, OrderSide, OrderStatus
from eventcontracts.execution.latency import LatencyModel
from eventcontracts.execution.queue import QueueEstimate, QueuePositionEstimator
from eventcontracts.execution.simulator import OrderIntent


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
class PendingOrder:
    """A resting passive order inside the simulator's order book."""

    intent: OrderIntent
    submitted_at: datetime
    effective_at: datetime
    venue_order_id: VenueOrderId
    queue_ahead: Decimal
    filled_quantity: Decimal = Decimal("0")
    status: OrderStatus = OrderStatus.OPEN

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
            state.book = event.book
            return []

        if isinstance(event, QuoteEvent):
            # Quotes do not change resting orders, but we keep last price for marking.
            return []

        if isinstance(event, LifecycleEvent):
            lc = event.lifecycle
            state = self._state(lc.instrument_id)
            state.status = lc.kind
            if lc.kind in (
                MarketLifecycleKind.CLOSED,
                MarketLifecycleKind.DETERMINED,
                MarketLifecycleKind.FINALIZED,
            ):
                self._cancel_all_for(lc.instrument_id, reason="market_closed")
            return []

        if isinstance(event, TradeEvent):
            return self._apply_trade(event)

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
            if pending.intent.price != trade.price:
                continue
            # Only resting orders on the side the trade touched. A
            # trade lifts the asks (a buyer hit ask) or hits the bids
            # (a seller hit bid). We do not know aggressor for sure; use
            # trade.aggressor_side if present, otherwise both sides.
            if trade.aggressor_side is not None:
                # Buyer aggressor lifts asks → our resting SELLs fill.
                if trade.aggressor_side is OutcomeSide.YES and pending.intent.order_side is not OrderSide.SELL:
                    continue
                # Seller aggressor hits bids → our resting BUYs fill.
                if trade.aggressor_side is OutcomeSide.NO and pending.intent.order_side is not OrderSide.BUY:
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
                price=pending.intent.price,
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
        coid = ClientOrderId(intent.metadata.get("client_order_id") or f"sim-{venue_order_id}")
        self.pending[coid] = PendingOrder(
            intent=intent,
            submitted_at=now,
            effective_at=effective_at,
            venue_order_id=venue_order_id,
            queue_ahead=queue_est.ahead_quantity,
        )
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
        opposite = _opposite_levels(state.book, intent.side, intent.order_side)
        fills: list[Fill] = []
        remaining = intent.quantity
        for level in opposite:
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
        # Any unfilled remainder of a marketable limit order rests passive
        # at its original limit price.
        if remaining > 0 and intent.order_type != "market" and not intent.post_only:
            queue_est = self.queue_estimator.estimate(
                instrument_id=intent.instrument_id,
                side=intent.side,
                order_side=intent.order_side.value,
                price=intent.price,
                quantity=remaining,
                book=state.book,
            )
            coid = ClientOrderId(intent.metadata.get("client_order_id") or f"sim-{venue_order_id}")
            rest_intent = OrderIntent(
                instrument_id=intent.instrument_id,
                side=intent.side,
                order_side=intent.order_side,
                price=intent.price,
                quantity=remaining,
                order_type=intent.order_type,
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

    def cancel(self, client_order_id: ClientOrderId) -> bool:
        """Cancel a resting order. Returns True if it was open."""

        pending = self.pending.get(client_order_id)
        if pending is None or pending.status not in (
            OrderStatus.OPEN,
            OrderStatus.PARTIALLY_FILLED,
        ):
            return False
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

        price_changed = new_price is not None and new_price != pending.intent.price
        qty = new_quantity if new_quantity is not None else pending.intent.quantity
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
            post_only=pending.intent.post_only,
            metadata=pending.intent.metadata,
        )
        pending.intent = new_intent
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
