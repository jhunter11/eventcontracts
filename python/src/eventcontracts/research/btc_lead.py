"""BTC constituent-lead measurement and paper-only timing gates.

This module intentionally contains no venue credentials and no order path. It is
the pure core behind a read-only recorder: parse public exchange prints, build a
weighted synthetic BTC index, measure lead versus an official/reference print
when one is available, and decide whether a paper candidate even clears the
latency gate before looking at model-vs-market edge.
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from eventcontracts.domain.validation import require_aware_datetime, require_non_empty
from eventcontracts.research.btc_settlement import forecast_at
from eventcontracts.research.timing import age_ms


@dataclass(frozen=True)
class CryptoTick:
    """One public exchange/reference BTC price update."""

    venue: str
    symbol: str
    price: float
    received_at: datetime
    exchange_ts: datetime | None = None
    sequence: str | None = None

    def __post_init__(self) -> None:
        require_non_empty(self.venue, "venue")
        require_non_empty(self.symbol, "symbol")
        if not math.isfinite(self.price) or self.price <= 0:
            raise ValueError("price must be finite and positive")
        require_aware_datetime(self.received_at, "received_at")
        if self.exchange_ts is not None:
            require_aware_datetime(self.exchange_ts, "exchange_ts")

    @property
    def tick_age_ms(self) -> float | None:
        return age_ms(self.exchange_ts, self.received_at)


@dataclass(frozen=True)
class SyntheticIndexComponent:
    """One venue contribution to a synthetic index snapshot."""

    venue: str
    symbol: str
    price: float
    normalized_weight: float
    exchange_ts: datetime | None
    received_at: datetime
    age_ms: float | None


@dataclass(frozen=True)
class SyntheticIndexSnapshot:
    """Weighted synthetic BTC index built from reachable constituent feeds."""

    symbol: str
    price: float
    as_of: datetime
    components: tuple[SyntheticIndexComponent, ...]
    schema_version: str = "btc-synthetic-index-v1"

    def __post_init__(self) -> None:
        require_non_empty(self.symbol, "symbol")
        if not math.isfinite(self.price) or self.price <= 0:
            raise ValueError("price must be finite and positive")
        require_aware_datetime(self.as_of, "as_of")
        if not self.components:
            raise ValueError("components must not be empty")

    @property
    def max_component_age_ms(self) -> float | None:
        ages = [component.age_ms for component in self.components if component.age_ms is not None]
        return max(ages) if ages else None

    @property
    def venue_count(self) -> int:
        return len({component.venue for component in self.components})


@dataclass(frozen=True)
class LeadMeasurement:
    """Measured synthetic-vs-reference timing lead.

    ``lead_ms`` is positive when the synthetic snapshot was available before the
    reference print timestamp. It is not an execution edge by itself; it must be
    larger than residual submit/quote latency and survive spread + fees.
    """

    synthetic: SyntheticIndexSnapshot
    reference: CryptoTick
    lead_ms: float | None
    reason: str


@dataclass(frozen=True)
class Btc15mTimingDecision:
    """Paper-only decision gate for a BTC 15-minute settlement candidate."""

    market_id: str
    as_of: datetime
    strike: float
    seconds_to_expiry: float
    fair_yes: float
    side: str
    executable_price: float | None
    raw_edge: float | None
    fee: float | None
    net_edge: float | None
    measured_lead_ms: float | None
    residual_latency_ms: float
    candidate: bool
    reason: str
    schema_version: str = "btc15m-timing-decision-v1"

    def __post_init__(self) -> None:
        require_non_empty(self.market_id, "market_id")
        require_aware_datetime(self.as_of, "as_of")
        if not math.isfinite(self.strike) or self.strike <= 0:
            raise ValueError("strike must be finite and positive")
        if self.seconds_to_expiry < 0:
            raise ValueError("seconds_to_expiry must be non-negative")
        if not 0.0 <= self.fair_yes <= 1.0:
            raise ValueError("fair_yes must be in [0, 1]")
        if self.side not in {"YES", "NO", "NONE"}:
            raise ValueError("side must be YES, NO, or NONE")
        if self.residual_latency_ms < 0:
            raise ValueError("residual_latency_ms must be non-negative")
        require_non_empty(self.reason, "reason")


class SyntheticIndexState:
    """Maintain latest public ticks and produce weighted synthetic snapshots."""

    def __init__(self, weights: Mapping[str, float], *, symbol: str = "BTC-USD") -> None:
        if not weights:
            raise ValueError("weights must not be empty")
        clean: dict[str, float] = {}
        for venue, weight in weights.items():
            require_non_empty(venue, "venue")
            if not math.isfinite(weight) or weight <= 0:
                raise ValueError("weights must be finite and positive")
            clean[venue] = float(weight)
        self._weights = clean
        self._symbol = symbol
        self._ticks: dict[str, CryptoTick] = {}

    def update(self, tick: CryptoTick) -> None:
        if tick.venue not in self._weights:
            return
        self._ticks[tick.venue] = tick

    def snapshot(
        self,
        now: datetime,
        *,
        max_component_age_ms: float | None = 1_000.0,
        min_venues: int = 1,
    ) -> SyntheticIndexSnapshot | None:
        require_aware_datetime(now, "now")
        included: list[tuple[CryptoTick, float]] = []
        for venue, tick in self._ticks.items():
            component_age = age_ms(tick.exchange_ts, now) if tick.exchange_ts is not None else None
            if max_component_age_ms is not None and component_age is not None and component_age > max_component_age_ms:
                continue
            included.append((tick, self._weights[venue]))
        if len(included) < min_venues:
            return None
        total_weight = sum(weight for _, weight in included)
        components = tuple(
            SyntheticIndexComponent(
                venue=tick.venue,
                symbol=tick.symbol,
                price=tick.price,
                normalized_weight=weight / total_weight,
                exchange_ts=tick.exchange_ts,
                received_at=tick.received_at,
                age_ms=age_ms(tick.exchange_ts, now) if tick.exchange_ts is not None else None,
            )
            for tick, weight in included
        )
        price = sum(component.price * component.normalized_weight for component in components)
        as_of = max(component.received_at for component in components)
        return SyntheticIndexSnapshot(symbol=self._symbol, price=price, as_of=as_of, components=components)


class RollingRealizedVol:
    """Windowed realized volatility estimate from public tick prices."""

    def __init__(self, *, max_points: int = 300) -> None:
        if max_points < 3:
            raise ValueError("max_points must be >= 3")
        self._points: deque[tuple[datetime, float]] = deque(maxlen=max_points)

    def update(self, tick: CryptoTick | SyntheticIndexSnapshot) -> None:
        timestamp = tick.as_of if isinstance(tick, SyntheticIndexSnapshot) else tick.received_at
        price = tick.price
        if self._points and timestamp <= self._points[-1][0]:
            return
        self._points.append((timestamp, price))

    def sigma_per_second(self) -> float | None:
        if len(self._points) < 3:
            return None
        scaled_returns: list[float] = []
        prev_ts, prev_price = self._points[0]
        for ts, price in list(self._points)[1:]:
            dt = (ts - prev_ts).total_seconds()
            if dt > 0 and prev_price > 0 and price > 0:
                scaled_returns.append((math.log(price / prev_price) * prev_price) / math.sqrt(dt))
            prev_ts, prev_price = ts, price
        if len(scaled_returns) < 2:
            return None
        mean = sum(scaled_returns) / len(scaled_returns)
        variance = sum((ret - mean) ** 2 for ret in scaled_returns) / (len(scaled_returns) - 1)
        return math.sqrt(max(variance, 0.0))


def parse_coinbase_ticker(payload: Mapping[str, Any], *, received_at: datetime) -> CryptoTick:
    """Parse a Coinbase public ticker/trade payload."""

    price = _float_field(payload, "price")
    exchange_ts = _parse_ts(payload.get("time"))
    sequence = payload.get("trade_id") or payload.get("sequence")
    return CryptoTick(
        venue="coinbase",
        symbol=str(payload.get("product_id") or "BTC-USD"),
        price=price,
        exchange_ts=exchange_ts,
        received_at=received_at,
        sequence=str(sequence) if sequence is not None else None,
    )


def parse_kraken_ticker(payload: Mapping[str, Any], *, received_at: datetime, pair: str = "XBT/USD") -> CryptoTick:
    """Parse a Kraken public ticker snapshot.

    Kraken's ticker payload usually has no exchange timestamp, so ``exchange_ts``
    is left ``None`` and lead measurement must be based on receipt time unless a
    richer trade feed is supplied.
    """

    result = payload.get("result")
    if not isinstance(result, Mapping) or not result:
        raise ValueError("Kraken payload missing result")
    first = next(iter(result.values()))
    if not isinstance(first, Mapping):
        raise ValueError("Kraken ticker result is malformed")
    close = first.get("c")
    if not isinstance(close, list | tuple) or not close:
        raise ValueError("Kraken ticker missing close price")
    return CryptoTick(
        venue="kraken",
        symbol=pair,
        price=float(close[0]),
        exchange_ts=None,
        received_at=received_at,
    )


def parse_cfb_rti_stats(payload: Mapping[str, Any], *, received_at: datetime) -> CryptoTick:
    """Parse an official CF Benchmarks ``rti_stats`` WebSocket update."""

    if payload.get("type") != "rti_stats":
        raise ValueError("CFB payload is not an rti_stats update")
    stream_id = str(payload.get("id") or "")
    require_non_empty(stream_id, "id")
    price = _float_field(payload, "value")
    raw_time = payload.get("time")
    if not isinstance(raw_time, int | float | str):
        raise ValueError("CFB payload field 'time' must be milliseconds since epoch")
    try:
        time_ms = float(raw_time)
    except ValueError as exc:
        raise ValueError("CFB payload field 'time' must be milliseconds since epoch") from exc
    exchange_ts = datetime.fromtimestamp(time_ms / 1000.0, tz=UTC)
    return CryptoTick(
        venue="cfb-rti",
        symbol=stream_id,
        price=price,
        exchange_ts=exchange_ts,
        received_at=received_at,
        sequence=str(raw_time),
    )


def measure_reference_lead(synthetic: SyntheticIndexSnapshot, reference: CryptoTick) -> LeadMeasurement:
    """Measure whether a synthetic snapshot leads an official/reference print."""

    if reference.exchange_ts is None:
        return LeadMeasurement(
            synthetic=synthetic,
            reference=reference,
            lead_ms=None,
            reason="reference_missing_exchange_timestamp",
        )
    return LeadMeasurement(
        synthetic=synthetic,
        reference=reference,
        lead_ms=(reference.exchange_ts - synthetic.as_of).total_seconds() * 1000.0,
        reason="measured",
    )


def evaluate_btc15m_timing_candidate(
    *,
    market_id: str,
    synthetic: SyntheticIndexSnapshot,
    strike: float,
    seconds_to_expiry: float,
    sigma_per_sec: float,
    yes_bid: Decimal | None,
    yes_ask: Decimal | None,
    measured_lead_ms: float | None,
    residual_latency_ms: float,
    min_net_edge: float = 0.02,
    fee_coeff: float = 0.07,
) -> Btc15mTimingDecision:
    """Paper-only executable-edge gate for the BTC settlement thesis."""

    fair_yes = forecast_at(seconds_to_expiry, synthetic.price, sigma_per_sec).prob_above(strike)
    lead_ok = measured_lead_ms is not None and measured_lead_ms > residual_latency_ms
    if yes_bid is None or yes_ask is None:
        return _no_candidate(
            market_id,
            synthetic.as_of,
            strike,
            seconds_to_expiry,
            fair_yes,
            measured_lead_ms,
            residual_latency_ms,
            "missing_two_sided_quote",
        )
    yes_bid_f = float(yes_bid)
    yes_ask_f = float(yes_ask)
    fee_yes = fee_coeff * yes_ask_f * (1.0 - yes_ask_f)
    yes_net = fair_yes - yes_ask_f - fee_yes
    no_price = 1.0 - yes_bid_f
    fair_no = 1.0 - fair_yes
    fee_no = fee_coeff * no_price * (1.0 - no_price)
    no_net = fair_no - no_price - fee_no
    if yes_net >= no_net:
        side, executable, raw_edge, fee, net_edge = "YES", yes_ask_f, fair_yes - yes_ask_f, fee_yes, yes_net
    else:
        side, executable, raw_edge, fee, net_edge = "NO", no_price, fair_no - no_price, fee_no, no_net
    if not lead_ok:
        reason = "lead_missing_or_below_residual_latency"
    elif net_edge < min_net_edge:
        reason = "net_edge_below_threshold"
    else:
        reason = "paper_candidate"
    return Btc15mTimingDecision(
        market_id=market_id,
        as_of=synthetic.as_of,
        strike=strike,
        seconds_to_expiry=seconds_to_expiry,
        fair_yes=fair_yes,
        side=side,
        executable_price=executable,
        raw_edge=raw_edge,
        fee=fee,
        net_edge=net_edge,
        measured_lead_ms=measured_lead_ms,
        residual_latency_ms=residual_latency_ms,
        candidate=lead_ok and net_edge >= min_net_edge,
        reason=reason,
    )


def _no_candidate(
    market_id: str,
    as_of: datetime,
    strike: float,
    seconds_to_expiry: float,
    fair_yes: float,
    measured_lead_ms: float | None,
    residual_latency_ms: float,
    reason: str,
) -> Btc15mTimingDecision:
    return Btc15mTimingDecision(
        market_id=market_id,
        as_of=as_of,
        strike=strike,
        seconds_to_expiry=seconds_to_expiry,
        fair_yes=fair_yes,
        side="NONE",
        executable_price=None,
        raw_edge=None,
        fee=None,
        net_edge=None,
        measured_lead_ms=measured_lead_ms,
        residual_latency_ms=residual_latency_ms,
        candidate=False,
        reason=reason,
    )


def _float_field(payload: Mapping[str, Any], field: str) -> float:
    raw = payload.get(field)
    if raw is None:
        raise ValueError(f"payload field {field!r} must be numeric")
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"payload field {field!r} must be numeric") from exc
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"payload field {field!r} must be finite and positive")
    return value


def _parse_ts(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, int | float):
        return datetime.fromtimestamp(float(value), tz=UTC)
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
