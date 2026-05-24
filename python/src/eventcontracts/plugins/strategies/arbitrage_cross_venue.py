"""Cross-venue quote arbitrage between Kalshi and Polymarket.

Hypothesis (per docs/strategy-specs.md #4): Kalshi and Polymarket pools are
disjoint, occasionally creating risk-free arbs after fees. Pure rules engine,
no ML.

Trigger: `QuoteEvent` from either venue. The strategy keeps the latest
best-bid/best-ask per `(market_id, venue)` and, when both venues have current
two-sided quotes, computes the cross-venue spread net of taker fees. When
`(other_bid - this_ask) > min_edge_bps + fee_bps`, it emits a market order
on the side this sleeve controls (the spec's `parameters.controlled_venue`).

The strategy explicitly does *not* leg both sides — it only places the order
on the venue it controls and assumes the other venue's quote represents
existing inventory or a manual offset.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from uuid import uuid4

from eventcontracts.domain.decisions import (
    NoAction,
    PlaceOrder,
    StrategyDecision,
)
from eventcontracts.domain.events import NormalizedEvent, QuoteEvent
from eventcontracts.domain.ids import ClientOrderId
from eventcontracts.domain.latency import ExecutionPriority, LatencyTier
from eventcontracts.domain.models import OutcomeSide, Venue
from eventcontracts.domain.orders import OrderSide, OrderType, TimeInForce
from eventcontracts.domain.spec import StrategySpec
from eventcontracts.strategy.base import StrategyBase
from eventcontracts.strategy.context import StrategyContext
from eventcontracts.strategy.registry import register


@dataclass
class _BestQuote:
    bid: Decimal | None = None
    bid_qty: Decimal | None = None
    ask: Decimal | None = None
    ask_qty: Decimal | None = None


class ArbitrageCrossVenueStrategy(StrategyBase):
    """Cross-venue marketable arbitrage on a single shared market_id."""

    def __init__(self, spec: StrategySpec) -> None:
        super().__init__(spec)
        controlled = str(spec.parameters.get("controlled_venue", "kalshi"))
        try:
            self.controlled_venue = Venue(controlled)
        except ValueError as exc:
            raise ValueError(f"unknown controlled_venue: {controlled}") from exc
        self.min_edge_bps = Decimal(str(spec.parameters.get("min_edge_bps", "100")))
        self.kalshi_taker_fee_bps = Decimal(
            str(spec.parameters.get("kalshi_taker_fee_bps", "175"))
        )
        self.poly_taker_fee_bps = Decimal(
            str(spec.parameters.get("poly_taker_fee_bps", "200"))
        )
        self.max_clip_size = Decimal(str(spec.parameters.get("max_clip_size", "20")))
        # Top of book per (market_id, venue).
        self._book: dict[tuple[str, Venue], _BestQuote] = {}

    def on_event(
        self, event: NormalizedEvent, ctx: StrategyContext
    ) -> Sequence[StrategyDecision]:
        if not isinstance(event, QuoteEvent):
            return (NoAction(reason="ignored:not_quote"),)

        quote = event.quote
        instrument = quote.instrument_id
        venue = instrument.venue
        key = (instrument.market_id, venue)
        entry = self._book.setdefault(key, _BestQuote())
        if quote.bid is not None:
            entry.bid = quote.bid.price
            entry.bid_qty = quote.bid.quantity
        if quote.ask is not None:
            entry.ask = quote.ask.price
            entry.ask_qty = quote.ask.quantity

        # Need both venues quoting on the same market_id.
        other_venue = self._opposite_venue(venue)
        other = self._book.get((instrument.market_id, other_venue))
        this = self._book[key]
        if other is None or other.bid is None or this.ask is None:
            return (NoAction(reason="warmup:waiting_for_both_venues"),)

        fee_bps = self.kalshi_taker_fee_bps + self.poly_taker_fee_bps
        edge_bps = (other.bid - this.ask) / this.ask * Decimal("10000")
        if edge_bps <= self.min_edge_bps + fee_bps:
            return (NoAction(reason=f"no_arb:edge_{edge_bps:.0f}bps"),)

        if venue is not self.controlled_venue:
            return (NoAction(reason=f"observe_only:venue={venue.value}"),)

        size = min(self.max_clip_size, other.bid_qty or self.max_clip_size, this.ask_qty or self.max_clip_size)
        if size <= 0:
            return (NoAction(reason="censored:no_size_available"),)

        return (
            PlaceOrder(
                client_order_id=ClientOrderId(uuid4().hex),
                instrument_id=instrument,
                outcome_side=OutcomeSide.YES,
                order_side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                time_in_force=TimeInForce.IOC,
                quantity=size,
                reason=f"cross_venue_arb_edge_{edge_bps:.0f}bps",
                expected_edge_bps=edge_bps,
                priority=ExecutionPriority(tier=LatencyTier.FAST),
            ),
        )

    def _opposite_venue(self, venue: Venue) -> Venue:
        if venue is Venue.KALSHI:
            # Two Polymarket SKUs exist; the operator configures which one is
            # the cross-venue counterparty.
            counter = str(self.spec.parameters.get("counter_venue", "polymarket_global"))
            try:
                return Venue(counter)
            except ValueError as exc:
                raise ValueError(f"unknown counter_venue: {counter}") from exc
        return Venue.KALSHI


@register("arbitrage_cross_venue")
def factory(spec: StrategySpec) -> ArbitrageCrossVenueStrategy:
    return ArbitrageCrossVenueStrategy(spec)
