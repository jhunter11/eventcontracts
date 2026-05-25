"""Crop drought yield reversion strategy.

Thesis: weather and commodity-adjacent event markets tend to extrapolate the
latest drought monitor map. Satellite vegetation anomalies, soil moisture, and
ensemble precipitation forecasts often predict reversion before narrative
markets do. This strategy trades a crop-yield or drought-threshold market from
an external calibrated probability.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import suppress
from decimal import Decimal
from uuid import uuid4

from eventcontracts.domain.decisions import NoAction, PlaceOrder, StrategyDecision
from eventcontracts.domain.events import ExternalSignalEvent, NormalizedEvent, QuoteEvent
from eventcontracts.domain.ids import ClientOrderId
from eventcontracts.domain.latency import ExecutionPriority, LatencyTier
from eventcontracts.domain.models import InstrumentId, OutcomeSide, Venue
from eventcontracts.domain.orders import OrderSide, OrderType, TimeInForce
from eventcontracts.domain.spec import StrategySpec
from eventcontracts.strategy.base import StrategyBase
from eventcontracts.strategy.context import StrategyContext
from eventcontracts.strategy.registry import register


class CropDroughtYieldReversionStrategy(StrategyBase):
    """Satellite and soil-moisture edge for slow ag/weather markets."""

    def __init__(self, spec: StrategySpec) -> None:
        super().__init__(spec)
        self.signal_source = str(spec.parameters.get("signal_source", "crop-water-balance"))
        self.min_edge_bps = Decimal(str(spec.parameters.get("min_edge_bps", "130")))
        self.min_confidence = Decimal(str(spec.parameters.get("min_confidence", "0.55")))
        self.size = Decimal(str(spec.parameters.get("size", "3")))
        self.venue = _venue(str(spec.parameters.get("venue", "kalshi")))
        self._mid_by_market: dict[str, Decimal] = {}

    def on_event(self, event: NormalizedEvent, ctx: StrategyContext) -> Sequence[StrategyDecision]:
        if isinstance(event, QuoteEvent):
            mid = _yes_mid(event)
            if mid is not None:
                self._mid_by_market[event.quote.instrument_id.market_id] = mid
            return (NoAction(reason="quote_mid_updated"),)
        if not isinstance(event, ExternalSignalEvent) or event.source != self.signal_source:
            return (NoAction(reason="ignored:not_crop_signal"),)

        market_id = _market_id(event.payload)
        probability = _bounded_decimal(event.payload.get("yield_reversion_probability"))
        confidence = _bounded_decimal(event.payload.get("confidence")) or Decimal("0")
        if market_id is None or probability is None:
            return (NoAction(reason="censored:missing_market_or_probability"),)
        if confidence < self.min_confidence:
            return (NoAction(reason="confidence_too_low"),)
        mid = self._mid_by_market.get(market_id)
        if mid is None:
            return (NoAction(reason="warmup:no_quote_mid"),)
        edge_bps = (probability - mid) * Decimal("10000")
        if abs(edge_bps) < self.min_edge_bps:
            return (NoAction(reason="edge_below_threshold"),)
        ndvi = event.payload.get("ndvi_anomaly", "na")
        soil = event.payload.get("soil_moisture_pctile", "na")
        return (
            _place(
                venue=self.venue,
                market_id=market_id,
                mid=mid,
                edge_bps=edge_bps,
                size=self.size,
                reason=f"crop_reversion prob={probability} confidence={confidence} ndvi={ndvi} soil={soil}",
            ),
        )


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
        priority=ExecutionPriority(tier=LatencyTier.RELAXED),
    )


def _yes_mid(event: QuoteEvent) -> Decimal | None:
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


@register("crop_drought_yield_reversion")
def factory(spec: StrategySpec) -> CropDroughtYieldReversionStrategy:
    return CropDroughtYieldReversionStrategy(spec)
