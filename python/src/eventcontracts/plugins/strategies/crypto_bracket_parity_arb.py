"""Crypto 15-min bracket parity arbitrage.

Hypothesis
----------
For each 15-minute Kalshi crypto expiry (e.g. ``KXBTCD-25NOV2415-EXACT``)
the venue lists a disjoint, exhaustive partition of the underlying's
price space — bracket markets that cover ``(-inf, K1)``, ``[K1, K2)``,
..., ``[Kn, +inf)``. Under any arbitrage-free pricing the sum of YES
mid-prices across the partition must equal 1. Retail flow buys
"interesting" brackets (round-number strikes, the bracket containing
spot) disproportionately and several distinct market makers quote each
bracket, so persistent parity deviations exceed the per-bracket fee.

The strategy maintains a running view of each bracket's mid, recomputes
the partition sum, and when ``|sum - 1| > min_parity_edge`` emits a
coordinated multi-leg order set:

* ``sum > 1 + edge`` — sell every bracket equally (buy NO on each).
* ``sum < 1 - edge`` — buy every bracket equally (buy YES on each).

Game theory
-----------
This is a no-arbitrage condition rather than a forecast. The win
condition does not depend on which bracket actually settles in the
money — the venue pays out exactly 1 unit to the winning bracket, so
having one unit of each pays one unit total minus the (n-1) losing
ones — leaving the bracket sum at settlement equal to 1 by construction.

Required spec parameters
------------------------
- ``bracket_market_ids``: semicolon-separated ``market_id:lower:upper``
  entries, in ascending strike order. ``lower`` and ``upper`` may be
  ``inf``/``-inf`` for the unbounded tails.
- ``min_parity_edge`` (default ``"0.015"``): minimum ``|sum - 1|`` to
  fire. Tune above 2 * (per-bracket round-trip fee) to net positive.
- ``size`` (default ``"5"``): contracts per bracket leg.
- ``max_spread_bps`` (default ``"500"``): if any bracket's spread is
  wider than this, skip the round to avoid adverse selection.
- ``venue`` (default ``"kalshi"``).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from uuid import uuid4

from eventcontracts.crypto import bracket_parity_deviation
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


@dataclass(frozen=True)
class _Bracket:
    market_id: str
    lower: Decimal | None
    upper: Decimal | None


class CryptoBracketParityArbStrategy(StrategyBase):
    """Sells (buys) every bracket of a disjoint partition when ``Σmid > 1`` (``< 1``)."""

    def __init__(self, spec: StrategySpec) -> None:
        super().__init__(spec)
        self.brackets = _parse_brackets(str(spec.parameters.get("bracket_market_ids", "")))
        self.min_parity_edge = Decimal(str(spec.parameters.get("min_parity_edge", "0.015")))
        self.size = Decimal(str(spec.parameters.get("size", "5")))
        self.max_spread_bps = Decimal(str(spec.parameters.get("max_spread_bps", "500")))
        venue_value = str(spec.parameters.get("venue", "kalshi"))
        try:
            self.venue = Venue(venue_value)
        except ValueError as exc:
            raise ValueError(f"unknown venue: {venue_value}") from exc

        self._mid_by_market: dict[str, Decimal] = {}
        self._spread_bps_by_market: dict[str, Decimal] = {}

    def on_event(
        self, event: NormalizedEvent, ctx: StrategyContext
    ) -> Sequence[StrategyDecision]:
        if not isinstance(event, QuoteEvent):
            return (NoAction(reason="ignored:not_quote"),)
        if not self.brackets:
            return (NoAction(reason="censored:no_brackets_configured"),)

        market_id = event.quote.instrument_id.market_id
        if not any(b.market_id == market_id for b in self.brackets):
            return (NoAction(reason="ignored:quote_not_in_partition"),)

        if not _track_quote(event, self._mid_by_market, self._spread_bps_by_market):
            return (NoAction(reason="censored:one_sided_quote"),)

        # Need a full picture of the partition before firing.
        if any(b.market_id not in self._mid_by_market for b in self.brackets):
            return (NoAction(reason="warmup:missing_bracket_mids"),)

        # Bail if any leg is too wide; multi-leg slippage compounds quickly.
        widest = max(self._spread_bps_by_market.values())
        if widest > self.max_spread_bps:
            return (NoAction(reason=f"ignored:wide_spread_{widest:.0f}bps"),)

        probs = {b.market_id: self._mid_by_market[b.market_id] for b in self.brackets}
        deviation = bracket_parity_deviation(probs)
        if abs(deviation) < self.min_parity_edge:
            return (NoAction(reason=f"edge_below_threshold:dev={deviation:.4f}"),)

        return tuple(self._build_legs(deviation))

    def _build_legs(self, deviation: Decimal) -> Sequence[StrategyDecision]:
        # deviation > 0 → partition overpriced → sell each bracket (buy NO).
        # deviation < 0 → partition underpriced → buy each bracket (buy YES).
        outcome_side = OutcomeSide.NO if deviation > 0 else OutcomeSide.YES
        edge_bps = abs(deviation) * Decimal("10000")
        decisions: list[StrategyDecision] = []
        for bracket in self.brackets:
            mid = self._mid_by_market[bracket.market_id]
            leg_price = mid if outcome_side is OutcomeSide.YES else Decimal("1") - mid
            decisions.append(
                PlaceOrder(
                    client_order_id=ClientOrderId(uuid4().hex),
                    instrument_id=InstrumentId(venue=self.venue, market_id=bracket.market_id),
                    outcome_side=outcome_side,
                    order_side=OrderSide.BUY,
                    order_type=OrderType.LIMIT,
                    time_in_force=TimeInForce.GTC,
                    quantity=self.size,
                    price=_clip_probability(leg_price),
                    reason=(
                        f"parity_dev_{deviation:+.4f}_target_{outcome_side.value}"
                        f"_leg_{bracket.market_id}"
                    ),
                    expected_edge_bps=edge_bps,
                    priority=ExecutionPriority(tier=LatencyTier.STANDARD),
                )
            )
        return decisions


def _track_quote(
    event: QuoteEvent,
    mid_store: dict[str, Decimal],
    spread_store: dict[str, Decimal],
) -> bool:
    quote = event.quote
    if quote.bid is None or quote.ask is None:
        return False
    mid = (quote.bid.price + quote.ask.price) / Decimal("2")
    if mid <= 0:
        return False
    mid_store[quote.instrument_id.market_id] = mid
    spread = quote.ask.price - quote.bid.price
    spread_store[quote.instrument_id.market_id] = spread / mid * Decimal("10000")
    return True


def _parse_brackets(raw: str) -> tuple[_Bracket, ...]:
    brackets: list[_Bracket] = []
    for item in raw.split(";"):
        if not item.strip():
            continue
        parts = [p.strip() for p in item.split(":")]
        if len(parts) != 3 or not parts[0]:
            raise ValueError(
                "bracket_market_ids must be semicolon-separated "
                "market_id:lower:upper entries"
            )
        market_id, lower_raw, upper_raw = parts
        lower = None if lower_raw.lower() in ("-inf", "neg_inf", "") else Decimal(lower_raw)
        upper = None if upper_raw.lower() in ("inf", "pos_inf", "") else Decimal(upper_raw)
        brackets.append(_Bracket(market_id=market_id, lower=lower, upper=upper))
    return tuple(brackets)


def _clip_probability(value: Decimal) -> Decimal:
    return max(Decimal("0.01"), min(Decimal("0.99"), value))


@register("crypto_bracket_parity_arb")
def factory(spec: StrategySpec) -> CryptoBracketParityArbStrategy:
    return CryptoBracketParityArbStrategy(spec)
