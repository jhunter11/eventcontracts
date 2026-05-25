"""Crypto 15-min vol-surface mispricer.

Hypothesis
----------
External crypto vol surfaces (Deribit ATM implied volatility at the
matching expiry, or the realized vol of CF Benchmarks index futures)
provide a model-implied probability for every Kalshi 15-min strike
through the Black-Scholes ``P(S_T >= K) = Φ(d2)`` identity. Kalshi
retail flow trades far-OTM strikes as lottery tickets and ignores the
much sharper signal from the listed crypto options market, so the
gap between the BS-implied probability and Kalshi's mid persists and
compensates for fees on size-disciplined entries.

Strategy
--------
* Track spot from an ``ExternalSignalEvent`` (source ``"binance"`` by
  default; payload carries ``last_price``).
* Track Deribit-style ATM IV from an ``ExternalSignalEvent`` (source
  ``"deribit"`` by default; payload carries ``atm_iv`` and the matching
  ``expiry_seconds`` so we know which Kalshi expiry it maps to).
* For each tracked Kalshi bracket, on ``QuoteEvent`` recompute the
  BS-implied probability and compare to the mid. When
  ``|bs_prob - kalshi_mid|`` exceeds ``min_edge_bps`` fire a single
  ``PlaceOrder`` on the favorable outcome side.

Game theory
-----------
The signal is strongest at moderately-OTM strikes (~0.5-1.5 σ from
spot). Retail loves these because they look "almost achievable",
which compresses the Kalshi probability above its BS-implied value.
The strategy is short directional exposure at the cohort level, taking
the other side of retail's persistent OTM bias.

Required spec parameters
------------------------
- ``strike_market_map``: semicolon-separated ``market_id:strike``
  entries — strikes in the same units as ``spot``.
- ``twap_window_seconds`` (default ``"60"``): TWAP settlement window
  Kalshi advertises (used to reduce the effective terminal variance).
- ``min_edge_bps`` (default ``"75"``): minimum ``|bs_prob - mid|`` to
  fire, expressed in bps of probability.
- ``size`` (default ``"5"``): contracts per fire.
- ``spot_source`` (default ``"binance"``), ``vol_source`` (default
  ``"deribit"``).
- ``venue`` (default ``"kalshi"``).
"""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import uuid4

from eventcontracts.crypto import bs_above_probability
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


class CryptoVolSurfaceMispricerStrategy(StrategyBase):
    """Compare external vol-surface BS probability to Kalshi mid; trade the gap."""

    def __init__(self, spec: StrategySpec) -> None:
        super().__init__(spec)
        self.strike_map: dict[str, _StrikeState] = _parse_strike_map(
            str(spec.parameters.get("strike_market_map", ""))
        )
        self.twap_window_seconds = Decimal(
            str(spec.parameters.get("twap_window_seconds", "60"))
        )
        self.min_edge_bps = Decimal(str(spec.parameters.get("min_edge_bps", "75")))
        self.size = Decimal(str(spec.parameters.get("size", "5")))
        self.spot_source = str(spec.parameters.get("spot_source", "binance"))
        self.vol_source = str(spec.parameters.get("vol_source", "deribit"))
        venue_value = str(spec.parameters.get("venue", "kalshi"))
        try:
            self.venue = Venue(venue_value)
        except ValueError as exc:
            raise ValueError(f"unknown venue: {venue_value}") from exc

        self._spot: Decimal | None = None
        self._spot_observed_at: datetime | None = None
        self._sigma_annual: Decimal | None = None
        self._sigma_observed_at: datetime | None = None
        # Expiry datetime is broadcast on the vol-source signal so the
        # strategy does not need to encode it per-strike in TOML.
        self._expiry_at: datetime | None = None

    def on_event(
        self, event: NormalizedEvent, ctx: StrategyContext
    ) -> Sequence[StrategyDecision]:
        if isinstance(event, ExternalSignalEvent):
            self._consume_signal(event)
            return (NoAction(reason=f"signal_updated:{event.source}"),)
        if not isinstance(event, QuoteEvent):
            return (NoAction(reason="ignored:not_quote_or_signal"),)

        market_id = event.quote.instrument_id.market_id
        if market_id not in self.strike_map:
            return (NoAction(reason="ignored:not_tracked_strike"),)

        mid = _mid_from_quote(event)
        if mid is None:
            return (NoAction(reason="censored:one_sided_quote"),)
        state = self.strike_map[market_id]
        state.mid = mid

        if self._spot is None or self._sigma_annual is None or self._expiry_at is None:
            return (NoAction(reason="warmup:missing_inputs"),)

        tau_seconds = Decimal(
            str(max(0.0, (self._expiry_at - ctx.now).total_seconds()))
        )
        if tau_seconds <= 0:
            return (NoAction(reason="censored:expiry_passed"),)

        bs_prob = bs_above_probability(
            spot=self._spot,
            strike=state.strike,
            sigma_annual=self._sigma_annual,
            tau_seconds=tau_seconds,
            twap_window_seconds=self.twap_window_seconds,
        )
        edge_bps = (bs_prob - mid) * Decimal("10000")
        if abs(edge_bps) < self.min_edge_bps:
            return (NoAction(reason=f"edge_below_threshold:{edge_bps:+.0f}bps"),)

        outcome_side = OutcomeSide.YES if edge_bps > 0 else OutcomeSide.NO
        leg_price = mid if outcome_side is OutcomeSide.YES else Decimal("1") - mid
        return (
            PlaceOrder(
                client_order_id=ClientOrderId(uuid4().hex),
                instrument_id=InstrumentId(venue=self.venue, market_id=market_id),
                outcome_side=outcome_side,
                order_side=OrderSide.BUY,
                order_type=OrderType.LIMIT,
                time_in_force=TimeInForce.GTC,
                quantity=self.size,
                price=_clip_probability(leg_price),
                reason=(
                    f"bs_mispricing bs={bs_prob:.4f} mid={mid:.4f} "
                    f"tau={tau_seconds:.0f}s sigma={self._sigma_annual:.3f}"
                ),
                expected_edge_bps=edge_bps,
                priority=ExecutionPriority(tier=LatencyTier.STANDARD),
            ),
        )

    def _consume_signal(self, event: ExternalSignalEvent) -> None:
        payload = event.payload
        if event.source == self.spot_source:
            with suppress(ValueError, ArithmeticError):
                last = payload.get("last_price") or payload.get("price")
                if last is not None:
                    self._spot = Decimal(str(last))
                    self._spot_observed_at = event.received_at
        if event.source == self.vol_source:
            with suppress(ValueError, ArithmeticError):
                atm_iv = payload.get("atm_iv") or payload.get("sigma_annual")
                if atm_iv is not None:
                    self._sigma_annual = Decimal(str(atm_iv))
                    self._sigma_observed_at = event.received_at
            expiry_iso = payload.get("expiry_iso")
            if isinstance(expiry_iso, str) and expiry_iso:
                with suppress(ValueError):
                    self._expiry_at = datetime.fromisoformat(expiry_iso)


def _mid_from_quote(event: QuoteEvent) -> Decimal | None:
    quote = event.quote
    if quote.bid is None or quote.ask is None:
        return None
    mid = (quote.bid.price + quote.ask.price) / Decimal("2")
    return mid if mid > 0 else None


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


@register("crypto_vol_surface_mispricer")
def factory(spec: StrategySpec) -> CryptoVolSurfaceMispricerStrategy:
    return CryptoVolSurfaceMispricerStrategy(spec)
