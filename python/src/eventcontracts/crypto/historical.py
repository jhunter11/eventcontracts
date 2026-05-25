"""Historical 1-minute loaders for Kalshi BTC brackets and Deribit OHLC/DVOL.

Both venues expose free public REST endpoints with no API key. This
module fetches a contiguous one-hour expiry's worth of data from
either side and reshapes it into ``NormalizedEvent`` values ready
for the ensemble strategy:

* Deribit ``BTC-PERPETUAL`` 1m close → ``ExternalSignalEvent(source="binance")``
  carrying ``last_price`` and ``expiry_iso``.
* Deribit BTC DVOL 1m → ``ExternalSignalEvent(source="deribit")``
  carrying ``atm_iv`` (DVOL is already annualized).
* Kalshi 1m candlesticks per bracket → ``QuoteEvent`` per market per
  minute using the bid/ask close. Bracket markets feed the parity
  source; "above $K" tickers (prefix ``T``) feed the vol-surface and
  skew sources.

The HTTP layer uses plain ``urllib.request`` and ``ssl`` so the loader
runs in any environment that can reach the open internet — no third
party deps. Set ``EVENTCONTRACTS_INSECURE_TLS=1`` in research VMs
whose host clock breaks Deribit's TLS validation.
"""

from __future__ import annotations

import json
import os
import re
import ssl
import time
import urllib.parse
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

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

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
DERIBIT_BASE = "https://www.deribit.com/api/v2"


def _ssl_context() -> ssl.SSLContext | None:
    if os.environ.get("EVENTCONTRACTS_INSECURE_TLS") == "1":
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    return None


_LAST_REQUEST_TS: dict[str, float] = {}


def _http_get(
    base: str,
    path: str,
    params: dict[str, object],
    *,
    timeout: float = 15.0,
    min_interval_s: float = 0.0,
    max_retries: int = 3,
) -> dict:
    """GET wrapper with per-host throttling and 429-aware retry."""

    url = f"{base}{path}?{urllib.parse.urlencode(params)}"
    if min_interval_s > 0:
        last = _LAST_REQUEST_TS.get(base, 0.0)
        elapsed = time.monotonic() - last
        if elapsed < min_interval_s:
            time.sleep(min_interval_s - elapsed)
        _LAST_REQUEST_TS[base] = time.monotonic()

    backoff = 1.0
    for attempt in range(max_retries + 1):
        req = urllib.request.Request(url, headers={"User-Agent": "eventcontracts/0.1"})
        try:
            with urllib.request.urlopen(
                req, timeout=timeout, context=_ssl_context()
            ) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt < max_retries:
                time.sleep(backoff)
                backoff *= 2.0
                continue
            raise


# ----------------------------- Kalshi -----------------------------


@dataclass(frozen=True)
class KalshiMarket:
    """Metadata for one Kalshi BTC bracket or above-market."""

    ticker: str
    subtitle: str
    open_time: datetime
    close_time: datetime
    # Bracket layout extracted from the ticker.
    kind: str            # "between" | "above" | "below"
    lower: Decimal | None
    upper: Decimal | None


_TICKER_PATTERN = re.compile(
    r"^(?P<series>KXBTC)-"
    r"(?P<expiry>[0-9A-Z]+)-"
    r"(?P<type>[TB])(?P<strike>[0-9.]+)$"
)


def parse_kalshi_btc_ticker(ticker: str, *, bracket_step: Decimal = Decimal("100")) -> KalshiMarket | None:
    """Parse the strike layout out of a KXBTC market ticker.

    Examples
    --------
    ``KXBTC-26MAY2508-B77250``  → between [$77,200, $77,299.99]
    ``KXBTC-26MAY2508-T85799.99`` → above $85,799.99
    ``KXBTC-26MAY2508-T67200`` → below $67,200 (high tail is ``T<largest>``
    and low tail is ``T<smallest>`` — Kalshi reuses the ``T`` prefix
    for both unbounded tails; the subtitle disambiguates).

    Returns ``None`` when the ticker does not look like a strike-based
    bracket (e.g. range-roundup variants).
    """

    match = _TICKER_PATTERN.match(ticker)
    if match is None:
        return None
    type_char = match.group("type")
    strike = Decimal(match.group("strike"))
    if type_char == "B":
        # "between [strike, strike + bracket_step)"
        lower = strike
        upper = strike + bracket_step
        kind = "between"
    else:
        # "T" is used for both unbounded tails. The subtitle on the
        # market record clarifies which. The loader uses subtitle when
        # available; here we conservatively call it ``above`` and let
        # the caller swap to ``below`` if needed.
        lower = strike
        upper = None
        kind = "above"
    return KalshiMarket(
        ticker=ticker,
        subtitle="",
        open_time=datetime.fromtimestamp(0, tz=UTC),
        close_time=datetime.fromtimestamp(0, tz=UTC),
        kind=kind,
        lower=lower,
        upper=upper,
    )


