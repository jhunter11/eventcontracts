"""Crypto signal ensemble — confluence-based buy / sell / hold.

Meta-strategy that maintains state for five independent crypto signal
sources (bracket parity, Black-Scholes vol-surface mispricing,
terminal stale-quote pickoff, realized-vol regime trade, cross-strike
butterfly skew) and combines their outputs into a single typed
decision per instrument.

The ensemble does **not** replace the standalone strategies — each
underlying strategy can still run as its own sleeve. Use the ensemble
when you want a higher-confluence trade that requires multiple
independent edges to agree before firing.

Signal aggregation
------------------
For each tracked instrument with at least ``min_confluence`` sources
contributing signals, compute the weighted net edge in bps:

    net_edge = Σ weight[source] * edge_bps * confidence * sign(side)

where ``sign(YES) = +1`` and ``sign(NO) = -1``. Fire a
``PlaceOrder`` on the dominant side when ``|net_edge| >
min_combined_edge_bps``; otherwise emit ``NoAction`` (the "HOLD"
verdict) with the per-source breakdown in the reason string. Every
firing also emits an ``Alert`` carrying the per-source edge map for
audit and observability.

Required spec parameters
------------------------
- ``bracket_market_ids``: parity partition, ``market_id:lower:upper``
  semicolon-separated. ``inf``/``-inf`` for unbounded tails.
- ``strike_market_map``: ``market_id:strike`` for the strikes the
  ``vol_surface``, ``terminal`` and ``skew`` sources watch.
- ``atm_market_id`` + ``atm_strike``: ATM bracket the ``regime``
  source backs out Kalshi-implied vol from.
- ``tail_market_map``: ``market_id:strike`` tail brackets the
  ``regime`` source directionally trades.
- ``enabled_sources`` (default ``"parity,vol_surface,terminal,regime,skew"``).
- ``weights`` (default each ``"1.0"``): per-source weight as
  ``source:weight;source:weight``.
- ``min_combined_edge_bps`` (default ``"50"``): aggregate edge
  threshold in bps to fire.
- ``min_confluence`` (default ``"2"``): minimum distinct sources
  required to produce a non-HOLD verdict.
- ``size`` (default ``"5"``): contracts per fired order.
- ``max_spread_bps`` (default ``"500"``).
- ``terminal_window_seconds`` (default ``"60"``).
- ``terminal_min_realized_samples`` (default ``"30"``).
- ``rv_window_samples`` (default ``"600"``).
- ``twap_window_seconds`` (default ``"60"``).
- ``timer_label`` (default ``"crypto_ensemble_evaluate"``).
- ``spot_source`` (default ``"binance"``), ``vol_source`` (default
  ``"deribit"``).
- ``venue`` (default ``"kalshi"``).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import suppress
from decimal import Decimal
from uuid import uuid4

from eventcontracts.crypto import (
    BracketVolState,
    EnsembleVerdict,
    ParityState,
    RegimeState,
    Signal,
    SkewState,
    TerminalState,
    VolSurfaceState,
    bracket_vol_signals,
    combine_signals,
    parity_signals,
    regime_signals,
    skew_signals,
    terminal_signals,
    vol_surface_signals,
)
from eventcontracts.domain.decisions import (
    Alert,
    AlertSeverity,
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
from eventcontracts.domain.models import OutcomeSide, Venue
from eventcontracts.domain.orders import OrderSide, OrderType, TimeInForce
from eventcontracts.domain.spec import StrategySpec
from eventcontracts.strategy.base import StrategyBase
from eventcontracts.strategy.context import StrategyContext
from eventcontracts.strategy.registry import register

_ALL_SOURCES = (
    "parity",
    "vol_surface",
    "bracket_vol",
    "terminal",
    "regime",
    "skew",
)


class CryptoSignalEnsembleStrategy(StrategyBase):
    """Confluence-based crypto signal aggregator."""

    def __init__(self, spec: StrategySpec) -> None:
        super().__init__(spec)
        self.enabled_sources = _parse_enabled_sources(
            str(spec.parameters.get("enabled_sources", ",".join(_ALL_SOURCES)))
        )
        self.weights = _parse_weights(str(spec.parameters.get("weights", "")))
        self.min_combined_edge_bps = Decimal(
            str(spec.parameters.get("min_combined_edge_bps", "50"))
        )
        self.min_confluence = int(spec.parameters.get("min_confluence", 2))
        self.size = Decimal(str(spec.parameters.get("size", "5")))
        self.max_spread_bps = Decimal(str(spec.parameters.get("max_spread_bps", "500")))
        self.terminal_window_seconds = Decimal(
            str(spec.parameters.get("terminal_window_seconds", "60"))
        )
        self.terminal_min_realized_samples = int(
            spec.parameters.get("terminal_min_realized_samples", 30)
        )
        self.rv_window_samples = int(spec.parameters.get("rv_window_samples", 600))
        self.twap_window_seconds = Decimal(
            str(spec.parameters.get("twap_window_seconds", "60"))
        )
        self.timer_label = str(spec.parameters.get("timer_label", "crypto_ensemble_evaluate"))
        self.spot_source = str(spec.parameters.get("spot_source", "binance"))
        self.vol_source = str(spec.parameters.get("vol_source", "deribit"))
        self.venue = _venue(str(spec.parameters.get("venue", "kalshi")))

        bracket_ids = _parse_bracket_ids(
            str(spec.parameters.get("bracket_market_ids", ""))
        )
        # Bracket source uses the real partition: each between-bracket
        # exposes ``(lower, upper)`` so the BS-derived interval
        # probability can be compared to the Kalshi mid.
        bracket_intervals = _parse_bracket_intervals(
            str(spec.parameters.get("bracket_market_ids", ""))
        )
        strike_map = _parse_strike_map(
            str(spec.parameters.get("strike_market_map", ""))
        )
        tail_map = _parse_strike_map(str(spec.parameters.get("tail_market_map", "")))
        atm_market_id = str(spec.parameters.get("atm_market_id", ""))
        atm_strike_raw = str(spec.parameters.get("atm_strike", "0"))

        self.parity_state = ParityState(bracket_market_ids=tuple(bracket_ids))
        self.bracket_vol_state = BracketVolState(intervals_by_market=bracket_intervals)
        self.vol_state = VolSurfaceState(strike_by_market=strike_map)
        self.terminal_state = TerminalState(strike_by_market=strike_map)
        self.regime_state = (
            RegimeState(
                atm_market_id=atm_market_id,
                atm_strike=Decimal(atm_strike_raw),
                tail_strike_by_market=tail_map,
            )
            if atm_market_id and atm_strike_raw and tail_map
            else None
        )
        ascending_strikes = sorted(strike_map.items(), key=lambda kv: kv[1])
        self.skew_state = SkewState(strikes=tuple(ascending_strikes))

        # Track which strike-bracket markets we are willing to act on. The
        # decision emitter needs the latest mid and side for each.
        self.tracked_markets: dict[str, _MarketState] = {}
        for market_id in bracket_ids:
            self.tracked_markets.setdefault(market_id, _MarketState())
        for market_id in strike_map:
            self.tracked_markets.setdefault(market_id, _MarketState())
        for market_id in tail_map:
            self.tracked_markets.setdefault(market_id, _MarketState())
        if atm_market_id:
            self.tracked_markets.setdefault(atm_market_id, _MarketState())

    # ----------------------------- event ingestion -----------------------------

    def on_event(
        self, event: NormalizedEvent, ctx: StrategyContext
    ) -> Sequence[StrategyDecision]:
        if isinstance(event, ExternalSignalEvent):
            self._consume_signal(event)
            return (NoAction(reason=f"signal_updated:{event.source}"),)
        if isinstance(event, QuoteEvent):
            self._track_quote(event)
            return self._evaluate(ctx, trigger="quote")
        if isinstance(event, TimerEvent) and event.label == self.timer_label:
            return self._evaluate(ctx, trigger="timer")
        return (NoAction(reason="ignored:not_signal_quote_or_timer"),)

    def _consume_signal(self, event: ExternalSignalEvent) -> None:
        payload = event.payload
        if event.source == self.spot_source:
            with suppress(ValueError, ArithmeticError):
                last = payload.get("last_price") or payload.get("price")
                if last is not None:
                    price = Decimal(str(last))
                    self.terminal_state.push_spot(price)
                    if self.regime_state is not None:
                        self.regime_state.push_spot(price)
                    self.vol_state.spot = price
                    self.bracket_vol_state.spot = price
            expiry_iso = payload.get("expiry_iso")
            if isinstance(expiry_iso, str) and expiry_iso:
                self.vol_state.expiry_at_iso = expiry_iso
                self.terminal_state.expiry_at_iso = expiry_iso
                self.bracket_vol_state.expiry_at_iso = expiry_iso
                if self.regime_state is not None:
                    self.regime_state.expiry_at_iso = expiry_iso
        if event.source == self.vol_source:
            with suppress(ValueError, ArithmeticError):
                atm_iv = payload.get("atm_iv") or payload.get("sigma_annual")
                if atm_iv is not None:
                    sigma = Decimal(str(atm_iv))
                    self.vol_state.sigma_annual = sigma
                    self.bracket_vol_state.sigma_annual = sigma
            expiry_iso = payload.get("expiry_iso")
            if isinstance(expiry_iso, str) and expiry_iso:
                self.vol_state.expiry_at_iso = expiry_iso
                self.terminal_state.expiry_at_iso = expiry_iso
                self.bracket_vol_state.expiry_at_iso = expiry_iso
                if self.regime_state is not None:
                    self.regime_state.expiry_at_iso = expiry_iso

    def _track_quote(self, event: QuoteEvent) -> None:
        quote = event.quote
        market_id = quote.instrument_id.market_id
        if quote.bid is None or quote.ask is None:
            return
        mid = (quote.bid.price + quote.ask.price) / Decimal("2")
        if mid <= 0:
            return
        spread = quote.ask.price - quote.bid.price
        spread_bps = spread / mid * Decimal("10000")
        # Update every source's view of this market.
        if market_id in self.parity_state.bracket_market_ids:
            self.parity_state.mid_by_market[market_id] = mid
            self.parity_state.spread_bps_by_market[market_id] = spread_bps
        if market_id in self.bracket_vol_state.intervals_by_market:
            self.bracket_vol_state.mid_by_market[market_id] = mid
        if market_id in self.vol_state.strike_by_market:
            self.vol_state.mid_by_market[market_id] = mid
        if market_id in self.terminal_state.strike_by_market:
            self.terminal_state.mid_by_market[market_id] = mid
        if self.regime_state is not None:
            if market_id == self.regime_state.atm_market_id:
                self.regime_state.atm_mid = mid
            if market_id in self.regime_state.tail_strike_by_market:
                self.regime_state.tail_mid_by_market[market_id] = mid
        if any(m == market_id for m, _ in self.skew_state.strikes):
            self.skew_state.mid_by_market[market_id] = mid
            self.skew_state.spread_bps_by_market[market_id] = spread_bps
        if market_id in self.tracked_markets:
            self.tracked_markets[market_id].mid = mid
            self.tracked_markets[market_id].spread_bps = spread_bps

    # ----------------------------- evaluation -----------------------------

    def _evaluate(
        self, ctx: StrategyContext, *, trigger: str
    ) -> Sequence[StrategyDecision]:
        now_iso = ctx.now.isoformat()
        all_signals: list[Signal] = []
        if "parity" in self.enabled_sources:
            all_signals.extend(
                parity_signals(
                    self.parity_state,
                    self.venue,
                    max_spread_bps=self.max_spread_bps,
                )
            )
        if "vol_surface" in self.enabled_sources:
            all_signals.extend(
                vol_surface_signals(
                    self.vol_state,
                    self.venue,
                    now_iso=now_iso,
                    twap_window_seconds=self.twap_window_seconds,
                )
            )
        if "bracket_vol" in self.enabled_sources:
            all_signals.extend(
                bracket_vol_signals(
                    self.bracket_vol_state,
                    self.venue,
                    now_iso=now_iso,
                    twap_window_seconds=self.twap_window_seconds,
                )
            )
        if "terminal" in self.enabled_sources:
            all_signals.extend(
                terminal_signals(
                    self.terminal_state,
                    self.venue,
                    now_iso=now_iso,
                    terminal_window_seconds=self.terminal_window_seconds,
                    min_realized_samples=self.terminal_min_realized_samples,
                )
            )
        if "regime" in self.enabled_sources and self.regime_state is not None:
            all_signals.extend(
                regime_signals(
                    self.regime_state,
                    self.venue,
                    now_iso=now_iso,
                    rv_window_samples=self.rv_window_samples,
                )
            )
        if "skew" in self.enabled_sources:
            all_signals.extend(
                skew_signals(
                    self.skew_state,
                    self.venue,
                    max_spread_bps=self.max_spread_bps,
                )
            )

        verdicts = combine_signals(
            all_signals,
            weights=self.weights,
            min_combined_edge_bps=self.min_combined_edge_bps,
            min_confluence=self.min_confluence,
        )
        if not verdicts:
            return (NoAction(reason=f"no_confluence:trigger={trigger}"),)

        decisions: list[StrategyDecision] = []
        for verdict in verdicts:
            decisions.extend(self._verdict_to_decisions(verdict))
        if not decisions:
            return (NoAction(reason=f"verdicts_hold:trigger={trigger}"),)
        return tuple(decisions)

    def _verdict_to_decisions(
        self, verdict: EnsembleVerdict
    ) -> Sequence[StrategyDecision]:
        market_state = self.tracked_markets.get(verdict.instrument_id.market_id)
        if market_state is None or market_state.mid is None:
            return ()

        breakdown = "|".join(
            f"{src}={edge:+.1f}" for src, edge in sorted(verdict.per_source_edge_bps.items())
        )
        alert = Alert(
            severity=AlertSeverity.INFO,
            message=(
                f"crypto_ensemble verdict={_verdict_kind(verdict)} "
                f"net={verdict.net_edge_bps:+.1f}bps {breakdown}"
            ),
            tags={
                "market_id": verdict.instrument_id.market_id,
                "net_edge_bps": f"{verdict.net_edge_bps:+.1f}",
                "sources": ",".join(verdict.contributing_sources),
            },
        )
        if verdict.side is None:
            return (
                alert,
                NoAction(reason=f"hold:{breakdown}"),
            )

        if (
            market_state.spread_bps is not None
            and market_state.spread_bps > self.max_spread_bps
        ):
            return (
                alert,
                NoAction(
                    reason=f"spread_too_wide:{market_state.spread_bps:.0f}bps"
                ),
            )

        leg_price = (
            market_state.mid
            if verdict.side is OutcomeSide.YES
            else Decimal("1") - market_state.mid
        )
        place = PlaceOrder(
            client_order_id=ClientOrderId(uuid4().hex),
            instrument_id=verdict.instrument_id,
            outcome_side=verdict.side,
            order_side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            time_in_force=TimeInForce.GTC,
            quantity=self.size,
            price=_clip_probability(leg_price),
            reason=f"ensemble:{breakdown}",
            expected_edge_bps=abs(verdict.net_edge_bps),
            priority=ExecutionPriority(tier=LatencyTier.STANDARD),
        )
        return (alert, place)


# ----------------------------- helpers -----------------------------


def _verdict_kind(verdict: EnsembleVerdict) -> str:
    if verdict.side is None:
        return "HOLD"
    return "BUY_YES" if verdict.side is OutcomeSide.YES else "BUY_NO"


class _MarketState:
    """Lightweight per-market mid + spread cache used to size orders."""

    __slots__ = ("mid", "spread_bps")

    def __init__(self) -> None:
        self.mid: Decimal | None = None
        self.spread_bps: Decimal | None = None


def _parse_enabled_sources(raw: str) -> tuple[str, ...]:
    sources = tuple(s.strip() for s in raw.split(",") if s.strip())
    unknown = tuple(s for s in sources if s not in _ALL_SOURCES)
    if unknown:
        raise ValueError(f"unknown enabled_sources: {unknown}")
    return sources or _ALL_SOURCES


def _parse_weights(raw: str) -> Mapping[str, Decimal]:
    out: dict[str, Decimal] = {}
    for item in raw.split(";"):
        if not item.strip():
            continue
        parts = [p.strip() for p in item.split(":")]
        if len(parts) != 2 or parts[0] not in _ALL_SOURCES:
            raise ValueError("weights must be source:weight pairs separated by ;")
        out[parts[0]] = Decimal(parts[1])
    return out


def _parse_bracket_intervals(raw: str) -> Mapping[str, tuple[Decimal, Decimal | None]]:
    """Parse ``market_id:lower:upper`` into ``{market_id: (lower, upper)}``.

    ``inf``/``-inf`` map to ``None`` on the appropriate side. The
    bracket-vol source treats ``lower=0`` for the low-tail entry and
    ``upper=None`` for the high-tail entry.
    """

    intervals: dict[str, tuple[Decimal, Decimal | None]] = {}
    for item in raw.split(";"):
        if not item.strip():
            continue
        parts = [p.strip() for p in item.split(":")]
        if len(parts) != 3 or not parts[0]:
            raise ValueError(
                "bracket_market_ids entries must be market_id:lower:upper"
            )
        market_id, lower_raw, upper_raw = parts
        lower = (
            Decimal("0")
            if lower_raw.lower() in ("-inf", "neg_inf", "")
            else Decimal(lower_raw)
        )
        upper = (
            None
            if upper_raw.lower() in ("inf", "pos_inf", "")
            else Decimal(upper_raw)
        )
        intervals[market_id] = (lower, upper)
    return intervals


def _parse_bracket_ids(raw: str) -> tuple[str, ...]:
    """Pull just the ``market_id`` from each ``market_id:lower:upper`` entry."""

    ids: list[str] = []
    for item in raw.split(";"):
        if not item.strip():
            continue
        parts = [p.strip() for p in item.split(":")]
        if not parts or not parts[0]:
            raise ValueError("bracket_market_ids entries must have non-empty market_id")
        ids.append(parts[0])
    return tuple(ids)


def _parse_strike_map(raw: str) -> dict[str, Decimal]:
    out: dict[str, Decimal] = {}
    for item in raw.split(";"):
        if not item.strip():
            continue
        parts = [p.strip() for p in item.split(":")]
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise ValueError(
                "strike maps must be semicolon-separated market_id:strike entries"
            )
        out[parts[0]] = Decimal(parts[1])
    return out


def _clip_probability(value: Decimal) -> Decimal:
    return max(Decimal("0.01"), min(Decimal("0.99"), value))


def _venue(value: str) -> Venue:
    try:
        return Venue(value)
    except ValueError as exc:
        raise ValueError(f"unknown venue: {value}") from exc


@register("crypto_signal_ensemble")
def factory(spec: StrategySpec) -> CryptoSignalEnsembleStrategy:
    return CryptoSignalEnsembleStrategy(spec)
