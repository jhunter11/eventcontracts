"""Spatiotemporal temperature arbitrage strategy.

Hypothesis (per docs/strategy-specs.md #1): retail traders over-extrapolate
morning temperatures and ignore systemic afternoon cloud-cover dynamics that
cap daily highs. The strategy reads a point-in-time weather forecast
(Open-Meteo / NOAA HRRR ingested as `ExternalSignalEvent`) and compares the
forecast-implied probability of hitting a Kalshi temperature bracket against
the current market mid. When the edge exceeds `min_edge_bps`, it submits a
passive limit order.

Implementation note: the spec lists a "Spatiotemporal Transformer" as the
model. The model pipeline is still scaffolded in this repo, so this module
runs in **rules mode**: the `ExternalSignalEvent.payload["implied_prob"]`
field is treated as the forecast-implied probability directly. Once the model
runner is implemented, swap the rules-mode read for `ctx.predict(...)`. The
decision shape (PlaceOrder vs NoAction) is unchanged.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
from uuid import uuid4

from eventcontracts.domain.decisions import (
    NoAction,
    PlaceOrder,
    StrategyDecision,
)
from eventcontracts.domain.events import (
    ExternalSignalEvent,
    NormalizedEvent,
    QuoteEvent,
)
from eventcontracts.domain.ids import ClientOrderId
from eventcontracts.domain.models import InstrumentId, OutcomeSide
from eventcontracts.domain.orders import OrderSide, OrderType, TimeInForce
from eventcontracts.domain.spec import StrategySpec
from eventcontracts.strategy.base import StrategyBase
from eventcontracts.strategy.context import StrategyContext
from eventcontracts.strategy.registry import register


class WeatherTemperatureArbitrageStrategy(StrategyBase):
    """Edge-based passive limit order on Kalshi temperature brackets."""

    def __init__(self, spec: StrategySpec) -> None:
        super().__init__(spec)
        self.min_edge_bps = Decimal(str(spec.parameters.get("min_edge_bps", "75")))
        self.max_size = Decimal(str(spec.parameters.get("max_size", "10")))
        self.kelly_fraction = Decimal(str(spec.parameters.get("kelly_fraction", "0.10")))
        self.signal_source = str(spec.parameters.get("signal_source", "open-meteo"))
        # Last known market mid per instrument; updated on QuoteEvent.
        self._mid_by_instrument: dict[InstrumentId, Decimal] = {}

    def on_event(
        self, event: NormalizedEvent, ctx: StrategyContext
    ) -> Sequence[StrategyDecision]:
        if isinstance(event, QuoteEvent):
            self._track_mid(event)
            return (NoAction(reason="quote_mid_updated"),)
        if not isinstance(event, ExternalSignalEvent):
            return (NoAction(reason="ignored:not_external_signal"),)
        if event.source != self.signal_source:
            return (NoAction(reason=f"ignored:source!={self.signal_source}"),)

        implied_prob = event.payload.get("implied_prob")
        instrument_payload = event.payload.get("instrument_id")
        if implied_prob is None or instrument_payload is None:
            return (NoAction(reason="censored:missing_forecast_or_instrument"),)

        try:
            forecast_prob = Decimal(str(implied_prob))
        except (ValueError, ArithmeticError):
            return (NoAction(reason="censored:forecast_unparsable"),)
        if not Decimal("0") <= forecast_prob <= Decimal("1"):
            return (NoAction(reason="censored:forecast_out_of_range"),)

        instrument_id = self._instrument_from_payload(instrument_payload)
        if instrument_id is None:
            return (NoAction(reason="censored:instrument_unresolved"),)

        mid = self._mid_by_instrument.get(instrument_id)
        if mid is None:
            return (NoAction(reason="warmup:no_mid_yet"),)

        edge_bps = (forecast_prob - mid) * Decimal("10000")
        if abs(edge_bps) < self.min_edge_bps:
            return (NoAction(reason="edge_below_threshold"),)

        side = OutcomeSide.YES if edge_bps > 0 else OutcomeSide.NO
        order_side = OrderSide.BUY
        # Conservative passive sizing: scale by abs(edge) and Kelly fraction,
        # capped by configured max_size.
        edge_fraction = min(abs(edge_bps) / Decimal("10000"), Decimal("1"))
        raw_size = (edge_fraction * self.kelly_fraction * self.max_size).quantize(
            Decimal("1")
        )
        size = max(raw_size, Decimal("1"))
        limit_price = mid if side is OutcomeSide.YES else Decimal("1") - mid

        return (
            PlaceOrder(
                client_order_id=ClientOrderId(uuid4().hex),
                instrument_id=instrument_id,
                outcome_side=side,
                order_side=order_side,
                order_type=OrderType.LIMIT,
                time_in_force=TimeInForce.GTC,
                quantity=size,
                price=limit_price,
                reason=f"edge_{edge_bps:+.0f}bps_vs_mid_{mid}",
                expected_edge_bps=edge_bps,
            ),
        )

    def _track_mid(self, event: QuoteEvent) -> None:
        quote = event.quote
        if quote.bid is None or quote.ask is None:
            return
        mid = (quote.bid.price + quote.ask.price) / Decimal("2")
        self._mid_by_instrument[quote.instrument_id] = mid

    @staticmethod
    def _instrument_from_payload(payload: object) -> InstrumentId | None:
        if not isinstance(payload, Mapping):
            return None
        venue = payload.get("venue")
        market_id = payload.get("market_id")
        if not isinstance(venue, str) or not isinstance(market_id, str):
            return None
        from eventcontracts.domain.models import Venue

        try:
            venue_enum = Venue(venue)
        except ValueError:
            return None
        outcome_id = payload.get("outcome_id")
        outcome_str = outcome_id if isinstance(outcome_id, str) else None
        return InstrumentId(
            venue=venue_enum,
            market_id=market_id,
            outcome_id=outcome_str,
        )


@register("weather_temperature_arbitrage")
def factory(spec: StrategySpec) -> WeatherTemperatureArbitrageStrategy:
    return WeatherTemperatureArbitrageStrategy(spec)
