"""Order-book imbalance scalper.

Hypothesis (per docs/strategy-specs.md #2): severe queue imbalances at the
top of book immediately precede short-horizon spread crossings. The strategy
listens to `OrderBookEvent`, computes the L1 imbalance ratio, and emits a
`PlaceOrder` with `FAST` priority when the imbalance crosses the configured
threshold. A `CancelOrder` with `CRITICAL` priority is emitted when an open
order is on the wrong side of an adverse imbalance flip.

Implementation note: the spec lists a LightGBM classifier. The model pipeline
is still scaffolded, so this module runs in **rules mode**: imbalance ratio
threshold drives the decision directly. Swap for `ctx.predict(...)` once the
runner is implemented; decision shape (PlaceOrder/CancelOrder) is unchanged.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from uuid import uuid4

from eventcontracts.domain.decisions import (
    CancelOrder,
    NoAction,
    PlaceOrder,
    StrategyDecision,
)
from eventcontracts.domain.events import NormalizedEvent, OrderBookEvent
from eventcontracts.domain.ids import ClientOrderId
from eventcontracts.domain.latency import ExecutionPriority, LatencyTier
from eventcontracts.domain.models import InstrumentId, OrderBook, OutcomeSide
from eventcontracts.domain.orders import OrderSide, OrderType, TimeInForce
from eventcontracts.domain.spec import StrategySpec
from eventcontracts.strategy.base import StrategyBase
from eventcontracts.strategy.context import StrategyContext
from eventcontracts.strategy.registry import register


class MicrostructureObiScalperStrategy(StrategyBase):
    """L1 imbalance scalper with protective cancels."""

    def __init__(self, spec: StrategySpec) -> None:
        super().__init__(spec)
        self.imbalance_threshold = Decimal(
            str(spec.parameters.get("imbalance_threshold", "0.70"))
        )
        self.cancel_threshold = Decimal(
            str(spec.parameters.get("cancel_threshold", "0.30"))
        )
        self.clip_size = Decimal(str(spec.parameters.get("clip_size", "5")))
        self.max_spread_bps = Decimal(str(spec.parameters.get("max_spread_bps", "100")))
        self._open_buy_orders: dict[InstrumentId, ClientOrderId] = {}

    def on_event(
        self, event: NormalizedEvent, ctx: StrategyContext
    ) -> Sequence[StrategyDecision]:
        if not isinstance(event, OrderBookEvent):
            return (NoAction(reason="ignored:not_book"),)

        book = event.book
        bid_qty, ask_qty = _l1_quantities(book)
        if bid_qty <= 0 and ask_qty <= 0:
            return (NoAction(reason="censored:empty_book"),)

        total = bid_qty + ask_qty
        imbalance = bid_qty / total if total > 0 else Decimal("0")

        spread_bps = _spread_bps(book)
        if spread_bps is None:
            return (NoAction(reason="censored:no_two_sided_book"),)
        if spread_bps > self.max_spread_bps:
            return (NoAction(reason="ignored:spread_too_wide"),)

        decisions: list[StrategyDecision] = []
        existing = self._open_buy_orders.get(book.instrument_id)

        if imbalance >= self.imbalance_threshold:
            best_bid = book.yes_bids[0].price if book.yes_bids else None
            if best_bid is None:
                return (NoAction(reason="censored:no_bid_for_placement"),)
            coid = ClientOrderId(uuid4().hex)
            self._open_buy_orders[book.instrument_id] = coid
            decisions.append(
                PlaceOrder(
                    client_order_id=coid,
                    instrument_id=book.instrument_id,
                    outcome_side=OutcomeSide.YES,
                    order_side=OrderSide.BUY,
                    order_type=OrderType.LIMIT,
                    time_in_force=TimeInForce.IOC,
                    quantity=self.clip_size,
                    price=best_bid,
                    reason=f"obi_buy_imbalance_{imbalance:.2f}",
                    priority=ExecutionPriority(tier=LatencyTier.FAST),
                )
            )
        elif existing is not None and imbalance <= self.cancel_threshold:
            decisions.append(
                CancelOrder(
                    client_order_id=existing,
                    reason=f"obi_flip_imbalance_{imbalance:.2f}",
                    priority=ExecutionPriority(tier=LatencyTier.CRITICAL),
                )
            )
            self._open_buy_orders.pop(book.instrument_id, None)

        if not decisions:
            return (NoAction(reason=f"no_signal_imbalance_{imbalance:.2f}"),)
        return tuple(decisions)


def _l1_quantities(book: OrderBook) -> tuple[Decimal, Decimal]:
    bid_qty = book.yes_bids[0].quantity if book.yes_bids else Decimal("0")
    ask_qty = book.yes_asks[0].quantity if book.yes_asks else Decimal("0")
    return bid_qty, ask_qty


def _spread_bps(book: OrderBook) -> Decimal | None:
    if not book.yes_bids or not book.yes_asks:
        return None
    bid = book.yes_bids[0].price
    ask = book.yes_asks[0].price
    if bid <= 0 or ask <= 0:
        return None
    mid = (bid + ask) / Decimal("2")
    if mid == 0:
        return None
    return (ask - bid) / mid * Decimal("10000")


@register("microstructure_obi_scalper")
def factory(spec: StrategySpec) -> MicrostructureObiScalperStrategy:
    return MicrostructureObiScalperStrategy(spec)
