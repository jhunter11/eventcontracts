"""Label construction primitives.

Labels are the training targets paired with each `FeatureVector`. Each
labeler is a deterministic function of the vector's instrument + as_of
timestamp + the events whose event-time falls in
``(as_of, as_of + horizon_seconds]``. The dataset builder ([dataset.py])
buffers per-vector future windows and asks the labeler to produce a
label once enough wall-clock has elapsed.

Reference labelers shipped here cover three of the documented label
shapes from `docs/ml-strategy-researcher-guide.md`:

* :class:`NextMidChangeBpsLabeler` — continuous bps change.
* :class:`BinaryProfitableAfterFeesLabeler` — binary classifier target.
* :class:`SettlementProbabilityLabeler` — held-to-expiry target.

Adding a new labeler is a single class implementing the :class:`Labeler`
protocol; the dataset builder doesn't need to change.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Protocol, runtime_checkable

from eventcontracts.domain.events import (
    NormalizedEvent,
    OrderBookEvent,
    QuoteEvent,
    SettlementResolvedEvent,
    TradeEvent,
)
from eventcontracts.domain.fees import FeeModel, FillContext
from eventcontracts.domain.models import InstrumentId, OutcomeSide


@runtime_checkable
class Labeler(Protocol):
    """Compute a target value for one feature vector.

    Implementations must be pure functions of their inputs — the same
    `(instrument_id, as_of, future_events)` triple must always yield the
    same label. The dataset builder relies on that determinism for replay
    and for the no-leakage guarantee.
    """

    @property
    def name(self) -> str:
        ...

    @property
    def horizon_seconds(self) -> int:
        ...

    def label(
        self,
        instrument_id: InstrumentId | None,
        as_of: datetime,
        as_of_mid: Decimal | None,
        future_events: Sequence[NormalizedEvent],
    ) -> float | None:
        """Return the label, or `None` if the example must be censored."""


def _first_mid_after(
    events: Iterable[NormalizedEvent],
    threshold: datetime,
    instrument_id: InstrumentId | None,
) -> Decimal | None:
    """Find the earliest two-sided mid price at or after `threshold`."""

    for evt in events:
        ts = _event_ts(evt)
        if ts is None or ts < threshold:
            continue
        if instrument_id is not None and _instrument_of(evt) != instrument_id:
            continue
        if isinstance(evt, QuoteEvent):
            q = evt.quote
            if q.bid is not None and q.ask is not None:
                return (q.bid.price + q.ask.price) / Decimal("2")
        elif isinstance(evt, OrderBookEvent):
            book = evt.book
            top_bid = book.yes_bids[0] if book.yes_bids else None
            top_ask = book.yes_asks[0] if book.yes_asks else None
            if top_bid is not None and top_ask is not None:
                return (top_bid.price + top_ask.price) / Decimal("2")
    return None


def _event_ts(event: NormalizedEvent) -> datetime | None:
    if isinstance(event, QuoteEvent):
        return event.quote.exchange_ts or event.quote.received_at
    if isinstance(event, TradeEvent):
        return event.trade.exchange_ts or event.trade.received_at
    if isinstance(event, OrderBookEvent):
        return event.book.exchange_ts or event.book.received_at
    if isinstance(event, SettlementResolvedEvent):
        return event.settlement.settled_at
    return getattr(event, "received_at", None)


def _instrument_of(event: NormalizedEvent) -> InstrumentId | None:
    if isinstance(event, QuoteEvent):
        return event.quote.instrument_id
    if isinstance(event, TradeEvent):
        return event.trade.instrument_id
    if isinstance(event, OrderBookEvent):
        return event.book.instrument_id
    if isinstance(event, SettlementResolvedEvent):
        return event.settlement.instrument_id
    return None


@dataclass(frozen=True)
class NextMidChangeBpsLabeler:
    """Label = ``(future_mid - as_of_mid) / as_of_mid * 1e4`` in bps.

    Censors when neither a future mid nor an as_of mid is available.
    """

    horizon_seconds_value: int
    name_value: str = "next_mid_change_bps"

    @property
    def name(self) -> str:
        return self.name_value

    @property
    def horizon_seconds(self) -> int:
        return self.horizon_seconds_value

    def label(
        self,
        instrument_id: InstrumentId | None,
        as_of: datetime,
        as_of_mid: Decimal | None,
        future_events: Sequence[NormalizedEvent],
    ) -> float | None:
        if as_of_mid is None or as_of_mid <= 0:
            return None
        threshold = as_of + timedelta(seconds=self.horizon_seconds_value)
        future_mid = _first_mid_after(future_events, threshold, instrument_id)
        if future_mid is None:
            return None
        return float((future_mid - as_of_mid) / as_of_mid * Decimal("10000"))


@dataclass(frozen=True)
class BinaryProfitableAfterFeesLabeler:
    """1.0 if a modeled BUY would profit after fees over the horizon, else 0.0.

    Models a hypothetical taker BUY of one contract at ``as_of_mid``,
    marked out against the first mid at the end of the horizon, net of
    `fee_model` estimates.
    """

    horizon_seconds_value: int
    fee_model: FeeModel
    side: OutcomeSide = OutcomeSide.YES
    name_value: str = "binary_profitable_after_fees"

    @property
    def name(self) -> str:
        return self.name_value

    @property
    def horizon_seconds(self) -> int:
        return self.horizon_seconds_value

    def label(
        self,
        instrument_id: InstrumentId | None,
        as_of: datetime,
        as_of_mid: Decimal | None,
        future_events: Sequence[NormalizedEvent],
    ) -> float | None:
        if as_of_mid is None or as_of_mid <= 0:
            return None
        threshold = as_of + timedelta(seconds=self.horizon_seconds_value)
        future_mid = _first_mid_after(future_events, threshold, instrument_id)
        if future_mid is None:
            return None
        fee = self.fee_model.estimate(
            FillContext(
                instrument_id=instrument_id
                or InstrumentId(venue=_DEFAULT_VENUE, market_id="UNKNOWN"),
                side=self.side,
                price=as_of_mid,
                quantity=Decimal("1"),
                liquidity="taker",
            )
        )
        pnl = future_mid - as_of_mid - fee.amount
        return 1.0 if pnl > Decimal("0") else 0.0


@dataclass(frozen=True)
class SettlementProbabilityLabeler:
    """1.0 if instrument settles YES in the horizon window, 0.0 if NO, else None.

    For event contracts this is the "did the contract pay out" label.
    Vectors with no settlement event inside the horizon are censored.
    """

    horizon_seconds_value: int
    name_value: str = "settlement_probability"

    @property
    def name(self) -> str:
        return self.name_value

    @property
    def horizon_seconds(self) -> int:
        return self.horizon_seconds_value

    def label(
        self,
        instrument_id: InstrumentId | None,
        as_of: datetime,
        as_of_mid: Decimal | None,
        future_events: Sequence[NormalizedEvent],
    ) -> float | None:
        for evt in future_events:
            if not isinstance(evt, SettlementResolvedEvent):
                continue
            if (
                instrument_id is not None
                and evt.settlement.instrument_id != instrument_id
            ):
                continue
            resolved = evt.settlement.resolved_side
            if resolved is None:
                return None
            return 1.0 if resolved is OutcomeSide.YES else 0.0
        return None


# Lazy-imported to avoid a circular reference with domain.models when this
# module is imported during the framework's __init__ chain.
from eventcontracts.domain.models import Venue as _DEFAULT_VENUE_ENUM  # noqa: E402

_DEFAULT_VENUE = _DEFAULT_VENUE_ENUM.KALSHI
