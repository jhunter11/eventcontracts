"""SpaceX/NASA launch delay predictor.

Hypothesis (per docs/strategy-specs.md #6): launch probability decays
predictably in the final hours of a window, and markets misprice the final
~2 hours when no fueling event has been observed.

Trigger: `TimerEvent` (5-minute heartbeat, label ``launch_check``), with
state updated from `ExternalSignalEvent` (launch metadata, wind shear) and
`LifecycleEvent` (market lifecycle).

Decision rule (rules-mode survival analysis): if
``time_to_window_close_mins < scrub_window_mins`` AND ``fueling_confirmed
== False``, place a NO order sized linearly with how late we are in the
window.

The spec calls for a Cox proportional-hazards model; the model pipeline is
scaffolded only, so this module ships the proportional-decay rule.

Required spec parameters:
- ``launch_market_id``: market_id of the launch contract
- ``signal_source`` (default ``"space-devs"``)
- ``scrub_window_mins`` (default 120)
- ``size_multiplier`` (default ``"0.2"``)
- ``max_size`` (default ``"50"``)
"""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import suppress
from decimal import Decimal
from uuid import uuid4

from eventcontracts.domain.decisions import (
    NoAction,
    PlaceOrder,
    StrategyDecision,
)
from eventcontracts.domain.events import (
    ExternalSignalEvent,
    LifecycleEvent,
    NormalizedEvent,
    TimerEvent,
)
from eventcontracts.domain.ids import ClientOrderId
from eventcontracts.domain.lifecycle import MarketLifecycleKind
from eventcontracts.domain.models import InstrumentId, OutcomeSide, Venue
from eventcontracts.domain.orders import OrderSide, OrderType, TimeInForce
from eventcontracts.domain.spec import StrategySpec
from eventcontracts.strategy.base import StrategyBase
from eventcontracts.strategy.context import StrategyContext
from eventcontracts.strategy.registry import register

TIMER_LABEL = "launch_check"


class AerospaceLaunchDelayStrategy(StrategyBase):
    """Time-decay NO bets on stalled launch windows."""

    def __init__(self, spec: StrategySpec) -> None:
        super().__init__(spec)
        self.launch_market_id = str(spec.parameters["launch_market_id"])
        self.signal_source = str(spec.parameters.get("signal_source", "space-devs"))
        self.scrub_window_mins = int(spec.parameters.get("scrub_window_mins", 120))
        self.size_multiplier = Decimal(str(spec.parameters.get("size_multiplier", "0.2")))
        self.max_size = Decimal(str(spec.parameters.get("max_size", "50")))
        venue_value = str(spec.parameters.get("venue", "kalshi"))
        try:
            self.venue = Venue(venue_value)
        except ValueError as exc:
            raise ValueError(f"unknown venue: {venue_value}") from exc

        # Mutable rolling state.
        self._time_to_window_mins: Decimal | None = None
        self._fueling_confirmed = False
        self._market_paused = False

    def on_event(
        self, event: NormalizedEvent, ctx: StrategyContext
    ) -> Sequence[StrategyDecision]:
        if isinstance(event, ExternalSignalEvent):
            self._update_from_signal(event)
            return (NoAction(reason="signal_state_updated"),)
        if isinstance(event, LifecycleEvent):
            self._update_from_lifecycle(event)
            return (NoAction(reason="lifecycle_updated"),)
        if not isinstance(event, TimerEvent) or event.label != TIMER_LABEL:
            return (NoAction(reason="ignored:not_launch_timer"),)

        if self._market_paused:
            return (NoAction(reason="censored:market_paused"),)
        if self._time_to_window_mins is None:
            return (NoAction(reason="warmup:no_time_to_window"),)
        if self._time_to_window_mins >= self.scrub_window_mins:
            return (NoAction(reason="too_early_in_window"),)
        if self._fueling_confirmed:
            return (NoAction(reason="fueling_confirmed:no_delay_bet"),)

        # Size grows as we approach window close without fueling.
        late_factor = max(
            Decimal("0"),
            Decimal(self.scrub_window_mins) - self._time_to_window_mins,
        )
        raw_size = (late_factor * self.size_multiplier).quantize(Decimal("1"))
        size = max(Decimal("1"), min(raw_size, self.max_size))

        return (
            PlaceOrder(
                client_order_id=ClientOrderId(uuid4().hex),
                instrument_id=InstrumentId(
                    venue=self.venue,
                    market_id=self.launch_market_id,
                    outcome_id="NO",
                ),
                outcome_side=OutcomeSide.NO,
                order_side=OrderSide.BUY,
                order_type=OrderType.LIMIT,
                time_in_force=TimeInForce.GTC,
                quantity=size,
                price=Decimal("0.50"),
                reason=(
                    f"launch_delay_t_minus_{self._time_to_window_mins}_mins_"
                    f"no_fueling"
                ),
            ),
        )

    def _update_from_signal(self, event: ExternalSignalEvent) -> None:
        if event.source != self.signal_source:
            return
        market_id = event.payload.get("launch_market_id")
        if market_id != self.launch_market_id:
            return
        ttwc = event.payload.get("time_to_window_close_mins")
        if ttwc is not None:
            with suppress(ValueError, ArithmeticError):
                self._time_to_window_mins = Decimal(str(ttwc))
        fueling = event.payload.get("fueling_confirmed")
        if isinstance(fueling, bool):
            self._fueling_confirmed = fueling

    def _update_from_lifecycle(self, event: LifecycleEvent) -> None:
        if event.lifecycle.instrument_id.market_id != self.launch_market_id:
            return
        self._market_paused = event.lifecycle.kind in (
            MarketLifecycleKind.PAUSED,
            MarketLifecycleKind.CLOSED,
        )


@register("aerospace_launch_delay")
def factory(spec: StrategySpec) -> AerospaceLaunchDelayStrategy:
    return AerospaceLaunchDelayStrategy(spec)
