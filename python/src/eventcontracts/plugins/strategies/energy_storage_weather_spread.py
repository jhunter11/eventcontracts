"""Energy storage weather spread strategy.

Thesis: power outage, grid emergency, and scarcity-pricing event markets lag
joint signals from load forecasts, renewable intermittency, and battery state of
charge. The strategy converts an external grid-stress signal into a probability
and buys the underpriced side of a relevant event market.

Rules-mode payload contract:
- source: `signal_source`, default `grid-risk`
- payload["market_id"]
- payload["stress_probability"] in [0, 1]
- optional payload["reserve_margin_pct"] and payload["battery_soc_pct"] for reason text
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
from eventcontracts.domain.models import InstrumentId, OutcomeSide, Venue
from eventcontracts.domain.orders import OrderSide, OrderType, TimeInForce
from eventcontracts.domain.spec import StrategySpec
from eventcontracts.strategy.base import StrategyBase
from eventcontracts.strategy.context import StrategyContext
from eventcontracts.strategy.registry import register


class EnergyStorageWeatherSpreadStrategy(StrategyBase):
    """Grid-stress probability strategy for slow weather/load edges."""

    def __init__(self, spec: StrategySpec) -> None:
        super().__init__(spec)
        self.signal_source = str(spec.parameters.get("signal_source", "grid-risk"))
        self.min_edge_bps = Decimal(str(spec.parameters.get("min_edge_bps", "100")))
        self.size = Decimal(str(spec.parameters.get("size", "4")))
        self.venue = _venue(str(spec.parameters.get("venue", "kalshi")))
        self._yes_mid_by_market: dict[str, Decimal] = {}

    def on_event(self, event: NormalizedEvent, ctx: StrategyContext) -> Sequence[StrategyDecision]:
        if isinstance(event, QuoteEvent):
            mid = _yes_mid_from_quote(event)
            if mid is not None:
                self._yes_mid_by_market[event.quote.instrument_id.market_id] = mid
            return (NoAction(reason="quote_mid_updated"),)
        if isinstance(event, OrderBookEvent):
            mid = _yes_mid_from_book(event)
            if mid is not None:
                self._yes_mid_by_market[event.book.instrument_id.market_id] = mid
            return (NoAction(reason="book_mid_updated"),)
        if not isinstance(event, ExternalSignalEvent) or event.source != self.signal_source:
            return (NoAction(reason="ignored:not_grid_signal"),)

        market_id = _market_id(event.payload)
        probability = _decimal(event.payload.get("stress_probability"))
        if market_id is None or probability is None:
            return (NoAction(reason="censored:missing_market_or_probability"),)
        mid = self._yes_mid_by_market.get(market_id)
        if mid is None:
            return (NoAction(reason="warmup:no_market_mid"),)
        edge_bps = (probability - mid) * Decimal("10000")
        if abs(edge_bps) < self.min_edge_bps:
            return (NoAction(reason="edge_below_threshold"),)
        reserve = event.payload.get("reserve_margin_pct", "na")
        soc = event.payload.get("battery_soc_pct", "na")
        return (
            _order(
                venue=self.venue,
                market_id=market_id,
                mid=mid,
                edge_bps=edge_bps,
                size=self.size,
                reason=f"grid_stress prob={probability} mid={mid} reserve={reserve} soc={soc}",
            ),
        )


def _order(*, venue: Venue, market_id: str, mid: Decimal, edge_bps: Decimal, size: Decimal, reason: str) -> PlaceOrder:
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
        price=_clip_prob(price),
        reason=reason,
        expected_edge_bps=edge_bps,
        priority=ExecutionPriority(tier=LatencyTier.RELAXED),
    )


def _yes_mid_from_quote(event: QuoteEvent) -> Decimal | None:
    quote = event.quote
    if quote.bid is None or quote.ask is None:
        return None
    mid = (quote.bid.price + quote.ask.price) / Decimal("2")
    return mid if quote.side is OutcomeSide.YES else Decimal("1") - mid


def _yes_mid_from_book(event: OrderBookEvent) -> Decimal | None:
    book = event.book
    if not book.yes_bids or not book.yes_asks:
        return None
    return (book.yes_bids[0].price + book.yes_asks[0].price) / Decimal("2")


def _market_id(payload: Mapping[str, object]) -> str | None:
    value = payload.get("market_id")
    return value if isinstance(value, str) and value else None


def _decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    with suppress(ValueError, ArithmeticError):
        parsed = Decimal(str(value))
        return parsed if Decimal("0") <= parsed <= Decimal("1") else None
    return None


def _clip_prob(value: Decimal) -> Decimal:
    return max(Decimal("0.01"), min(Decimal("0.99"), value))


def _venue(value: str) -> Venue:
    try:
        return Venue(value)
    except ValueError as exc:
        raise ValueError(f"unknown venue: {value}") from exc


@register("energy_storage_weather_spread")
def factory(spec: StrategySpec) -> EnergyStorageWeatherSpreadStrategy:
    return EnergyStorageWeatherSpreadStrategy(spec)
