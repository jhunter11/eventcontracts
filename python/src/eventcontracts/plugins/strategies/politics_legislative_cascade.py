"""Legislative approval cascade strategy.

Hypothesis (per docs/strategy-specs.md #9): NLP sentiment on swing senators
provides a measurable delta to bill-passage probability. The strategy
listens to `ExternalSignalEvent`s carrying per-senator sentiment readings
and updates a leveraged whip-count score. When the cumulative score crosses
the negative threshold, it cancels existing YES bets with `CRITICAL`
priority and places a NO bet at `STANDARD`.

Implementation note: the spec calls for a fine-tuned RoBERTa LLM. The model
pipeline is scaffolded only, so this module runs in **rules mode** — the
sentiment score and whip-leverage factor are read directly from the
external signal payload. Replace with `ctx.predict(...)` once the LLM
runner exists.

Required spec parameters:
- ``bill_market_id``: market_id of the bill-passage contract
- ``signal_source`` (default ``"gdelt-roberta"``)
- ``negative_threshold`` (default ``"-1.5"``)
- ``protective_yes_coid``: client_order_id of the YES bet to cancel when
  cumulative sentiment turns sharply negative (optional)
- ``size`` (default ``"10"``)
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from eventcontracts.domain.decisions import (
    CancelOrder,
    NoAction,
    PlaceOrder,
    StrategyDecision,
)
from eventcontracts.domain.events import ExternalSignalEvent, NormalizedEvent
from eventcontracts.domain.ids import ClientOrderId
from eventcontracts.domain.latency import ExecutionPriority, LatencyTier
from eventcontracts.domain.models import InstrumentId, OutcomeSide, Venue
from eventcontracts.domain.orders import OrderSide, OrderType, TimeInForce
from eventcontracts.domain.spec import StrategySpec
from eventcontracts.strategy.base import StrategyBase, StrategyFeedback
from eventcontracts.strategy.context import StrategyContext
from eventcontracts.strategy.registry import register


class PoliticsLegislativeCascadeStrategy(StrategyBase):
    """Sentiment-driven cancel-and-flip on legislative passage markets."""

    def __init__(self, spec: StrategySpec) -> None:
        super().__init__(spec)
        self.bill_market_id = str(spec.parameters["bill_market_id"])
        self.signal_source = str(spec.parameters.get("signal_source", "gdelt-roberta"))
        self.negative_threshold = Decimal(
            str(spec.parameters.get("negative_threshold", "-1.5"))
        )
        self.size = Decimal(str(spec.parameters.get("size", "10")))
        self.score_ttl_secs = int(spec.parameters.get("score_ttl_secs", 86400))
        protective = spec.parameters.get("protective_yes_coid")
        self.protective_yes_coid = (
            ClientOrderId(str(protective)) if protective else None
        )
        venue_value = str(spec.parameters.get("venue", "kalshi"))
        try:
            self.venue = Venue(venue_value)
        except ValueError as exc:
            raise ValueError(f"unknown venue: {venue_value}") from exc

        # Cumulative weighted sentiment across observed senators.
        self._cumulative_score: Decimal = Decimal("0")
        # Per-senator latest score so re-observations replace prior values.
        self._per_senator: dict[str, tuple[Decimal, datetime]] = {}

    def on_event(
        self, event: NormalizedEvent, ctx: StrategyContext
    ) -> Sequence[StrategyDecision]:
        if not isinstance(event, ExternalSignalEvent):
            return (NoAction(reason="ignored:not_external_signal"),)
        if event.source != self.signal_source:
            return (NoAction(reason=f"ignored:source!={self.signal_source}"),)
        self._evict_expired_scores(ctx.now)

        payload = event.payload
        if payload.get("bill_market_id") != self.bill_market_id:
            return (NoAction(reason="ignored:other_bill"),)

        senator_id = payload.get("senator_id")
        sentiment = payload.get("sentiment_score")
        whip_leverage = payload.get("whip_leverage_factor", "1")
        if not isinstance(senator_id, str) or sentiment is None:
            return (NoAction(reason="censored:missing_sentiment_or_senator"),)
        try:
            sentiment_d = Decimal(str(sentiment))
            leverage_d = Decimal(str(whip_leverage))
        except (ValueError, ArithmeticError):
            return (NoAction(reason="censored:unparsable_sentiment"),)

        prior = self._per_senator.get(senator_id, (Decimal("0"), ctx.now))[0]
        weighted = sentiment_d * leverage_d
        self._per_senator[senator_id] = (weighted, ctx.now)
        self._cumulative_score = self._cumulative_score - prior + weighted

        if self._cumulative_score >= self.negative_threshold:
            return (NoAction(reason=f"score_above_threshold:{self._cumulative_score:.2f}"),)

        decisions: list[StrategyDecision] = []
        if self.protective_yes_coid is not None:
            decisions.append(
                CancelOrder(
                    client_order_id=self.protective_yes_coid,
                    reason=f"cascade_negative_score_{self._cumulative_score:.2f}",
                    priority=ExecutionPriority(tier=LatencyTier.CRITICAL),
                )
            )
        decisions.append(
            PlaceOrder(
                client_order_id=ClientOrderId(uuid4().hex),
                instrument_id=InstrumentId(
                    venue=self.venue, market_id=self.bill_market_id
                ),
                outcome_side=OutcomeSide.NO,
                order_side=OrderSide.BUY,
                order_type=OrderType.LIMIT,
                time_in_force=TimeInForce.GTC,
                quantity=self.size,
                price=Decimal("0.50"),
                reason=f"sentiment_cascade_score_{self._cumulative_score:.2f}",
            )
        )
        return tuple(decisions)

    def on_feedback(self, feedback: StrategyFeedback, ctx: StrategyContext) -> None:
        del ctx
        if feedback.kind not in {"IntentAccepted", "VenueAcked"}:
            return
        if self.protective_yes_coid is None:
            return
        decision = feedback.envelope.decision
        if (
            isinstance(decision, CancelOrder)
            and decision.client_order_id == self.protective_yes_coid
        ):
            self.protective_yes_coid = None

    def _evict_expired_scores(self, now: datetime) -> None:
        cutoff = now - timedelta(seconds=self.score_ttl_secs)
        for senator_id, (score, updated_at) in list(self._per_senator.items()):
            if updated_at < cutoff:
                self._per_senator.pop(senator_id, None)
                self._cumulative_score -= score


@register("politics_legislative_cascade")
def factory(spec: StrategySpec) -> PoliticsLegislativeCascadeStrategy:
    return PoliticsLegislativeCascadeStrategy(spec)
