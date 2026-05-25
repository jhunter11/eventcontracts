"""Liquidity tail-risk insurance strategy.

Thesis: thin event markets often underprice tail outcomes immediately after
liquidity vanishes from the opposing book. This is not a tick-arb: it runs on
REST-level books and trades only when a slow liquidity regime signal persists.
The strategy looks for cheap YES or NO insurance when book depth collapses and
an external tail-risk probability confirms the move.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import suppress
from decimal import Decimal
from uuid import uuid4

from eventcontracts.domain.decisions import NoAction, PlaceOrder, StrategyDecision
from eventcontracts.domain.events import ExternalSignalEvent, NormalizedEvent, OrderBookEvent, QuoteEvent
from eventcontracts.domain.ids import ClientOrderId
from eventcontracts.domain.latency import ExecutionPriority, LatencyTier
from eventcontracts.domain.models import InstrumentId, OrderBookLevel, OutcomeSide, Venue
from eventcontracts.domain.orders import OrderSide, OrderType, TimeInForce
from eventcontracts.domain.spec import StrategySpec
from eventcontracts.strategy.base import StrategyBase
from eventcontracts.strategy.context import StrategyContext
from eventcontracts.strategy.registry import register


class LiquidityTailRiskInsuranceStrategy(StrategyBase):
    """Slow liquidity-regime tail insurance."""

    def __init__(self, spec: StrategySpec) -> None:
        super().__init__(spec)
        self.signal_source = str(spec.parameters.get("signal_source", "tail-risk"))
        self.min_edge_bps = Decimal(str(spec.parameters.get("min_edge_bps", "175")))
        self.max_top_depth = Decimal(str(spec.parameters.get("max_top_depth", "25")))
        self.size = Decimal(str(spec.parameters.get("size", "2")))
        self.venue = _venue(str(spec.parameters.get("venue", "kalshi")))
        self._yes_mid_by_market: dict[str, Decimal] = {}
        self._top_depth_by_market: dict[str, Decimal] = {}

    def on_event(self, event: NormalizedEvent, ctx: StrategyContext) -> Sequence[StrategyDecision]:
        if isinstance(event, QuoteEvent):
            mid = _yes_mid_from_quote(event)
            if mid is not None:
                self._yes_mid_by_market[event.quote.instrument_id.market_id] = mid
            return (NoAction(reason="quote_mid_updated"),)
        if isinstance(event, OrderBookEvent):
            self._track_book(event)
            return (NoAction(reason="book_depth_updated"),)
        if not isinstance(event, ExternalSignalEvent) or event.source != self.signal_source:
            return (NoAction(reason="ignored:not_tail_signal"),)

        market_id = _market_id(event.payload)
        tail_probability = _bounded_decimal(event.payload.get("tail_probability"))
        if market_id is None or tail_probability is None:
            return (NoAction(reason="censored:missing_market_or_probability"),)
        mid = self._yes_mid_by_market.get(market_id)
        top_depth = self._top_depth_by_market.get(market_id)
        if mid is None or top_depth is None:
            return (NoAction(reason="warmup:no_book_state"),)
        if top_depth > self.max_top_depth:
            return (NoAction(reason="liquidity_not_thin_enough"),)
        edge_bps = (tail_probability - mid) * Decimal("10000")
        if abs(edge_bps) < self.min_edge_bps:
            return (NoAction(reason="edge_below_threshold"),)
        return (
            _place(
                venue=self.venue,
                market_id=market_id,
                mid=mid,
                edge_bps=edge_bps,
                size=self.size,
                reason=f"tail_insurance prob={tail_probability} mid={mid} top_depth={top_depth}",
            ),
        )

    def _track_book(self, event: OrderBookEvent) -> None:
        book = event.book
        if book.yes_bids and book.yes_asks:
            self._yes_mid_by_market[book.instrument_id.market_id] = (
                book.yes_bids[0].price + book.yes_asks[0].price
            ) / Decimal("2")
        self._top_depth_by_market[book.instrument_id.market_id] = _top_depth(book.yes_bids, book.yes_asks)


def _place(*, venue: Venue, market_id: str, mid: Decimal, edge_bps: Decimal, size: Decimal, reason: str) -> PlaceOrder:
    side = OutcomeSide.YES if edge_bps > 0 else OutcomeSide.NO
    price = mid if side is OutcomeSide.YES else Decimal("1") - mid
    return PlaceOrder(
        client_order_id=ClientOrderId(uuid4().hex),
        instrument_id=InstrumentId(venue=venue, market_id=market_id),
        outcome_side=side,
        order_side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.GTC,
        quantity=size,
        price=_clip(price),
        reason=reason,
        expected_edge_bps=edge_bps,
        priority=ExecutionPriority(tier=LatencyTier.STANDARD),
    )


def _top_depth(bids: tuple[OrderBookLevel, ...], asks: tuple[OrderBookLevel, ...]) -> Decimal:
    bid_qty = bids[0].quantity if bids else Decimal("0")
    ask_qty = asks[0].quantity if asks else Decimal("0")
    return bid_qty + ask_qty


def _yes_mid_from_quote(event: QuoteEvent) -> Decimal | None:
    quote = event.quote
    if quote.bid is None or quote.ask is None:
        return None
    mid = (quote.bid.price + quote.ask.price) / Decimal("2")
    return mid if quote.side is OutcomeSide.YES else Decimal("1") - mid


def _market_id(payload: Mapping[str, object]) -> str | None:
    value = payload.get("market_id")
    return value if isinstance(value, str) and value else None


def _bounded_decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    with suppress(ValueError, ArithmeticError):
        parsed = Decimal(str(value))
        if Decimal("0") <= parsed <= Decimal("1"):
            return parsed
    return None


def _clip(value: Decimal) -> Decimal:
    return max(Decimal("0.01"), min(Decimal("0.99"), value))


def _venue(value: str) -> Venue:
    try:
        return Venue(value)
    except ValueError as exc:
        raise ValueError(f"unknown venue: {value}") from exc


@register("liquidity_tail_risk_insurance")
def factory(spec: StrategySpec) -> LiquidityTailRiskInsuranceStrategy:
    return LiquidityTailRiskInsuranceStrategy(spec)
