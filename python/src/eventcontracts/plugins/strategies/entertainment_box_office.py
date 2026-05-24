"""Box-office velocity extrapolator.

Hypothesis (per docs/strategy-specs.md #7): Friday-night seat-booking
velocity (from theater capacity scrapes) strongly correlates with total
weekend gross. The strategy triggers on a Friday 8:00 PM ET `TimerEvent`,
reads the latest seat-occupancy and ticket-velocity readings from
`ExternalSignalEvent` state, and emits a passive limit order if the
implied edge exceeds `min_edge_bps`.

Implementation note: the spec calls for an ARIMA time-series extrapolator.
The model pipeline is scaffolded only, so this module runs in **rules
mode** — extrapolated weekend gross = `seat_occupancy_pct * baseline_gross
+ ticket_velocity_per_hour * extrapolation_hours`. A linear mapping turns
the extrapolated gross into an implied YES probability via a sigmoid-style
clamp around the spec's `target_gross` parameter.
"""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import suppress
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
    TimerEvent,
)
from eventcontracts.domain.ids import ClientOrderId
from eventcontracts.domain.latency import ExecutionPriority, LatencyTier
from eventcontracts.domain.models import InstrumentId, OutcomeSide, Venue
from eventcontracts.domain.orders import OrderSide, OrderType, TimeInForce
from eventcontracts.domain.spec import StrategySpec
from eventcontracts.strategy.base import StrategyBase
from eventcontracts.strategy.context import StrategyContext
from eventcontracts.strategy.registry import register

TIMER_LABEL = "friday_8pm_et"


class EntertainmentBoxOfficeStrategy(StrategyBase):
    """Friday-night extrapolation of weekend box-office gross."""

    def __init__(self, spec: StrategySpec) -> None:
        super().__init__(spec)
        self.market_id = str(spec.parameters["market_id"])
        venue_value = str(spec.parameters.get("venue", "kalshi"))
        try:
            self.venue = Venue(venue_value)
        except ValueError as exc:
            raise ValueError(f"unknown venue: {venue_value}") from exc

        self.signal_source = str(spec.parameters.get("signal_source", "apify-fandango"))
        self.target_gross = Decimal(str(spec.parameters["target_gross_usd"]))
        self.baseline_gross = Decimal(
            str(spec.parameters.get("baseline_gross_usd", "1000000"))
        )
        self.extrapolation_hours = Decimal(
            str(spec.parameters.get("extrapolation_hours", "48"))
        )
        self.min_edge_bps = Decimal(str(spec.parameters.get("min_edge_bps", "500")))
        self.size = Decimal(str(spec.parameters.get("size", "10")))
        self.confidence_floor = Decimal(
            str(spec.parameters.get("confidence_floor", "0.8"))
        )

        # Mutable rolling state.
        self._seat_occupancy_pct: Decimal | None = None
        self._ticket_velocity_per_hour: Decimal | None = None
        self._signal_confidence: Decimal = Decimal("0")
        self._last_mid: Decimal | None = None

    def on_event(
        self, event: NormalizedEvent, ctx: StrategyContext
    ) -> Sequence[StrategyDecision]:
        if isinstance(event, ExternalSignalEvent):
            self._update_from_signal(event)
            return (NoAction(reason="box_office_signal_updated"),)
        if isinstance(event, QuoteEvent):
            self._update_mid(event)
            return (NoAction(reason="mid_updated"),)
        if not isinstance(event, TimerEvent) or event.label != TIMER_LABEL:
            return (NoAction(reason="ignored:not_friday_timer"),)

        if (
            self._seat_occupancy_pct is None
            or self._ticket_velocity_per_hour is None
            or self._last_mid is None
        ):
            return (NoAction(reason="warmup:insufficient_state"),)
        if self._signal_confidence < self.confidence_floor:
            return (NoAction(reason="confidence_below_floor"),)

        extrapolated = (
            self._seat_occupancy_pct * self.baseline_gross
            + self._ticket_velocity_per_hour * self.extrapolation_hours
        )
        implied_prob = self._gross_to_prob(extrapolated)
        edge_bps = (implied_prob - self._last_mid) * Decimal("10000")
        if abs(edge_bps) < self.min_edge_bps:
            return (NoAction(reason=f"edge_below_threshold:{edge_bps:.0f}bps"),)

        side = OutcomeSide.YES if edge_bps > 0 else OutcomeSide.NO
        return (
            PlaceOrder(
                client_order_id=ClientOrderId(uuid4().hex),
                instrument_id=InstrumentId(
                    venue=self.venue, market_id=self.market_id
                ),
                outcome_side=side,
                order_side=OrderSide.BUY,
                order_type=OrderType.LIMIT,
                time_in_force=TimeInForce.GTC,
                quantity=self.size,
                price=self._last_mid,
                reason=(
                    f"box_office_extrapolated_{extrapolated:.0f}_implied_"
                    f"{implied_prob:.3f}"
                ),
                expected_edge_bps=edge_bps,
                priority=ExecutionPriority(tier=LatencyTier.RELAXED),
            ),
        )

    def _update_from_signal(self, event: ExternalSignalEvent) -> None:
        if event.source != self.signal_source:
            return
        payload = event.payload
        occ = payload.get("seat_occupancy_pct")
        vel = payload.get("ticket_velocity_per_hour")
        conf = payload.get("confidence")
        if occ is not None:
            with suppress(ValueError, ArithmeticError):
                self._seat_occupancy_pct = Decimal(str(occ))
        if vel is not None:
            with suppress(ValueError, ArithmeticError):
                self._ticket_velocity_per_hour = Decimal(str(vel))
        if conf is not None:
            try:
                self._signal_confidence = Decimal(str(conf))
            except (ValueError, ArithmeticError):
                self._signal_confidence = Decimal("0")

    def _update_mid(self, event: QuoteEvent) -> None:
        quote = event.quote
        if quote.instrument_id.market_id != self.market_id:
            return
        if quote.bid is None or quote.ask is None:
            return
        self._last_mid = (quote.bid.price + quote.ask.price) / Decimal("2")

    def _gross_to_prob(self, extrapolated: Decimal) -> Decimal:
        ratio = extrapolated / self.target_gross if self.target_gross > 0 else Decimal("0")
        # Linear clamp between 0.05 and 0.95 around the target ratio of 1.0.
        if ratio <= Decimal("0.5"):
            return Decimal("0.05")
        if ratio >= Decimal("1.5"):
            return Decimal("0.95")
        return Decimal("0.05") + (ratio - Decimal("0.5")) * Decimal("0.9")


@register("entertainment_box_office")
def factory(spec: StrategySpec) -> EntertainmentBoxOfficeStrategy:
    return EntertainmentBoxOfficeStrategy(spec)