def list_kalshi_btc_markets(
    *,
    status: str = "settled",
    series_ticker: str = "KXBTC",
    expiry_hour_token: str | None = None,
    limit: int = 1000,
    max_pages: int = 20,
) -> list[KalshiMarket]:
    """List Kalshi BTC markets, parse their strike layouts.

    Walks the cursor pagination on ``/markets`` up to ``max_pages``
    pages so callers can reach cohorts older than the first page.
    ``expiry_hour_token`` filters to one settlement hour by the
    embedded date+hour string from the ticker (e.g. ``"26MAY2508"``
    selects every bracket settling at 12:00 UTC on 2026-05-25 — the
    cohort named "08" because Kalshi names them by US Eastern
    hour-of-day). When ``expiry_hour_token`` is set the pagination
    stops as soon as the cohort is fully captured.
    """

    out: list[KalshiMarket] = []
    cursor: str | None = None
    for _ in range(max_pages):
        params: dict[str, object] = {
            "series_ticker": series_ticker,
            "status": status,
            "limit": limit,
        }
        if cursor:
            params["cursor"] = cursor
        response = _http_get(KALSHI_BASE, "/markets", params)
        page = response.get("markets", [])
        if not page:
            break
        token_hit = False
        for raw in page:
            ticker = raw["ticker"]
            if expiry_hour_token and expiry_hour_token not in ticker:
                continue
            token_hit = bool(expiry_hour_token)
            parsed = parse_kalshi_btc_ticker(ticker)
            if parsed is None:
                continue
            subtitle = raw.get("subtitle", "") or ""
            kind = parsed.kind
            lower = parsed.lower
            upper = parsed.upper
            # Subtitle disambiguates the two unbounded tails.
            lowered = subtitle.lower()
            if kind == "above" and "below" in lowered:
                kind = "below"
                upper = lower
                lower = None
            out.append(
                KalshiMarket(
                    ticker=ticker,
                    subtitle=subtitle,
                    open_time=datetime.fromisoformat(raw["open_time"].replace("Z", "+00:00")),
                    close_time=datetime.fromisoformat(raw["close_time"].replace("Z", "+00:00")),
                    kind=kind,
                    lower=lower,
                    upper=upper,
                )
            )
        cursor = response.get("cursor")
        if not cursor:
            break
        # Stop early once we've left the target cohort: tickers come
        # ordered newest-first, so once we land in a cohort that
        # *doesn't* match the token we know the requested cohort is
        # complete on disk.
        if expiry_hour_token and out and not token_hit:
            break
    return out


def fetch_kalshi_candlesticks(
    *,
    series_ticker: str,
    market_ticker: str,
    start_ts_epoch: int,
    end_ts_epoch: int,
    period_interval: int = 1,
) -> list[dict]:
    """Pull the raw candlestick list for one bracket market.

    Returns Kalshi's native shape — each record carries dict-shaped
    ``yes_bid``, ``yes_ask``, and ``price`` blocks with
    ``open/high/low/close_dollars`` strings.
    """

    response = _http_get(
        KALSHI_BASE,
        f"/series/{series_ticker}/markets/{market_ticker}/candlesticks",
        {
            "start_ts": start_ts_epoch,
            "end_ts": end_ts_epoch,
            "period_interval": period_interval,
        },
        min_interval_s=0.25,  # Kalshi rate-limit
    )
    return response.get("candlesticks", [])


