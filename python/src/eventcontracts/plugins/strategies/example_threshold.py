"""Reference strategy.

The smallest possible strategy that exercises the full plug-in surface:

* reads a parameter from its spec
* watches trade events on a single instrument
* emits a ``PlaceOrder`` when the last trade price crosses a threshold
* emits ``NoAction`` otherwise so the decision log stays explicit

The point of this file is to show *how* a new strategy is added — copy this
shape, drop it into ``strategies/``, register it with a unique name.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

from eventcontracts.domain.decisions import (
    NoAction,
    PlaceOrder,
    StrategyDecision,
)
from eventcontracts.domain.events import (
    NormalizedEvent,
    QuoteEvent,
    TradeEvent,
    market_snapshot_from_quote_event,
)
from eventcontracts.domain.ids import ClientOrderId
from eventcontracts.domain.models import InstrumentId, MarketSnapshot, OutcomeSide
from eventcontracts.domain.orders import OrderSide, OrderType, TimeInForce
from eventcontracts.domain.spec import StrategySpec
from eventcontracts.strategy.base import StrategyBase
from eventcontracts.strategy.context import StrategyContext
from eventcontracts.strategy.registry import register


class ExampleThresholdStrategy(StrategyBase):
    """Buy YES when trade price falls below ``buy_below``."""

    def __init__(self, spec: StrategySpec) -> None:
        super().__init__(spec)
        self.buy_below = Decimal(str(spec.parameters.get("buy_below", "0.40")))
        self.size = Decimal(str(spec.parameters.get("size", "10")))
        self.order_ttl_ms = int(spec.parameters.get("order_ttl_ms", 5000))
        self._snapshots: dict[InstrumentId, MarketSnapshot] = {}

    def on_event(self, event: NormalizedEvent, ctx: StrategyContext) -> Sequence[StrategyDecision]:
        if isinstance(event, QuoteEvent):
            self._snapshots[event.quote.instrument_id] = market_snapshot_from_quote_event(
                event,
                side=OutcomeSide.YES,
            )
            return (NoAction(reason="quote_snapshot_updated"),)
        if not isinstance(event, TradeEvent):
            return (NoAction(reason="ignored: not a trade"),)
        trade = event.trade
        if trade.price >= self.buy_below:
            return (NoAction(reason="price above threshold"),)
        snapshot = self._snapshots.get(trade.instrument_id)
        if snapshot is None or snapshot.ask is None:
            return (NoAction(reason="missing_executable_quote_snapshot"),)
        return (
            PlaceOrder(
                client_order_id=ClientOrderId(uuid4().hex),
                instrument_id=trade.instrument_id,
                outcome_side=OutcomeSide.YES,
                order_side=OrderSide.BUY,
                order_type=OrderType.LIMIT,
                time_in_force=TimeInForce.GTD,
                quantity=self.size,
                price=snapshot.ask.price,
                expires_at=ctx.now + timedelta(milliseconds=self.order_ttl_ms),
                market_snapshot=snapshot,
                reason=(f"trade {trade.price} below threshold {self.buy_below}; quoted ask {snapshot.ask.price}"),
            ),
        )


@register("example_threshold")
def factory(spec: StrategySpec) -> ExampleThresholdStrategy:
    return ExampleThresholdStrategy(spec)
