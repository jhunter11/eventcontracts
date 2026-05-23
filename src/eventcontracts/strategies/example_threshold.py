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
from decimal import Decimal
from uuid import uuid4

from eventcontracts.domain.decisions import (
    NoAction,
    PlaceOrder,
    StrategyDecision,
)
from eventcontracts.domain.events import NormalizedEvent, TradeEvent
from eventcontracts.domain.ids import ClientOrderId
from eventcontracts.domain.models import OutcomeSide
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

    def on_event(
        self, event: NormalizedEvent, ctx: StrategyContext
    ) -> Sequence[StrategyDecision]:
        if not isinstance(event, TradeEvent):
            return (NoAction(reason="ignored: not a trade"),)
        trade = event.trade
        if trade.price >= self.buy_below:
            return (NoAction(reason="price above threshold"),)
        return (
            PlaceOrder(
                client_order_id=ClientOrderId(uuid4().hex),
                instrument_id=trade.instrument_id,
                outcome_side=OutcomeSide.YES,
                order_side=OrderSide.BUY,
                order_type=OrderType.LIMIT,
                time_in_force=TimeInForce.GTC,
                quantity=self.size,
                price=trade.price,
                reason=f"trade {trade.price} below threshold {self.buy_below}",
            ),
        )


@register("example_threshold")
def factory(spec: StrategySpec) -> ExampleThresholdStrategy:
    return ExampleThresholdStrategy(spec)
