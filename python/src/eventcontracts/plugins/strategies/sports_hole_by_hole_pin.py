"""Live hole-by-hole pin access exploiter.

Hypothesis (per docs/golf-strategy-specs.md #4): live "Next Hole Birdie"
markets misprice because they evaluate a player's general driving
accuracy rather than specific proximity metrics. The true edge:
cross-reference the exact daily pin location difficulty (tucked back
left over a bunker, etc.) with the player's Strokes Gained: Approach
from the *specific yardage* they just drove the ball to in the fairway.

Latency-critical (~20ms floor). The strategy trades market orders to
jump the spread before manual bookmaker updates.

Trigger: ``ExternalSignalEvent`` from ShotLink the moment a drive lands
and stops. Payload must carry:

* ``player_id``: the player who just drove.
* ``market_id``: the matching "Will [Player] Birdie Hole X?" market.
* ``distance_to_pin_yards`` (int).
* ``pin_difficulty_index`` (float, historical birdie rate at the pin).
* ``player_sg_approach_from_distance`` (float, player's SG from the
  matching 25-yd bucket).
* ``lie`` (``"fairway"`` / ``"rough"`` / ``"sand"``).

Decision: when ``model_or_rules_prob - current_mid > min_edge_bps``,
emit ``PlaceOrder(priority=FAST, order_type=MARKET, time_in_force=IOC)``.
Sizing is intentionally deterministic (``static_clip_size``) to keep
the critical path branch-free.

Position cap: any single hole gets at most one place-order, tracked by
``self._fired_for_hole`` keyed on ``(player_id, hole_event_token)``
where ``hole_event_token`` is supplied in the payload (typically the
ShotLink shot id of the drive).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from math import exp
from typing import Any
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

# Encoding for the lie categorical so the same numeric feature flows to a
# model or a rules-mode heuristic without separate paths.
_LIE_ENCODING = {
    "fairway": 0.0,
    "rough": 1.0,
    "sand": 2.0,
    "bunker": 2.0,
    "fringe": 0.5,
    "tee": 0.0,
}


@dataclass
class _MarketState:
    """Latest known mid per tracked market_id."""

    last_mid: Decimal | None = None


class SportsHoleByHolePinStrategy(StrategyBase):
    """Live birdie-market taker; cross-references pin difficulty + drive yardage."""

    def __init__(self, spec: StrategySpec) -> None:
        super().__init__(spec)
        self.signal_source = str(spec.parameters.get("signal_source", "shotlink"))
        self.min_edge_bps = Decimal(str(spec.parameters.get("min_edge_bps", "200")))
        self.static_clip_size = Decimal(
            str(spec.parameters.get("static_clip_size", "3"))
        )
        # Rules-mode coefficients tuned to give a sane baseline; replaced when
        # a real LGBM artifact is loaded via ctx.predict.
        self.distance_coef = Decimal(
            str(spec.parameters.get("rules_distance_coef", "0.012"))
        )
        self.sg_coef = Decimal(str(spec.parameters.get("rules_sg_coef", "0.20")))
        self.pin_coef = Decimal(str(spec.parameters.get("rules_pin_coef", "0.80")))
        self.lie_penalty = Decimal(str(spec.parameters.get("rules_lie_penalty", "0.15")))
        self.feature_schema_version = str(
            spec.parameters.get("feature_schema_version", "v1")
        )
        venue_value = str(spec.parameters.get("venue", "polymarket_global"))
        try:
            self.venue = Venue(venue_value)
        except ValueError as exc:
            raise ValueError(f"unknown venue: {venue_value}") from exc

        self._markets: dict[str, _MarketState] = {}
        self._fired_for_hole: set[tuple[str, str]] = set()

    def on_event(
        self, event: NormalizedEvent, ctx: StrategyContext
    ) -> Sequence[StrategyDecision]:
        if isinstance(event, QuoteEvent):
            self._track_mid(event)
            return (NoAction(reason="hole_market_mid_updated"),)
        if not isinstance(event, ExternalSignalEvent):
            return (NoAction(reason="ignored:not_external_signal"),)
        if event.source != self.signal_source:
            return (NoAction(reason=f"ignored:source!={self.signal_source}"),)
        return self._on_drive(event, ctx)

    def _on_drive(
        self, event: ExternalSignalEvent, ctx: StrategyContext
    ) -> Sequence[StrategyDecision]:
        payload = event.payload
        # FrozenMap (Mapping, not dict) once events flow through the
        # ingestion/normalization layer.
        if not isinstance(payload, Mapping):
            return (NoAction(reason="censored:payload_not_object"),)

        player_id = payload.get("player_id")
        market_id = payload.get("market_id")
        hole_event_token = str(payload.get("hole_event_token") or "")
        if not isinstance(player_id, str) or not isinstance(market_id, str):
            return (NoAction(reason="censored:missing_player_or_market"),)

        # One protective shot per hole, even if ShotLink re-emits.
        fire_key = (player_id, hole_event_token or market_id)
        if fire_key in self._fired_for_hole:
            return (NoAction(reason="already_fired_for_hole"),)

        state = self._markets.get(market_id)
        if state is None or state.last_mid is None:
            return (NoAction(reason="warmup:no_market_mid_for_hole"),)

        feature_values = _extract_features(payload)
        if feature_values is None:
            return (NoAction(reason="censored:incomplete_drive_payload"),)

        implied = self._implied_probability(market_id, feature_values, ctx)
        if implied is None:
            return (NoAction(reason="censored:no_probability"),)

        edge_bps = (implied - state.last_mid) * Decimal("10000")
        # Birdie markets are one-sided in practice — only take YES when the
        # model says the market is too cheap. NO bets on birdie are noisy.
        if edge_bps < self.min_edge_bps:
            return (NoAction(reason=f"edge_below_threshold:{edge_bps:+.0f}bps"),)

        self._fired_for_hole.add(fire_key)
        return (
            PlaceOrder(
                client_order_id=ClientOrderId(uuid4().hex),
                instrument_id=InstrumentId(
                    venue=self.venue, market_id=market_id
                ),
                outcome_side=OutcomeSide.YES,
                order_side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                time_in_force=TimeInForce.IOC,
                quantity=self.static_clip_size,
                reason=(
                    f"pin_access_edge:player={player_id} "
                    f"dist={feature_values.distance_to_pin_yards} "
                    f"sg={feature_values.player_sg_approach_from_distance:.3f} "
                    f"pin_diff={feature_values.pin_difficulty_index:.3f} "
                    f"edge={edge_bps:+.0f}bps"
                ),
                expected_edge_bps=edge_bps,
                priority=ExecutionPriority(tier=LatencyTier.FAST, max_delay_ms=20),
            ),
        )

    def _implied_probability(
        self,
        market_id: str,
        features: _DriveFeatures,
        ctx: StrategyContext,
    ) -> Decimal | None:
        if self.spec.model is not None:
            vector = self._feature_vector(market_id, features, ctx)
            if vector is not None:
                try:
                    raw = float(ctx.predict(str(self.spec.model.name), vector).value)
                except KeyError:
                    raw = None
                else:
                    return _clip01(Decimal(str(raw)))
        return self._rules_mode_probability(features)

    def _feature_vector(
        self,
        market_id: str,
        features: _DriveFeatures,
        ctx: StrategyContext,
    ) -> FeatureVector | None:
        schema_id = self.spec.feature_schema_id or FeatureSchemaId(
            "sports_hole_by_hole_features"
        )
        values: tuple[tuple[str, float], ...] = (
            ("t0_distance_to_pin_yards", float(features.distance_to_pin_yards)),
            ("t0_pin_difficulty_index", float(features.pin_difficulty_index)),
            (
                "t0_player_sg_approach_from_distance",
                float(features.player_sg_approach_from_distance),
            ),
            ("t0_lie_condition", float(features.lie_encoding)),
        )
        return FeatureVector(
            schema_id=schema_id,
            schema_version=self.feature_schema_version,
            instrument_id=InstrumentId(venue=self.venue, market_id=market_id),
            timestamp=ctx.now,
            values=values,
        )

    def _rules_mode_probability(self, features: _DriveFeatures) -> Decimal:
        """Logistic of a small linear combo of the documented features."""

        # All multiplications happen in Decimal to keep determinism with the
        # rest of the codebase, then we move to float for the sigmoid.
        score = (
            self.pin_coef * Decimal(str(features.pin_difficulty_index))
            + self.sg_coef * Decimal(str(features.player_sg_approach_from_distance))
            - self.distance_coef * Decimal(features.distance_to_pin_yards)
            - self.lie_penalty * Decimal(str(features.lie_encoding))
        )
        prob = 1.0 / (1.0 + exp(-float(score)))
        return _clip01(Decimal(str(prob)))

    def _track_mid(self, event: QuoteEvent) -> None:
        quote = event.quote
        if quote.bid is None or quote.ask is None:
            return
        state = self._markets.setdefault(quote.instrument_id.market_id, _MarketState())
        state.last_mid = (quote.bid.price + quote.ask.price) / Decimal("2")


@dataclass(frozen=True)
class _DriveFeatures:
    """Extracted, validated features from one ShotLink drive payload."""

    distance_to_pin_yards: int
    pin_difficulty_index: float
    player_sg_approach_from_distance: float
    lie_encoding: float


def _extract_features(payload: Any) -> _DriveFeatures | None:
    try:
        distance = int(payload["distance_to_pin_yards"])
        pin = float(payload["pin_difficulty_index"])
        sg = float(payload["player_sg_approach_from_distance"])
        lie = payload.get("lie", "fairway")
    except (KeyError, TypeError, ValueError):
        return None
    if not isinstance(lie, str):
        return None
    encoding = _LIE_ENCODING.get(lie.lower())
    if encoding is None:
        return None
    if distance <= 0:
        return None
    return _DriveFeatures(
        distance_to_pin_yards=distance,
        pin_difficulty_index=pin,
        player_sg_approach_from_distance=sg,
        lie_encoding=encoding,
    )


def _clip01(value: Decimal) -> Decimal:
    return max(Decimal("0.01"), min(Decimal("0.99"), value))


@register("sports_hole_by_hole_pin")
def factory(spec: StrategySpec) -> SportsHoleByHolePinStrategy:
    return SportsHoleByHolePinStrategy(spec)
