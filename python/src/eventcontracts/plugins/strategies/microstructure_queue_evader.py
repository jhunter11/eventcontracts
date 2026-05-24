"""Queue-position evader (protective cancel of stale resting orders).

Hypothesis (per docs/strategy-specs.md #10): resting orders at the back of
the queue suffer adverse selection on volatility spikes. The strategy
observes `OwnOrderUpdateEvent`s to learn which orders this sleeve owns,
`OrderBookEvent`s to estimate queue position, and `TradeEvent`s to detect
large taker prints. When estimated queue-ahead approaches zero and recent
taker volume is high, it emits a `CancelOrder` with `CRITICAL` priority.

No ML — heuristic queue-position estimator only.

Required spec parameters:
- ``queue_evacuation_threshold`` (default 5): cancel when remaining ahead < this
- ``adverse_volume_threshold`` (default ``"50"``)
- ``recent_volume_window_seconds`` (default 5)
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from eventcontracts.domain.decisions import (
    CancelOrder,
    NoAction,
    StrategyDecision,
)
from eventcontracts.domain.events import (
    NormalizedEvent,
    OrderBookEvent,
    OwnOrderUpdateEvent,
    TradeEvent,
)
from eventcontracts.domain.ids import ClientOrderId
from eventcontracts.domain.latency import ExecutionPriority, LatencyTier
from eventcontracts.domain.models import InstrumentId
from eventcontracts.domain.orders import OrderSide, OrderStatus
from eventcontracts.domain.spec import StrategySpec
from eventcontracts.strategy.base import StrategyBase
from eventcontracts.strategy.context import StrategyContext
from eventcontracts.strategy.registry import register


@dataclass
class _RestingOrder:
    client_order_id: ClientOrderId
    instrument_id: InstrumentId
    side: OrderSide
    price: Decimal
    quantity: Decimal
    estimated_queue_ahead: Decimal = Decimal("0")
    recent_taker_volume: Decimal = Decimal("0")
    last_book_quantity: Decimal = Decimal("0")
    recent_taker_history: list[Decimal] = field(default_factory=list)


class MicrostructureQueueEvaderStrategy(StrategyBase):
    """Protective cancel of resting orders before adverse run-through."""

    def __init__(self, spec: StrategySpec) -> None:
        super().__init__(spec)
        self.queue_evacuation_threshold = Decimal(
            str(spec.parameters.get("queue_evacuation_threshold", "5"))
        )
        self.adverse_volume_threshold = Decimal(
            str(spec.parameters.get("adverse_volume_threshold", "50"))
        )
        self.recent_volume_window_seconds = int(
            spec.parameters.get("recent_volume_window_seconds", 5)
        )
        # Tracked by (instrument_id, price, side).
        self._resting: dict[tuple[InstrumentId, Decimal, OrderSide], _RestingOrder] = {}

    def on_event(
        self, event: NormalizedEvent, ctx: StrategyContext
    ) -> Sequence[StrategyDecision]:
        if isinstance(event, OwnOrderUpdateEvent):
            self._update_own_order(event)
            return (NoAction(reason="own_order_tracked"),)
        if isinstance(event, OrderBookEvent):
            return self._update_queue_estimates(event)
        if isinstance(event, TradeEvent):
            return self._observe_trade(event)
        return (NoAction(reason="ignored:not_book_trade_or_own_order"),)

    def _update_own_order(self, event: OwnOrderUpdateEvent) -> None:
        order = event.order
        key = (order.instrument_id, order.price or Decimal("0"), order.order_side)
        if order.status in (
            OrderStatus.FILLED,
            OrderStatus.CANCELED,
            OrderStatus.EXPIRED,
            OrderStatus.REJECTED,
        ):
            self._resting.pop(key, None)
            return
        existing = self._resting.get(key)
        if existing is None:
            self._resting[key] = _RestingOrder(
                client_order_id=order.client_order_id,
                instrument_id=order.instrument_id,
                side=order.order_side,
                price=order.price or Decimal("0"),
                quantity=order.quantity - order.filled_quantity,
            )
        else:
            existing.client_order_id = order.client_order_id
            existing.quantity = order.quantity - order.filled_quantity

    def _update_queue_estimates(self, event: OrderBookEvent) -> Sequence[StrategyDecision]:
        book = event.book
        instrument_id = book.instrument_id
        for key, resting in list(self._resting.items()):
            if resting.instrument_id != instrument_id:
                continue
            levels = (
                book.yes_bids if resting.side is OrderSide.BUY else book.yes_asks
            )
            depth_at_price = sum(
                (lvl.quantity for lvl in levels if lvl.price == resting.price),
                Decimal("0"),
            )
            # Conservative: when depth shrinks, assume our order moved forward
            # proportionally; when depth grows, we joined the back.
            if depth_at_price < resting.last_book_quantity:
                resting.estimated_queue_ahead = max(
                    Decimal("0"),
                    resting.estimated_queue_ahead
                    - (resting.last_book_quantity - depth_at_price),
                )
            elif resting.last_book_quantity == Decimal("0"):
                resting.estimated_queue_ahead = depth_at_price
            resting.last_book_quantity = depth_at_price
            self._resting[key] = resting
        return (NoAction(reason="queue_estimates_updated"),)

    def _observe_trade(self, event: TradeEvent) -> Sequence[StrategyDecision]:
        trade = event.trade
        decisions: list[StrategyDecision] = []
        for key, resting in list(self._resting.items()):
            if resting.instrument_id != trade.instrument_id:
                continue
            # Trades at or through our price level adversely affect queue position.
            adverse = (
                trade.price <= resting.price
                if resting.side is OrderSide.BUY
                else trade.price >= resting.price
            )
            if adverse:
                resting.recent_taker_volume += trade.quantity
                resting.recent_taker_history.append(trade.quantity)
                if len(resting.recent_taker_history) > 20:
                    drop = resting.recent_taker_history.pop(0)
                    resting.recent_taker_volume -= drop

            should_cancel = (
                resting.estimated_queue_ahead <= self.queue_evacuation_threshold
                and resting.recent_taker_volume >= self.adverse_volume_threshold
            )
            if should_cancel:
                decisions.append(
                    CancelOrder(
                        client_order_id=resting.client_order_id,
                        reason=(
                            f"queue_evade:ahead_{resting.estimated_queue_ahead}"
                            f"_recent_vol_{resting.recent_taker_volume}"
                        ),
                        priority=ExecutionPriority(tier=LatencyTier.CRITICAL),
                    )
                )
                self._resting.pop(key, None)
        if not decisions:
            return (NoAction(reason="trade_observed_no_cancel"),)
        return tuple(decisions)


@register("microstructure_queue_evader")
def factory(spec: StrategySpec) -> MicrostructureQueueEvaderStrategy:
    return MicrostructureQueueEvaderStrategy(spec)
