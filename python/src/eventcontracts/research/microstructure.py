"""Order-book microstructure feasibility analyzer (Lane-2 pre-screen).

Answers the only question that decides whether a latency edge is real *before*
spending on a colocated VPS: when the reference moves, is the Kalshi liquidity
still there when your order would land, or is it pulled first?

Pure functions over two lightweight timelines, so they run identically on a
fixture, a REST poll, or a full WS capture:

* ``BookTop``    -- top of book (bid/ask price+size) at a timestamp.
* ``TradePrint`` -- an executed print (price, size, aggressor).

Metrics:

* ``quote_lifetimes_ms``      -- how long a best price rests before it changes
  (fleeting liquidity). Short lifetime => liquidity vanishes before you land.
* ``depth_summary``           -- spread + top size (capacity, queue-ahead).
* ``feed_staleness_ms``       -- ``received_at - exchange_ts`` (how stale the view is).
* ``reaction_lag``            -- lag (s) maximizing correlation of reference
  returns with Kalshi-mid returns (how long Kalshi takes to reprice a move).
* ``repricing_decomposition`` -- fraction of top-of-book moves that coincided
  with a trade (**catchable**) vs a bare cancel (**pulled** -- no VPS fixes a cancel).

The verdict is the caller's: compare ``reaction_lag`` / quote lifetime to the
detect+submit budget. WS + the trade host give ms-scale numbers; REST polling
gives a coarse (~1 s) but real first read.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

import numpy as np


@dataclass(frozen=True)
class BookTop:
    """Top of book at one instant."""

    ts: datetime
    bid: float | None
    bid_size: float
    ask: float | None
    ask_size: float
    recv_ts: datetime | None = None

    @property
    def mid(self) -> float | None:
        if self.bid is None or self.ask is None:
            return None
        return (self.bid + self.ask) / 2.0

    @property
    def spread(self) -> float | None:
        if self.bid is None or self.ask is None:
            return None
        return self.ask - self.bid


@dataclass(frozen=True)
class TradePrint:
    ts: datetime
    price: float
    size: float
    aggressor: str | None = None


@dataclass(frozen=True)
class Summary:
    n: int
    median: float
    p10: float
    p90: float
    mean: float


def summarize(values: Sequence[float]) -> Summary:
    clean = [v for v in values if v is not None and math.isfinite(v)]
    if not clean:
        return Summary(0, float("nan"), float("nan"), float("nan"), float("nan"))
    arr = np.asarray(clean, dtype=float)
    return Summary(
        n=len(clean),
        median=float(np.median(arr)),
        p10=float(np.percentile(arr, 10)),
        p90=float(np.percentile(arr, 90)),
        mean=float(arr.mean()),
    )


def quote_lifetimes_ms(tops: Sequence[BookTop], *, side: str = "ask") -> list[float]:
    """Durations (ms) each best price on ``side`` held before it changed."""

    if side not in ("ask", "bid"):
        raise ValueError("side must be 'ask' or 'bid'")
    pick = (lambda t: t.ask) if side == "ask" else (lambda t: t.bid)
    out: list[float] = []
    cur: float | None = None
    start: datetime | None = None
    for t in tops:
        v = pick(t)
        if v is None:
            continue
        if cur is None:
            cur, start = v, t.ts
        elif v != cur:
            assert start is not None
            out.append((t.ts - start).total_seconds() * 1000.0)
            cur, start = v, t.ts
    return out


def depth_summary(tops: Sequence[BookTop]) -> dict[str, Summary]:
    return {
        "spread": summarize([t.spread for t in tops if t.spread is not None]),
        "bid_size": summarize([t.bid_size for t in tops if t.bid is not None]),
        "ask_size": summarize([t.ask_size for t in tops if t.ask is not None]),
    }


def feed_staleness_ms(tops: Sequence[BookTop]) -> Summary:
    vals = [(t.recv_ts - t.ts).total_seconds() * 1000.0 for t in tops if t.recv_ts is not None]
    return summarize(vals)


def _ffill(grid: np.ndarray, times: np.ndarray, values: np.ndarray) -> np.ndarray:
    idx = np.searchsorted(times, grid, side="right") - 1
    idx = np.clip(idx, 0, len(values) - 1)
    return values[idx]


@dataclass(frozen=True)
class ReactionLag:
    best_lag_s: float
    correlation: float
    n: int


def reaction_lag(
    ref_times: Sequence[datetime],
    ref_prices: Sequence[float],
    kal_times: Sequence[datetime],
    kal_mids: Sequence[float],
    *,
    max_lag_s: float = 10.0,
    step_s: float = 1.0,
) -> ReactionLag | None:
    """Lag (s) that maximizes corr(reference returns, Kalshi-mid returns).

    Positive lag => Kalshi reprices *after* the reference (a stale window).
    """

    if len(ref_times) < 3 or len(kal_times) < 3 or step_s <= 0:
        return None
    rt = np.array([t.timestamp() for t in ref_times], dtype=float)
    kt = np.array([t.timestamp() for t in kal_times], dtype=float)
    rp = np.asarray(ref_prices, dtype=float)
    kp = np.asarray(kal_mids, dtype=float)
    t0, t1 = max(rt[0], kt[0]), min(rt[-1], kt[-1])
    if t1 - t0 < 3 * step_s:
        return None
    grid = np.arange(t0, t1, step_s)
    rg, kg = _ffill(grid, rt, rp), _ffill(grid, kt, kp)
    rr, kr = np.diff(rg), np.diff(kg)
    if rr.std() == 0 or kr.std() == 0:
        return None
    max_steps = int(max_lag_s / step_s)
    best_lag, best_corr = 0.0, -2.0
    for lag in range(-max_steps, max_steps + 1):
        if lag >= 0:
            a, b = rr[: len(rr) - lag], kr[lag:]
        else:
            a, b = rr[-lag:], kr[: len(kr) + lag]
        if len(a) < 3 or a.std() == 0 or b.std() == 0:
            continue
        c = float(np.corrcoef(a, b)[0, 1])
        if c > best_corr:
            best_corr, best_lag = c, lag * step_s
    return ReactionLag(best_lag_s=best_lag, correlation=best_corr, n=len(grid))


@dataclass(frozen=True)
class RepricingMix:
    n_changes: int
    trade_driven: float
    cancel_driven: float


def repricing_decomposition(
    tops: Sequence[BookTop],
    prints: Sequence[TradePrint],
    *,
    side: str = "ask",
    window_ms: float = 250.0,
) -> RepricingMix:
    """Fraction of best-price moves that coincided with a trade vs a bare cancel.

    A move that was *traded through* is liquidity you could have taken; a move
    that was a bare cancel was pulled before any trade -- no latency catches it.
    """

    pick = (lambda t: t.ask) if side == "ask" else (lambda t: t.bid)
    print_times = np.array([p.ts.timestamp() for p in prints], dtype=float) if prints else np.empty(0)
    changes = 0
    trade_driven = 0
    cur: float | None = None
    for t in tops:
        v = pick(t)
        if v is None:
            continue
        if cur is None:
            cur = v
            continue
        if v != cur:
            changes += 1
            if print_times.size:
                ct = t.ts.timestamp()
                if np.any(np.abs(print_times - ct) <= window_ms / 1000.0):
                    trade_driven += 1
            cur = v
    if changes == 0:
        return RepricingMix(0, float("nan"), float("nan"))
    return RepricingMix(changes, trade_driven / changes, 1.0 - trade_driven / changes)


# --- domain adapters ---------------------------------------------------------


def top_from_orderbook_event(event: object) -> BookTop | None:
    """Extract a ``BookTop`` from a normalized ``OrderBookEvent``."""

    book = getattr(event, "book", None)
    if book is None:
        return None
    best_bid = book.yes_bids[0] if book.yes_bids else None
    best_ask = book.yes_asks[0] if book.yes_asks else None
    ts = book.exchange_ts or book.received_at
    return BookTop(
        ts=ts,
        bid=float(best_bid.price) if best_bid else None,
        bid_size=float(best_bid.quantity) if best_bid else 0.0,
        ask=float(best_ask.price) if best_ask else None,
        ask_size=float(best_ask.quantity) if best_ask else 0.0,
        recv_ts=book.received_at,
    )


def print_from_trade_event(event: object) -> TradePrint | None:
    trade = getattr(event, "trade", None)
    if trade is None:
        return None
    return TradePrint(
        ts=trade.exchange_ts or trade.received_at,
        price=float(trade.price),
        size=float(trade.quantity),
        aggressor=getattr(trade.aggressor_side, "value", None) if trade.aggressor_side else None,
    )
