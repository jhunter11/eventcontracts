"""Full-ladder CDF strategy: price a whole threshold/range ladder coherently.

Brainstorm addendum §"Full-Ladder Pricing" / build-queue #4. The repo's other
predictive sleeves price each bracket independently against an external
probability. This one is the *coherent* alternative: an external producer
publishes a single latent distribution for the underlying (mean + sigma), and
the strategy maps **every** configured bracket to a model probability off the
same CDF — so the ladder is internally consistent (monotone, mass-correct) by
construction instead of bracket-by-bracket noise.

It serves several brainstorm variants on one runtime:

* `weather_ladder_cdf`           — one station-day distribution over KXHIGH brackets.
* `macro_cpi_cdf_arb`            — CPI/Core-CPI implied CDF vs a nowcast distribution.
* `equity_close_range_cdf`       — one close distribution over KXINX/KXNASDAQ ranges.
* `commodity_brent_threshold_cdf`— Brent daily-threshold CDF (KXBRENTD).

Signal contract (`ExternalSignalEvent` from `signal_source`):

    payload = {"mean": <float>, "sigma": <float>, "dist": "normal"|"logistic"}

`dist` is optional (defaults to the spec's `dist`). On each signal the whole
ladder is repriced; on each quote the per-bracket mid is cached. Brackets are
declared `TICKER:lo:hi;...` (same convention as macro_cpi / the no-arb scanner):

* `ladder_kind = "exclusive"`  → P(bracket) = CDF(hi) − CDF(lo)  (range markets)
* `ladder_kind = "cumulative"` → P(bracket) = 1 − CDF(lo)        (">= threshold")

Latency class: relaxed/standard — the edge is a better *distribution*, not a
race. Like the other signal sleeves it needs an external producer (the "CDF
engine") to emit `mean/sigma`; fed that, it emits coherent per-bracket orders.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
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

FOUR_DP = Decimal("0.0001")


class _Bracket:
    __slots__ = ("market_id", "lo", "hi")

    def __init__(self, market_id: str, lo: float, hi: float) -> None:
        self.market_id = market_id
        self.lo = lo
        self.hi = hi


class LadderCdfStrategy(StrategyBase):
    """Coherent ladder pricing off one externally supplied distribution."""

    def __init__(self, spec: StrategySpec) -> None:
        super().__init__(spec)
        self.signal_source = str(spec.parameters.get("signal_source", "ladder-dist"))
        self.brackets = _parse_brackets(str(spec.parameters.get("brackets", "")))
        self.ladder_kind = str(spec.parameters.get("ladder_kind", "exclusive")).lower()
        self.dist = str(spec.parameters.get("dist", "normal")).lower()
        self.min_edge_bps = Decimal(str(spec.parameters.get("min_edge_bps", "200")))
        self.size = Decimal(str(spec.parameters.get("size", "5")))
        self.max_orders = int(spec.parameters.get("max_orders", 8))
        self.venue = _venue(str(spec.parameters.get("venue", "kalshi")))
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
            return (NoAction(reason="ignored:not_ladder_dist_signal"),)

        mean = _decimal_float(event.payload.get("mean"))
        sigma = _decimal_float(event.payload.get("sigma"))
        if mean is None or sigma is None or sigma <= 0:
            return (NoAction(reason="warmup:missing_or_invalid_distribution"),)
        dist = str(event.payload.get("dist", self.dist)).lower()

        # Reprice every bracket off the one distribution, rank by |edge|, emit
        # the strongest up to max_orders.
        candidates: list[tuple[Decimal, PlaceOrder]] = []
        for b in self.brackets:
            mid = self._mid_by_market.get(b.market_id)
            if mid is None:
                continue
            model_prob = _bracket_prob(b, mean, sigma, self.ladder_kind, dist)
            edge_bps = (model_prob - mid) * Decimal("10000")
            if abs(edge_bps) < self.min_edge_bps:
                continue
            candidates.append(
                (
                    abs(edge_bps),
                    _place(
                        venue=self.venue,
                        market_id=b.market_id,
                        mid=mid,
                        implied_prob=model_prob,
                        edge_bps=edge_bps,
                        min_edge_bps=self.min_edge_bps,
                        size=self.size,
                        quote_event=self._quote_by_market.get(b.market_id),
                        reason=(
                            f"ladder_cdf {dist} mean={mean} sigma={sigma} "
                            f"p={model_prob} mid={mid} edge_bps={edge_bps}"
                        ),
                    ),
                )
            )
        if not candidates:
            return (NoAction(reason="no_bracket_clears_edge"),)
        candidates.sort(key=lambda kv: kv[0], reverse=True)
        return [order for _, order in candidates[: self.max_orders]]


def _bracket_prob(
    b: _Bracket, mean: Decimal, sigma: Decimal, kind: str, dist: str
) -> Decimal:
    mu, s = float(mean), float(sigma)
    # cumulative = P(X >= lo) for ">= threshold" markets; exclusive = CDF(hi)-CDF(lo).
    p = (
        1.0 - _cdf(b.lo, mu, s, dist)
        if kind == "cumulative"
        else _cdf(b.hi, mu, s, dist) - _cdf(b.lo, mu, s, dist)
    )
    p = min(1.0, max(0.0, p))
    return Decimal(str(round(p, 6)))


def _cdf(x: float, mu: float, sigma: float, dist: str) -> float:
    if dist == "logistic":
        # scale matched to sigma: s = sigma * sqrt(3) / pi
        s = sigma * math.sqrt(3.0) / math.pi
        return 1.0 / (1.0 + math.exp(-(x - mu) / s))
    # normal (default)
    return 0.5 * (1.0 + math.erf((x - mu) / (sigma * math.sqrt(2.0))))


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


def _parse_brackets(raw: str) -> list[_Bracket]:
    out: list[_Bracket] = []
    for chunk in raw.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = chunk.split(":")
        if len(parts) != 3:
            raise ValueError(f"bad bracket spec {chunk!r}; expected TICKER:lo:hi")
        ticker, lo, hi = parts
        out.append(_Bracket(ticker.strip(), float(lo), float(hi)))
    return out


def _decimal_float(value: object) -> Decimal | None:
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


@register("ladder_cdf")
def factory(spec: StrategySpec) -> LadderCdfStrategy:
    return LadderCdfStrategy(spec)
