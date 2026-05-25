"""Flu hospitalization surge predictor.

Thesis: health-event markets underweight wastewater, pharmacy visits, and ER
chief-complaint signals because official hospitalization data arrives with a
lag. This strategy converts a point-in-time public-health nowcast into a
probability for a hospitalization threshold market.

Rules-mode payload contract:
- source: `signal_source`, default `public-health-nowcast`
- payload["market_id"]
- payload["surge_probability"] in [0, 1]
- optional payload["wastewater_z"], payload["ili_z"], payload["region"]
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


class FluHospitalizationSurgeStrategy(StrategyBase):
    """Slow public-health nowcast edge on hospitalization markets."""

    def __init__(self, spec: StrategySpec) -> None:
        super().__init__(spec)
        self.signal_source = str(spec.parameters.get("signal_source", "public-health-nowcast"))
        self.min_edge_bps = Decimal(str(spec.parameters.get("min_edge_bps", "150")))
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
            return (NoAction(reason="ignored:not_health_signal"),)

        market_id = _market_id(event.payload)
        probability = _decimal(event.payload.get("surge_probability"))
        if market_id is None or probability is None:
            return (NoAction(reason="censored:missing_market_or_probability"),)
        mid = self._mid_by_market.get(market_id)
        if mid is None:
            return (NoAction(reason="warmup:no_quote_mid"),)
        edge_bps = (probability - mid) * Decimal("10000")
        if abs(edge_bps) < self.min_edge_bps:
            return (NoAction(reason="edge_below_threshold"),)
        region = event.payload.get("region", "unknown")
        wastewater_z = event.payload.get("wastewater_z", "na")
        return (
            _place(
                venue=self.venue,
                market_id=market_id,
                mid=mid,
                edge_bps=edge_bps,
                size=self.size,
                reason=f"flu_surge region={region} prob={probability} wastewater_z={wastewater_z}",
            ),
        )


def _place(*, venue: Venue, market_id: str, mid: Decimal, edge_bps: Decimal, size: Decimal, reason: str) -> PlaceOrder:
    side = OutcomeSide.YES if edge_bps > 0 else OutcomeSide.NO
    return PlaceOrder(
        client_order_id=ClientOrderId(uuid4().hex),
        instrument_id=InstrumentId(venue=venue, market_id=market_id),
        outcome_side=side,
        order_side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.GTC,
        quantity=size,
        price=_clip(mid if side is OutcomeSide.YES else Decimal("1") - mid),
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


def _decimal(value: object) -> Decimal | None:
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


@register("flu_hospitalization_surge")
def factory(spec: StrategySpec) -> FluHospitalizationSurgeStrategy:
    return FluHospitalizationSurgeStrategy(spec)
