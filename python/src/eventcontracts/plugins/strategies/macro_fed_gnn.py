"""Federal Reserve rate-network propagation strategy.

Hypothesis (per docs/strategy-specs.md #5): retail updates front-month
Fed-funds futures markets quickly but fails to propagate conditional
probabilities to +2/+3 month Fed-funds bracket markets. The strategy watches
front-month `TradeEvent`s (mid drift) and back-month `QuoteEvent`s, and
when the front-month move implies a back-month repricing greater than the
spec's `min_edge_bps`, it submits a limit order on the back-month.

Implementation note: the spec lists a Graph Neural Network. The model
pipeline is still scaffolded; this module runs in **rules mode** — the
propagation is a linear `propagation_beta * front_change_bps` heuristic that
the GNN will eventually replace. Decision shape (PlaceOrder) is unchanged.

Required spec parameters:
- ``front_month_market_id``: market_id of the front-month contract
- ``back_month_market_id``: market_id of the back-month contract
- ``min_edge_bps`` (default 50)
- ``propagation_beta`` (default "0.6", dimensionless)
- ``size`` (default "10")
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
from eventcontracts.domain.events import (
    NormalizedEvent,
    QuoteEvent,
    TradeEvent,
)
from eventcontracts.domain.ids import ClientOrderId
from eventcontracts.domain.latency import ExecutionPriority, LatencyTier
from eventcontracts.domain.models import InstrumentId, OutcomeSide
from eventcontracts.domain.orders import OrderSide, OrderType, TimeInForce
from eventcontracts.domain.spec import StrategySpec
from eventcontracts.strategy.base import StrategyBase
from eventcontracts.strategy.context import StrategyContext
from eventcontracts.strategy.registry import register


class MacroFedGnnStrategy(StrategyBase):
    """Rules-mode propagation of front-month moves into back-month brackets."""

    def __init__(self, spec: StrategySpec) -> None:
        super().__init__(spec)
        self.front_market_id = str(spec.parameters["front_month_market_id"])
        self.back_market_id = str(spec.parameters["back_month_market_id"])
        self.min_edge_bps = Decimal(str(spec.parameters.get("min_edge_bps", "50")))
        self.beta = Decimal(str(spec.parameters.get("propagation_beta", "0.6")))
        self.size = Decimal(str(spec.parameters.get("size", "10")))
        # Track last-known prices.
        self._front_anchor: Decimal | None = None
        self._back_mid: Decimal | None = None
        self._back_instrument: InstrumentId | None = None

    def on_event(
        self, event: NormalizedEvent, ctx: StrategyContext
    ) -> Sequence[StrategyDecision]:
        if isinstance(event, TradeEvent):
            return self._handle_front_trade(event)
        if isinstance(event, QuoteEvent):
            return self._handle_back_quote(event)
        return (NoAction(reason="ignored:not_trade_or_quote"),)

    def _handle_front_trade(self, event: TradeEvent) -> Sequence[StrategyDecision]:
        if event.trade.instrument_id.market_id != self.front_market_id:
            return (NoAction(reason="ignored:not_front_market"),)

        new_price = event.trade.price
        if self._front_anchor is None:
            self._front_anchor = new_price
            return (NoAction(reason="warmup:front_anchor_set"),)

        front_change_bps = (
            (new_price - self._front_anchor) / self._front_anchor * Decimal("10000")
        )
        self._front_anchor = new_price

        if (
            self._back_mid is None
            or self._back_instrument is None
            or abs(front_change_bps) < self.min_edge_bps
        ):
            return (NoAction(reason=f"no_signal:front_move_{front_change_bps:.0f}bps"),)

        implied_back_change_bps = front_change_bps * self.beta
        target_back_price = self._back_mid * (
            Decimal("1") + implied_back_change_bps / Decimal("10000")
        )
        target_back_price = max(Decimal("0.01"), min(Decimal("0.99"), target_back_price))

        side = OutcomeSide.YES if implied_back_change_bps > 0 else OutcomeSide.NO
        order_side = OrderSide.BUY
        return (
            PlaceOrder(
                client_order_id=ClientOrderId(uuid4().hex),
                instrument_id=self._back_instrument,
                outcome_side=side,
                order_side=order_side,
                order_type=OrderType.LIMIT,
                time_in_force=TimeInForce.GTC,
                quantity=self.size,
                price=target_back_price,
                reason=(
                    f"fed_propagation_front_{front_change_bps:+.0f}bps"
                    f"_back_target_{target_back_price}"
                ),
                expected_edge_bps=implied_back_change_bps,
                priority=ExecutionPriority(tier=LatencyTier.FAST),
            ),
        )

    def _handle_back_quote(self, event: QuoteEvent) -> Sequence[StrategyDecision]:
        if event.quote.instrument_id.market_id != self.back_market_id:
            return (NoAction(reason="ignored:not_back_market"),)

        quote = event.quote
        if quote.bid is None or quote.ask is None:
            return (NoAction(reason="censored:no_two_sided_back_quote"),)

        self._back_instrument = quote.instrument_id
        self._back_mid = (quote.bid.price + quote.ask.price) / Decimal("2")
        return (NoAction(reason="back_quote_tracked"),)


@register("macro_fed_gnn")
def factory(spec: StrategySpec) -> MacroFedGnnStrategy:
    return MacroFedGnnStrategy(spec)
