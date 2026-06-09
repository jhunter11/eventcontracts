"""Generic external-edge strategy (slow external probability vs quote mid).

This is the Python mirror of the Rust ``external_edge`` archetype
(`docs/strategy-promotion.md`). It is deliberately model-agnostic: an external
producer scores a market and publishes an ``ExternalSignalEvent`` whose payload
carries a YES probability for a ``market_id``; this strategy compares that
probability to the cached YES mid and buys YES/NO when the absolute edge clears
``min_edge_bps`` (and an optional ``confidence`` clears ``confidence_floor``).

It powers any non-latency sleeve whose alpha is "we have a better probability
than the Kalshi mid and no sharp reference is repricing faster than us":

* ``entertainment-awards``         — Oscars/Emmys/Grammys win probabilities.
* ``equity-index-range-ladder``    — per-bracket terminal-distribution probs.
* future slow alternative-data sleeves.

Latency class: relaxed (signal half-life is minutes-to-days). The decision fires
on signal arrival, not a timer — windowing is the producer's job, exactly like
``entertainment_box_office``.

The order carries a 4dp ``fair_price`` and an executable ``market_snapshot`` so
the post-fee edge gate fires and the risk gate has BBO evidence (V6-S2). Both
YES and NO are expressed as BUYs priced from fair, matching the Rust archetype.
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

# 4dp venue grid: >4dp fair_price is rejected by the risk/gateway (the live
# tennis InvalidNumeric failure mode); also matches Rust 4dp half-up rounding.
FOUR_DP = Decimal("0.0001")

# Payload keys that may carry the producer's YES probability, in priority order.
_PROB_KEYS = ("probability", "implied_prob", "prob", "yes_probability")


class ExternalEdgeStrategy(StrategyBase):
    """Buy when an external YES probability diverges from the quote mid."""

    def __init__(self, spec: StrategySpec) -> None:
        super().__init__(spec)
        self.signal_source = str(spec.parameters.get("signal_source", "external"))
        self.min_edge_bps = Decimal(str(spec.parameters.get("min_edge_bps", "250")))
        self.size = Decimal(str(spec.parameters.get("size", "5")))
        # Confidence gate. Canonical key is ``confidence_floor``; ``min_confidence``
        # is accepted as an alias so a spec written for the Rust archetype (which
        # reads ``min_confidence``) still enforces the gate here — they used to read
        # different keys and silently disagree (audit F2).
        self.confidence_floor = Decimal(str(
            spec.parameters.get(
                "confidence_floor",
                spec.parameters.get("min_confidence", "0"),
            )
        ))
        self.venue = _venue(str(spec.parameters.get("venue", "kalshi")))
        # Cache YES mid + last quote per market so signal-triggered orders carry
        # executable BBO evidence (the runner does not backfill on a signal).
        self._mid_by_market: dict[str, Decimal] = {}
        self._quote_by_market: dict[str, QuoteEvent] = {}

    def on_event(
        self, event: NormalizedEvent, ctx: StrategyContext
    ) -> Sequence[StrategyDecision]:
        if isinstance(event, QuoteEvent):
            mid = _yes_mid(event)
            if mid is not None:
                self._mid_by_market[event.quote.instrument_id.market_id] = mid
                self._quote_by_market[event.quote.instrument_id.market_id] = event
            return (NoAction(reason="quote_mid_updated"),)

        if not isinstance(event, ExternalSignalEvent) or event.source != self.signal_source:
            return (NoAction(reason="ignored:not_external_edge_signal"),)

        market_id = _market_id(event.payload)
        if market_id is None:
            return (NoAction(reason="censored:missing_market_id"),)

        prob = _first_decimal(event.payload, _PROB_KEYS)
        if prob is None:
            return (NoAction(reason="warmup:no_probability_in_payload"),)
        if not (Decimal("0") <= prob <= Decimal("1")):
            return (NoAction(reason="censored:probability_out_of_range"),)

        # Fail-closed: when a floor is configured, a missing/unparseable
        # confidence must NOT pass (it used to — audit F2 fail-open). Mirrors the
        # Rust archetype, which treats absent confidence as 0 when the gate is on.
        confidence = _decimal(event.payload.get("confidence"))
        if self.confidence_floor > 0:
            if confidence is None:
                return (NoAction(reason="confidence_missing_fail_closed"),)
            if confidence < self.confidence_floor:
                return (NoAction(reason="confidence_below_floor"),)

        mid = self._mid_by_market.get(market_id)
        if mid is None:
            return (NoAction(reason="warmup:no_quote_mid"),)

        edge_bps = (prob - mid) * Decimal("10000")
        if abs(edge_bps) < self.min_edge_bps:
            return (NoAction(reason="edge_below_threshold"),)

        return (
            _place(
                venue=self.venue,
                market_id=market_id,
                mid=mid,
                implied_prob=prob,
                edge_bps=edge_bps,
                min_edge_bps=self.min_edge_bps,
                size=self.size,
                quote_event=self._quote_by_market.get(market_id),
                reason=f"external_edge prob={prob} mid={mid} edge_bps={edge_bps}",
            ),
        )


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


def _first_decimal(payload: Mapping[str, object], keys: Sequence[str]) -> Decimal | None:
    for key in keys:
        if key in payload:
            parsed = _decimal(payload.get(key))
            if parsed is not None:
                return parsed
    return None


def _venue(value: str) -> Venue:
    try:
        return Venue(value)
    except ValueError as exc:
        raise ValueError(f"unknown venue: {value}") from exc


@register("external_edge")
def factory(spec: StrategySpec) -> ExternalEdgeStrategy:
    return ExternalEdgeStrategy(spec)
