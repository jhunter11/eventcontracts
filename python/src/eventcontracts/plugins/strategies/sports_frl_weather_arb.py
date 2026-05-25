"""First Round Leader weather wave arbitrage.

Hypothesis (per docs/golf-strategy-specs.md #3): retail markets price
the "First Round Leader" (FRL) / "Top-5 after R1" markets off long-term
baseline skill and fundamentally underprice the structural stroke
advantage of the morning (AM) tee-time wave when a weather front causes
severe afternoon (PM) winds. Players in the favorable wave have a
mathematically higher probability of going low.

Triggers on a single ``ExternalSignalEvent`` (final tee times + final
wind forecast aligned, typically at ~05:00 local on Thursday) that
carries:

* ``am_wave_wind_mph`` / ``pm_wave_wind_mph`` (floats).
* A list of ``players`` entries, each with ``player_id``,
  ``market_id``, ``wave`` (``"am"`` / ``"pm"``), and
  ``wind_sg_baseline`` (per-player strokes-gained advantage in wind).

For each player the strategy computes an adjusted FRL probability
(model-mode via ``ctx.predict`` if a model is configured, rules-mode
otherwise) and emits one ``PlaceOrder(priority=RELAXED, GTC)`` per
player whose edge versus the latest Kalshi/Polymarket mid exceeds
``min_edge_bps``.

Rules-mode probability:

    baseline_prob = 1 / field_size
    wave_advantage_strokes = (other_wave_wind - own_wave_wind) * wind_penalty_strokes_per_mph
    boost = wave_advantage_strokes * wind_sg_baseline_weight
        + wind_sg_baseline * wind_sg_baseline_weight
    implied = clip(baseline_prob + boost, 0.005, 0.40)
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from decimal import Decimal
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


@dataclass
class _PlayerWaveState:
    """Rolling state per tracked player."""

    player_id: str
    market_id: str
    wave: str = "am"
    wind_sg_baseline: Decimal = field(default_factory=lambda: Decimal("0"))
    last_mid: Decimal | None = None


class SportsFrlWeatherArbStrategy(StrategyBase):
    """Wave-driven FRL probability adjustment."""

    def __init__(self, spec: StrategySpec) -> None:
        super().__init__(spec)
        self.signal_source = str(
            spec.parameters.get("signal_source", "weather_tee_combined")
        )
        self.field_size = int(spec.parameters.get("field_size", 156))
        self.wind_penalty_strokes_per_mph = Decimal(
            str(spec.parameters.get("wind_penalty_strokes_per_mph", "0.05"))
        )
        self.wind_sg_baseline_weight = Decimal(
            str(spec.parameters.get("wind_sg_baseline_weight", "0.02"))
        )
        self.min_edge_bps = Decimal(str(spec.parameters.get("min_edge_bps", "150")))
        self.size = Decimal(str(spec.parameters.get("size", "5")))
        venue_value = str(spec.parameters.get("venue", "polymarket_global"))
        try:
            self.venue = Venue(venue_value)
        except ValueError as exc:
            raise ValueError(f"unknown venue: {venue_value}") from exc

        # Pre-declared player roster from spec; the signal event refreshes
        # wave + wind_sg_baseline at trigger time.
        self._players: dict[str, _PlayerWaveState] = {
            player_id: _PlayerWaveState(player_id=player_id, market_id=market_id)
            for player_id, market_id in _parse_player_market_map(
                str(spec.parameters.get("player_market_map", ""))
            ).items()
        }
        self._market_to_player: dict[str, str] = {
            state.market_id: state.player_id for state in self._players.values()
        }
        # Wave-level wind forecasts; refreshed by signal events.
        self._am_wind_mph: Decimal | None = None
        self._pm_wind_mph: Decimal | None = None

    def on_event(
        self, event: NormalizedEvent, ctx: StrategyContext
    ) -> Sequence[StrategyDecision]:
        if isinstance(event, QuoteEvent):
            self._track_mid(event)
            return (NoAction(reason="frl_mid_updated"),)
        if not isinstance(event, ExternalSignalEvent):
            return (NoAction(reason="ignored:not_external_signal"),)
        if event.source != self.signal_source:
            return (NoAction(reason=f"ignored:source!={self.signal_source}"),)

        self._absorb_signal(event.payload)
        return self._propose_orders(ctx)

    def _absorb_signal(self, payload: Any) -> None:
        # ``ExternalSignalEvent.payload`` is a FrozenMap (Mapping, not dict);
        # the nested ``players`` entries are also FrozenMaps. Accept any
        # Mapping so the strategy works against captured events as well as
        # hand-built fixtures.
        if not isinstance(payload, Mapping):
            return
        am = payload.get("am_wave_wind_mph")
        pm = payload.get("pm_wave_wind_mph")
        if am is not None:
            with suppress(ValueError, ArithmeticError):
                self._am_wind_mph = Decimal(str(am))
        if pm is not None:
            with suppress(ValueError, ArithmeticError):
                self._pm_wind_mph = Decimal(str(pm))
        for entry in payload.get("players") or ():
            if not isinstance(entry, Mapping):
                continue
            player_id = entry.get("player_id")
            if not isinstance(player_id, str) or player_id not in self._players:
                continue
            state = self._players[player_id]
            wave = entry.get("wave")
            if isinstance(wave, str) and wave.lower() in ("am", "pm"):
                state.wave = wave.lower()
            baseline = entry.get("wind_sg_baseline")
            if baseline is not None:
                with suppress(ValueError, ArithmeticError):
                    state.wind_sg_baseline = Decimal(str(baseline))

    def _propose_orders(self, ctx: StrategyContext) -> Sequence[StrategyDecision]:
        if self._am_wind_mph is None or self._pm_wind_mph is None:
            return (NoAction(reason="warmup:wind_forecast_incomplete"),)
        decisions: list[StrategyDecision] = []
        for state in sorted(self._players.values(), key=lambda s: s.player_id):
            if state.last_mid is None:
                continue
            implied = self._implied_probability(state, ctx)
            if implied is None:
                continue
            edge_bps = (implied - state.last_mid) * Decimal("10000")
            if abs(edge_bps) < self.min_edge_bps:
                continue
            side = OutcomeSide.YES if edge_bps > 0 else OutcomeSide.NO
            limit_price = state.last_mid if side is OutcomeSide.YES else (
                Decimal("1") - state.last_mid
            )
            limit_price = max(Decimal("0.01"), min(Decimal("0.99"), limit_price))
            decisions.append(
                PlaceOrder(
                    client_order_id=ClientOrderId(uuid4().hex),
                    instrument_id=InstrumentId(
                        venue=self.venue, market_id=state.market_id
                    ),
                    outcome_side=side,
                    order_side=OrderSide.BUY,
                    order_type=OrderType.LIMIT,
                    time_in_force=TimeInForce.GTC,
                    quantity=self.size,
                    price=limit_price,
                    reason=(
                        f"frl_wave:player={state.player_id} wave={state.wave} "
                        f"am={self._am_wind_mph} pm={self._pm_wind_mph} "
                        f"edge={edge_bps:+.0f}bps"
                    ),
                    expected_edge_bps=edge_bps,
                    priority=ExecutionPriority(tier=LatencyTier.RELAXED),
                )
            )
        if not decisions:
            return (NoAction(reason="no_player_edge_after_signal"),)
        return tuple(decisions)

    def _implied_probability(
        self, state: _PlayerWaveState, ctx: StrategyContext
    ) -> Decimal | None:
        if self.spec.model is not None:
            vector = self._feature_vector(state, ctx)
            if vector is not None:
                try:
                    raw = float(ctx.predict(str(self.spec.model.name), vector).value)
                except KeyError:
                    raw = None
                else:
                    return _clip(Decimal(str(raw)), Decimal("0.005"), Decimal("0.40"))
        return self._rules_mode_probability(state)

    def _feature_vector(
        self, state: _PlayerWaveState, ctx: StrategyContext
    ) -> FeatureVector | None:
        if self._am_wind_mph is None or self._pm_wind_mph is None:
            return None
        schema_id = self.spec.feature_schema_id or FeatureSchemaId(
            "sports_frl_weather_features"
        )
        values: tuple[tuple[str, float], ...] = (
            ("t0_am_wave_avg_wind_mph", float(self._am_wind_mph)),
            ("t0_pm_wave_avg_wind_mph", float(self._pm_wind_mph)),
            ("t0_player_historical_wind_sg", float(state.wind_sg_baseline)),
            ("t0_player_wave_assignment", 0.0 if state.wave == "am" else 1.0),
        )
        return FeatureVector(
            schema_id=schema_id,
            schema_version=str(self.spec.parameters.get("feature_schema_version", "v1")),
            instrument_id=InstrumentId(
                venue=self.venue, market_id=state.market_id
            ),
            timestamp=ctx.now,
            values=values,
        )

    def _rules_mode_probability(self, state: _PlayerWaveState) -> Decimal:
        baseline = Decimal("1") / Decimal(max(1, self.field_size))
        own_wind = self._am_wind_mph if state.wave == "am" else self._pm_wind_mph
        other_wind = self._pm_wind_mph if state.wave == "am" else self._am_wind_mph
        if own_wind is None or other_wind is None:
            return baseline
        wave_strokes = (other_wind - own_wind) * self.wind_penalty_strokes_per_mph
        boost = (wave_strokes + state.wind_sg_baseline) * self.wind_sg_baseline_weight
        return _clip(baseline + boost, Decimal("0.005"), Decimal("0.40"))

    def _track_mid(self, event: QuoteEvent) -> None:
        quote = event.quote
        player_id = self._market_to_player.get(quote.instrument_id.market_id)
        if player_id is None:
            return
        if quote.bid is None or quote.ask is None:
            return
        self._players[player_id].last_mid = (
            quote.bid.price + quote.ask.price
        ) / Decimal("2")


def _clip(value: Decimal, lo: Decimal, hi: Decimal) -> Decimal:
    return max(lo, min(hi, value))


def _parse_player_market_map(raw: str) -> dict[str, str]:
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


@register("sports_frl_weather_arb")
def factory(spec: StrategySpec) -> SportsFrlWeatherArbStrategy:
    return SportsFrlWeatherArbStrategy(spec)
