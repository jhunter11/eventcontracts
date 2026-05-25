"""Crypto 15-min cross-strike skew arbitrage.

Hypothesis
----------
Across strikes ``K1 < K2 < ... < Kn`` at the same Kalshi 15-min
expiry, the probability ``P(S_T >= K)`` must be monotone non-increasing
in ``K`` under any arbitrage-free pricing. Each strike's "above"
bracket (``settles above K``) therefore has a Kalshi mid that should
decrease in ``K``. When two adjacent strikes violate this — the
higher strike claims a *larger* P(YES) than the lower strike — the
pair is a butterfly arbitrage: buy YES on the cheap (lower-strike)
side and buy NO on the rich (higher-strike) side; the spread closes
by expiry regardless of which side wins.

Game theory
-----------
Butterfly violations exist because each strike has its own MM cohort.
Information asymmetry inside a single 15-min cohort, plus the
diminutive size at any single strike, makes it expensive for one MM
to enforce monotonicity across the whole grid. The strategy
opportunistically harvests these crossings.

Required spec parameters
------------------------
- ``strike_market_map``: semicolon-separated ``market_id:strike``
  entries in **ascending strike order**. The strategy enforces the
  ordering at load time.
- ``min_skew_edge`` (default ``"0.01"``): minimum
  ``p_high - p_low`` (probability gap) to fire.
- ``max_spread_bps`` (default ``"500"``): skip the pair if either
  leg's spread is wider than this.
- ``size`` (default ``"3"``): contracts per leg.
- ``venue`` (default ``"kalshi"``).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from uuid import uuid4

from eventcontracts.crypto import monotone_violations
from eventcontracts.domain.decisions import (
    NoAction,
    PlaceOrder,
    StrategyDecision,
)
from eventcontracts.domain.events import NormalizedEvent, QuoteEvent
from eventcontracts.domain.ids import ClientOrderId
from eventcontracts.domain.latency import ExecutionPriority, LatencyTier
from eventcontracts.domain.models import InstrumentId, OutcomeSide, Venue
from eventcontracts.domain.orders import OrderSide, OrderType, TimeInForce
from eventcontracts.domain.spec import StrategySpec
from eventcontracts.strategy.base import StrategyBase
from eventcontracts.strategy.context import StrategyContext
from eventcontracts.strategy.registry import register


@dataclass
class _Strike:
    market_id: str
    strike: Decimal
    mid: Decimal | None = None
    spread_bps: Decimal | None = None


class CryptoCrossStrikeSkewArbStrategy(StrategyBase):
    """Detects non-monotone P(YES) across ascending strikes; trades the inversion."""

    def __init__(self, spec: StrategySpec) -> None:
        super().__init__(spec)
        strikes = _parse_strike_map(str(spec.parameters.get("strike_market_map", "")))
        # Enforce ascending order at load — strategy invariant.
        if strikes != sorted(strikes, key=lambda s: s.strike):
            raise ValueError("strike_market_map must be in ascending strike order")
        self.strikes = strikes
        self.min_skew_edge = Decimal(str(spec.parameters.get("min_skew_edge", "0.01")))
        self.max_spread_bps = Decimal(str(spec.parameters.get("max_spread_bps", "500")))
        self.size = Decimal(str(spec.parameters.get("size", "3")))
        venue_value = str(spec.parameters.get("venue", "kalshi"))
        try:
            self.venue = Venue(venue_value)
        except ValueError as exc:
            raise ValueError(f"unknown venue: {venue_value}") from exc

    def on_event(
        self, event: NormalizedEvent, ctx: StrategyContext
    ) -> Sequence[StrategyDecision]:
        if not isinstance(event, QuoteEvent):
            return (NoAction(reason="ignored:not_quote"),)

        market_id = event.quote.instrument_id.market_id
        target = next((s for s in self.strikes if s.market_id == market_id), None)
        if target is None:
            return (NoAction(reason="ignored:not_tracked_strike"),)
        if not _update_strike(target, event):
            return (NoAction(reason="censored:one_sided_quote"),)
        if any(s.mid is None for s in self.strikes):
            return (NoAction(reason="warmup:missing_strike_mids"),)

        violations = monotone_violations(
            tuple((s.strike, s.mid) for s in self.strikes if s.mid is not None)
        )
        # Filter by edge and per-leg spread.
        decisions: list[StrategyDecision] = []
        for strike_low, p_low, strike_high, p_high in violations:
            edge = p_high - p_low
            if edge < self.min_skew_edge:
                continue
            low_leg = next(s for s in self.strikes if s.strike == strike_low)
            high_leg = next(s for s in self.strikes if s.strike == strike_high)
            if low_leg.spread_bps is None or high_leg.spread_bps is None:
                continue
            if (
                low_leg.spread_bps > self.max_spread_bps
                or high_leg.spread_bps > self.max_spread_bps
            ):
                continue

            # Long YES on the low strike (cheap P), long NO on the high
            # strike (rich P). The spread must converge by expiry.
            decisions.extend(self._butterfly(low_leg, high_leg, edge))

        if not decisions:
            return (NoAction(reason="no_violations_above_edge"),)
        return tuple(decisions)

    def _butterfly(
        self,
        low: _Strike,
        high: _Strike,
        edge: Decimal,
    ) -> Sequence[PlaceOrder]:
        edge_bps = edge * Decimal("10000")
        assert low.mid is not None and high.mid is not None
        return (
            PlaceOrder(
                client_order_id=ClientOrderId(uuid4().hex),
                instrument_id=InstrumentId(venue=self.venue, market_id=low.market_id),
                outcome_side=OutcomeSide.YES,
                order_side=OrderSide.BUY,
                order_type=OrderType.LIMIT,
                time_in_force=TimeInForce.GTC,
                quantity=self.size,
                price=_clip_probability(low.mid),
                reason=(
                    f"skew_arb_low_leg low_strike={low.strike} p_low={low.mid:.4f} "
                    f"high_strike={high.strike} p_high={high.mid:.4f}"
                ),
                expected_edge_bps=edge_bps,
                priority=ExecutionPriority(tier=LatencyTier.STANDARD),
            ),
            PlaceOrder(
                client_order_id=ClientOrderId(uuid4().hex),
                instrument_id=InstrumentId(venue=self.venue, market_id=high.market_id),
                outcome_side=OutcomeSide.NO,
                order_side=OrderSide.BUY,
                order_type=OrderType.LIMIT,
                time_in_force=TimeInForce.GTC,
                quantity=self.size,
                price=_clip_probability(Decimal("1") - high.mid),
                reason=(
                    f"skew_arb_high_leg low_strike={low.strike} p_low={low.mid:.4f} "
                    f"high_strike={high.strike} p_high={high.mid:.4f}"
                ),
                expected_edge_bps=edge_bps,
                priority=ExecutionPriority(tier=LatencyTier.STANDARD),
            ),
        )


def _update_strike(target: _Strike, event: QuoteEvent) -> bool:
    quote = event.quote
    if quote.bid is None or quote.ask is None:
        return False
    mid = (quote.bid.price + quote.ask.price) / Decimal("2")
    if mid <= 0:
        return False
    target.mid = mid
    spread = quote.ask.price - quote.bid.price
    target.spread_bps = spread / mid * Decimal("10000")
    return True


def _parse_strike_map(raw: str) -> list[_Strike]:
    out: list[_Strike] = []
    for item in raw.split(";"):
        if not item.strip():
            continue
        parts = [p.strip() for p in item.split(":")]
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise ValueError(
                "strike_market_map must be semicolon-separated market_id:strike entries"
            )
        market_id, strike_raw = parts
        out.append(_Strike(market_id=market_id, strike=Decimal(strike_raw)))
    return out


def _clip_probability(value: Decimal) -> Decimal:
    return max(Decimal("0.01"), min(Decimal("0.99"), value))


@register("crypto_cross_strike_skew_arb")
def factory(spec: StrategySpec) -> CryptoCrossStrikeSkewArbStrategy:
    return CryptoCrossStrikeSkewArbStrategy(spec)
