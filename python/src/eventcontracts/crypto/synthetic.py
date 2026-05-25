"""Synthetic 15-min crypto data generator for ensemble validation.

Produces a deterministic stream of ``NormalizedEvent`` values that
realistically exercise every signal source in
:mod:`eventcontracts.crypto.signals`:

* ``ExternalSignalEvent(source="binance")`` — spot ticks driven by a
  seeded geometric Brownian motion.
* ``ExternalSignalEvent(source="deribit")`` — periodic ATM IV updates
  derived from the realized vol of the spot path. Real Deribit data
  can be plugged in via :func:`replace_deribit_iv`.
* ``QuoteEvent`` — Kalshi-style bracket mids derived from the BS
  probability at each strike, with optional per-source mispricings
  injected (parity bump, skew inversion).

The generator is pure-Python and side-effect-free apart from the
seeded RNG. Use it from unit tests, a CLI backtest run, or notebook
prototyping. The math lives in :mod:`eventcontracts.crypto.pricing`,
so the synthetic stream produces fair prices that a *correctly-
implemented* ensemble strategy should leave alone — any non-zero
trading came from injected mispricings, which is what we want to
study.
"""

from __future__ import annotations

import math
import random
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from eventcontracts.crypto.pricing import bs_above_probability_unclipped
from eventcontracts.domain.events import (
    EventProvenance,
    ExternalSignalEvent,
    NormalizedEvent,
    QuoteEvent,
)
from eventcontracts.domain.ids import EventId
from eventcontracts.domain.models import (
    InstrumentId,
    OrderBookLevel,
    OutcomeSide,
    Quote,
    Venue,
)


@dataclass(frozen=True)
class SyntheticBracket:
    """One bracket in the generated Kalshi-style partition."""

    market_id: str
    strike: Decimal  # lower bound of the bracket
    upper: Decimal | None = None  # None = unbounded (last bracket)


@dataclass(frozen=True)
class SyntheticConfig:
    """All knobs the generator exposes.

    The strike grid is the ordered set of ``strikes``; the synthetic
    partition is ``(-inf, strikes[0]) [strikes[0], strikes[1]) ...
    [strikes[-1], +inf)``. ``market_ids`` must be the same length so
    each bracket has a recognizable market id (e.g.
    ``["BTCD-LO", "BTCD-92K", ..., "BTCD-HI"]``).
    """

    spot_start: Decimal = Decimal("100000")
    sigma_annual: Decimal = Decimal("0.55")
    duration_seconds: int = 900  # one 15-min expiry
    spot_step_seconds: int = 1
    quote_step_seconds: int = 5  # Kalshi cadence
    # Real Kalshi 15-min crypto brackets cluster within ~1σ of spot
    # (with 55% annual vol over 900s that is ~$500, so strikes at
    # $99,500 / $100,000 / $100,500 give us meaningful in-between mass).
    # Convention: ``market_ids`` is **one longer** than ``strikes`` —
    # one bracket per "between strikes" interval plus the two unbounded
    # tails.
    market_ids: tuple[str, ...] = (
        "BTCD-LO",
        "BTCD-K99K5",
        "BTCD-K100K",
        "BTCD-K100K5",
    )
    strikes: tuple[Decimal, ...] = (
        Decimal("99500"),
        Decimal("100000"),
        Decimal("100500"),
    )
    #: Market IDs for the "above $K" market at each strike. Length must
    #: match ``strikes``. Set to ``()`` to skip emitting above-markets.
    above_market_ids: tuple[str, ...] = (
        "BTCD-A99K5",
        "BTCD-A100K",
        "BTCD-A100K5",
    )
    twap_window_seconds: Decimal = Decimal("60")
    seed: int = 1234
    # Mispricing injectors: each is added to the fair mid at the named
    # source. parity bump is added to every bracket; skew bump is added
    # to one specific bracket id to create a butterfly violation.
    parity_bump: Decimal = Decimal("0")
    skew_bump_market_id: str = ""
    skew_bump: Decimal = Decimal("0")


@dataclass(frozen=True)
class SyntheticScenario:
    """Generator output: the event stream plus context the test needs."""

    events: tuple[NormalizedEvent, ...]
    expiry_at: datetime
    bracket_partition: tuple[SyntheticBracket, ...]
    strike_market_map: dict[str, Decimal]
    #: ``above`` market id → strike. These are the "P(S_T >= K)" markets
    #: that the cross-strike skew and vol-surface strategies trade.
    above_strike_map: dict[str, Decimal]


