"""CPI / inflation bracket predictor.

Hypothesis (per docs/strategy-specs.md #11): web-scraped retail-price
aggregations and decentralized inflation oracles (Truflation) lead the BLS
CPI release. The strategy listens to `ExternalSignalEvent`s carrying an
alt-data CPI nowcast, combines them with a configured Cleveland-Fed
nowcast, and on a `TimerEvent` fires a `RELAXED`-priority order on the
appropriate inflation-bracket market if the model-implied mean diverges
from the current implied bracket mean by more than `min_shift_bps`.

Implementation note: the spec calls for a Time-Series Transformer. The
model pipeline is scaffolded only, so this module runs in **rules mode** —
implied mean = ``alpha * alt_data + (1 - alpha) * cleveland_nowcast``.

Required spec parameters:
- ``brackets``: ordered TOML array of mappings,
  ``{market_id, lower_bound, upper_bound}``
- ``signal_source`` (default ``"truflation"``)
- ``alpha`` (default ``"0.6"``)
- ``min_shift_bps`` (default ``50``)
- ``size`` (default ``"5"``)
"""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import timedelta
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

TIMER_LABEL = "cpi_recompute"


@dataclass(frozen=True)
class _Bracket:
    market_id: str
    lower_bound: Decimal
    upper_bound: Decimal

    def midpoint(self) -> Decimal:
        return (self.lower_bound + self.upper_bound) / Decimal("2")


