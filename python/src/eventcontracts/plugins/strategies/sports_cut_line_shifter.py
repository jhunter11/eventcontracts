"""Aggregate cut line shifter (Bayesian field modeling).

Hypothesis (per docs/golf-strategy-specs.md #2): the tournament cut line
(Top-65-and-ties) is anchored to historical course averages but shifts
dynamically with early Thursday/Friday scoring averages and afternoon
wind forecasts. Retail markets are slow to adjust the exact integer
bracket until late Friday, but the probabilistic shift is statistically
locked in by Thursday afternoon's scoring differential.

Triggers on ``TimerEvent`` (label = ``cut_line_recompute``, typically
hourly during play). Reads the latest field scoring delta and wind
forecast from the most recent ``ExternalSignalEvent``, then evaluates
each tracked Kalshi bracket (``"Will the cut line be exactly -2?"`` etc.)
against the model-implied (or rules-mode-implied) PMF and places
``PlaceOrder(priority=STANDARD)`` per bracket where the edge crosses
``min_edge_bps``.

Model mode: ``spec.model`` set + the runner has the model loaded ->
``ctx.predict`` is called with a per-bracket feature vector and
interpreted as the implied probability for that bracket. Rules mode:
Gaussian PMF centered on
``historical_course_cut_avg + alpha * field_scoring_delta + beta *
wind_forecast_mph`` with configurable ``stddev``.

The strategy trades across mutually exclusive brackets, so the central
``RiskGate`` is what enforces overall sleeve notional. This strategy
respects ``max_orders_per_timer`` as a soft self-throttle so a single
timer does not flood the gate.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from math import exp, pi, sqrt
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
)
from eventcontracts.domain.features import FeatureVector
from eventcontracts.domain.ids import ClientOrderId, FeatureSchemaId
from eventcontracts.domain.latency import ExecutionPriority, LatencyTier
from eventcontracts.domain.models import InstrumentId, OutcomeSide, Venue
from eventcontracts.domain.orders import OrderSide, OrderType, TimeInForce
from eventcontracts.domain.spec import StrategySpec
from eventcontracts.strategy.base import StrategyBase
from eventcontracts.strategy.context import StrategyContext
from eventcontracts.strategy.registry import register

TIMER_LABEL_DEFAULT = "cut_line_recompute"


@dataclass(frozen=True)
class _Bracket:
    """One mutually-exclusive cut-line outcome."""

    cut_line: int
    market_id: str


class SportsCutLineShifterStrategy(StrategyBase):
    """PMF-over-cut-line strategy that fades stale brackets."""

    def __init__(self, spec: StrategySpec) -> None:
        super().__init__(spec)
        self.brackets = _parse_bracket_map(
            str(spec.parameters.get("bracket_market_map", ""))
        )
        self.signal_source = str(spec.parameters.get("signal_source", "pga-tour"))
        self.timer_label = str(spec.parameters.get("timer_label", TIMER_LABEL_DEFAULT))
        self.min_edge_bps = Decimal(str(spec.parameters.get("min_edge_bps", "150")))
        self.size = Decimal(str(spec.parameters.get("size", "5")))
        self.max_orders_per_timer = int(
            spec.parameters.get("max_orders_per_timer", 3)
        )
        # Rules-mode parameters.
        self.historical_course_cut_avg = Decimal(
            str(spec.parameters.get("historical_course_cut_avg", "-1.2"))
        )
        self.field_alpha = Decimal(str(spec.parameters.get("field_alpha", "1.0")))
        self.wind_beta = Decimal(str(spec.parameters.get("wind_beta", "0.04")))
        self.pmf_stddev = Decimal(str(spec.parameters.get("pmf_stddev", "1.2")))
        venue_value = str(spec.parameters.get("venue", "kalshi"))
        try:
            self.venue = Venue(venue_value)
        except ValueError as exc:
            raise ValueError(f"unknown venue: {venue_value}") from exc

        self._mid_by_market: dict[str, Decimal] = {}
        self._field_scoring_delta: Decimal | None = None
        self._wind_forecast_mph: Decimal | None = None
        self._top65_current_score: Decimal | None = None

    def on_event(
        self, event: NormalizedEvent, ctx: StrategyContext
    ) -> Sequence[StrategyDecision]:
        if isinstance(event, QuoteEvent):
            self._track_mid(event)
            return (NoAction(reason="bracket_mid_updated"),)
        if isinstance(event, ExternalSignalEvent):
            self._update_signal(event)
            return (NoAction(reason="field_signal_updated"),)
        if not isinstance(event, TimerEvent) or event.label != self.timer_label:
            return (NoAction(reason="ignored:not_cut_line_timer"),)
        return self._on_timer(ctx)

    def _on_timer(self, ctx: StrategyContext) -> Sequence[StrategyDecision]:
        if not self.brackets or self._field_scoring_delta is None:
            return (NoAction(reason="warmup:insufficient_state"),)

        decisions: list[StrategyDecision] = []
        candidates: list[tuple[Decimal, _Bracket, Decimal, Decimal]] = []
        for bracket in self.brackets:
            mid = self._mid_by_market.get(bracket.market_id)
            implied = self._implied_probability(bracket, ctx)
            if mid is None or implied is None:
                continue
            edge_bps = (implied - mid) * Decimal("10000")
            if abs(edge_bps) < self.min_edge_bps:
                continue
            candidates.append((abs(edge_bps), bracket, mid, edge_bps))
        # Largest |edge| first, capped by the throttle.
        candidates.sort(key=lambda item: (-item[0], item[1].cut_line))
        for _abs_edge, bracket, mid, edge_bps in candidates[: self.max_orders_per_timer]:
            decisions.append(self._build_order(bracket, mid, edge_bps))
        if not decisions:
            return (NoAction(reason="no_bracket_edge"),)
        return tuple(decisions)

    def _build_order(
        self, bracket: _Bracket, mid: Decimal, edge_bps: Decimal
    ) -> PlaceOrder:
        side = OutcomeSide.YES if edge_bps > 0 else OutcomeSide.NO
        limit_price = mid if side is OutcomeSide.YES else (Decimal("1") - mid)
        limit_price = max(Decimal("0.01"), min(Decimal("0.99"), limit_price))
        return PlaceOrder(
            client_order_id=ClientOrderId(uuid4().hex),
            instrument_id=InstrumentId(
                venue=self.venue, market_id=bracket.market_id
            ),
            outcome_side=side,
            order_side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            time_in_force=TimeInForce.GTC,
            quantity=self.size,
            price=limit_price,
            reason=(
                f"cut_line_bracket={bracket.cut_line} "
                f"field_delta={self._field_scoring_delta} "
                f"wind={self._wind_forecast_mph} edge={edge_bps:+.0f}bps"
            ),
            expected_edge_bps=edge_bps,
            priority=ExecutionPriority(tier=LatencyTier.STANDARD),
        )

    def _implied_probability(
        self, bracket: _Bracket, ctx: StrategyContext
    ) -> Decimal | None:
        if self.spec.model is not None:
            vector = self._feature_vector(bracket, ctx)
            if vector is not None:
                try:
                    raw = float(
                        ctx.predict(str(self.spec.model.name), vector).value
                    )
                except KeyError:
                    raw = None
                else:
                    return _clip01(Decimal(str(raw)))
        return self._rules_mode_probability(bracket)

    def _feature_vector(
        self, bracket: _Bracket, ctx: StrategyContext
    ) -> FeatureVector | None:
        if self._field_scoring_delta is None:
            return None
        schema_id = self.spec.feature_schema_id or FeatureSchemaId(
            "sports_cut_line_features"
        )
        values: tuple[tuple[str, float], ...] = (
            ("t0_historical_course_cut_avg", float(self.historical_course_cut_avg)),
            ("t0_field_scoring_avg_delta_vs_par", float(self._field_scoring_delta)),
            (
                "t0_afternoon_wind_forecast_mph",
                float(self._wind_forecast_mph or Decimal("0")),
            ),
            (
                "t0_top_65_current_score",
                float(self._top65_current_score or Decimal("0")),
            ),
            ("t0_bracket_cut_line", float(bracket.cut_line)),
        )
        return FeatureVector(
            schema_id=schema_id,
            schema_version=str(self.spec.parameters.get("feature_schema_version", "v1")),
            instrument_id=InstrumentId(
                venue=self.venue, market_id=bracket.market_id
            ),
            timestamp=ctx.now,
            values=values,
        )

    def _rules_mode_probability(self, bracket: _Bracket) -> Decimal | None:
        if self._field_scoring_delta is None:
            return None
        mean = (
            self.historical_course_cut_avg
            + self.field_alpha * self._field_scoring_delta
            + self.wind_beta * (self._wind_forecast_mph or Decimal("0"))
        )
        return _gaussian_mass(Decimal(bracket.cut_line), mean, self.pmf_stddev)

    def _track_mid(self, event: QuoteEvent) -> None:
        quote = event.quote
        market_id = quote.instrument_id.market_id
        if market_id not in {b.market_id for b in self.brackets}:
            return
        if quote.bid is None or quote.ask is None:
            return
        self._mid_by_market[market_id] = (
            quote.bid.price + quote.ask.price
        ) / Decimal("2")

    def _update_signal(self, event: ExternalSignalEvent) -> None:
        if event.source != self.signal_source:
            return
        payload = event.payload
        for source_field, attr_name in (
            ("field_scoring_avg_delta_vs_par", "_field_scoring_delta"),
            ("afternoon_wind_forecast_mph", "_wind_forecast_mph"),
            ("top_65_current_score", "_top65_current_score"),
        ):
            value = payload.get(source_field)
            if value is None:
                continue
            try:
                setattr(self, attr_name, Decimal(str(value)))
            except (ValueError, ArithmeticError):
                continue


def _gaussian_mass(cut_line: Decimal, mean: Decimal, stddev: Decimal) -> Decimal:
    """Approximate the probability mass for an integer cut line.

    Returns the density at the integer scaled so plausible-stddev values
    sum to approximately 1 across the integer support. Good enough for
    rules-mode pricing; the real model returns calibrated mass.
    """

    if stddev <= 0:
        return Decimal("0")
    x = float(cut_line)
    m = float(mean)
    s = float(stddev)
    density = (1.0 / (s * sqrt(2.0 * pi))) * exp(-0.5 * ((x - m) / s) ** 2)
    return _clip01(Decimal(str(density)))


def _clip01(value: Decimal) -> Decimal:
    return max(Decimal("0.01"), min(Decimal("0.99"), value))


def _parse_bracket_map(raw: str) -> tuple[_Bracket, ...]:
    """Parse ``-2:KX-PGA-CUT-NEG2;-1:KX-PGA-CUT-NEG1;0:KX-PGA-CUT-ZERO``."""

    brackets: list[_Bracket] = []
    for item in raw.split(";"):
        if not item.strip():
            continue
        cut_str, sep, market = item.partition(":")
        if not sep or not cut_str.strip() or not market.strip():
            raise ValueError(
                "bracket_market_map must be semicolon-separated cut_line:market_id pairs"
            )
        try:
            cut_line = int(cut_str.strip())
        except ValueError as exc:
            raise ValueError(
                f"bracket_market_map cut_line must be an integer: {cut_str!r}"
            ) from exc
        brackets.append(_Bracket(cut_line=cut_line, market_id=market.strip()))
    return tuple(sorted(brackets, key=lambda b: b.cut_line))


@register("sports_cut_line_shifter")
def factory(spec: StrategySpec) -> SportsCutLineShifterStrategy:
    return SportsCutLineShifterStrategy(spec)
