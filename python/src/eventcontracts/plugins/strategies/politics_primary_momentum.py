"""Presidential-primary contagion momentum strategy.

Hypothesis (per docs/strategy-specs.md #8): early state wins alter posterior
priors for subsequent states faster than retail can re-price them. The
strategy listens for `SettlementResolvedEvent`s (state-A primary results)
and `QuoteEvent`s on related state-B markets, and when a state-A resolution
diverges from polling priors, it sends an IOC limit order at the latest
state-B executable ask with `FAST` priority.

Implementation note: the spec calls for a Bayesian updating model. The model
pipeline is scaffolded only, so this module runs in **rules mode** — a
simple linear update: ``posterior = prior + impact_beta * (1 - prior)`` when
state-A resolves YES, ``posterior = prior - impact_beta * prior`` when NO.
Trades are taken when ``abs(posterior - state_b_implied) >
min_edge_bps``.

Required spec parameters:
- ``primary_pairs``: TOML array of mappings, each
  ``{state_a, state_b, prior, impact_beta}``
- ``min_edge_bps`` (default 200)
- ``size`` (default 5)
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
from eventcontracts.domain.events import (
    NormalizedEvent,
    QuoteEvent,
    SettlementResolvedEvent,
    market_snapshot_from_quote_event,
)
from eventcontracts.domain.ids import ClientOrderId
from eventcontracts.domain.latency import ExecutionPriority, LatencyTier
from eventcontracts.domain.models import InstrumentId, MarketSnapshot, OutcomeSide, Venue
from eventcontracts.domain.orders import OrderSide, OrderType, TimeInForce
from eventcontracts.domain.spec import StrategySpec
from eventcontracts.strategy.base import StrategyBase
from eventcontracts.strategy.context import StrategyContext
from eventcontracts.strategy.registry import register


@dataclass(frozen=True)
class _PrimaryPair:
    state_a_market_id: str
    state_b_market_id: str
    prior: Decimal
    impact_beta: Decimal


class PoliticsPrimaryMomentumStrategy(StrategyBase):
    """Bayesian-style contagion bets across primary state markets."""

    def __init__(self, spec: StrategySpec) -> None:
        super().__init__(spec)
        self.pairs = _parse_primary_pairs(
            str(spec.parameters.get("primary_pairs", ""))
        )
        self.min_edge_bps = Decimal(str(spec.parameters.get("min_edge_bps", "200")))
        self.size = Decimal(str(spec.parameters.get("size", "5")))
        venue_value = str(spec.parameters.get("venue", "kalshi"))
        try:
            self.venue = Venue(venue_value)
        except ValueError as exc:
            raise ValueError(f"unknown venue: {venue_value}") from exc

        # Latest state-B mid per market_id.
        self._state_b_mids: dict[str, Decimal] = {}
        self._state_b_snapshots: dict[tuple[str, OutcomeSide], MarketSnapshot] = {}
        self._state_b_quotes: dict[str, QuoteEvent] = {}
        # Latest state-A outcome (True for YES, False for NO).
        self._state_a_resolved: dict[str, bool] = {}

    def on_event(
        self, event: NormalizedEvent, ctx: StrategyContext
    ) -> Sequence[StrategyDecision]:
        if isinstance(event, QuoteEvent):
            self._track_state_b(event)
            return (NoAction(reason="state_b_mid_updated"),)
        if not isinstance(event, SettlementResolvedEvent):
            return (NoAction(reason="ignored:not_settlement_or_quote"),)

        market_id = event.settlement.instrument_id.market_id
        pair = next(
            (p for p in self.pairs if p.state_a_market_id == market_id), None
        )
        if pair is None:
            return (NoAction(reason="ignored:state_a_not_tracked"),)

        resolved_side = event.settlement.resolved_side
        if resolved_side is None:
            return (NoAction(reason="censored:state_a_unresolved"),)

        self._state_a_resolved[market_id] = resolved_side is OutcomeSide.YES
        b_mid = self._state_b_mids.get(pair.state_b_market_id)
        if b_mid is None:
            return (NoAction(reason="warmup:state_b_mid_unknown"),)

        if resolved_side is OutcomeSide.YES:
            posterior = pair.prior + pair.impact_beta * (Decimal("1") - pair.prior)
        else:
            posterior = pair.prior - pair.impact_beta * pair.prior

        edge_bps = (posterior - b_mid) * Decimal("10000")
        if abs(edge_bps) < self.min_edge_bps:
            return (NoAction(reason=f"edge_below_threshold:{edge_bps:.0f}bps"),)

        side = OutcomeSide.YES if edge_bps > 0 else OutcomeSide.NO
        snapshot = self._snapshot_for_state_b(pair.state_b_market_id, side)
        if snapshot is None or snapshot.ask is None:
            return (NoAction(reason="warmup:missing_state_b_executable_snapshot"),)
        return (
            PlaceOrder(
                client_order_id=ClientOrderId(uuid4().hex),
                instrument_id=InstrumentId(
                    venue=self.venue, market_id=pair.state_b_market_id
                ),
                outcome_side=side,
                order_side=OrderSide.BUY,
                order_type=OrderType.LIMIT,
                time_in_force=TimeInForce.IOC,
                quantity=self.size,
                price=snapshot.ask.price,
                market_snapshot=snapshot,
                reason=(
                    f"contagion:{pair.state_a_market_id}={resolved_side.value}"
                    f"->state_b_posterior_{posterior:.3f}"
                ),
                expected_edge_bps=edge_bps,
                priority=ExecutionPriority(tier=LatencyTier.FAST),
            ),
        )

    def _track_state_b(self, event: QuoteEvent) -> None:
        quote = event.quote
        if not any(
            p.state_b_market_id == quote.instrument_id.market_id for p in self.pairs
        ):
            return
        if quote.bid is None or quote.ask is None:
            return
        mid = (quote.bid.price + quote.ask.price) / Decimal("2")
        self._state_b_mids[quote.instrument_id.market_id] = mid
        self._state_b_quotes[quote.instrument_id.market_id] = event
        self._state_b_snapshots[(quote.instrument_id.market_id, OutcomeSide.YES)] = (
            market_snapshot_from_quote_event(event, side=OutcomeSide.YES)
        )
        self._state_b_snapshots[(quote.instrument_id.market_id, OutcomeSide.NO)] = (
            market_snapshot_from_quote_event(event, side=OutcomeSide.NO)
        )

    def _snapshot_for_state_b(
        self, market_id: str, side: OutcomeSide
    ) -> MarketSnapshot | None:
        latest_quote = self._state_b_quotes.get(market_id)
        if latest_quote is not None:
            return market_snapshot_from_quote_event(latest_quote, side=side)
        return self._state_b_snapshots.get((market_id, side))


@register("politics_primary_momentum")
def factory(spec: StrategySpec) -> PoliticsPrimaryMomentumStrategy:
    return PoliticsPrimaryMomentumStrategy(spec)


def _parse_primary_pairs(raw: str) -> tuple[_PrimaryPair, ...]:
    """Parse ``state_a:state_b:prior:impact_beta`` entries separated by semicolons."""

    pairs: list[_PrimaryPair] = []
    for item in raw.split(";"):
        if not item.strip():
            continue
        parts = [part.strip() for part in item.split(":")]
        if len(parts) != 4 or not all(parts):
            raise ValueError(
                "primary_pairs must be semicolon-separated "
                "state_a:state_b:prior:impact_beta entries"
            )
        state_a, state_b, prior, impact_beta = parts
        pairs.append(
            _PrimaryPair(
                state_a_market_id=state_a,
                state_b_market_id=state_b,
                prior=Decimal(prior),
                impact_beta=Decimal(impact_beta),
            )
        )
    return tuple(pairs)
