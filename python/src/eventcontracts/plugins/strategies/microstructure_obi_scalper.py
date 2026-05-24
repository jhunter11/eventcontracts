"""Order-book imbalance scalper.

Hypothesis (per docs/strategy-specs.md #2): severe queue imbalances at the
top of book immediately precede short-horizon spread crossings. The strategy
listens to `OrderBookEvent`, computes the L1 imbalance ratio, and emits a
`PlaceOrder` with `FAST` priority when the imbalance crosses the configured
threshold. A `CancelOrder` with `CRITICAL` priority is emitted when an open
order is on the wrong side of an adverse imbalance flip.

Two operating modes are supported:

* **Rules mode** (default): the L1 imbalance ratio compared against
  ``imbalance_threshold`` / ``cancel_threshold`` drives the decision.
  Useful before a model is trained, and as the determinism baseline.
* **Model mode**: when ``StrategySpec.model`` is set and
  ``StrategyContext.feature_vector()`` returns a vector, the strategy
  calls ``ctx.predict(model_name, vector).value`` and routes:

  - logistic-regression style predictions (output in ``[0, 1]``) above
    ``model_long_threshold`` trigger a BUY, below ``model_cancel_threshold``
    trigger a cancel.
  - regression-style predictions (any sign) above
    ``model_long_bps_threshold`` trigger a BUY, below the negative of the
    same trigger a cancel.

  The selection between the two is via the ``model_kind`` parameter
  (``classification`` or ``regression``).
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
        # Model-mode parameters; only used if spec.model is set.
        self.model_kind = str(spec.parameters.get("model_kind", "classification"))
        self.model_long_threshold = Decimal(
            str(spec.parameters.get("model_long_threshold", "0.60"))
        )
        self.model_cancel_threshold = Decimal(
            str(spec.parameters.get("model_cancel_threshold", "0.40"))
        )
        self.model_long_bps_threshold = Decimal(
            str(spec.parameters.get("model_long_bps_threshold", "5"))
        )
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

        # Prefer model mode if the spec declares a model and the context
        # has a current feature vector; fall back to rules otherwise so a
        # missing/unwarmed model never silently halts trading.
        prediction = self._maybe_predict(ctx)
        if prediction is not None:
            return self._decide_from_prediction(book, prediction)
        return self._decide_from_rules(book, imbalance)

    def _maybe_predict(self, ctx: StrategyContext) -> float | None:
        if self.spec.model is None:
            return None
        vector = ctx.feature_vector()
        if vector is None:
            return None
        try:
            return float(ctx.predict(str(self.spec.model.name), vector).value)
        except KeyError:
            # Model declared in spec but runner doesn't have it loaded —
            # safer to fall back to rules than emit decisions on stale state.
            return None

    def _decide_from_rules(
        self, book: OrderBook, imbalance: Decimal
    ) -> Sequence[StrategyDecision]:
        existing = self._open_buy_orders.get(book.instrument_id)
        if imbalance >= self.imbalance_threshold:
            return self._buy(book, reason=f"obi_buy_imbalance_{imbalance:.2f}")
        if existing is not None and imbalance <= self.cancel_threshold:
            return self._cancel(book, existing, reason=f"obi_flip_imbalance_{imbalance:.2f}")
        return (NoAction(reason=f"no_signal_imbalance_{imbalance:.2f}"),)

    def _decide_from_prediction(
        self, book: OrderBook, prediction: float
    ) -> Sequence[StrategyDecision]:
        existing = self._open_buy_orders.get(book.instrument_id)
        if self.model_kind == "classification":
            if Decimal(str(prediction)) >= self.model_long_threshold:
                return self._buy(book, reason=f"model_p={prediction:.3f}")
            if existing is not None and Decimal(str(prediction)) <= self.model_cancel_threshold:
                return self._cancel(book, existing, reason=f"model_p={prediction:.3f}")
            return (NoAction(reason=f"model_idle_p={prediction:.3f}"),)
        # Regression: prediction is in bps; positive = up, negative = down.
        if Decimal(str(prediction)) >= self.model_long_bps_threshold:
            return self._buy(book, reason=f"model_bps={prediction:.1f}")
        if existing is not None and Decimal(str(prediction)) <= -self.model_long_bps_threshold:
            return self._cancel(book, existing, reason=f"model_bps={prediction:.1f}")
        return (NoAction(reason=f"model_idle_bps={prediction:.1f}"),)

    def _buy(self, book: OrderBook, *, reason: str) -> Sequence[StrategyDecision]:
        best_bid = book.yes_bids[0].price if book.yes_bids else None
        if best_bid is None:
            return (NoAction(reason="censored:no_bid_for_placement"),)
        coid = ClientOrderId(uuid4().hex)
        self._open_buy_orders[book.instrument_id] = coid
        return (
            PlaceOrder(
                client_order_id=coid,
                instrument_id=book.instrument_id,
                outcome_side=OutcomeSide.YES,
                order_side=OrderSide.BUY,
                order_type=OrderType.LIMIT,
                time_in_force=TimeInForce.IOC,
                quantity=self.clip_size,
                price=best_bid,
                reason=reason,
                priority=ExecutionPriority(tier=LatencyTier.FAST),
            ),
        )

    def _cancel(
        self, book: OrderBook, existing: ClientOrderId, *, reason: str
    ) -> Sequence[StrategyDecision]:
        self._open_buy_orders.pop(book.instrument_id, None)
        return (
            CancelOrder(
                client_order_id=existing,
                reason=reason,
                priority=ExecutionPriority(tier=LatencyTier.CRITICAL),
            ),
        )


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
