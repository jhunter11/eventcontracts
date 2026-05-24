"""Reference deterministic feature builders.

Two contracts live here:

1. :class:`DeterministicFeatureBuilder` — concrete base class that gives
   subclasses a strict, leakage-resistant scaffold: the public ``warmup`` and
   ``update`` methods enforce monotonic event-time, hold per-instrument
   state, and run schema sanity checks on every emitted vector. Subclasses
   override two pure-functional hooks (``_event_time`` and ``_compute``).

2. :class:`RollingMidVwapImbalanceBuilder` — a small reference builder that
   computes three features from quote, trade, and order-book events:

   * ``mid_<n>s_ewm``: exponentially-weighted moving mid price over
     ``window_seconds`` of half-life.
   * ``vwap_<n>s``: rolling trade VWAP over the last ``window_seconds``.
   * ``imbalance_l1``: latest L1 (bid_qty - ask_qty) / (bid_qty + ask_qty).

   The implementation is intentionally small so it can serve as the pattern
   future builders copy. It uses ``Decimal`` for price arithmetic and casts
   to float only at the FeatureVector boundary.

Leakage rules enforced (see also ``tests/test_no_leakage.py``):

* The builder may only consume events whose event-time is ``<=`` the
  reported ``as_of`` timestamp of the most recently emitted vector.
* The builder may not look at future events or settlement labels.
* Two builders fed the same sequence of events in the same order must
  return byte-equal vectors.
"""

from __future__ import annotations

from abc import abstractmethod
from collections import deque
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from eventcontracts.domain.events import (
    LifecycleEvent,
    NormalizedEvent,
    OrderBookEvent,
    OwnFillEvent,
    OwnOrderRejectEvent,
    OwnOrderUpdateEvent,
    QuoteEvent,
    SettlementResolvedEvent,
    TimerEvent,
    TradeEvent,
)
from eventcontracts.domain.features import FeatureVector
from eventcontracts.domain.ids import FeatureSchemaId
from eventcontracts.domain.models import InstrumentId
from eventcontracts.features.pipeline import (
    FeatureBuilder,
    FeatureDefinition,
    FeatureDType,
    FeatureSchema,
    OnlineFeatureState,
)


def event_time(event: NormalizedEvent) -> datetime | None:
    """Best-effort event-time for any normalized variant.

    Returns ``None`` for variants that don't carry a clock (own-event
    rejects can land at any time; callers should drop those before feeding
    a deterministic builder).
    """

    if isinstance(event, QuoteEvent):
        return event.quote.exchange_ts or event.quote.received_at
    if isinstance(event, TradeEvent):
        return event.trade.exchange_ts or event.trade.received_at
    if isinstance(event, OrderBookEvent):
        return event.book.exchange_ts or event.book.received_at
    if isinstance(event, LifecycleEvent):
        return event.lifecycle.exchange_ts or event.lifecycle.received_at
    if isinstance(event, SettlementResolvedEvent):
        return event.settlement.settled_at
    if isinstance(event, TimerEvent):
        return event.timestamp
    if isinstance(event, OwnFillEvent):
        return event.fill.exchange_ts or event.fill.filled_at
    if isinstance(event, OwnOrderUpdateEvent):
        return event.order.updated_at
    if isinstance(event, OwnOrderRejectEvent):
        return event.reject.rejected_at
    return getattr(event, "received_at", None)


class FeatureLeakageError(RuntimeError):
    """Raised when a builder is asked to consume an event from the past.

    A future-leaking pipeline would silently re-order events; this error
    fails loud so the bug is caught before any backtest claim is made.
    """


