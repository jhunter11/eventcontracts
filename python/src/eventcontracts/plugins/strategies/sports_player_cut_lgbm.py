"""Player-level cut predictor (mean-reversion exploitation).

Hypothesis (per docs/golf-strategy-specs.md #1): retail markets misprice
post-Round-1 cut probabilities by over-weighting putting (volatile,
mean-reverting) and under-weighting Strokes Gained: Approach (sticky,
predictive). A player who shot over par while losing strokes on approach
is a structural fade; a player who shot over par but gained on approach
(cold putter) is a positive regression candidate.

Triggers on ``TimerEvent`` (label = ``player_round_complete``) once
DataGolf / PGA stats have settled. Reads the latest per-player Strokes
Gained components from the most recent ``ExternalSignalEvent`` carrying
that player's tournament stats, compares the model-implied (or
rules-mode-implied) cut probability against the latest Kalshi mid, and
emits ``PlaceOrder(priority=RELAXED, time_in_force=GTC)`` when the edge
exceeds ``min_edge_bps``.

Implementation note: the spec calls for a LightGBM classifier. The model
pipeline currently supports linear/logistic regression in numpy. This
module runs in **model mode** when ``spec.model`` is set and the runtime
has registered a model under that name (an externally-trained logistic
regression artifact works fine), and falls back to a deterministic
rules-mode heuristic otherwise. The rules-mode heuristic computes:

    base_prob       = sigmoid(-strokes_to_cut / 2.0)
    mean_reversion  = (sg_approach - sg_putting) * mean_reversion_weight
    implied_prob    = clip(base_prob + mean_reversion, 0.05, 0.95)

so the same decision shape (PlaceOrder vs NoAction) works whether or not
a model has been trained — the only thing that changes is how the
probability is computed.

Sizing follows the spec: ``conviction_multiplier = max(0.5, 1 + (sg_app
- sg_putt) / 2)`` scales the raw Kelly size. ``ctx.cash(USD)`` is used
when non-zero, otherwise ``cash_fallback_usd`` provides a deterministic
sizing base for paper/backtest runs.

Required spec parameters:
- ``player_market_map`` (string, semicolon-separated
  ``player_id:market_id`` pairs).
- ``signal_source`` (default ``"datagolf"``).
- ``timer_label`` (default ``"player_round_complete"``).
- ``min_edge_bps`` (default 100).
- ``kelly_fraction`` (default ``"0.05"``).
- ``max_size`` (default ``"25"``).
- ``mean_reversion_weight`` (default ``"0.08"``).
- ``cash_fallback_usd`` (default ``"1000"``).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from math import exp
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
from eventcontracts.domain.ids import ClientOrderId
from eventcontracts.domain.latency import ExecutionPriority, LatencyTier
from eventcontracts.domain.models import InstrumentId, OutcomeSide, Venue
from eventcontracts.domain.orders import OrderSide, OrderType, TimeInForce
from eventcontracts.domain.spec import StrategySpec
from eventcontracts.strategy.base import StrategyBase
from eventcontracts.strategy.context import StrategyContext
from eventcontracts.strategy.registry import register


@dataclass
class _PlayerState:
    """Mutable per-player rolling state."""

    player_id: str
    market_id: str
    sg_approach: Decimal | None = None
    sg_putting: Decimal | None = None
    strokes_to_cut: Decimal | None = None
    wave_weather_delta: Decimal | None = None
    last_mid: Decimal | None = None
    last_baseline_prob: Decimal | None = None


class SportsPlayerCutLgbmStrategy(StrategyBase):
    """SG-based cut probability strategy with mean-reversion sizing."""

    def __init__(self, spec: StrategySpec) -> None:
        super().__init__(spec)
        self.players = _parse_player_market_map(
            str(spec.parameters.get("player_market_map", ""))
        )
        self.signal_source = str(spec.parameters.get("signal_source", "datagolf"))
        self.timer_label = str(
            spec.parameters.get("timer_label", "player_round_complete")
        )
        self.min_edge_bps = Decimal(str(spec.parameters.get("min_edge_bps", "100")))
        self.kelly_fraction = Decimal(
            str(spec.parameters.get("kelly_fraction", "0.05"))
        )
        self.max_size = Decimal(str(spec.parameters.get("max_size", "25")))
        self.mean_reversion_weight = Decimal(
            str(spec.parameters.get("mean_reversion_weight", "0.08"))
        )
        self.cash_fallback_usd = Decimal(
            str(spec.parameters.get("cash_fallback_usd", "1000"))
        )
        self.feature_schema_version = str(
            spec.parameters.get("feature_schema_version", "v1")
        )
        venue_value = str(spec.parameters.get("venue", "kalshi"))
        try:
            self.venue = Venue(venue_value)
        except ValueError as exc:
            raise ValueError(f"unknown venue: {venue_value}") from exc

        self._state: dict[str, _PlayerState] = {
            player_id: _PlayerState(player_id=player_id, market_id=market_id)
            for player_id, market_id in self.players.items()
        }
        # Reverse lookup: market_id -> player_id, so QuoteEvents can be routed.
        self._market_to_player: dict[str, str] = {
            state.market_id: state.player_id for state in self._state.values()
        }

    def on_event(
        self, event: NormalizedEvent, ctx: StrategyContext
    ) -> Sequence[StrategyDecision]:
        if isinstance(event, QuoteEvent):
            self._update_mid(event)
            return (NoAction(reason="quote_mid_updated"),)
        if isinstance(event, ExternalSignalEvent):
            self._update_signal(event)
            return (NoAction(reason="signal_state_updated"),)
        if not isinstance(event, TimerEvent) or event.label != self.timer_label:
            return (NoAction(reason=f"ignored:{type(event).__name__}"),)
        return self._on_timer(ctx)

    def _on_timer(self, ctx: StrategyContext) -> Sequence[StrategyDecision]:
        decisions: list[StrategyDecision] = []
        for state in sorted(self._state.values(), key=lambda s: s.player_id):
            decision = self._maybe_place_order(state, ctx)
            if decision is not None:
                decisions.append(decision)
        if not decisions:
            return (NoAction(reason="no_player_edge_at_timer"),)
        return tuple(decisions)

    def _maybe_place_order(
        self, state: _PlayerState, ctx: StrategyContext
    ) -> StrategyDecision | None:
        if state.last_mid is None or state.strokes_to_cut is None:
            return None
        implied_prob = self._implied_probability(state, ctx)
        if implied_prob is None:
            return None
        edge_bps = (implied_prob - state.last_mid) * Decimal("10000")
        if abs(edge_bps) < self.min_edge_bps:
            return None

        side = OutcomeSide.YES if edge_bps > 0 else OutcomeSide.NO
        size = self._sizing(state, edge_bps, ctx)
        if size <= 0:
            return None
        # Limit price respects the side: when going long YES we offer at mid;
        # when going long NO we offer at (1 - mid). Both stay between 0 and 1.
        limit_price = state.last_mid if side is OutcomeSide.YES else (
            Decimal("1") - state.last_mid
        )
        limit_price = max(Decimal("0.01"), min(Decimal("0.99"), limit_price))

        return PlaceOrder(
            client_order_id=ClientOrderId(uuid4().hex),
            instrument_id=InstrumentId(
                venue=self.venue, market_id=state.market_id
            ),
            outcome_side=side,
            order_side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            time_in_force=TimeInForce.GTC,
            quantity=size,
            price=limit_price,
            reason=(
                f"sg_mean_reversion:player={state.player_id} "
                f"strokes_to_cut={state.strokes_to_cut} "
                f"sg_app={state.sg_approach} sg_putt={state.sg_putting} "
                f"edge={edge_bps:+.0f}bps"
            ),
            expected_edge_bps=edge_bps,
            priority=ExecutionPriority(tier=LatencyTier.RELAXED),
        )

    def _implied_probability(
        self, state: _PlayerState, ctx: StrategyContext
    ) -> Decimal | None:
        # Model mode: spec.model is set and ctx.predict can find it.
        if self.spec.model is not None:
            vector = self._feature_vector(state, ctx)
            if vector is not None:
                try:
                    raw = float(
                        ctx.predict(str(self.spec.model.name), vector).value
                    )
                except KeyError:
                    raw = None
                else:
                    return _clip01(Decimal(str(raw)))
        # Rules mode fallback.
        return _rules_mode_probability(state, self.mean_reversion_weight)

    def _feature_vector(
        self, state: _PlayerState, ctx: StrategyContext
    ) -> FeatureVector | None:
        if state.strokes_to_cut is None:
            return None
        values: tuple[tuple[str, float], ...] = (
            ("t0_current_score_to_projected_cut", float(state.strokes_to_cut)),
            (
                "t0_sg_approach_current_tournament",
                float(state.sg_approach if state.sg_approach is not None else 0.0),
            ),
            (
                "t0_sg_putting_current_tournament",
                float(state.sg_putting if state.sg_putting is not None else 0.0),
            ),
            (
                "t0_wave_weather_stroke_delta",
                float(
                    state.wave_weather_delta
                    if state.wave_weather_delta is not None
                    else 0.0
                ),
            ),
        )
        # FeatureVector requires a schema_id; we reuse the spec's
        # feature_schema_id if declared, otherwise synthesize a stable one.
        from eventcontracts.domain.ids import FeatureSchemaId

        schema_id = self.spec.feature_schema_id or FeatureSchemaId(
            "sports_player_cut_features"
        )
        return FeatureVector(
            schema_id=schema_id,
            schema_version=self.feature_schema_version,
            instrument_id=InstrumentId(
                venue=self.venue, market_id=state.market_id
            ),
            timestamp=ctx.now,
            values=values,
        )

    def _sizing(
        self, state: _PlayerState, edge_bps: Decimal, ctx: StrategyContext
    ) -> Decimal:
        cash = self._cash(ctx)
        sg_delta = (
            (state.sg_approach or Decimal("0"))
            - (state.sg_putting or Decimal("0"))
        )
        conviction = max(Decimal("0.5"), Decimal("1") + sg_delta / Decimal("2"))
        edge_fraction = min(abs(edge_bps) / Decimal("10000"), Decimal("1"))
        raw = edge_fraction * cash * self.kelly_fraction * conviction
        capped = min(raw, self.max_size)
        # Round down to whole contracts.
        return max(Decimal("1"), capped.quantize(Decimal("1")))

    def _cash(self, ctx: StrategyContext) -> Decimal:
        balance = ctx.cash("USD")
        if balance.total > 0:
            return balance.total
        return self.cash_fallback_usd

    def _update_mid(self, event: QuoteEvent) -> None:
        quote = event.quote
        player_id = self._market_to_player.get(quote.instrument_id.market_id)
        if player_id is None:
            return
        if quote.bid is None or quote.ask is None:
            return
        self._state[player_id].last_mid = (
            quote.bid.price + quote.ask.price
        ) / Decimal("2")

    def _update_signal(self, event: ExternalSignalEvent) -> None:
        if event.source != self.signal_source:
            return
        payload = event.payload
        player_id = payload.get("player_id")
        if not isinstance(player_id, str) or player_id not in self._state:
            return
        state = self._state[player_id]
        for source_field, decimal_field in (
            ("strokes_to_cut", "strokes_to_cut"),
            ("sg_approach", "sg_approach"),
            ("sg_putting", "sg_putting"),
            ("wave_weather_delta", "wave_weather_delta"),
        ):
            value = payload.get(source_field)
            if value is None:
                continue
            try:
                setattr(state, decimal_field, Decimal(str(value)))
            except (ValueError, ArithmeticError):
                continue


def _rules_mode_probability(
    state: _PlayerState, mean_reversion_weight: Decimal
) -> Decimal | None:
    """Deterministic mean-reversion heuristic used when no model is loaded."""

    if state.strokes_to_cut is None:
        return None
    # `strokes_to_cut`: positive = above the cutline (BAD for YES).
    sigmoid_arg = max(-50.0, min(50.0, float(state.strokes_to_cut) / 2.0))
    base = Decimal(str(1.0 / (1.0 + exp(sigmoid_arg))))
    sg_delta = (
        (state.sg_approach or Decimal("0"))
        - (state.sg_putting or Decimal("0"))
    )
    adjustment = sg_delta * mean_reversion_weight
    weather_penalty = (
        (state.wave_weather_delta or Decimal("0")) * Decimal("0.02")
    )
    return _clip01(base + adjustment - weather_penalty)


def _clip01(value: Decimal) -> Decimal:
    return max(Decimal("0.05"), min(Decimal("0.95"), value))


def _parse_player_market_map(raw: str) -> dict[str, str]:
    """Parse ``player_a:market_a;player_b:market_b`` form."""

    out: dict[str, str] = {}
    for item in raw.split(";"):
        if not item.strip():
            continue
        player, sep, market = item.partition(":")
        if not sep or not player.strip() or not market.strip():
            raise ValueError(
                "player_market_map must be semicolon-separated player_id:market_id pairs"
            )
        out[player.strip()] = market.strip()
    return out


@register("sports_player_cut_lgbm")
def factory(spec: StrategySpec) -> SportsPlayerCutLgbmStrategy:
    return SportsPlayerCutLgbmStrategy(spec)
