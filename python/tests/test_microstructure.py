"""Order-book microstructure feasibility analyzer."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

from eventcontracts.research.microstructure import (
    BookTop,
    TradePrint,
    depth_summary,
    feed_staleness_ms,
    quote_lifetimes_ms,
    reaction_lag,
    repricing_decomposition,
    summarize,
    top_from_orderbook_event,
)

BASE = datetime(2026, 6, 3, 12, 0, tzinfo=UTC)


def _at(secs: float, *, bid=0.50, bid_size=10.0, ask=0.52, ask_size=8.0, recv=None) -> BookTop:
    ts = BASE + timedelta(seconds=secs)
    return BookTop(ts=ts, bid=bid, bid_size=bid_size, ask=ask, ask_size=ask_size, recv_ts=recv)


def test_quote_lifetimes_measures_best_price_durations() -> None:
    tops = [
        _at(0.0, ask=0.50),
        _at(0.1, ask=0.50),  # no change
        _at(0.5, ask=0.52),  # 0.50 held 500ms
        _at(0.9, ask=0.55),  # 0.52 held 400ms
    ]
    lifetimes = quote_lifetimes_ms(tops, side="ask")
    assert lifetimes == [500.0, 400.0]


def test_summarize_basic() -> None:
    s = summarize([1.0, 2.0, 3.0, 4.0, 5.0])
    assert s.n == 5
    assert abs(s.median - 3.0) < 1e-9
    assert math.isnan(summarize([]).median)


def test_depth_and_staleness_summaries() -> None:
    tops = [_at(0.0, ask_size=6.0, recv=BASE + timedelta(milliseconds=20)), _at(1.0, ask_size=10.0)]
    depth = depth_summary(tops)
    assert depth["ask_size"].n == 2
    assert abs(depth["spread"].median - 0.02) < 1e-9
    stale = feed_staleness_ms(tops)
    assert stale.n == 1 and abs(stale.median - 20.0) < 1e-6


def test_reaction_lag_recovers_a_known_delay() -> None:
    times = [BASE + timedelta(seconds=i) for i in range(41)]
    ref = [100.0 + 5.0 * math.sin(i / 2.0) for i in range(41)]
    # Kalshi mid lags the reference by 3 seconds
    kal = [ref[max(0, i - 3)] for i in range(41)]
    rl = reaction_lag(times, ref, times, kal, max_lag_s=8.0, step_s=1.0)
    assert rl is not None
    assert 2.0 <= rl.best_lag_s <= 4.0  # ~3s, positive => Kalshi reprices after
    assert rl.correlation > 0.8


def test_repricing_decomposition_splits_trade_vs_cancel() -> None:
    tops = [_at(0.0, ask=0.50), _at(1.0, ask=0.51), _at(2.0, ask=0.52)]
    prints = [TradePrint(ts=BASE + timedelta(seconds=1.0), price=0.50, size=5.0)]  # coincident with first move
    mix = repricing_decomposition(tops, prints, side="ask", window_ms=250.0)
    assert mix.n_changes == 2
    assert abs(mix.trade_driven - 0.5) < 1e-9
    assert abs(mix.cancel_driven - 0.5) < 1e-9


def test_top_from_orderbook_event_adapter() -> None:
    ts = BASE
    recv = BASE + timedelta(milliseconds=15)
    book = SimpleNamespace(
        yes_bids=[SimpleNamespace(price=Decimal("0.45"), quantity=Decimal("20"))],
        yes_asks=[SimpleNamespace(price=Decimal("0.47"), quantity=Decimal("6"))],
        exchange_ts=ts,
        received_at=recv,
    )
    top = top_from_orderbook_event(SimpleNamespace(book=book))
    assert top is not None
    assert top.bid == 0.45 and top.ask == 0.47
    assert top.ask_size == 6.0 and top.recv_ts == recv
    assert abs((top.spread or 0.0) - 0.02) < 1e-9
