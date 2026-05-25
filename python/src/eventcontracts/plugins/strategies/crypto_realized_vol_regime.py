"""Crypto 15-min realized-vol regime trader.

Hypothesis
----------
Kalshi 15-min crypto markets price the next-window volatility through
their bracket structure. The *Kalshi-implied* annualized volatility
can be backed out from the price of a bracket straddle: if the
at-the-money bracket pays YES with probability ``p_atm`` and the
straddle width is one bracket size in dollars, then implied vol is
roughly ``Δ / spot * √(year / τ)`` where ``Δ`` is the bracket width.

This strategy compares Kalshi-implied vol against a short-window
realized vol forecast (5-15 minute trailing) and trades the gap:

* ``predicted_RV > kalshi_iv + min_vol_edge`` → buy "wide tail"
  brackets (far-OTM YES) — the market underprices tails.
* ``predicted_RV < kalshi_iv - min_vol_edge`` → sell wide-tail
  brackets (buy NO on each) — the market overprices tails.

Game theory
-----------
Realized vol is autocorrelated at the 1-15 minute horizon (the
"volatility clustering" stylized fact). Retail buys lottery tickets
on far-OTM strikes regardless of vol regime, so wide-tail brackets
tend to print structurally rich in calm regimes and structurally
cheap in storms. The strategy is short retail's flat tail-pricing.

Required spec parameters
------------------------
- ``tail_market_map``: semicolon-separated ``market_id:strike`` entries.
  These are the wide-tail brackets the strategy trades. Strikes are in
  the same units as ``spot``.
- ``atm_strike``: the at-the-money strike used to back out Kalshi IV.
- ``atm_market_id``: the Kalshi market for the ATM bracket.
- ``min_vol_edge`` (default ``"0.10"``): minimum annualized
  ``|rv - kalshi_iv|`` to fire (e.g. 10 vol points).
- ``rv_window_samples`` (default ``"600"``): seconds of spot history
  to use for the realized-vol forecast.
- ``size`` (default ``"3"``): contracts per tail leg.
- ``spot_source`` (default ``"binance"``).
- ``venue`` (default ``"kalshi"``).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from uuid import uuid4

from eventcontracts.crypto import realized_volatility
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
class _TailLeg:
    strike: Decimal
    mid: Decimal | None = None


@dataclass
class _SpotState:
    history: list[Decimal] = field(default_factory=list)
    capacity: int = 1800  # 30 minutes at 1Hz

    def push(self, price: Decimal) -> None:
        self.history.append(price)
        if len(self.history) > self.capacity:
            self.history.pop(0)


class CryptoRealizedVolRegimeStrategy(StrategyBase):
    """Long/short the tail brackets when realized vs implied vol diverges."""

    def __init__(self, spec: StrategySpec) -> None:
        super().__init__(spec)
        self.tail_map: dict[str, _TailLeg] = _parse_tail_map(
            str(spec.parameters.get("tail_market_map", ""))
        )
        self.atm_strike = Decimal(str(spec.parameters["atm_strike"]))
        self.atm_market_id = str(spec.parameters["atm_market_id"])
        self.min_vol_edge = Decimal(str(spec.parameters.get("min_vol_edge", "0.10")))
        self.rv_window_samples = int(spec.parameters.get("rv_window_samples", 600))
        self.size = Decimal(str(spec.parameters.get("size", "3")))
        self.spot_source = str(spec.parameters.get("spot_source", "binance"))
        venue_value = str(spec.parameters.get("venue", "kalshi"))
        try:
            self.venue = Venue(venue_value)
        except ValueError as exc:
            raise ValueError(f"unknown venue: {venue_value}") from exc

        self._spot = _SpotState()
        self._expiry_at: datetime | None = None
        self._atm_mid: Decimal | None = None
        # The bracket *width* in dollar terms: distance from the ATM strike
        # to the closest tail strike. Used to back out Kalshi-implied vol.
        self._bracket_width: Decimal | None = None

    def on_event(
        self, event: NormalizedEvent, ctx: StrategyContext
    ) -> Sequence[StrategyDecision]:
        if isinstance(event, ExternalSignalEvent):
            self._consume_signal(event)
            return (NoAction(reason=f"signal_updated:{event.source}"),)
        if not isinstance(event, QuoteEvent):
            return (NoAction(reason="ignored:not_signal_or_quote"),)

        return self._handle_quote(event, ctx)

    def _consume_signal(self, event: ExternalSignalEvent) -> None:
        payload = event.payload
        if event.source != self.spot_source:
            return
        with suppress(ValueError, ArithmeticError):
            last = payload.get("last_price") or payload.get("price")
            if last is not None:
                self._spot.push(Decimal(str(last)))
        expiry_iso = payload.get("expiry_iso")
        if isinstance(expiry_iso, str) and expiry_iso:
            with suppress(ValueError):
                self._expiry_at = datetime.fromisoformat(expiry_iso)

    def _handle_quote(
        self, event: QuoteEvent, ctx: StrategyContext
    ) -> Sequence[StrategyDecision]:
        market_id = event.quote.instrument_id.market_id
        mid = _mid_from_quote(event)
        if mid is None:
            return (NoAction(reason="censored:one_sided_quote"),)

        if market_id == self.atm_market_id:
            self._atm_mid = mid
            return (NoAction(reason="atm_mid_updated"),)
        if market_id not in self.tail_map:
            return (NoAction(reason="ignored:not_tracked_market"),)
        self.tail_map[market_id].mid = mid

        return self._evaluate(ctx)

    def _evaluate(self, ctx: StrategyContext) -> Sequence[StrategyDecision]:
        spot_latest = self._spot.history[-1] if self._spot.history else None
        if (
            spot_latest is None
            or self._expiry_at is None
            or self._atm_mid is None
            or len(self._spot.history) < self.rv_window_samples
        ):
            return (NoAction(reason="warmup:missing_inputs"),)
        tau_seconds = max(
            Decimal("1"), Decimal(str((self._expiry_at - ctx.now).total_seconds()))
        )

        # Back out Kalshi-implied vol from the ATM bracket using the
        # normal-approx straddle relation: p_atm ≈ Φ(0.5 σ √τ / σ √τ) =
        # 0.5, and the spread away from 0.5 maps to a vol/spot-distance
        # ratio. We use the closest tail strike to define bracket width.
        closest_strike = min(
            (leg.strike for leg in self.tail_map.values()),
            key=lambda s: abs(s - self.atm_strike),
        )
        bracket_width = abs(closest_strike - self.atm_strike)
        if bracket_width <= 0 or spot_latest <= 0:
            return (NoAction(reason="censored:degenerate_strike_grid"),)
        # Implied σ from p_atm under the lognormal-approx bracket model:
        #   p_atm ≈ Φ(half_width / (spot * σ * √τ))
        # Invert: σ = half_width / (spot * √τ * Φ⁻¹(p_atm))
        # When p_atm is close to 0.5 we use an asymptotic expansion.
        kalshi_iv = _kalshi_implied_vol(
            spot=spot_latest,
            bracket_width=bracket_width,
            atm_mid=self._atm_mid,
            tau_seconds=tau_seconds,
        )
        if kalshi_iv is None:
            return (NoAction(reason="censored:degenerate_kalshi_iv"),)

        rv = realized_volatility(tuple(self._spot.history[-self.rv_window_samples:]))
        if rv <= 0:
            return (NoAction(reason="censored:zero_realized_vol"),)

        edge = rv - kalshi_iv
        if abs(edge) < self.min_vol_edge:
            return (NoAction(reason=f"edge_below_threshold:rv={rv:.3f}_iv={kalshi_iv:.3f}"),)

        outcome_side = OutcomeSide.YES if edge > 0 else OutcomeSide.NO
        decisions: list[StrategyDecision] = []
        for market_id, leg in self.tail_map.items():
            if leg.mid is None:
                continue
            leg_price = leg.mid if outcome_side is OutcomeSide.YES else Decimal("1") - leg.mid
            decisions.append(
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
                        f"vol_regime rv={rv:.3f} kalshi_iv={kalshi_iv:.3f} "
                        f"tau={tau_seconds:.0f}s leg={market_id}"
                    ),
                    expected_edge_bps=edge * Decimal("10000"),
                    priority=ExecutionPriority(tier=LatencyTier.STANDARD),
                )
            )
        if not decisions:
            return (NoAction(reason="vol_regime_no_legs"),)
        return tuple(decisions)


def _kalshi_implied_vol(
    *,
    spot: Decimal,
    bracket_width: Decimal,
    atm_mid: Decimal,
    tau_seconds: Decimal,
) -> Decimal | None:
    """Back out Kalshi-implied annualized vol from the ATM bracket price.

    Approximation: assume the ATM bracket covers ``[K_atm - w/2, K_atm + w/2]``,
    so ``p_atm ≈ Φ(w / 2 / (spot σ √τ)) - Φ(-w / 2 / (spot σ √τ))
              ≈ 2 Φ(z) - 1``
    where ``z = w / (2 spot σ √τ)``. Inverting:
    ``σ = w / (2 spot √τ Φ⁻¹((p_atm + 1) / 2))``.

    For ``p_atm`` outside ``(0, 1)`` the function returns ``None``.
    """

    p = float(atm_mid)
    if not 0.0 < p < 1.0:
        return None
    z = _norm_ppf((p + 1.0) / 2.0)
    if z <= 0:
        return None
    seconds_per_year = 365.25 * 24 * 60 * 60
    tau_years = float(tau_seconds) / seconds_per_year
    if tau_years <= 0:
        return None
    sigma = float(bracket_width) / (2.0 * float(spot) * math.sqrt(tau_years) * z)
    return Decimal(str(sigma))


def _norm_ppf(p: float) -> float:
    """Inverse standard-normal CDF via the Beasley-Springer-Moro approximation.

    Accurate enough for the strategy's purposes (~1e-6 in ``[0.01, 0.99]``).
    """

    # Coefficients from Moro (1995).
    a = (
        2.50662823884,
        -18.61500062529,
        41.39119773534,
        -25.44106049637,
    )
    b = (
        -8.47351093090,
        23.08336743743,
        -21.06224101826,
        3.13082909833,
    )
    c = (
        0.3374754822726147,
        0.9761690190917186,
        0.1607979714918209,
        0.0276438810333863,
        0.0038405729373609,
        0.0003951896511919,
        0.0000321767881768,
        0.0000002888167364,
        0.0000003960315187,
    )

    y = p - 0.5
    if abs(y) < 0.42:
        r = y * y
        return (
            y
            * (((a[3] * r + a[2]) * r + a[1]) * r + a[0])
            / ((((b[3] * r + b[2]) * r + b[1]) * r + b[0]) * r + 1.0)
        )
    r = p if y < 0 else 1.0 - p
    r = math.log(-math.log(r))
    out = c[0]
    for i in range(1, 9):
        out += c[i] * r ** i
    return out if y >= 0 else -out


def _mid_from_quote(event: QuoteEvent) -> Decimal | None:
    quote = event.quote
    if quote.bid is None or quote.ask is None:
        return None
    mid = (quote.bid.price + quote.ask.price) / Decimal("2")
    return mid if mid > 0 else None


def _parse_tail_map(raw: str) -> dict[str, _TailLeg]:
    out: dict[str, _TailLeg] = {}
    for item in raw.split(";"):
        if not item.strip():
            continue
        parts = [p.strip() for p in item.split(":")]
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise ValueError(
                "tail_market_map must be semicolon-separated market_id:strike entries"
            )
        market_id, strike_raw = parts
        out[market_id] = _TailLeg(strike=Decimal(strike_raw))
    return out


def _clip_probability(value: Decimal) -> Decimal:
    return max(Decimal("0.01"), min(Decimal("0.99"), value))


@register("crypto_realized_vol_regime")
def factory(spec: StrategySpec) -> CryptoRealizedVolRegimeStrategy:
    return CryptoRealizedVolRegimeStrategy(spec)