def kalshi_quote_events(
    market: KalshiMarket,
    candles: Sequence[dict],
) -> list[QuoteEvent]:
    """Turn one bracket's candlesticks into per-minute ``QuoteEvent``s."""

    events: list[QuoteEvent] = []
    for c in candles:
        ts = datetime.fromtimestamp(int(c["end_period_ts"]), tz=UTC)
        bid_raw = c.get("yes_bid", {}).get("close_dollars")
        ask_raw = c.get("yes_ask", {}).get("close_dollars")
        if bid_raw is None or ask_raw is None:
            continue
        bid_dec = Decimal(bid_raw)
        ask_dec = Decimal(ask_raw)
        # Kalshi's tick is $0.01; brackets with no live bid quote
        # ``0.00 / 0.01`` rather than no quote at all. Treat the bid
        # floor as a probability floor so parity has a valid mid.
        if bid_dec < 0 or ask_dec <= 0 or bid_dec >= ask_dec:
            continue
        # Force a strictly-positive bid so OrderBookLevel validates.
        bid_for_book = max(bid_dec, Decimal("0.0001"))
        events.append(
            QuoteEvent(
                event_id=EventId(f"kalshi-{market.ticker}-{int(ts.timestamp())}"),
                quote=Quote(
                    instrument_id=InstrumentId(venue=Venue.KALSHI, market_id=market.ticker),
                    side=OutcomeSide.YES,
                    bid=OrderBookLevel(price=bid_for_book, quantity=Decimal("100")),
                    ask=OrderBookLevel(price=ask_dec, quantity=Decimal("100")),
                    exchange_ts=ts,
                    received_at=ts,
                ),
                provenance=EventProvenance(
                    source="kalshi",
                    channel="candlestick",
                    venue=Venue.KALSHI,
                ),
            )
        )
    return events


# ----------------------------- Deribit -----------------------------


@dataclass(frozen=True)
class DeribitOhlc:
    timestamp: datetime
    close: Decimal


@dataclass(frozen=True)
class DeribitDvol:
    timestamp: datetime
    dvol_pct: Decimal  # annualized vol in fraction (0.34 = 34%)


def fetch_deribit_ohlc(
    *,
    instrument: str = "BTC-PERPETUAL",
    start_ms: int,
    end_ms: int,
    resolution: str = "1",
) -> list[DeribitOhlc]:
    """Pull 1-minute (or other) OHLC from Deribit's TradingView endpoint."""

    response = _http_get(
        DERIBIT_BASE,
        "/public/get_tradingview_chart_data",
        {
            "instrument_name": instrument,
            "start_timestamp": start_ms,
            "end_timestamp": end_ms,
            "resolution": resolution,
        },
    )
    result = response.get("result", {})
    out: list[DeribitOhlc] = []
    for ts_ms, close in zip(result.get("ticks", []), result.get("close", []), strict=False):
        if close is None:
            continue
        out.append(
            DeribitOhlc(
                timestamp=datetime.fromtimestamp(ts_ms / 1000.0, tz=UTC),
                close=Decimal(str(close)),
            )
        )
    return out


def fetch_deribit_dvol(
    *,
    currency: str = "BTC",
    start_ms: int,
    end_ms: int,
    resolution_seconds: int = 60,
) -> list[DeribitDvol]:
    """Pull historical DVOL (Deribit's vol index).

    DVOL is the annualized 30-day implied vol expressed in *percent*
    in the Deribit API; we convert to fraction here so the value can
    be passed directly to BS pricing helpers.
    """

    response = _http_get(
        DERIBIT_BASE,
        "/public/get_volatility_index_data",
        {
            "currency": currency,
            "start_timestamp": start_ms,
            "end_timestamp": end_ms,
            "resolution": resolution_seconds,
        },
    )
    result = response.get("result", {})
    out: list[DeribitDvol] = []
    for row in result.get("data", []):
        # row: [timestamp_ms, open, high, low, close]
        ts_ms, _o, _h, _l, close = row
        if close is None:
            continue
        out.append(
            DeribitDvol(
                timestamp=datetime.fromtimestamp(ts_ms / 1000.0, tz=UTC),
                dvol_pct=Decimal(str(close)) / Decimal("100"),
            )
        )
    return out