def generate_scenario(
    config: SyntheticConfig,
    *,
    start: datetime | None = None,
    deribit_iv: Decimal | None = None,
) -> SyntheticScenario:
    """Generate one 15-min expiry's worth of events.

    Parameters
    ----------
    config
        Knobs for the spot path, strike grid, expiry length, and any
        mispricings to inject.
    start
        Wall-clock start. Defaults to ``2026-01-01 14:00 UTC``.
    deribit_iv
        If provided, used in place of the realized-vol-derived ATM IV
        on every Deribit event. Pass real Deribit data here to swap a
        live IV in.
    """

    start = start or datetime(2026, 1, 1, 14, 0, tzinfo=UTC)
    expiry_at = start + timedelta(seconds=config.duration_seconds)
    rng = random.Random(config.seed)
    spot_path = _simulate_spot_path(
        spot_start=float(config.spot_start),
        sigma_annual=float(config.sigma_annual),
        duration_seconds=config.duration_seconds,
        step_seconds=config.spot_step_seconds,
        rng=rng,
    )
    bracket_partition = _build_partition(config)
    # The strategies that work on "above" markets need a different
    # market_id → strike map. We expose both to the test layer.
    strike_market_map = {
        market_id: strike
        for market_id, strike in zip(
            config.above_market_ids, config.strikes, strict=False
        )
    }
    above_strike_map = dict(strike_market_map)

    events: list[NormalizedEvent] = []
    spot_times = [
        start + timedelta(seconds=i * config.spot_step_seconds)
        for i in range(len(spot_path))
    ]

    # Schedule spot ticks (1 Hz) and Kalshi quote ticks (every quote_step_seconds).
    quote_step = config.quote_step_seconds
    quote_indices = set(range(0, len(spot_path), max(1, quote_step // max(1, config.spot_step_seconds))))

    for i, (ts, spot) in enumerate(zip(spot_times, spot_path, strict=False)):
        events.append(_spot_event(ts, spot, expiry_at, i))
        # ATM IV update every minute.
        if i > 0 and i % 60 == 0:
            iv = deribit_iv or _empirical_iv(spot_path, i, sigma_annual=config.sigma_annual)
            events.append(_deribit_event(ts, iv, expiry_at, i))
        if i not in quote_indices:
            continue
        # Generate quotes for every bracket using the fair BS probability,
        # then inject configured mispricings.
        tau_seconds = max(1, (expiry_at - ts).total_seconds())
        fair_bracket_mids = _fair_bracket_mids(
            spot=Decimal(str(spot)),
            bracket_partition=bracket_partition,
            sigma_annual=config.sigma_annual,
            tau_seconds=Decimal(str(tau_seconds)),
            twap_window_seconds=config.twap_window_seconds,
        )
        fair_above_mids = _fair_above_mids(
            spot=Decimal(str(spot)),
            above_strike_map=above_strike_map,
            sigma_annual=config.sigma_annual,
            tau_seconds=Decimal(str(tau_seconds)),
            twap_window_seconds=config.twap_window_seconds,
        )
        # Apply parity bump uniformly across the partition.
        bumped_brackets = {
            mid: fair_bracket_mids[mid] + config.parity_bump
            for mid in fair_bracket_mids
        }
        # Skew bump targets one "above" market.
        bumped_above = dict(fair_above_mids)
        if (
            config.skew_bump_market_id
            and config.skew_bump_market_id in bumped_above
        ):
            bumped_above[config.skew_bump_market_id] += config.skew_bump
        for market_id, mid in bumped_brackets.items():
            events.append(_quote_event(ts, market_id, _clip_probability(mid), i))
        for market_id, mid in bumped_above.items():
            events.append(_quote_event(ts, market_id, _clip_probability(mid), i))

    return SyntheticScenario(
        events=tuple(events),
        expiry_at=expiry_at,
        bracket_partition=bracket_partition,
        strike_market_map=strike_market_map,
        above_strike_map=above_strike_map,
    )


def _fair_above_mids(
    *,
    spot: Decimal,
    above_strike_map: dict[str, Decimal],
    sigma_annual: Decimal,
    tau_seconds: Decimal,
    twap_window_seconds: Decimal,
) -> dict[str, Decimal]:
    """Per-strike ``P(S_T >= K)`` for the "above" market layer."""

    return {
        market_id: bs_above_probability_unclipped(
            spot=spot,
            strike=strike,
            sigma_annual=sigma_annual,
            tau_seconds=tau_seconds,
            twap_window_seconds=twap_window_seconds,
        )
        for market_id, strike in above_strike_map.items()
    }


def replace_deribit_iv(scenario: SyntheticScenario, iv: Decimal) -> SyntheticScenario:
    """Rewrite every Deribit IV event in ``scenario`` to use ``iv``.

    Useful when calling the live Deribit REST helper once and reusing
    the same IV for an entire generated scenario.
    """

    new_events: list[NormalizedEvent] = []
    for event in scenario.events:
        if (
            isinstance(event, ExternalSignalEvent)
            and event.source == "deribit"
        ):
            new_events.append(
                ExternalSignalEvent(
                    event_id=event.event_id,
                    source=event.source,
                    exchange_ts=event.exchange_ts,
                    received_at=event.received_at,
                    schema_version=event.schema_version,
                    payload={
                        **dict(event.payload),
                        "atm_iv": str(iv),
                    },
                    provenance=event.provenance,
                )
            )
        else:
            new_events.append(event)
    return SyntheticScenario(
        events=tuple(new_events),
        expiry_at=scenario.expiry_at,
        bracket_partition=scenario.bracket_partition,
        strike_market_map=scenario.strike_market_map,
        above_strike_map=scenario.above_strike_map,
    )


# ----------------------------- internals -----------------------------


def _simulate_spot_path(
    *,
    spot_start: float,
    sigma_annual: float,
    duration_seconds: int,
    step_seconds: int,
    rng: random.Random,
) -> list[float]:
    """Geometric Brownian motion with zero drift, seeded RNG."""

    dt = step_seconds / (365.25 * 24 * 60 * 60)
    sigma_step = sigma_annual * math.sqrt(dt)
    n_steps = duration_seconds // step_seconds
    out = [spot_start]
    for _ in range(n_steps):
        z = rng.gauss(0.0, 1.0)
        out.append(out[-1] * math.exp(-0.5 * sigma_step ** 2 + sigma_step * z))
    return out


def _build_partition(config: SyntheticConfig) -> tuple[SyntheticBracket, ...]:
    """Layout for ``n`` strikes is ``n+1`` brackets:

    * ``(-inf, strikes[0])`` — low unbounded
    * ``[strikes[i], strikes[i+1])`` for each interior pair
    * ``[strikes[-1], +inf)`` — high unbounded
    """

    if len(config.market_ids) != len(config.strikes) + 1:
        raise ValueError(
            "market_ids must be exactly one entry longer than strikes "
            "(one bracket per interior interval plus the two unbounded tails)"
        )
    out: list[SyntheticBracket] = []
    # Low unbounded bracket.
    out.append(
        SyntheticBracket(
            market_id=config.market_ids[0],
            strike=Decimal("0"),
            upper=config.strikes[0],
        )
    )
    # Interior brackets.
    for i in range(len(config.strikes) - 1):
        out.append(
            SyntheticBracket(
                market_id=config.market_ids[i + 1],
                strike=config.strikes[i],
                upper=config.strikes[i + 1],
            )
        )
    # High unbounded bracket.
    out.append(
        SyntheticBracket(
            market_id=config.market_ids[-1],
            strike=config.strikes[-1],
            upper=None,
        )
    )
    return tuple(out)


def _fair_bracket_mids(
    *,
    spot: Decimal,
    bracket_partition: tuple[SyntheticBracket, ...],
    sigma_annual: Decimal,
    tau_seconds: Decimal,
    twap_window_seconds: Decimal,
) -> dict[str, Decimal]:
    """Per-bracket fair YES probabilities, summing to ~1.0 across the partition.

    Each bracket's probability is ``P(S_T < upper) - P(S_T < lower)``.
    We use the YES convention (``settle in bracket``).
    """

    mids: dict[str, Decimal] = {}
    for bracket in bracket_partition:
        # Probability that S_T lies in [strike, upper).
        if bracket.upper is None:
            p_below_upper = Decimal("1")
        else:
            p_below_upper = Decimal("1") - bs_above_probability_unclipped(
                spot=spot,
                strike=bracket.upper,
                sigma_annual=sigma_annual,
                tau_seconds=tau_seconds,
                twap_window_seconds=twap_window_seconds,
            )
        if bracket.strike <= 0:
            p_below_lower = Decimal("0")
        else:
            p_below_lower = Decimal("1") - bs_above_probability_unclipped(
                spot=spot,
                strike=bracket.strike,
                sigma_annual=sigma_annual,
                tau_seconds=tau_seconds,
                twap_window_seconds=twap_window_seconds,
            )
        mids[bracket.market_id] = p_below_upper - p_below_lower
    return mids


def _spot_event(
    ts: datetime, spot: float, expiry_at: datetime, sequence: int
) -> ExternalSignalEvent:
    return ExternalSignalEvent(
        event_id=EventId(f"synth-spot-{sequence:06d}"),
        source="binance",
        exchange_ts=ts,
        received_at=ts,
        schema_version="binance-spot-v1",
        payload={
            "last_price": f"{spot:.2f}",
            "expiry_iso": expiry_at.isoformat(),
        },
        provenance=EventProvenance(source="binance", channel="spot", venue=None),
    )


def _deribit_event(
    ts: datetime, iv: Decimal, expiry_at: datetime, sequence: int
) -> ExternalSignalEvent:
    return ExternalSignalEvent(
        event_id=EventId(f"synth-deribit-{sequence:06d}"),
        source="deribit",
        exchange_ts=ts,
        received_at=ts,
        schema_version="deribit-iv-v1",
        payload={
            "atm_iv": str(iv),
            "expiry_iso": expiry_at.isoformat(),
        },
        provenance=EventProvenance(source="deribit", channel="iv", venue=None),
    )


def _quote_event(
    ts: datetime, market_id: str, mid: Decimal, sequence: int
) -> QuoteEvent:
    # One Kalshi tick on each side. Real crypto books are even tighter
    # on the centre but this is realistic for tail brackets.
    half_spread = Decimal("0.005")
    bid = max(Decimal("0.01"), mid - half_spread)
    ask = min(Decimal("0.99"), mid + half_spread)
    return QuoteEvent(
        event_id=EventId(f"synth-q-{market_id}-{sequence:06d}"),
        quote=Quote(
            instrument_id=InstrumentId(venue=Venue.KALSHI, market_id=market_id),
            side=OutcomeSide.YES,
            bid=OrderBookLevel(price=bid, quantity=Decimal("100")),
            ask=OrderBookLevel(price=ask, quantity=Decimal("100")),
            exchange_ts=ts,
            received_at=ts,
        ),
        provenance=EventProvenance(source="kalshi", channel="quote", venue=Venue.KALSHI),
    )


def _empirical_iv(
    spot_path: list[float], up_to_index: int, *, sigma_annual: Decimal
) -> Decimal:
    """Estimate an instantaneous ATM IV from the last 60 spot ticks.

    Returns the configured ``sigma_annual`` when there is not enough
    history to estimate (the first minute of the scenario).
    """

    window = max(2, min(60, up_to_index))
    if window < 2:
        return sigma_annual
    sub = spot_path[up_to_index - window : up_to_index + 1]
    log_returns = []
    for i in range(1, len(sub)):
        if sub[i - 1] <= 0 or sub[i] <= 0:
            return sigma_annual
        log_returns.append(math.log(sub[i] / sub[i - 1]))
    if len(log_returns) < 2:
        return sigma_annual
    mean = sum(log_returns) / len(log_returns)
    variance = sum((r - mean) ** 2 for r in log_returns) / (len(log_returns) - 1)
    stddev = math.sqrt(variance)
    seconds_per_year = 365.25 * 24 * 60 * 60
    annual = stddev * math.sqrt(seconds_per_year)
    return Decimal(str(annual))


def _clip_probability(value: Decimal) -> Decimal:
    return max(Decimal("0.01"), min(Decimal("0.99"), value))


# ----------------------------- iteration helpers -----------------------------


def event_stream(scenario: SyntheticScenario) -> Iterator[NormalizedEvent]:
    """Yield the scenario events in (already-sorted) order."""

    yield from scenario.events
