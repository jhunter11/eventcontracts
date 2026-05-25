"""Crypto 15-min terminal-drift tracker.

Hypothesis
----------
As time-to-expiry ``τ → 0`` the binary outcome of a Kalshi 15-min
crypto market converges to a step function of ``spot ≥ K``. Retail
mid-pricing on Kalshi updates only on each retail tick, which means
during the last 30-90 seconds of the contract there are persistent
windows where the spot has already strongly favored one outcome but
the Kalshi book has not caught up.

The strategy continuously re-evaluates the BS-implied probability
using the current spot, an internal short-window realized vol
estimate (so it does not need the Deribit feed), and the remaining
time-to-expiry. When ``τ`` drops below the configured terminal window
**and** the implied probability differs from the Kalshi mid by more
than ``min_terminal_edge``, it fires a ``FAST`` taker order.

Game theory
-----------
The edge is latency, not forecasting. Once the spot is more than ~1σ
inside the bracket of the in-the-money side, the binary is effectively
resolved; any Kalshi quote on the wrong side is mispriced. Faster
participants harvest this from retail. The strategy degrades gracefully
when there are many fast participants — the only feature that matters
is whether *this* sleeve is faster than the slowest counterparty
willing to leave a stale quote.

Required spec parameters
------------------------
- ``strike_market_map``: semicolon-separated ``market_id:strike`` entries.
- ``terminal_window_seconds`` (default ``"60"``): only fire when ``τ``
  is at or below this many seconds.
- ``min_terminal_edge`` (default ``"0.05"``): minimum
  ``|bs_prob - kalshi_mid|`` to fire.
- ``min_realized_samples`` (default ``"30"``): seconds of spot history
  required before computing realized vol.
- ``size`` (default ``"5"``).
- ``spot_source`` (default ``"binance"``).
- ``timer_label`` (default ``"crypto_terminal_check"``).
- ``venue`` (default ``"kalshi"``).
"""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from uuid import uuid4

from eventcontracts.crypto import bs_above_probability, realized_volatility
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
from eventcontracts.domain.ids import ClientOrderId
from eventcontracts.domain.latency import ExecutionPriority, LatencyTier
from eventcontracts.domain.models import InstrumentId, OutcomeSide, Venue
from eventcontracts.domain.orders import OrderSide, OrderType, TimeInForce
from eventcontracts.domain.spec import StrategySpec
from eventcontracts.strategy.base import StrategyBase
from eventcontracts.strategy.context import StrategyContext
from eventcontracts.strategy.registry import register


@dataclass
class _StrikeState:
    strike: Decimal
    mid: Decimal | None = None


@dataclass
class _SpotState:
    history: list[Decimal] = field(default_factory=list)
    capacity: int = 600  # ten minutes at 1Hz

    def push(self, price: Decimal) -> None:
        self.history.append(price)
        if len(self.history) > self.capacity:
            self.history.pop(0)

    def latest(self) -> Decimal | None:
        return self.history[-1] if self.history else None


