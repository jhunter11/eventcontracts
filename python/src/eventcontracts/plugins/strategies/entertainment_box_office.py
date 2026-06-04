"""Box-office velocity extrapolator (promoted, signal-triggered).

Hypothesis (per docs/strategy-specs.md #7): Friday-night seat-booking velocity
(from theater capacity scrapes) strongly correlates with total weekend gross.

Promotion note: the live Rust runtime has no timer event, so the decision fires
on the external signal's arrival rather than a Friday-8pm `TimerEvent` — the
"only act Friday night" gating becomes the producer's job. The seat-occupancy /
ticket-velocity -> implied weekend gross -> YES-probability model is computed
here AND mirrored bit-for-bit in the Rust ``EntertainmentBoxOfficeStrategy`` so
the two languages agree on the parity fixtures.

The strategy caches the YES mid per market from `QuoteEvent`s, and on an
`ExternalSignalEvent` from ``signal_source`` extrapolates the weekend gross,
maps it to an implied YES probability, and buys YES/NO when the absolute edge
clears `min_edge_bps` (and the signal confidence clears `confidence_floor`). The
order carries `fair_price` + a `market_snapshot` so the post-fee edge gate fires
and the risk gate has executable BBO evidence (V6-T3 / V6-S2).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import suppress
from decimal import ROUND_HALF_UP, Decimal
from uuid import uuid4

from eventcontracts.domain.decisions import NoAction, PlaceOrder, StrategyDecision
from eventcontracts.domain.events import (
    ExternalSignalEvent,
    NormalizedEvent,
    QuoteEvent,
    market_snapshot_from_quote_event,
)
from eventcontracts.domain.ids import ClientOrderId
from eventcontracts.domain.latency import ExecutionPriority, LatencyTier
from eventcontracts.domain.models import InstrumentId, OutcomeSide, Venue
from eventcontracts.domain.orders import OrderSide, OrderType, TimeInForce
from eventcontracts.domain.spec import StrategySpec
from eventcontracts.strategy.base import StrategyBase
from eventcontracts.strategy.context import StrategyContext
from eventcontracts.strategy.pricing import buy_limit_from_fair
from eventcontracts.strategy.registry import register

# Emit fair_price on the 4dp venue grid the risk gate accepts (the >4dp
# fair_price reject is the live-tennis InvalidNumeric failure mode); this also
# matches the Rust `format_decimal_ticks` 4dp half-up rounding for parity.
FOUR_DP = Decimal("0.0001")


class EntertainmentBoxOfficeStrategy(StrategyBase):
    """Friday-night extrapolation of weekend box-office gross (signal-triggered)."""

    def __init__(self, spec: StrategySpec) -> None:
        super().__init__(spec)
        self.market_id = str(spec.parameters.get("market_id", ""))
        self.signal_source = str(spec.parameters.get("signal_source", "apify-fandango"))
        self.target_gross = Decimal(str(spec.parameters["target_gross_usd"]))
        self.baseline_gross = Decimal(str(spec.parameters.get("baseline_gross_usd", "1000000")))
        self.extrapolation_hours = Decimal(str(spec.parameters.get("extrapolation_hours", "48")))
        self.min_edge_bps = Decimal(str(spec.parameters.get("min_edge_bps", "500")))
        self.confidence_floor = Decimal(str(spec.parameters.get("confidence_floor", "0.8")))
        self.size = Decimal(str(spec.parameters.get("size", "10")))
        self.venue = _venue(str(spec.parameters.get("venue", "kalshi")))
        self._mid_by_market: dict[str, Decimal] = {}
        # Cache the latest quote so the order carries executable BBO evidence;
        # orders fire on the external signal (not a quote), so the runner does
        # not backfill a snapshot — without this the risk gate rejects every
        # order with `missing_market_snapshot` (V6-S2).
        self._quote_by_market: dict[str, QuoteEvent] = {}

    def on_event(self, event: NormalizedEvent, ctx: StrategyContext) -> Sequence[StrategyDecision]:
        if isinstance(event, QuoteEvent):
            mid = _yes_mid(event)
            if mid is not None:
                self._mid_by_market[event.quote.instrument_id.market_id] = mid
                self._quote_by_market[event.quote.instrument_id.market_id] = event
            return (NoAction(reason="quote_mid_updated"),)
        if not isinstance(event, ExternalSignalEvent) or event.source != self.signal_source:
            return (NoAction(reason="ignored:not_box_office_signal"),)

        market_id = _market_id(event.payload)
        if market_id is None or (self.market_id and market_id != self.market_id):
            return (NoAction(reason="censored:missing_or_unmatched_market"),)
        occ = _decimal(event.payload.get("seat_occupancy_pct"))
        velocity = _decimal(event.payload.get("ticket_velocity_per_hour"))
        confidence = _decimal(event.payload.get("confidence")) or Decimal("0")
        if occ is None or velocity is None:
            return (NoAction(reason="warmup:insufficient_signal"),)
        if confidence < self.confidence_floor:
            return (NoAction(reason="confidence_below_floor"),)
        mid = self._mid_by_market.get(market_id)
        if mid is None:
            return (NoAction(reason="warmup:no_quote_mid"),)

        extrapolated = occ * self.baseline_gross + velocity * self.extrapolation_hours
        implied_prob = self._gross_to_prob(extrapolated)
        edge_bps = (implied_prob - mid) * Decimal("10000")
        if abs(edge_bps) < self.min_edge_bps:
            return (NoAction(reason="edge_below_threshold"),)
        return (
            _place(
                venue=self.venue,
                market_id=market_id,
                mid=mid,
                implied_prob=implied_prob,
                edge_bps=edge_bps,
                min_edge_bps=self.min_edge_bps,
                size=self.size,
                quote_event=self._quote_by_market.get(market_id),
                reason=(
                    f"box_office prob={implied_prob} occ={occ} velocity={velocity} "
                    f"confidence={confidence}"
                ),
            ),
        )

    def _gross_to_prob(self, extrapolated: Decimal) -> Decimal:
        ratio = extrapolated / self.target_gross if self.target_gross > 0 else Decimal("0")
        # Linear clamp between 0.05 and 0.95 around the target ratio of 1.0
        # (mirrors Rust `gross_to_prob_ticks`).
        if ratio <= Decimal("0.5"):
            return Decimal("0.05")
        if ratio >= Decimal("1.5"):
            return Decimal("0.95")
        return Decimal("0.05") + (ratio - Decimal("0.5")) * Decimal("0.9")


def _place(
    *,
    venue: Venue,
    market_id: str,
    mid: Decimal,
    implied_prob: Decimal,
    edge_bps: Decimal,
    min_edge_bps: Decimal,
    size: Decimal,
    quote_event: QuoteEvent | None,
    reason: str,
) -> PlaceOrder:
    side = OutcomeSide.YES if edge_bps > 0 else OutcomeSide.NO
    raw_price = mid if side is OutcomeSide.YES else Decimal("1") - mid
    fair = implied_prob if side is OutcomeSide.YES else Decimal("1") - implied_prob
    fair_price = fair.quantize(FOUR_DP, rounding=ROUND_HALF_UP)
    snapshot = (
        market_snapshot_from_quote_event(quote_event, side=side)
        if quote_event is not None
        else None
    )
    return PlaceOrder(
        client_order_id=ClientOrderId(uuid4().hex),
        instrument_id=InstrumentId(venue=venue, market_id=market_id),
        outcome_side=side,
        order_side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.GTC,
        quantity=size,
        market_snapshot=snapshot,
        # Both YES and NO are BUYs -> floor the per-side price to the venue cent
        # (never pay above fair). Parity-matched to Rust `buy_limit_from_fair`.
        price=buy_limit_from_fair(raw_price),
        reason=reason,
        expected_edge_bps=edge_bps,
        priority=ExecutionPriority(tier=LatencyTier.RELAXED),
        metadata={
            "fair_price": str(fair_price),
            "min_executable_edge_ticks": str(int(min_edge_bps)),
            "fee_rate_bps": "700",
        },
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
        return Decimal(str(value))
    return None


def _venue(value: str) -> Venue:
    try:
        return Venue(value)
    except ValueError as exc:
        raise ValueError(f"unknown venue: {value}") from exc


@register("entertainment_box_office")
def factory(spec: StrategySpec) -> EntertainmentBoxOfficeStrategy:
    return EntertainmentBoxOfficeStrategy(spec)