class DeterministicFeatureBuilder(FeatureBuilder):
    """Base class enforcing monotonic event-time and schema sanity.

    Subclasses implement two hooks:

    * :meth:`_compute` — given a normalized event and the current state,
      return the new state and (optionally) a new ``FeatureVector``.
    * :meth:`schema` — declare the feature schema, including ordering.

    The base class:

    * Validates that incoming event-times are non-decreasing per call to
      ``update`` and ``warmup``; raises :class:`FeatureLeakageError` on
      regression.
    * Asserts that every emitted vector's ``timestamp`` matches the
      consumed event's time (or earlier).
    * Verifies that emitted ``values`` match the declared schema's names
      and order.
    """

    def warmup(self, events: Sequence[NormalizedEvent]) -> OnlineFeatureState:
        state = self._initial_state()
        for evt in events:
            state = self.update(state, evt)
        return state

    def update(
        self, state: OnlineFeatureState, event: NormalizedEvent
    ) -> OnlineFeatureState:
        ts = event_time(event)
        if ts is None:
            return state
        last_seen = state.as_of
        if ts < last_seen:
            raise FeatureLeakageError(
                f"out-of-order event for {type(self).__name__}: "
                f"event_time={ts.isoformat()} < last_as_of={last_seen.isoformat()}"
            )
        new_state, vector = self._compute(state, event, ts)
        if vector is not None:
            self._validate_vector(vector, observed_at=ts)
        return new_state

    def build_offline(
        self, events: Sequence[NormalizedEvent]
    ) -> Sequence[FeatureVector]:
        state = self._initial_state()
        emitted: list[FeatureVector] = []
        for evt in events:
            ts = event_time(evt)
            if ts is None:
                continue
            if ts < state.as_of:
                raise FeatureLeakageError(
                    f"build_offline received out-of-order event: "
                    f"{ts.isoformat()} < {state.as_of.isoformat()}"
                )
            state, vector = self._compute(state, evt, ts)
            if vector is not None:
                self._validate_vector(vector, observed_at=ts)
                emitted.append(vector)
        return tuple(emitted)

    @abstractmethod
    def _initial_state(self) -> OnlineFeatureState:
        """Return the empty state used for offline builds and warmup."""

    @abstractmethod
    def _compute(
        self,
        state: OnlineFeatureState,
        event: NormalizedEvent,
        ts: datetime,
    ) -> tuple[OnlineFeatureState, FeatureVector | None]:
        """Advance state and optionally emit a vector for this event."""

    def _validate_vector(self, vector: FeatureVector, *, observed_at: datetime) -> None:
        if vector.timestamp > observed_at:
            raise FeatureLeakageError(
                f"emitted vector timestamp {vector.timestamp} > observed event "
                f"time {observed_at}; deterministic builders must not look ahead"
            )
        expected = tuple(f.name for f in self.schema().features)
        actual = tuple(name for name, _value in vector.values)
        if actual != expected:
            raise ValueError(
                f"feature vector ordering mismatch: schema={expected}, vector={actual}"
            )


@dataclass
class _RollingState:
    """Mutable per-instrument state for the reference builder."""

    instrument_id: InstrumentId | None
    as_of: datetime
    # Last seen ewma mid (price).
    ewma_mid: Decimal | None = None
    # Trade deque of (timestamp, price, qty) for VWAP window.
    trade_window: deque[tuple[datetime, Decimal, Decimal]] = field(default_factory=deque)
    last_imbalance: Decimal | None = None