# ----------------------------- Stream construction -----------------------------


@dataclass(frozen=True)
class CohortSettlement:
    """Ground-truth settlement information for one expiry cohort.

    ``settlement_price`` is the midpoint of the unique between-bracket
    whose Kalshi ``result`` is ``yes`` — Kalshi sets the bracket
    width to $100 so the midpoint approximates the true TWAP within
    $50. ``yes_market_ticker`` records which bracket actually settled
    so the backtester can audit the chain.
    """

    yes_market_ticker: str | None
    settlement_price: Decimal | None
    bracket_results: dict[str, str]  # market_id → "yes" | "no"


def fetch_cohort_settlement(
    *,
    expiry_hour_token: str,
    series_ticker: str = "KXBTC",
    limit: int = 1000,
    max_pages: int = 20,
) -> CohortSettlement:
    """Pull settled bracket results for one Kalshi expiry cohort.

    Paginates through ``/markets`` until the requested cohort is
    fully captured.
    """

    results: dict[str, str] = {}
    yes_market: str | None = None
    settle_price: Decimal | None = None
    cursor: str | None = None
    for _ in range(max_pages):
        params: dict[str, object] = {
            "series_ticker": series_ticker,
            "status": "settled",
            "limit": limit,
        }
        if cursor:
            params["cursor"] = cursor
        response = _http_get(KALSHI_BASE, "/markets", params)
        page = response.get("markets", [])
        if not page:
            break
        cohort_seen = False
        for raw in page:
            ticker = raw["ticker"]
            if expiry_hour_token not in ticker:
                continue
            cohort_seen = True
            result = raw.get("result")
            if not result:
                continue
            results[ticker] = result
            if result == "yes" and yes_market is None:
                parsed = parse_kalshi_btc_ticker(ticker)
                if parsed is not None and parsed.kind == "between":
                    yes_market = ticker
                    if parsed.lower is not None and parsed.upper is not None:
                        settle_price = (parsed.lower + parsed.upper) / Decimal("2")
        cursor = response.get("cursor")
        if not cursor:
            break
        if results and not cohort_seen:
            break
    return CohortSettlement(
        yes_market_ticker=yes_market,
        settlement_price=settle_price,
        bracket_results=results,
    )


@dataclass
class HistoricalStream:
    """Container for all events that drive one historical expiry."""

    events: list[NormalizedEvent] = field(default_factory=list)
    expiry_at: datetime | None = None
    kalshi_markets: list[KalshiMarket] = field(default_factory=list)

    def sort(self) -> None:
        """Sort by ``(timestamp, kind)`` for deterministic replay."""

        def _ts(event: NormalizedEvent) -> datetime:
            if isinstance(event, ExternalSignalEvent):
                return event.received_at
            if isinstance(event, QuoteEvent):
                return event.quote.received_at
            return datetime.fromtimestamp(0, tz=UTC)

        self.events.sort(key=_ts)