class CryptoTerminalDriftTrackerStrategy(StrategyBase):
    """Take stale Kalshi quotes inside the last ``terminal_window_seconds``."""

    def __init__(self, spec: StrategySpec) -> None:
        super().__init__(spec)
        self.strike_map: dict[str, _StrikeState] = _parse_strike_map(
            str(spec.parameters.get("strike_market_map", ""))
        )
        self.terminal_window_seconds = Decimal(
            str(spec.parameters.get("terminal_window_seconds", "60"))
        )
        self.min_terminal_edge = Decimal(
            str(spec.parameters.get("min_terminal_edge", "0.05"))
        )
        self.min_realized_samples = int(spec.parameters.get("min_realized_samples", 30))
        self.size = Decimal(str(spec.parameters.get("size", "5")))
        self.spot_source = str(spec.parameters.get("spot_source", "binance"))
        self.timer_label = str(spec.parameters.get("timer_label", "crypto_terminal_check"))
        venue_value = str(spec.parameters.get("venue", "kalshi"))
        try:
            self.venue = Venue(venue_value)
        except ValueError as exc:
            raise ValueError(f"unknown venue: {venue_value}") from exc

        self._spot = _SpotState()
        self._expiry_at: datetime | None = None

    def on_event(
        self, event: NormalizedEvent, ctx: StrategyContext
    ) -> Sequence[StrategyDecision]:
        if isinstance(event, ExternalSignalEvent):
            self._consume_signal(event)
            return (NoAction(reason=f"signal_updated:{event.source}"),)
        if isinstance(event, QuoteEvent):
            self._track_quote(event)
            return (NoAction(reason="quote_tracked"),)
        if isinstance(event, TimerEvent) and event.label == self.timer_label:
            return self._evaluate(ctx)
        return (NoAction(reason="ignored:not_signal_quote_or_timer"),)

    def _consume_signal(self, event: ExternalSignalEvent) -> None:
        payload = event.payload
        if event.source == self.spot_source:
            with suppress(ValueError, ArithmeticError):
                last = payload.get("last_price") or payload.get("price")
                if last is not None:
                    self._spot.push(Decimal(str(last)))
            expiry_iso = payload.get("expiry_iso")
            if isinstance(expiry_iso, str) and expiry_iso:
                with suppress(ValueError):
                    self._expiry_at = datetime.fromisoformat(expiry_iso)

    def _track_quote(self, event: QuoteEvent) -> None:
        market_id = event.quote.instrument_id.market_id
        if market_id not in self.strike_map:
            return
        quote = event.quote
        if quote.bid is None or quote.ask is None:
            return
        mid = (quote.bid.price + quote.ask.price) / Decimal("2")
        if mid <= 0:
            return
        self.strike_map[market_id].mid = mid

    def _evaluate(self, ctx: StrategyContext) -> Sequence[StrategyDecision]:
        spot = self._spot.latest()
        if spot is None or self._expiry_at is None:
            return (NoAction(reason="warmup:missing_spot_or_expiry"),)
        tau_seconds = Decimal(str(max(0.0, (self._expiry_at - ctx.now).total_seconds())))
        if tau_seconds > self.terminal_window_seconds:
            return (NoAction(reason=f"tau_above_terminal_window:{tau_seconds:.0f}s"),)
        if tau_seconds <= 0:
            return (NoAction(reason="censored:expiry_passed"),)
        if len(self._spot.history) < self.min_realized_samples:
            return (NoAction(reason="warmup:realized_vol_history"),)

        sigma_annual = realized_volatility(tuple(self._spot.history))
        if sigma_annual <= 0:
            return (NoAction(reason="censored:realized_vol_zero"),)

        decisions: list[StrategyDecision] = []
        for market_id, state in self.strike_map.items():
            if state.mid is None:
                continue
            bs_prob = bs_above_probability(
                spot=spot,
                strike=state.strike,
                sigma_annual=sigma_annual,
                tau_seconds=tau_seconds,
            )
            edge = bs_prob - state.mid
            if abs(edge) < self.min_terminal_edge:
                continue
            outcome_side = OutcomeSide.YES if edge > 0 else OutcomeSide.NO
            leg_price = state.mid if outcome_side is OutcomeSide.YES else Decimal("1") - state.mid
            decisions.append(
                PlaceOrder(
                    client_order_id=ClientOrderId(uuid4().hex),
                    instrument_id=InstrumentId(venue=self.venue, market_id=market_id),
                    outcome_side=outcome_side,
                    order_side=OrderSide.BUY,
                    order_type=OrderType.LIMIT,
                    time_in_force=TimeInForce.IOC,
                    quantity=self.size,
                    price=_clip_probability(leg_price),
                    reason=(
                        f"terminal_drift tau={tau_seconds:.0f}s "
                        f"bs={bs_prob:.4f} mid={state.mid:.4f}"
                    ),
                    expected_edge_bps=edge * Decimal("10000"),
                    priority=ExecutionPriority(
                        tier=LatencyTier.FAST,
                        max_delay_ms=50,
                        expires_after_ms=300,
                        reason="terminal-window stale-quote pickoff",
                    ),
                )
            )
        if not decisions:
            return (NoAction(reason="terminal_evaluated_no_edge"),)
        return tuple(decisions)


def _parse_strike_map(raw: str) -> dict[str, _StrikeState]:
    out: dict[str, _StrikeState] = {}
    for item in raw.split(";"):
        if not item.strip():
            continue
        parts = [p.strip() for p in item.split(":")]
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise ValueError(
                "strike_market_map must be semicolon-separated market_id:strike entries"
            )
        market_id, strike_raw = parts
        out[market_id] = _StrikeState(strike=Decimal(strike_raw))
    return out


def _clip_probability(value: Decimal) -> Decimal:
    return max(Decimal("0.01"), min(Decimal("0.99"), value))


@register("crypto_terminal_drift_tracker")
def factory(spec: StrategySpec) -> CryptoTerminalDriftTrackerStrategy:
    return CryptoTerminalDriftTrackerStrategy(spec)