class RollingMidVwapImbalanceBuilder(DeterministicFeatureBuilder):
    """Reference builder: ewma mid + rolling-window VWAP + L1 imbalance."""

    def __init__(
        self,
        *,
        schema_id: FeatureSchemaId,
        schema_version: str = "v1",
        window_seconds: int = 30,
        ewma_half_life_seconds: int = 5,
        instrument_id: InstrumentId | None = None,
    ) -> None:
        if window_seconds <= 0:
            raise ValueError("window_seconds must be > 0")
        if ewma_half_life_seconds <= 0:
            raise ValueError("ewma_half_life_seconds must be > 0")
        self._schema_id = schema_id
        self._schema_version = schema_version
        self.window = timedelta(seconds=window_seconds)
        self.half_life = timedelta(seconds=ewma_half_life_seconds)
        self._instrument_filter = instrument_id
        self._states: dict[InstrumentId | None, _RollingState] = {}

    def schema(self) -> FeatureSchema:
        return FeatureSchema(
            schema_id=self._schema_id,
            schema_version=self._schema_version,
            features=(
                FeatureDefinition(
                    name="mid_ewma",
                    dtype=FeatureDType.FLOAT64,
                    nullable=True,
                    description=f"EWMA mid (half-life {self.half_life})",
                ),
                FeatureDefinition(
                    name="trade_vwap",
                    dtype=FeatureDType.FLOAT64,
                    nullable=True,
                    description=f"Rolling-window VWAP over {self.window}",
                ),
                FeatureDefinition(
                    name="imbalance_l1",
                    dtype=FeatureDType.FLOAT64,
                    nullable=True,
                    description="L1 (bid_qty - ask_qty) / (bid_qty + ask_qty)",
                ),
            ),
        )

    def _initial_state(self) -> OnlineFeatureState:
        # Use min datetime safely with UTC awareness.
        from datetime import UTC

        return OnlineFeatureState(
            instrument_id=self._instrument_filter,
            as_of=datetime(1970, 1, 1, tzinfo=UTC),
        )

    def _compute(
        self,
        state: OnlineFeatureState,
        event: NormalizedEvent,
        ts: datetime,
    ) -> tuple[OnlineFeatureState, FeatureVector | None]:
        instrument = _instrument_of(event)
        if self._instrument_filter is not None and instrument != self._instrument_filter:
            # Still advance as_of so leakage checks remain monotonic.
            return (
                OnlineFeatureState(
                    instrument_id=state.instrument_id,
                    as_of=ts,
                    last_event=event,
                ),
                None,
            )
        per = self._states.setdefault(
            instrument,
            _RollingState(instrument_id=instrument, as_of=ts),
        )
        per.as_of = ts

        if isinstance(event, QuoteEvent):
            quote = event.quote
            mid = _mid_from_quote(quote.bid, quote.ask)
            if mid is not None:
                per.ewma_mid = _ewma_step(per.ewma_mid, mid, per.as_of, self.half_life)
        elif isinstance(event, OrderBookEvent):
            book = event.book
            top_bid = book.yes_bids[0] if book.yes_bids else None
            top_ask = book.yes_asks[0] if book.yes_asks else None
            mid = _mid_from_quote(top_bid, top_ask)
            if mid is not None:
                per.ewma_mid = _ewma_step(per.ewma_mid, mid, per.as_of, self.half_life)
            if top_bid is not None and top_ask is not None:
                total = top_bid.quantity + top_ask.quantity
                if total > 0:
                    per.last_imbalance = (
                        (top_bid.quantity - top_ask.quantity) / total
                    )
        elif isinstance(event, TradeEvent):
            trade = event.trade
            per.trade_window.append((ts, trade.price, trade.quantity))
            cutoff = ts - self.window
            while per.trade_window and per.trade_window[0][0] < cutoff:
                per.trade_window.popleft()

        vector = self._emit_vector(per)
        new_state = OnlineFeatureState(
            instrument_id=per.instrument_id,
            as_of=per.as_of,
            last_event=event,
            vector=vector,
        )
        return new_state, vector

    def _emit_vector(self, per: _RollingState) -> FeatureVector | None:
        mid_value = float(per.ewma_mid) if per.ewma_mid is not None else 0.0
        vwap_value = float(_vwap(per.trade_window))
        imbalance_value = (
            float(per.last_imbalance) if per.last_imbalance is not None else 0.0
        )
        return FeatureVector(
            schema_id=self._schema_id,
            schema_version=self._schema_version,
            instrument_id=per.instrument_id,
            timestamp=per.as_of,
            values=(
                ("mid_ewma", mid_value),
                ("trade_vwap", vwap_value),
                ("imbalance_l1", imbalance_value),
            ),
        )


def _instrument_of(event: NormalizedEvent) -> InstrumentId | None:
    if isinstance(event, QuoteEvent):
        return event.quote.instrument_id
    if isinstance(event, TradeEvent):
        return event.trade.instrument_id
    if isinstance(event, OrderBookEvent):
        return event.book.instrument_id
    if isinstance(event, LifecycleEvent):
        return event.lifecycle.instrument_id
    if isinstance(event, SettlementResolvedEvent):
        return event.settlement.instrument_id
    if isinstance(event, OwnFillEvent):
        return event.fill.instrument_id
    if isinstance(event, OwnOrderUpdateEvent):
        return event.order.instrument_id
    return None


def _mid_from_quote(
    bid: object | None, ask: object | None
) -> Decimal | None:
    bid_price = getattr(bid, "price", None) if bid is not None else None
    ask_price = getattr(ask, "price", None) if ask is not None else None
    if isinstance(bid_price, Decimal) and isinstance(ask_price, Decimal):
        return (bid_price + ask_price) / Decimal("2")
    if isinstance(bid_price, Decimal):
        return bid_price
    if isinstance(ask_price, Decimal):
        return ask_price
    return None


def _ewma_step(
    previous: Decimal | None,
    sample: Decimal,
    now: datetime,
    half_life: timedelta,
) -> Decimal:
    """Single EWMA update; first sample seeds the average."""

    if previous is None:
        return sample
    # Use a fixed-step weight to keep the computation deterministic without
    # relying on the inter-event interval; a more sophisticated builder
    # would weight by elapsed time. The fixed weight here is the standard
    # EWMA alpha derived from one half-life step.
    alpha = Decimal("0.5") ** (Decimal("1") / Decimal(max(1, half_life.seconds)))
    return alpha * previous + (Decimal("1") - alpha) * sample


def _vwap(trades: Iterable[tuple[datetime, Decimal, Decimal]]) -> Decimal:
    notional = Decimal("0")
    quantity = Decimal("0")
    for _ts, price, qty in trades:
        notional += price * qty
        quantity += qty
    if quantity == 0:
        return Decimal("0")
    return notional / quantity