def build_historical_stream(
    *,
    expiry_hour_token: str,
    series_ticker: str = "KXBTC",
    market_prefix_filter: str | None = None,
    atm_radius_dollars: Decimal | None = Decimal("500"),
) -> HistoricalStream:
    """Assemble a one-hour-expiry stream from Kalshi + Deribit historical APIs.

    ``expiry_hour_token`` selects one settlement cohort from the
    Kalshi listing (e.g. ``"26MAY2508"``). Each bracket and
    above/below market whose ticker contains that token is pulled and
    converted into ``QuoteEvent``s; Deribit OHLC and DVOL covering the
    same window seed the spot and vol-surface external signals.

    ``market_prefix_filter`` optionally restricts the Kalshi markets
    to a prefix substring after the token (e.g. ``"B772"`` to only
    take brackets near $77,200).

    ``atm_radius_dollars`` further restricts the markets to the
    cluster within ``radius`` of the spot price observed at the start
    of the expiry window. This keeps the Kalshi rate budget under
    control — for a one-hour cohort with ~70 brackets fetching all of
    them takes ~20 seconds and risks 429s; the strategies only need
    the cluster near spot anyway. Pass ``None`` to fetch every market.
    """

    markets = list_kalshi_btc_markets(
        status="settled",
        series_ticker=series_ticker,
        expiry_hour_token=expiry_hour_token,
    )
    if not markets:
        # Try open markets as a fallback (current-hour cohorts).
        markets = list_kalshi_btc_markets(
            status="open",
            series_ticker=series_ticker,
            expiry_hour_token=expiry_hour_token,
        )
    if not markets:
        raise RuntimeError(
            f"no Kalshi markets matched expiry token {expiry_hour_token}"
        )
    if market_prefix_filter:
        markets = [m for m in markets if market_prefix_filter in m.ticker]

    expiry_at = max(m.close_time for m in markets)
    open_at = min(m.open_time for m in markets)
    start_ts = int(open_at.timestamp())
    end_ts = int(expiry_at.timestamp())
    start_ms = start_ts * 1000
    end_ms = end_ts * 1000

    stream = HistoricalStream(expiry_at=expiry_at, kalshi_markets=markets)

    # Deribit spot ticks → binance source events (the strategy reads
    # ``spot_source="binance"`` by default; we keep that name for
    # config compatibility).
    spot_series = fetch_deribit_ohlc(start_ms=start_ms, end_ms=end_ms, resolution="1")
    # Narrow the Kalshi market set to those within ``atm_radius_dollars``
    # of the opening spot — strategies only act on the ATM cluster and
    # this avoids hammering Kalshi's rate limit with dozens of OTM
    # brackets that are flat-pinned at $0.01.
    if atm_radius_dollars is not None and spot_series:
        opening_spot = spot_series[0].close
        radius = atm_radius_dollars
        # Always keep the unbounded tails so the parity partition is
        # exhaustive — otherwise Σmid < 1 by construction and parity
        # is structurally violated.
        tails = [m for m in markets if m.kind != "between"]
        between_within = [
            m
            for m in markets
            if m.kind == "between"
            and (
                (m.lower is not None and abs(m.lower - opening_spot) <= radius * 2)
                or (m.upper is not None and abs(m.upper - opening_spot) <= radius * 2)
            )
        ]
        markets = sorted(
            tails + between_within,
            key=lambda m: (
                Decimal("-1e18") if m.lower is None else m.lower,
            ),
        )
        stream.kalshi_markets = markets
    for i, tick in enumerate(spot_series):
        stream.events.append(
            ExternalSignalEvent(
                event_id=EventId(f"hist-spot-{i:06d}"),
                source="binance",
                exchange_ts=tick.timestamp,
                received_at=tick.timestamp,
                schema_version="deribit-perpetual-v1",
                payload={
                    "last_price": str(tick.close),
                    "expiry_iso": expiry_at.isoformat(),
                },
                provenance=EventProvenance(
                    source="binance", channel="ohlc", venue=None
                ),
            )
        )

    # Deribit DVOL → deribit source events.
    dvol_series = fetch_deribit_dvol(start_ms=start_ms, end_ms=end_ms)
    for i, vol in enumerate(dvol_series):
        stream.events.append(
            ExternalSignalEvent(
                event_id=EventId(f"hist-dvol-{i:06d}"),
                source="deribit",
                exchange_ts=vol.timestamp,
                received_at=vol.timestamp,
                schema_version="deribit-dvol-v1",
                payload={
                    "atm_iv": str(vol.dvol_pct),
                    "expiry_iso": expiry_at.isoformat(),
                },
                provenance=EventProvenance(
                    source="deribit", channel="dvol", venue=None
                ),
            )
        )

    # Kalshi bracket candlesticks → QuoteEvents. Real prices only —
    # no synthetic markets are derived. Sources that need monotone
    # ``P(S>=K)`` data should use bracket-aware signal functions on
    # the real interval probabilities instead.
    for market in markets:
        candles = fetch_kalshi_candlesticks(
            series_ticker=series_ticker,
            market_ticker=market.ticker,
            start_ts_epoch=start_ts,
            end_ts_epoch=end_ts,
        )
        stream.events.extend(kalshi_quote_events(market, candles))

    stream.sort()
    return stream
