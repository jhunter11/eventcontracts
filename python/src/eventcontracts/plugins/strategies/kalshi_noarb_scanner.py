"""Intra-Kalshi logical no-arbitrage scanner (deterministic, no model).

Hypothesis (per docs/kalshi-strategy-latency-playbook.md #6): related Kalshi
contracts must obey logical no-arbitrage constraints that retail flow violates
faster than it corrects. This scanner watches one configured ladder group and
checks two constraints on every quote update:

* **exclusive** ladders (mutually-exclusive ranges, e.g. KXHIGH temperature
  brackets): exactly one bracket resolves YES, so buying one YES contract on
  *every* bracket pays exactly $1. If the summed YES asks cost less than
  ``1 - fees - min_edge``, that is a risk-free lock and the scanner emits a
  BUY-YES IOC leg on each bracket.

* **cumulative** ladders ("≥ threshold" contracts, monotone in the threshold):
  P(≥t) must be non-increasing in t. A lower threshold trading *cheaper* than a
  higher one (``ask(low) < bid(high)``) is a violation; the clean lock is
  BUY low-YES + BUY high-NO. Auto-executing that two-leg lock carries leg risk,
  so for now the scanner *flags* the violation as a logged ``NoAction`` with the
  legs in metadata — turning it into live two-leg execution is the promotion
  step (see the v7 spec).

Latency class: relaxed/standard. The edge is logical, not a race; the binding
constraint is a fresh two-sided book on every leg, not microseconds.

Config (``configs/strategies/kalshi-noarb-scanner.toml``):

    brackets   = "KXHIGHNY-26JUN02-B71:71:73;KXHIGHNY-26JUN02-B73:73:75;..."
    ladder_kind = "exclusive"   # or "cumulative"
    min_edge_bps = "100"
    size = "5"
    max_quote_age_seconds = "30"

Leg risk is real: legs are emitted as independent IOC orders, so a partial fill
leaves an unhedged book. This is acceptable for paper/dry-run capture and is
called out as the headline promotion blocker in the spec.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from uuid import uuid4

from eventcontracts.domain.decisions import NoAction, PlaceOrder, StrategyDecision
from eventcontracts.domain.events import (
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
from eventcontracts.strategy.pricing import clamp_price
from eventcontracts.strategy.registry import register

FOUR_DP = Decimal("0.0001")
# Kalshi per-contract fee curve: 0.07 * price * (1 - price). Matches the repo's
# fee model (see the tennis-tradeability memory: fee != flat 7%).
FEE_COEFF = Decimal("0.07")


@dataclass
class _Bracket:
    market_id: str
    lo: Decimal
    hi: Decimal


@dataclass
class _Book:
    yes_bid: Decimal | None
    yes_ask: Decimal | None
    ask_qty: Decimal | None
    quote: QuoteEvent
    seen_at: datetime


class KalshiNoArbScanner(StrategyBase):
    """Deterministic logical-arbitrage scanner over one configured ladder."""

    def __init__(self, spec: StrategySpec) -> None:
        super().__init__(spec)
        self.brackets = _parse_brackets(str(spec.parameters.get("brackets", "")))
        self.bracket_ids = {b.market_id for b in self.brackets}
        self.ladder_kind = str(spec.parameters.get("ladder_kind", "exclusive")).lower()
        self.min_edge_bps = Decimal(str(spec.parameters.get("min_edge_bps", "100")))
        self.size = Decimal(str(spec.parameters.get("size", "5")))
        self.max_quote_age = timedelta(
            seconds=float(spec.parameters.get("max_quote_age_seconds", "30"))
        )
        self.venue = _venue(str(spec.parameters.get("venue", "kalshi")))
        self._books: dict[str, _Book] = {}

    def on_event(
        self, event: NormalizedEvent, ctx: StrategyContext
    ) -> Sequence[StrategyDecision]:
        if not isinstance(event, QuoteEvent):
            return (NoAction(reason="ignored:not_quote"),)
        market_id = event.quote.instrument_id.market_id
        if market_id not in self.bracket_ids:
            return (NoAction(reason="ignored:not_in_ladder"),)

        yes_bid, yes_ask, ask_qty = _yes_book(event)
        self._books[market_id] = _Book(yes_bid, yes_ask, ask_qty, event, ctx.now)

        fresh = self._fresh_books(ctx.now)
        if len(fresh) < len(self.brackets):
            return (NoAction(reason="warmup:incomplete_or_stale_ladder"),)

        if self.ladder_kind == "exclusive":
            return self._scan_exclusive(fresh)
        if self.ladder_kind == "cumulative":
            return self._scan_cumulative(fresh)
        return (NoAction(reason=f"unknown_ladder_kind:{self.ladder_kind}"),)

    # -- freshness -----------------------------------------------------------
    def _fresh_books(self, now: datetime) -> dict[str, _Book]:
        out: dict[str, _Book] = {}
        for b in self.brackets:
            book = self._books.get(b.market_id)
            if book is None or book.yes_ask is None or book.yes_bid is None:
                continue
            if now - book.seen_at > self.max_quote_age:
                continue
            out[b.market_id] = book
        return out

    # -- exclusive (sum-to-one) lock ----------------------------------------
    def _scan_exclusive(self, fresh: dict[str, _Book]) -> Sequence[StrategyDecision]:
        total_ask = Decimal("0")
        total_fee = Decimal("0")
        for book in fresh.values():
            assert book.yes_ask is not None
            total_ask += book.yes_ask
            total_fee += _fee(book.yes_ask)
        # Buying one YES on every bracket pays exactly $1 (one bracket wins).
        edge_dollars = Decimal("1") - total_ask - total_fee
        edge_bps = edge_dollars * Decimal("10000")
        if edge_bps < self.min_edge_bps:
            return (
                NoAction(
                    reason=(
                        f"no_lock:sum_ask={total_ask} fee={total_fee} "
                        f"edge_bps={edge_bps}"
                    ),
                ),
            )
        size = self._lock_size(fresh)
        orders: list[StrategyDecision] = []
        for b in self.brackets:
            book = fresh[b.market_id]
            assert book.yes_ask is not None
            orders.append(
                _buy(
                    venue=self.venue,
                    market_id=b.market_id,
                    side=OutcomeSide.YES,
                    exec_price=book.yes_ask,
                    size=size,
                    quote_event=book.quote,
                    edge_bps=edge_bps,
                    reason=(
                        f"exclusive_lock sum_ask={total_ask} fee={total_fee} "
                        f"edge_bps={edge_bps}"
                    ),
                )
            )
        return orders

    # -- cumulative (monotonicity) detection --------------------------------
    def _scan_cumulative(self, fresh: dict[str, _Book]) -> Sequence[StrategyDecision]:
        ordered = sorted(
            (b for b in self.brackets if b.market_id in fresh), key=lambda b: b.lo
        )
        for low, high in zip(ordered, ordered[1:], strict=False):
            lb, hb = fresh[low.market_id], fresh[high.market_id]
            assert lb.yes_ask is not None and hb.yes_bid is not None
            # P(>=low) must be >= P(>=high); violation = low cheaper than high.
            margin = lb.yes_ask - hb.yes_bid + _fee(lb.yes_ask) + _fee(hb.yes_bid)
            edge_bps = -margin * Decimal("10000")
            if edge_bps >= self.min_edge_bps:
                return (
                    NoAction(
                        reason=(
                            "cumulative_monotonicity_violation: "
                            f"lock=BUY {low.market_id} YES @ {lb.yes_ask} "
                            f"+ BUY {high.market_id} NO @ {Decimal('1') - hb.yes_bid}; "
                            f"edge_bps={edge_bps} (two-leg execution is the "
                            "promotion step; flagged only)"
                        ),
                    ),
                )
        return (NoAction(reason="ladder_monotone_ok"),)

    def _lock_size(self, fresh: dict[str, _Book]) -> Decimal:
        qtys = [b.ask_qty for b in fresh.values() if b.ask_qty is not None]
        return min([self.size, *qtys]) if qtys else self.size


def _buy(
    *,
    venue: Venue,
    market_id: str,
    side: OutcomeSide,
    exec_price: Decimal,
    size: Decimal,
    quote_event: QuoteEvent,
    edge_bps: Decimal,
    reason: str,
) -> PlaceOrder:
    fair_price = exec_price.quantize(FOUR_DP, rounding=ROUND_HALF_UP)
    return PlaceOrder(
        client_order_id=ClientOrderId(uuid4().hex),
        instrument_id=InstrumentId(venue=venue, market_id=market_id),
        outcome_side=side,
        order_side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        # IOC: an arb leg must fill now or be abandoned (leg risk is the headline
        # caveat — partial fills leave an unhedged book).
        time_in_force=TimeInForce.IOC,
        quantity=size,
        market_snapshot=market_snapshot_from_quote_event(quote_event, side=side),
        price=clamp_price(exec_price),
        reason=reason,
        expected_edge_bps=edge_bps,
        priority=ExecutionPriority(tier=LatencyTier.STANDARD),
        metadata={
            "fair_price": str(fair_price),
            # The executable-edge floor is proven at the group level (the lock),
            # not per leg, so the per-leg floor is 0.
            "min_executable_edge_ticks": "0",
            "fee_rate_bps": "700",
        },
    )


def _yes_book(event: QuoteEvent) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
    """Return (yes_bid, yes_ask, ask_qty) regardless of the quote's side."""
    quote = event.quote
    bid = quote.bid
    ask = quote.ask
    if bid is None or ask is None:
        return (None, None, None)
    bid_qty = getattr(bid, "quantity", None)
    ask_qty = getattr(ask, "quantity", None)
    if quote.side is OutcomeSide.YES:
        return (bid.price, ask.price, _as_decimal(ask_qty))
    # NO-side quote: YES ask = 1 - NO bid, YES bid = 1 - NO ask.
    return (Decimal("1") - ask.price, Decimal("1") - bid.price, _as_decimal(bid_qty))


def _as_decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (ValueError, ArithmeticError):
        return None


def _fee(price: Decimal) -> Decimal:
    return FEE_COEFF * price * (Decimal("1") - price)


def _parse_brackets(raw: str) -> list[_Bracket]:
    out: list[_Bracket] = []
    for chunk in raw.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = chunk.split(":")
        if len(parts) != 3:
            raise ValueError(
                f"bad bracket spec {chunk!r}; expected TICKER:lo:hi"
            )
        ticker, lo, hi = parts
        out.append(_Bracket(ticker.strip(), Decimal(lo), Decimal(hi)))
    return out


def _venue(value: str) -> Venue:
    try:
        return Venue(value)
    except ValueError as exc:
        raise ValueError(f"unknown venue: {value}") from exc


@register("kalshi_noarb_scanner")
def factory(spec: StrategySpec) -> KalshiNoArbScanner:
    return KalshiNoArbScanner(spec)
