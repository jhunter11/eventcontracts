"""Build training examples from a stream of normalized events.

A `TrainingExampleBuilder` consumes the same event stream a backtest
sees, runs a `DeterministicFeatureBuilder`, holds a per-feature-vector
buffer of "future" events, and asks the `Labeler` for a label once
enough event-time has elapsed.

The output is an ordered list of `TrainingExample`s preserving
feature-vector timestamp order, which the trainer then splits
chronologically into train/validate (never shuffles).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from eventcontracts.domain.events import (
    NormalizedEvent,
    OrderBookEvent,
    QuoteEvent,
    TradeEvent,
)
from eventcontracts.domain.features import FeatureVector
from eventcontracts.domain.models import InstrumentId
from eventcontracts.features.builders import (
    DeterministicFeatureBuilder,
    event_time,
)
from eventcontracts.models.labelers import Labeler


@dataclass(frozen=True)
class TrainingExample:
    features: FeatureVector
    label: float


class TrainingExampleBuilder:
    """Stream-driven example construction with a per-vector future buffer."""

    def __init__(
        self,
        feature_builder: DeterministicFeatureBuilder,
        labeler: Labeler,
    ) -> None:
        self.feature_builder = feature_builder
        self.labeler = labeler
        self._fb_state = feature_builder.warmup(())
        # Each pending entry: (vector, as_of_mid, future_events_list).
        self._pending: list[tuple[FeatureVector, Decimal | None, list[NormalizedEvent]]] = []
        self._last_mid_by_instrument: dict[InstrumentId | None, Decimal] = {}

    def feed(self, event: NormalizedEvent) -> list[TrainingExample]:
        """Process one event; return any newly-completed examples."""

        self._update_mid_cache(event)
        # 1. Feed feature builder. If a vector is emitted, queue it.
        self._fb_state = self.feature_builder.update(self._fb_state, event)
        new_vector = self._fb_state.vector
        if new_vector is not None:
            as_of_mid = self._last_mid_by_instrument.get(new_vector.instrument_id)
            self._pending.append((new_vector, as_of_mid, []))

        # 2. Append this event to every pending vector's future buffer.
        for _vec, _mid, future_buffer in self._pending:
            future_buffer.append(event)

        # 3. Try to label any pending vectors whose horizon has elapsed.
        now = event_time(event)
        completed: list[TrainingExample] = []
        remaining: list[tuple[FeatureVector, Decimal | None, list[NormalizedEvent]]] = []
        horizon = timedelta(seconds=self.labeler.horizon_seconds)
        for vec, as_of_mid, future_buffer in self._pending:
            if now is None or (now - vec.timestamp) < horizon:
                remaining.append((vec, as_of_mid, future_buffer))
                continue
            label = self.labeler.label(
                instrument_id=vec.instrument_id,
                as_of=vec.timestamp,
                as_of_mid=as_of_mid,
                future_events=tuple(future_buffer),
            )
            if label is not None:
                completed.append(TrainingExample(features=vec, label=label))
        self._pending = remaining
        return completed

    def flush(self) -> list[TrainingExample]:
        """Emit examples for any vectors whose horizon has not closed.

        Called once the source stream ends. The labeler still has the
        chance to return a label from the partial future window, but in
        practice this usually censors.
        """

        completed: list[TrainingExample] = []
        for vec, as_of_mid, future_buffer in self._pending:
            label = self.labeler.label(
                instrument_id=vec.instrument_id,
                as_of=vec.timestamp,
                as_of_mid=as_of_mid,
                future_events=tuple(future_buffer),
            )
            if label is not None:
                completed.append(TrainingExample(features=vec, label=label))
        self._pending = []
        return completed

    def build(self, events: Iterable[NormalizedEvent]) -> list[TrainingExample]:
        out: list[TrainingExample] = []
        for evt in events:
            out.extend(self.feed(evt))
        out.extend(self.flush())
        out.sort(key=lambda ex: ex.features.timestamp)
        return out

    def _update_mid_cache(self, event: NormalizedEvent) -> None:
        """Track the most recent two-sided mid per instrument.

        Labelers that need an entry price (NextMidChange, BinaryProfitable)
        rely on this cache so the example builder doesn't force every
        feature builder to expose a mid feature.
        """

        if isinstance(event, QuoteEvent):
            q = event.quote
            if q.bid is not None and q.ask is not None:
                self._last_mid_by_instrument[q.instrument_id] = (
                    q.bid.price + q.ask.price
                ) / Decimal("2")
        elif isinstance(event, OrderBookEvent):
            book = event.book
            top_bid = book.yes_bids[0] if book.yes_bids else None
            top_ask = book.yes_asks[0] if book.yes_asks else None
            if top_bid is not None and top_ask is not None:
                self._last_mid_by_instrument[book.instrument_id] = (
                    top_bid.price + top_ask.price
                ) / Decimal("2")
        elif isinstance(event, TradeEvent):
            # Use the last trade price as a fallback mid if no quotes have
            # arrived yet. Conservative — trade prints lead a mid update by
            # design in the live capture path.
            if event.trade.instrument_id not in self._last_mid_by_instrument:
                self._last_mid_by_instrument[event.trade.instrument_id] = event.trade.price
