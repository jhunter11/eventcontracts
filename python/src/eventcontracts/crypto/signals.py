"""Crypto-strategy signal abstraction shared by the ensemble.

A :class:`Signal` is a per-instrument directional view computed at a
point in time by one strategy family. The ensemble strategy
(``crypto_signal_ensemble``) maintains a small piece of state for each
signal source, calls the matching ``*_signal`` function on every
relevant event, and aggregates the resulting signals into a single
typed decision per instrument.

Each signal function is intentionally stateless apart from the small
state dataclass it consumes — this keeps the ensemble deterministic,
auditable, and easy to extend with new sources.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from eventcontracts.crypto.pricing import (
    bracket_parity_deviation,
    bs_above_probability,
    monotone_violations,
    realized_volatility,
)
from eventcontracts.domain.models import InstrumentId, OutcomeSide

# ----------------------------- Signal types -----------------------------


@dataclass(frozen=True)
class Signal:
    """Per-instrument directional view at a point in time.

    ``side`` is the outcome bracket the signal recommends *buying*:

    * ``OutcomeSide.YES`` — buy YES contracts (predict mid will rise).
    * ``OutcomeSide.NO``  — buy NO contracts (predict mid will fall).

    ``edge_bps`` is always non-negative; the direction lives in
    ``side``. ``confidence`` is in ``[0, 1]`` and represents the
    source's self-rated reliability for this signal.
    """

    instrument_id: InstrumentId
    side: OutcomeSide
    edge_bps: Decimal
    confidence: Decimal
    horizon_seconds: int
    source: str
    reason: str


# ----------------------------- Signal state -----------------------------


@dataclass
class ParityState:
    """Tracks mids across a disjoint, exhaustive bracket partition."""

    bracket_market_ids: tuple[str, ...]
    mid_by_market: dict[str, Decimal] = field(default_factory=dict)
    spread_bps_by_market: dict[str, Decimal] = field(default_factory=dict)

    def has_all_mids(self) -> bool:
        return all(mid in self.mid_by_market for mid in self.bracket_market_ids)


@dataclass
class VolSurfaceState:
    """Tracks spot, ATM IV, expiry, and Kalshi mid per strike."""

    strike_by_market: Mapping[str, Decimal]
    spot: Decimal | None = None
    sigma_annual: Decimal | None = None
    expiry_at_iso: str | None = None
    mid_by_market: dict[str, Decimal] = field(default_factory=dict)


@dataclass
class TerminalState:
    """Tracks spot history, expiry, and Kalshi mid per strike."""

    strike_by_market: Mapping[str, Decimal]
    spot_history: list[Decimal] = field(default_factory=list)
    spot_capacity: int = 600
    expiry_at_iso: str | None = None
    mid_by_market: dict[str, Decimal] = field(default_factory=dict)

    def push_spot(self, price: Decimal) -> None:
        self.spot_history.append(price)
        if len(self.spot_history) > self.spot_capacity:
            self.spot_history.pop(0)


@dataclass
class RegimeState:
    """Tracks spot history and ATM + tail bracket mids."""

    atm_market_id: str
    atm_strike: Decimal
    tail_strike_by_market: Mapping[str, Decimal]
    spot_history: list[Decimal] = field(default_factory=list)
    spot_capacity: int = 1800
    expiry_at_iso: str | None = None
    atm_mid: Decimal | None = None
    tail_mid_by_market: dict[str, Decimal] = field(default_factory=dict)

    def push_spot(self, price: Decimal) -> None:
        self.spot_history.append(price)
        if len(self.spot_history) > self.spot_capacity:
            self.spot_history.pop(0)


@dataclass
class SkewState:
    """Tracks mids and spreads across an ascending strike grid."""

    strikes: tuple[tuple[str, Decimal], ...]  # ascending by strike
    mid_by_market: dict[str, Decimal] = field(default_factory=dict)
    spread_bps_by_market: dict[str, Decimal] = field(default_factory=dict)


# ----------------------------- Signal functions -----------------------------

#: Default confidence values per source. Operators can override these via
#: ensemble configuration; the defaults reflect rough reliability ranking.
DEFAULT_CONFIDENCE: Mapping[str, Decimal] = {
    "parity": Decimal("0.95"),       # no-arb relationship; very high confidence
    "vol_surface": Decimal("0.70"),  # external data quality dependent
    "terminal": Decimal("0.80"),     # latency edge; degrades with competition
    "regime": Decimal("0.55"),       # noisier RV vs IV comparison
    "skew": Decimal("0.85"),         # no-arb butterfly; high confidence
}


def parity_signals(
    state: ParityState,
    venue,  # noqa: ANN001 (avoid circular import of Venue)
    *,
    min_parity_edge: Decimal = Decimal("0.015"),
    max_spread_bps: Decimal = Decimal("500"),
    confidence: Decimal | None = None,
) -> Sequence[Signal]:
    """Emit one signal per bracket when ``|Σmid - 1| > min_parity_edge``."""

    if not state.has_all_mids():
        return ()
    widest = max(state.spread_bps_by_market.values(), default=Decimal("0"))
    if widest > max_spread_bps:
        return ()

    probs = {mid: state.mid_by_market[mid] for mid in state.bracket_market_ids}
    deviation = bracket_parity_deviation(probs)
    if abs(deviation) < min_parity_edge:
        return ()

    # deviation > 0 → partition overpriced → buy NO on each
    side = OutcomeSide.NO if deviation > 0 else OutcomeSide.YES
    edge_bps = abs(deviation) * Decimal("10000")
    conf = confidence if confidence is not None else DEFAULT_CONFIDENCE["parity"]
    out: list[Signal] = []
    for market_id in state.bracket_market_ids:
        out.append(
            Signal(
                instrument_id=InstrumentId(venue=venue, market_id=market_id),
                side=side,
                edge_bps=edge_bps,
                confidence=conf,
                horizon_seconds=0,  # settles by partition definition
                source="parity",
                reason=f"parity_dev={deviation:+.4f}",
            )
        )
    return tuple(out)


def vol_surface_signals(
    state: VolSurfaceState,
    venue,  # noqa: ANN001
    *,
    now_iso: str,
    twap_window_seconds: Decimal = Decimal("60"),
    min_edge_bps: Decimal = Decimal("75"),
    confidence: Decimal | None = None,
) -> Sequence[Signal]:
    """Emit one signal per strike when ``|bs_prob - mid| × 10000 > min_edge_bps``."""

    if state.spot is None or state.sigma_annual is None or state.expiry_at_iso is None:
        return ()
    tau_seconds = _seconds_between(now_iso, state.expiry_at_iso)
    if tau_seconds is None or tau_seconds <= 0:
        return ()

    conf = confidence if confidence is not None else DEFAULT_CONFIDENCE["vol_surface"]
    out: list[Signal] = []
    for market_id, strike in state.strike_by_market.items():
        mid = state.mid_by_market.get(market_id)
        if mid is None:
            continue
        bs_prob = bs_above_probability(
            spot=state.spot,
            strike=strike,
            sigma_annual=state.sigma_annual,
            tau_seconds=tau_seconds,
            twap_window_seconds=twap_window_seconds,
        )
        edge_bps = (bs_prob - mid) * Decimal("10000")
        if abs(edge_bps) < min_edge_bps:
            continue
        side = OutcomeSide.YES if edge_bps > 0 else OutcomeSide.NO
        out.append(
            Signal(
                instrument_id=InstrumentId(venue=venue, market_id=market_id),
                side=side,
                edge_bps=abs(edge_bps),
                confidence=conf,
                horizon_seconds=int(tau_seconds),
                source="vol_surface",
                reason=f"bs={bs_prob:.4f}_mid={mid:.4f}_sigma={state.sigma_annual:.3f}",
            )
        )
    return tuple(out)


def terminal_signals(
    state: TerminalState,
    venue,  # noqa: ANN001
    *,
    now_iso: str,
    terminal_window_seconds: Decimal = Decimal("60"),
    min_terminal_edge: Decimal = Decimal("0.05"),
    min_realized_samples: int = 30,
    confidence: Decimal | None = None,
) -> Sequence[Signal]:
    """Emit per-strike signals when inside the terminal window and the
    realized-vol-based BS probability diverges from Kalshi mid."""

    if state.expiry_at_iso is None or not state.spot_history:
        return ()
    tau_seconds = _seconds_between(now_iso, state.expiry_at_iso)
    if tau_seconds is None:
        return ()
    if tau_seconds > terminal_window_seconds or tau_seconds <= 0:
        return ()
    if len(state.spot_history) < min_realized_samples:
        return ()

    sigma_annual = realized_volatility(tuple(state.spot_history))
    if sigma_annual <= 0:
        return ()
    spot = state.spot_history[-1]

    conf = confidence if confidence is not None else DEFAULT_CONFIDENCE["terminal"]
    out: list[Signal] = []
    for market_id, strike in state.strike_by_market.items():
        mid = state.mid_by_market.get(market_id)
        if mid is None:
            continue
        bs_prob = bs_above_probability(
            spot=spot,
            strike=strike,
            sigma_annual=sigma_annual,
            tau_seconds=tau_seconds,
        )
        edge = bs_prob - mid
        if abs(edge) < min_terminal_edge:
            continue
        side = OutcomeSide.YES if edge > 0 else OutcomeSide.NO
        out.append(
            Signal(
                instrument_id=InstrumentId(venue=venue, market_id=market_id),
                side=side,
                edge_bps=abs(edge) * Decimal("10000"),
                confidence=conf,
                horizon_seconds=int(tau_seconds),
                source="terminal",
                reason=f"tau={tau_seconds:.0f}s_bs={bs_prob:.4f}_mid={mid:.4f}",
            )
        )
    return tuple(out)


def regime_signals(
    state: RegimeState,
    venue,  # noqa: ANN001
    *,
    now_iso: str,
    min_vol_edge: Decimal = Decimal("0.10"),
    rv_window_samples: int = 600,
    confidence: Decimal | None = None,
) -> Sequence[Signal]:
    """Emit one signal per tail bracket when realized vol diverges from
    Kalshi-implied vol (backed out from the ATM bracket)."""

    if (
        state.atm_mid is None
        or state.expiry_at_iso is None
        or len(state.spot_history) < rv_window_samples
        or not state.tail_mid_by_market
    ):
        return ()
    spot = state.spot_history[-1]
    tau_seconds = _seconds_between(now_iso, state.expiry_at_iso)
    if tau_seconds is None or tau_seconds <= 0:
        return ()

    # Pick the tail strike closest to the ATM strike to define bracket width.
    closest_strike = min(
        state.tail_strike_by_market.values(),
        key=lambda s: abs(s - state.atm_strike),
    )
    bracket_width = abs(closest_strike - state.atm_strike)
    if bracket_width <= 0 or spot <= 0:
        return ()

    kalshi_iv = _kalshi_implied_vol_from_atm(
        spot=spot,
        bracket_width=bracket_width,
        atm_mid=state.atm_mid,
        tau_seconds=tau_seconds,
    )
    if kalshi_iv is None:
        return ()

    rv = realized_volatility(tuple(state.spot_history[-rv_window_samples:]))
    if rv <= 0:
        return ()
    edge = rv - kalshi_iv
    if abs(edge) < min_vol_edge:
        return ()
    side = OutcomeSide.YES if edge > 0 else OutcomeSide.NO
    conf = confidence if confidence is not None else DEFAULT_CONFIDENCE["regime"]
    out: list[Signal] = []
    for market_id in state.tail_strike_by_market:
        if market_id not in state.tail_mid_by_market:
            continue
        out.append(
            Signal(
                instrument_id=InstrumentId(venue=venue, market_id=market_id),
                side=side,
                edge_bps=abs(edge) * Decimal("10000"),
                confidence=conf,
                horizon_seconds=int(tau_seconds),
                source="regime",
                reason=f"rv={rv:.3f}_iv={kalshi_iv:.3f}",
            )
        )
    return tuple(out)


def skew_signals(
    state: SkewState,
    venue,  # noqa: ANN001
    *,
    min_skew_edge: Decimal = Decimal("0.01"),
    max_spread_bps: Decimal = Decimal("500"),
    confidence: Decimal | None = None,
) -> Sequence[Signal]:
    """Emit signals for ascending-strike inversions (butterfly violations)."""

    if any(market_id not in state.mid_by_market for market_id, _ in state.strikes):
        return ()
    violations = monotone_violations(
        tuple((strike, state.mid_by_market[market_id]) for market_id, strike in state.strikes)
    )
    if not violations:
        return ()
    market_for_strike = {strike: market_id for market_id, strike in state.strikes}

    conf = confidence if confidence is not None else DEFAULT_CONFIDENCE["skew"]
    out: list[Signal] = []
    for strike_low, _p_low, strike_high, p_high in violations:
        edge = p_high - state.mid_by_market[market_for_strike[strike_low]]
        if edge < min_skew_edge:
            continue
        low_market = market_for_strike[strike_low]
        high_market = market_for_strike[strike_high]
        low_spread = state.spread_bps_by_market.get(low_market)
        high_spread = state.spread_bps_by_market.get(high_market)
        if low_spread is None or high_spread is None:
            continue
        if low_spread > max_spread_bps or high_spread > max_spread_bps:
            continue
        edge_bps = edge * Decimal("10000")
        # Low strike: buy YES (cheap). High strike: buy NO (rich).
        out.append(
            Signal(
                instrument_id=InstrumentId(venue=venue, market_id=low_market),
                side=OutcomeSide.YES,
                edge_bps=edge_bps,
                confidence=conf,
                horizon_seconds=0,
                source="skew",
                reason=f"butterfly_low={strike_low}_high={strike_high}",
            )
        )
        out.append(
            Signal(
                instrument_id=InstrumentId(venue=venue, market_id=high_market),
                side=OutcomeSide.NO,
                edge_bps=edge_bps,
                confidence=conf,
                horizon_seconds=0,
                source="skew",
                reason=f"butterfly_low={strike_low}_high={strike_high}",
            )
        )
    return tuple(out)


# ----------------------------- Ensemble verdict -----------------------------


@dataclass(frozen=True)
class EnsembleVerdict:
    """Aggregate decision for one instrument."""

    instrument_id: InstrumentId
    side: OutcomeSide | None     # None when verdict is HOLD
    net_edge_bps: Decimal        # signed; >0 means YES, <0 means NO
    contributing_sources: tuple[str, ...]
    per_source_edge_bps: Mapping[str, Decimal]  # signed: + YES, - NO


def combine_signals(
    signals: Iterable[Signal],
    *,
    weights: Mapping[str, Decimal] | None = None,
    min_combined_edge_bps: Decimal = Decimal("50"),
    min_confluence: int = 2,
) -> tuple[EnsembleVerdict, ...]:
    """Aggregate signals into one verdict per instrument.

    For each instrument with at least ``min_confluence`` contributing
    sources, the function computes the weighted net edge

        net_edge = Σ_{s∈sources} weight[s.source] * edge_bps(s) * conf(s) * sign(s.side)

    where ``sign(YES) = +1`` and ``sign(NO) = -1``. If
    ``|net_edge| > min_combined_edge_bps`` the verdict carries the
    dominant side; otherwise the verdict is ``HOLD`` (``side = None``).
    """

    weights = weights or {}
    grouped: dict[InstrumentId, list[Signal]] = {}
    for signal in signals:
        grouped.setdefault(signal.instrument_id, []).append(signal)

    verdicts: list[EnsembleVerdict] = []
    for instrument_id, group in grouped.items():
        sources = {s.source for s in group}
        if len(sources) < min_confluence:
            continue

        per_source: dict[str, Decimal] = {}
        for signal in group:
            w = weights.get(signal.source, Decimal("1"))
            signed_edge = signal.edge_bps * signal.confidence * w
            if signal.side is OutcomeSide.NO:
                signed_edge = -signed_edge
            per_source[signal.source] = per_source.get(signal.source, Decimal("0")) + signed_edge

        net_edge = sum(per_source.values(), Decimal("0"))
        if abs(net_edge) < min_combined_edge_bps:
            side: OutcomeSide | None = None
        else:
            side = OutcomeSide.YES if net_edge > 0 else OutcomeSide.NO
        verdicts.append(
            EnsembleVerdict(
                instrument_id=instrument_id,
                side=side,
                net_edge_bps=net_edge,
                contributing_sources=tuple(sorted(sources)),
                per_source_edge_bps=per_source,
            )
        )
    return tuple(verdicts)


# ----------------------------- Internal helpers -----------------------------


def _seconds_between(now_iso: str, future_iso: str) -> Decimal | None:
    from datetime import datetime

    try:
        now = datetime.fromisoformat(now_iso)
        future = datetime.fromisoformat(future_iso)
    except ValueError:
        return None
    return Decimal(str(max(0.0, (future - now).total_seconds())))


def _kalshi_implied_vol_from_atm(
    *,
    spot: Decimal,
    bracket_width: Decimal,
    atm_mid: Decimal,
    tau_seconds: Decimal,
) -> Decimal | None:
    """Back out Kalshi-implied annualized vol from the ATM bracket price.

    See ``crypto_realized_vol_regime._kalshi_implied_vol`` for the math.
    Kept inline here so the ensemble does not import a strategy module.
    """

    import math

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
    """Inverse standard-normal CDF via the Beasley-Springer-Moro approximation."""

    import math

    a = (2.50662823884, -18.61500062529, 41.39119773534, -25.44106049637)
    b = (-8.47351093090, 23.08336743743, -21.06224101826, 3.13082909833)
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