class MacroCpiPredictorStrategy(StrategyBase):
    """Alt-data + Cleveland-Fed CPI bracket predictor."""

    def __init__(self, spec: StrategySpec) -> None:
        super().__init__(spec)
        self.brackets = _parse_brackets(str(spec.parameters.get("brackets", "")))
        self.signal_source = str(spec.parameters.get("signal_source", "truflation"))
        self.alpha = Decimal(str(spec.parameters.get("alpha", "0.6")))
        self.min_shift_bps = Decimal(str(spec.parameters.get("min_shift_bps", "50")))
        self.size = Decimal(str(spec.parameters.get("size", "5")))
        self.order_ttl_ms = int(spec.parameters.get("order_ttl_ms", 5000))
        venue_value = str(spec.parameters.get("venue", "kalshi"))
        try:
            self.venue = Venue(venue_value)
        except ValueError as exc:
            raise ValueError(f"unknown venue: {venue_value}") from exc

        self._alt_inflation: Decimal | None = None
        self._cleveland_nowcast: Decimal | None = None
        self._kalshi_implied_mean: Decimal | None = None
        # Latest implied probability per bracket market_id.
        self._bracket_implied: dict[str, Decimal] = {}
        self._bracket_snapshots: dict[str, MarketSnapshot] = {}

    def on_event(self, event: NormalizedEvent, ctx: StrategyContext) -> Sequence[StrategyDecision]:
        if isinstance(event, ExternalSignalEvent):
            self._update_signal(event)
            return (NoAction(reason="signal_updated"),)
        if isinstance(event, QuoteEvent):
            self._track_bracket_mid(event)
            return (NoAction(reason="bracket_mid_updated"),)
        if not isinstance(event, TimerEvent) or event.label != TIMER_LABEL:
            return (NoAction(reason="ignored:not_cpi_timer"),)

        if (
            self._alt_inflation is None
            or self._cleveland_nowcast is None
            or not self._bracket_implied
            or not self.brackets
        ):
            return (NoAction(reason="warmup:insufficient_state"),)

        model_mean = self.alpha * self._alt_inflation + (Decimal("1") - self.alpha) * self._cleveland_nowcast
        # Implied current mean = sum of (bracket midpoint * implied probability).
        if self._kalshi_implied_mean is None:
            implied_mean = sum(
                (
                    bracket.midpoint() * self._bracket_implied.get(bracket.market_id, Decimal("0"))
                    for bracket in self.brackets
                ),
                Decimal("0"),
            )
        else:
            implied_mean = self._kalshi_implied_mean

        shift_bps = (model_mean - implied_mean) * Decimal("10000")
        if abs(shift_bps) < self.min_shift_bps:
            return (NoAction(reason=f"shift_below_threshold:{shift_bps:.0f}bps"),)

        # Find the bracket whose midpoint is closest to the model_mean — go long YES
        # on that bracket and short YES (i.e. buy NO) on the bracket the implied mean
        # currently centers on. Simplification: only place one YES bet on the new mean.
        target_bracket = min(
            self.brackets,
            key=lambda b: abs(b.midpoint() - model_mean),
        )
        target_price = self._bracket_implied.get(target_bracket.market_id)
        if target_price is None:
            return (NoAction(reason="warmup:target_bracket_mid_unknown"),)
        snapshot = self._bracket_snapshots.get(target_bracket.market_id)

        return (
            PlaceOrder(
                client_order_id=ClientOrderId(uuid4().hex),
                instrument_id=InstrumentId(venue=self.venue, market_id=target_bracket.market_id),
                outcome_side=OutcomeSide.YES,
                order_side=OrderSide.BUY,
                order_type=OrderType.LIMIT,
                time_in_force=TimeInForce.GTD,
                quantity=self.size,
                price=target_price,
                expires_at=ctx.now + timedelta(milliseconds=self.order_ttl_ms),
                market_snapshot=snapshot,
                reason=(
                    f"cpi_model_mean_{model_mean:.4f}_implied_{implied_mean:.4f}_target_{target_bracket.market_id}"
                ),
                expected_edge_bps=shift_bps,
                priority=ExecutionPriority(tier=LatencyTier.RELAXED),
            ),
        )

    def _update_signal(self, event: ExternalSignalEvent) -> None:
        if event.source != self.signal_source:
            return
        payload = event.payload
        alt = payload.get("alt_data_mom_inflation")
        cleveland = payload.get("cleveland_fed_nowcast")
        kalshi_implied = payload.get("kalshi_implied_mean")
        if alt is not None:
            with suppress(ValueError, ArithmeticError):
                self._alt_inflation = Decimal(str(alt))
        if cleveland is not None:
            with suppress(ValueError, ArithmeticError):
                self._cleveland_nowcast = Decimal(str(cleveland))
        if kalshi_implied is not None:
            with suppress(ValueError, ArithmeticError):
                self._kalshi_implied_mean = Decimal(str(kalshi_implied))

    def _track_bracket_mid(self, event: QuoteEvent) -> None:
        quote = event.quote
        if not any(b.market_id == quote.instrument_id.market_id for b in self.brackets):
            return
        if quote.bid is None or quote.ask is None:
            return
        mid = (quote.bid.price + quote.ask.price) / Decimal("2")
        self._bracket_implied[quote.instrument_id.market_id] = mid
        self._bracket_snapshots[quote.instrument_id.market_id] = market_snapshot_from_quote_event(
            event,
            side=OutcomeSide.YES,
        )


@register("macro_cpi_predictor")
def factory(spec: StrategySpec) -> MacroCpiPredictorStrategy:
    return MacroCpiPredictorStrategy(spec)


def _parse_brackets(raw: str) -> tuple[_Bracket, ...]:
    """Parse ``market_id:lower_bound:upper_bound`` entries separated by semicolons."""

    brackets: list[_Bracket] = []
    for item in raw.split(";"):
        if not item.strip():
            continue
        parts = [part.strip() for part in item.split(":")]
        if len(parts) != 3 or not all(parts):
            raise ValueError("brackets must be semicolon-separated market_id:lower_bound:upper_bound entries")
        market_id, lower_bound, upper_bound = parts
        brackets.append(
            _Bracket(
                market_id=market_id,
                lower_bound=Decimal(lower_bound),
                upper_bound=Decimal(upper_bound),
            )
        )
    return tuple(brackets)
