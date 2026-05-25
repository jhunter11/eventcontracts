"""Tariff headline gap fader.

Thesis: event-contract markets overreact to first-order tariff headlines, then
mean-revert once policy text, exemption lists, or implementation calendars are
parsed. This strategy listens for an external NLP signal with a calibrated
`policy_probability` and fades quote gaps where the market-implied probability
deviates from that calibrated probability by more than `min_edge_bps`.

Rules-mode payload contract:
- source: `signal_source`, default `policy-nlp`
- payload["market_id"] or payload["instrument_id"]["market_id"]
- payload["policy_probability"] in [0, 1]
- optional payload["headline_shock_bps"] to require a minimum shock filter
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


class TariffHeadlineGapFaderStrategy(StrategyBase):
    """Fade headline-driven overshoots in trade-policy markets."""

    def __init__(self, spec: StrategySpec) -> None:
        super().__init__(spec)
        self.signal_source = str(spec.parameters.get("signal_source", "policy-nlp"))
        self.min_edge_bps = Decimal(str(spec.parameters.get("min_edge_bps", "125")))
        self.min_headline_shock_bps = Decimal(str(spec.parameters.get("min_headline_shock_bps", "50")))
        self.size = Decimal(str(spec.parameters.get("size", "5")))
        self.venue = _venue(str(spec.parameters.get("venue", "kalshi")))
        self._yes_mid_by_market: dict[str, Decimal] = {}

    def on_event(self, event: NormalizedEvent, ctx: StrategyContext) -> Sequence[StrategyDecision]:
        if isinstance(event, QuoteEvent):
            self._track_quote(event)
            return (NoAction(reason="quote_mid_updated"),)
        if not isinstance(event, ExternalSignalEvent) or event.source != self.signal_source:
            return (NoAction(reason="ignored:not_policy_signal"),)

        market_id = _market_id(event.payload)
        probability = _decimal(event.payload.get("policy_probability"))
        shock = _decimal(event.payload.get("headline_shock_bps")) or Decimal("0")
        if market_id is None or probability is None:
            return (NoAction(reason="censored:missing_market_or_probability"),)
        if abs(shock) < self.min_headline_shock_bps:
            return (NoAction(reason="shock_filter_not_met"),)
        mid = self._yes_mid_by_market.get(market_id)
        if mid is None:
            return (NoAction(reason="warmup:no_quote_mid"),)

        edge_bps = (probability - mid) * Decimal("10000")
        if abs(edge_bps) < self.min_edge_bps:
            return (NoAction(reason="edge_below_threshold"),)
        return (
            _order(
                venue=self.venue,
                market_id=market_id,
                yes_mid=mid,
                edge_bps=edge_bps,
                size=self.size,
                reason=f"tariff_gap_fade prob={probability} mid={mid} shock={shock}",
            ),
        )

    def _track_quote(self, event: QuoteEvent) -> None:
        mid = _yes_mid_from_quote(event)
        if mid is not None:
            self._yes_mid_by_market[event.quote.instrument_id.market_id] = mid


def _order(
    *,
    venue: Venue,
    market_id: str,
    yes_mid: Decimal,
    edge_bps: Decimal,
    size: Decimal,
    reason: str,
) -> PlaceOrder:
    side = OutcomeSide.YES if edge_bps > 0 else OutcomeSide.NO
    price = yes_mid if side is OutcomeSide.YES else Decimal("1") - yes_mid
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


def _market_id(payload: Mapping[str, object]) -> str | None:
    direct = payload.get("market_id")
    if isinstance(direct, str) and direct:
        return direct
    instrument = payload.get("instrument_id")
    if isinstance(instrument, Mapping):
        nested = instrument.get("market_id")
        if isinstance(nested, str) and nested:
            return nested
    return None


def _decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    with suppress(ValueError, ArithmeticError):
        return Decimal(str(value))
    return None


def _clip_prob(value: Decimal) -> Decimal:
    return max(Decimal("0.01"), min(Decimal("0.99"), value))


def _venue(value: str) -> Venue:
    try:
        return Venue(value)
    except ValueError as exc:
        raise ValueError(f"unknown venue: {value}") from exc


@register("tariff_headline_gap_fader")
def factory(spec: StrategySpec) -> TariffHeadlineGapFaderStrategy:
    return TariffHeadlineGapFaderStrategy(spec)
