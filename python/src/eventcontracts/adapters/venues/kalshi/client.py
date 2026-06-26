"""Kalshi public REST and WebSocket market-data clients.

The client keeps live connectivity at the adapter boundary. Strategies still
consume only normalized domain events produced later by the normalization
layer.
"""

from __future__ import annotations

import asyncio
import base64
import importlib
import json
import os
import random
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.parse import urlparse

import httpx

from eventcontracts.domain.models import (
    InstrumentId,
    Market,
    MarketStatus,
    OrderBook,
    OrderBookLevel,
    OutcomeSide,
    Trade,
    Venue,
)
from eventcontracts.domain.positions import CashBalance
from eventcontracts.storage.interfaces import EventEnvelope

KALSHI_REST_PROD = "https://external-api.kalshi.com/trade-api/v2"
KALSHI_REST_DEMO = "https://external-api.demo.kalshi.co/trade-api/v2"
KALSHI_WS_PROD = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"
KALSHI_WS_DEMO = "wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2"


class WebSocketConnection(Protocol):
    async def send(self, message: str) -> None: ...

    async def recv(self) -> str | bytes: ...


WebSocketConnector = Callable[[str, Mapping[str, str]], Any]


@dataclass(frozen=True)
class KalshiAuth:
    """RSA-PSS API-key authentication material.

    Authentication is optional at construction time so tests and public-fixture
    replay do not need credentials. Live REST/WS calls that require auth fail
    clearly if the key or private key path is missing.
    """

    api_key_id: str | None = None
    private_key_path: Path | None = None
    private_key_pem: str | None = None

    @classmethod
    def from_env(cls) -> KalshiAuth:
        key_id = os.getenv("KALSHI_API_KEY_ID") or None
        private_key = os.getenv("KALSHI_PRIVATE_KEY_PATH") or None
        private_key_pem = os.getenv("KALSHI_PRIVATE_KEY_PEM") or None
        return cls(
            api_key_id=key_id,
            private_key_path=Path(private_key).expanduser() if private_key else None,
            private_key_pem=private_key_pem,
        )

    def headers(self, method: str, url_or_path: str, *, timestamp_ms: int | None = None) -> dict[str, str]:
        if not self.api_key_id or (self.private_key_path is None and self.private_key_pem is None):
            return {}
        timestamp = str(timestamp_ms if timestamp_ms is not None else int(time.time() * 1000))
        path = _signing_path(url_or_path)
        signature = _sign_pss_sha256(
            self.private_key_path,
            self.private_key_pem,
            f"{timestamp}{method.upper()}{path}".encode(),
        )
        return {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
            "KALSHI-ACCESS-SIGNATURE": signature,
        }


@dataclass(frozen=True)
class KalshiEnvironment:
    rest_base_url: str
    ws_url: str

    @classmethod
    def from_env(cls) -> KalshiEnvironment:
        env = (os.getenv("KALSHI_ENV") or "demo").lower()
        if env == "prod" or env == "production":
            return cls(rest_base_url=KALSHI_REST_PROD, ws_url=KALSHI_WS_PROD)
        return cls(rest_base_url=KALSHI_REST_DEMO, ws_url=KALSHI_WS_DEMO)


@dataclass(frozen=True)
class KalshiBalanceSnapshot:
    """Authenticated account balance from Kalshi's portfolio endpoint."""

    available_cash: Decimal
    portfolio_value: Decimal
    updated_at: datetime
    raw: Mapping[str, Any]

    def cash_balance(self, *, currency: str = "USD") -> CashBalance:
        return CashBalance(
            currency=currency,
            total=self.available_cash,
            available=self.available_cash,
            held_for_orders=Decimal("0"),
            settling=Decimal("0"),
            updated_at=self.updated_at,
        )


class KalshiPublicClient:
    """Async REST client for public Kalshi market data."""

    def __init__(
        self,
        *,
        base_url: str = KALSHI_REST_DEMO,
        auth: KalshiAuth | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.auth = auth or KalshiAuth()
        self._client = http_client

    @classmethod
    def from_env(cls) -> KalshiPublicClient:
        environment = KalshiEnvironment.from_env()
        return cls(base_url=environment.rest_base_url, auth=KalshiAuth.from_env())

    async def _get(self, path: str, params: Mapping[str, str | int] | None = None) -> dict[str, Any]:
        return await self._request("GET", path, params=params)

    async def _post(self, path: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        return await self._request("POST", path, json_payload=payload)

    async def _delete(self, path: str) -> dict[str, Any]:
        return await self._request("DELETE", path)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str | int] | None = None,
        json_payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        headers = self.auth.headers(method, url)
        if json_payload is not None:
            headers["Content-Type"] = "application/json"
        if self._client is not None:
            response = await self._client.request(
                method,
                url,
                params=params,
                headers=headers,
                json=dict(json_payload) if json_payload is not None else None,
            )
            response.raise_for_status()
            return _as_dict(response.json())
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.request(
                method,
                url,
                params=params,
                headers=headers,
                json=dict(json_payload) if json_payload is not None else None,
            )
            response.raise_for_status()
            return _as_dict(response.json())

    async def get_balance_payload(self, *, subaccount: int | None = None) -> dict[str, Any]:
        params = {"subaccount": subaccount} if subaccount is not None else None
        return await self._get("/portfolio/balance", params=params)

    async def get_balance(self, *, subaccount: int | None = None) -> KalshiBalanceSnapshot:
        payload = await self.get_balance_payload(subaccount=subaccount)
        return _balance_snapshot_from_payload(payload)

    async def get_cash_balance(
        self,
        *,
        currency: str = "USD",
        subaccount: int | None = None,
    ) -> CashBalance:
        return (await self.get_balance(subaccount=subaccount)).cash_balance(currency=currency)

    async def create_event_order_payload(
        self,
        *,
        ticker: str,
        client_order_id: str,
        side: str,
        count: Decimal | str | int,
        price: Decimal | str | int,
        time_in_force: str = "good_till_canceled",
        self_trade_prevention_type: str = "taker_at_cross",
        post_only: bool = False,
        cancel_order_on_pause: bool = True,
        reduce_only: bool = False,
        subaccount: int = 0,
        exchange_index: int = 0,
    ) -> dict[str, Any]:
        """Submit a Kalshi V2 event-market order and return the raw payload.

        This is an authenticated write path. Callers must gate real usage with
        the operator/panel/spend controls outside this adapter.
        """
        payload = _event_order_payload(
            ticker=ticker,
            client_order_id=client_order_id,
            side=side,
            count=count,
            price=price,
            time_in_force=time_in_force,
            self_trade_prevention_type=self_trade_prevention_type,
            post_only=post_only,
            cancel_order_on_pause=cancel_order_on_pause,
            reduce_only=reduce_only,
            subaccount=subaccount,
            exchange_index=exchange_index,
        )
        return await self._post("/portfolio/events/orders", payload)

    async def cancel_event_order_payload(self, order_id: str) -> dict[str, Any]:
        """Cancel a Kalshi V2 event-market order by venue order id."""
        if not str(order_id).strip():
            raise ValueError("order_id must not be empty")
        return await self._delete(f"/portfolio/events/orders/{order_id}")

    async def get_markets_payload(
        self,
        *,
        limit: int = 1000,
        cursor: str | None = None,
        status: str | None = None,
        series_ticker: str | None = None,
        event_ticker: str | None = None,
        tickers: Sequence[str] = (),
    ) -> dict[str, Any]:
        params: dict[str, str | int] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        if status:
            params["status"] = status
        if series_ticker:
            params["series_ticker"] = series_ticker
        if event_ticker:
            params["event_ticker"] = event_ticker
        if tickers:
            params["tickers"] = ",".join(tickers)
        return await self._get("/markets", params=params)

    async def list_market_payloads(
        self,
        *,
        status: str | None = "open",
        series_ticker: str | None = None,
        event_ticker: str | None = None,
        tickers: Sequence[str] = (),
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        cursor: str | None = None
        markets: list[dict[str, Any]] = []
        while True:
            payload = await self.get_markets_payload(
                limit=limit,
                cursor=cursor,
                status=status,
                series_ticker=series_ticker,
                event_ticker=event_ticker,
                tickers=tickers,
            )
            markets.extend(_as_dict(item) for item in payload.get("markets", []))
            cursor_value = payload.get("cursor")
            cursor = str(cursor_value) if cursor_value else None
            if not cursor:
                return markets

    async def list_markets(self) -> list[Market]:
        return [_market_from_payload(payload) for payload in await self.list_market_payloads()]

    async def get_market_payload(self, ticker: str) -> dict[str, Any]:
        return await self._get(f"/markets/{ticker}")

    async def get_market(self, instrument_id: InstrumentId) -> Market:
        payload = await self.get_market_payload(instrument_id.market_id)
        market_payload = _as_dict(payload.get("market", payload))
        return _market_from_payload(market_payload)

    async def get_order_book_payload(self, ticker: str, *, depth: int = 0) -> dict[str, Any]:
        return await self._get(f"/markets/{ticker}/orderbook", params={"depth": depth})

    async def get_order_book(self, instrument_id: InstrumentId) -> OrderBook:
        payload = await self.get_order_book_payload(instrument_id.market_id)
        return _order_book_from_payload(payload, instrument_id=instrument_id, received_at=_utc_now())

    async def get_trades_payload(
        self,
        *,
        ticker: str | None = None,
        min_ts: int | None = None,
        max_ts: int | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, str | int] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        if ticker:
            params["ticker"] = ticker
        if min_ts is not None:
            params["min_ts"] = min_ts
        if max_ts is not None:
            params["max_ts"] = max_ts
        return await self._get("/markets/trades", params=params)

    async def get_historical_candlesticks_payload(
        self,
        *,
        ticker: str,
        start_ts: int,
        end_ts: int,
        period_interval: int = 1,
    ) -> dict[str, Any]:
        if period_interval not in {1, 60, 1440}:
            raise ValueError("period_interval must be one of 1, 60, or 1440 minutes")
        return await self._get(
            f"/historical/markets/{ticker}/candlesticks",
            params={
                "start_ts": start_ts,
                "end_ts": end_ts,
                "period_interval": period_interval,
            },
        )

    async def get_market_candlesticks_payload(
        self,
        *,
        tickers: Sequence[str],
        start_ts: int,
        end_ts: int,
        period_interval: int = 1,
    ) -> dict[str, Any]:
        """Fetch historical market candles from Kalshi's batch candle endpoint."""

        if not tickers:
            raise ValueError("tickers must not be empty")
        if period_interval not in {1, 60, 1440}:
            raise ValueError("period_interval must be one of 1, 60, or 1440 minutes")
        return await self._get(
            "/markets/candlesticks",
            params={
                "market_tickers": ",".join(tickers),
                "start_ts": start_ts,
                "end_ts": end_ts,
                "period_interval": period_interval,
            },
        )

    async def get_recent_trades(self, ticker: str, *, limit: int = 100) -> list[Trade]:
        payload = await self.get_trades_payload(ticker=ticker, limit=limit)
        trades = [_trade_from_payload(_as_dict(item), received_at=_utc_now()) for item in payload.get("trades", [])]
        return trades

    async def stream_order_books(self) -> AsyncIterator[OrderBook]:
        raise NotImplementedError("use KalshiWebSocketClient for streaming order books")
        yield

    async def stream_trades(self) -> AsyncIterator[Trade]:
        raise NotImplementedError("use KalshiWebSocketClient for streaming trades")
        yield


class KalshiWebSocketClient:
    """WebSocket capture client that yields raw `EventEnvelope` values."""

    def __init__(
        self,
        *,
        ws_url: str = KALSHI_WS_DEMO,
        auth: KalshiAuth | None = None,
        connector: WebSocketConnector | None = None,
        source: str = "kalshi-ws",
        base_backoff_seconds: float = 0.5,
        max_backoff_seconds: float = 30.0,
        max_sequence_cache_size: int = 4096,
    ) -> None:
        self.ws_url = ws_url
        self.auth = auth or KalshiAuth()
        self.connector = connector
        self.source = source
        self.base_backoff_seconds = base_backoff_seconds
        self.max_backoff_seconds = max_backoff_seconds
        self.max_sequence_cache_size = max_sequence_cache_size
        self._next_command_id = 1
        self._last_seq_by_sid: OrderedDict[int, int] = OrderedDict()

    @classmethod
    def from_env(cls) -> KalshiWebSocketClient:
        environment = KalshiEnvironment.from_env()
        return cls(ws_url=environment.ws_url, auth=KalshiAuth.from_env())

    def subscribe_command(self, channels: Sequence[str], market_tickers: Sequence[str]) -> dict[str, Any]:
        command_id = self._next_command_id
        self._next_command_id += 1
        params: dict[str, Any] = {"channels": list(channels)}
        if market_tickers:
            params["market_tickers"] = list(market_tickers)
        return {"id": command_id, "cmd": "subscribe", "params": params}

    async def stream(
        self,
        *,
        channels: Sequence[str],
        market_tickers: Sequence[str],
        max_messages: int | None = None,
    ) -> AsyncIterator[EventEnvelope]:
        seen = 0
        attempt = 0
        while max_messages is None or seen < max_messages:
            try:
                async for envelope in self._stream_once(
                    channels=channels,
                    market_tickers=market_tickers,
                    max_messages=None if max_messages is None else max_messages - seen,
                ):
                    seen += 1
                    attempt = 0
                    yield envelope
                    if max_messages is not None and seen >= max_messages:
                        return
            except asyncio.CancelledError:
                raise
            except Exception:
                attempt += 1
                await asyncio.sleep(self._backoff_delay(attempt))

    async def _stream_once(
        self,
        *,
        channels: Sequence[str],
        market_tickers: Sequence[str],
        max_messages: int | None,
    ) -> AsyncIterator[EventEnvelope]:
        headers = self.auth.headers("GET", "/trade-api/ws/v2")
        connector = self.connector or _default_websocket_connector
        async with connector(self.ws_url, headers) as websocket:
            await websocket.send(json.dumps(self.subscribe_command(channels, market_tickers)))
            seen = 0
            while max_messages is None or seen < max_messages:
                message = await websocket.recv()
                envelope = self.message_to_envelope(message)
                if envelope is None:
                    continue
                seen += 1
                yield envelope

    def message_to_envelope(
        self,
        message: str | bytes | Mapping[str, Any],
        *,
        received_at: datetime | None = None,
    ) -> EventEnvelope | None:
        payload = _decode_ws_message(message)
        msg_type = payload.get("type")
        if not isinstance(msg_type, str) or not msg_type:
            return None
        msg = _as_dict(payload.get("msg", {}))
        metadata: dict[str, Any] = {"ws_type": msg_type}
        sid_value = payload.get("sid")
        seq_value = payload.get("seq")
        if isinstance(sid_value, int):
            metadata["sid"] = sid_value
        if isinstance(seq_value, int):
            metadata["source_sequence"] = str(seq_value)
            if isinstance(sid_value, int):
                expected = self._last_seq_by_sid.get(sid_value)
                if expected is not None and seq_value != expected + 1:
                    metadata["sequence_gap"] = True
                    metadata["expected_sequence"] = expected + 1
                    metadata["actual_sequence"] = seq_value
                self._remember_sequence(sid_value, seq_value)

        exchange_ts = _message_time(msg)
        return EventEnvelope(
            venue=Venue.KALSHI,
            source=self.source,
            channel=msg_type,
            received_at=received_at or _utc_now(),
            exchange_ts=exchange_ts,
            payload=payload,
            schema_version="kalshi-ws-v1",
            metadata=metadata,
        )

    def _backoff_delay(self, attempt: int) -> float:
        capped = min(self.max_backoff_seconds, self.base_backoff_seconds * (2 ** max(0, attempt - 1)))
        return cast(float, capped + (random.random() * (capped / 4)))

    def _remember_sequence(self, sid: int, sequence: int) -> None:
        self._last_seq_by_sid[sid] = sequence
        self._last_seq_by_sid.move_to_end(sid)
        while len(self._last_seq_by_sid) > self.max_sequence_cache_size:
            self._last_seq_by_sid.popitem(last=False)


def rest_envelope(
    *,
    channel: str,
    payload: Mapping[str, Any],
    received_at: datetime | None = None,
    exchange_ts: datetime | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> EventEnvelope:
    return EventEnvelope(
        venue=Venue.KALSHI,
        source="kalshi-rest",
        channel=channel,
        received_at=received_at or _utc_now(),
        exchange_ts=exchange_ts,
        payload=payload,
        schema_version="kalshi-rest-v1",
        metadata=metadata or {},
    )


def _default_websocket_connector(url: str, headers: Mapping[str, str]) -> Any:
    websockets = importlib.import_module("websockets")
    return websockets.connect(
        url,
        additional_headers=dict(headers),
        ping_interval=20,
        ping_timeout=10,
    )


def _signing_path(url_or_path: str) -> str:
    parsed = urlparse(url_or_path)
    if parsed.scheme:
        return parsed.path
    return url_or_path.split("?", 1)[0]


def _sign_pss_sha256(private_key_path: Path | None, private_key_pem: str | None, message: bytes) -> str:
    if private_key_pem is not None:
        key_data = private_key_pem.encode()
    else:
        if private_key_path is None or not private_key_path.exists():
            raise FileNotFoundError(f"Kalshi private key file not found: {private_key_path}")
        key_data = private_key_path.read_bytes()
    serialization = importlib.import_module("cryptography.hazmat.primitives.serialization")
    hashes = importlib.import_module("cryptography.hazmat.primitives.hashes")
    padding = importlib.import_module("cryptography.hazmat.primitives.asymmetric.padding")
    private_key = serialization.load_pem_private_key(key_data, password=None)
    signature = private_key.sign(
        message,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode("ascii")


def _decode_ws_message(message: str | bytes | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(message, Mapping):
        return dict(message)
    raw = message.decode("utf-8") if isinstance(message, bytes) else message
    decoded = json.loads(raw)
    return _as_dict(decoded)


def _as_dict(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"expected object payload, got {type(value).__name__}")
    return dict(value)


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


def _parse_time(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, int | float):
        return datetime.fromtimestamp(float(value), tz=UTC)
    text = str(value)
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    return datetime.fromisoformat(text)


def _parse_unix_time(value: Any) -> datetime:
    if value is None:
        return _utc_now()
    seconds = Decimal(str(value))
    if seconds > Decimal("100000000000"):
        seconds = seconds / Decimal("1000")
    return datetime.fromtimestamp(float(seconds), tz=UTC)


def _dollars_from_cents_or_string(
    payload: Mapping[str, Any],
    *,
    dollars_key: str,
    cents_key: str,
) -> Decimal:
    dollars = payload.get(dollars_key)
    if dollars is not None:
        return Decimal(str(dollars))
    cents = payload.get(cents_key)
    if cents is None:
        return Decimal("0")
    return Decimal(str(cents)) / Decimal("100")


def _balance_snapshot_from_payload(payload: Mapping[str, Any]) -> KalshiBalanceSnapshot:
    return KalshiBalanceSnapshot(
        available_cash=_dollars_from_cents_or_string(
            payload,
            dollars_key="balance_dollars",
            cents_key="balance",
        ),
        portfolio_value=_dollars_from_cents_or_string(
            payload,
            dollars_key="portfolio_value_dollars",
            cents_key="portfolio_value",
        ),
        updated_at=_parse_unix_time(payload.get("updated_ts")),
        raw=dict(payload),
    )


def _event_order_payload(
    *,
    ticker: str,
    client_order_id: str,
    side: str,
    count: Decimal | str | int,
    price: Decimal | str | int,
    time_in_force: str,
    self_trade_prevention_type: str,
    post_only: bool,
    cancel_order_on_pause: bool,
    reduce_only: bool,
    subaccount: int,
    exchange_index: int,
) -> dict[str, Any]:
    ticker = str(ticker).strip()
    client_order_id = str(client_order_id).strip()
    side = str(side).strip().lower()
    if not ticker:
        raise ValueError("ticker must not be empty")
    if not client_order_id:
        raise ValueError("client_order_id must not be empty")
    if side not in {"bid", "ask"}:
        raise ValueError("side must be 'bid' or 'ask'")
    count_value = Decimal(str(count))
    price_value = Decimal(str(price))
    if count_value <= 0:
        raise ValueError("count must be positive")
    if price_value <= 0 or price_value >= 1:
        raise ValueError("price must be greater than 0 and less than 1")
    return {
        "ticker": ticker,
        "client_order_id": client_order_id,
        "side": side,
        "count": _format_decimal(count_value, places=2),
        "price": _format_decimal(price_value, places=4),
        "time_in_force": time_in_force,
        "self_trade_prevention_type": self_trade_prevention_type,
        "post_only": bool(post_only),
        "cancel_order_on_pause": bool(cancel_order_on_pause),
        "reduce_only": bool(reduce_only),
        "subaccount": int(subaccount),
        "exchange_index": int(exchange_index),
    }


def _format_decimal(value: Decimal, *, places: int) -> str:
    quant = Decimal("1").scaleb(-places)
    return format(value.quantize(quant), "f")


def _message_time(message: Mapping[str, Any]) -> datetime | None:
    ts_ms = message.get("ts_ms")
    if isinstance(ts_ms, int | float):
        return datetime.fromtimestamp(float(ts_ms) / 1000, tz=UTC)
    ts = message.get("ts")
    if isinstance(ts, int | float):
        return datetime.fromtimestamp(float(ts), tz=UTC)
    return _parse_time(message.get("time"))


def _market_from_payload(payload: Mapping[str, Any]) -> Market:
    ticker = str(payload.get("ticker") or payload.get("market_ticker"))
    return Market(
        instrument_id=InstrumentId(venue=Venue.KALSHI, market_id=ticker),
        title=str(payload.get("title") or payload.get("name") or ticker),
        category=str(payload["category"]) if payload.get("category") is not None else None,
        status=_market_status(str(payload.get("status", "unknown"))),
        opens_at=_parse_time(payload.get("open_time") or payload.get("open_ts")),
        closes_at=_parse_time(payload.get("close_time") or payload.get("close_ts")),
        settles_at=_parse_time(payload.get("settlement_time") or payload.get("settlement_ts")),
        metadata={key: value for key, value in payload.items() if key not in {"ticker", "market_ticker", "title"}},
    )


def _market_status(value: str) -> MarketStatus:
    mapping = {
        "initialized": MarketStatus.UNKNOWN,
        "unopened": MarketStatus.UNKNOWN,
        "active": MarketStatus.OPEN,
        "open": MarketStatus.OPEN,
        "inactive": MarketStatus.PAUSED,
        "paused": MarketStatus.PAUSED,
        "closed": MarketStatus.CLOSED,
        "determined": MarketStatus.DETERMINED,
        "disputed": MarketStatus.DISPUTED,
        "amended": MarketStatus.DETERMINED,
        "settled": MarketStatus.FINALIZED,
        "finalized": MarketStatus.FINALIZED,
    }
    return mapping.get(value, MarketStatus.UNKNOWN)


def _order_book_from_payload(
    payload: Mapping[str, Any],
    *,
    instrument_id: InstrumentId,
    received_at: datetime,
) -> OrderBook:
    book = _as_dict(payload.get("orderbook_fp", payload.get("orderbook", payload)))
    yes_bids = _levels(book.get("yes_dollars") or book.get("yes") or book.get("yes_dollars_fp"))
    no_bids = _levels(book.get("no_dollars") or book.get("no") or book.get("no_dollars_fp"))
    return _book_from_bid_ladders(
        instrument_id=instrument_id,
        yes_bids=yes_bids,
        no_bids=no_bids,
        exchange_ts=received_at,
        received_at=received_at,
    )


def _book_from_bid_ladders(
    *,
    instrument_id: InstrumentId,
    yes_bids: tuple[OrderBookLevel, ...],
    no_bids: tuple[OrderBookLevel, ...],
    exchange_ts: datetime | None,
    received_at: datetime,
) -> OrderBook:
    sorted_yes_bids = _sort_levels(yes_bids, descending=True)
    sorted_no_bids = _sort_levels(no_bids, descending=True)
    return OrderBook(
        instrument_id=instrument_id,
        yes_bids=sorted_yes_bids,
        yes_asks=_inverted_ask_levels(sorted_no_bids),
        no_bids=sorted_no_bids,
        no_asks=_inverted_ask_levels(sorted_yes_bids),
        exchange_ts=exchange_ts,
        received_at=received_at,
    )


def _sort_levels(levels: Sequence[OrderBookLevel], *, descending: bool) -> tuple[OrderBookLevel, ...]:
    return tuple(sorted(levels, key=lambda level: level.price, reverse=descending))


def _inverted_ask_levels(bids: Sequence[OrderBookLevel]) -> tuple[OrderBookLevel, ...]:
    levels = tuple(
        OrderBookLevel(price=Decimal("1") - level.price, quantity=level.quantity)
        for level in bids
        if Decimal("0") <= Decimal("1") - level.price <= Decimal("1")
    )
    return _sort_levels(levels, descending=False)


def _levels(raw_levels: Any) -> tuple[OrderBookLevel, ...]:
    if raw_levels is None:
        return ()
    levels: list[OrderBookLevel] = []
    for raw_level in raw_levels:
        if not isinstance(raw_level, Sequence) or len(raw_level) < 2:
            continue
        levels.append(OrderBookLevel(price=Decimal(str(raw_level[0])), quantity=Decimal(str(raw_level[1]))))
    return tuple(levels)


def _trade_from_payload(payload: Mapping[str, Any], *, received_at: datetime) -> Trade:
    ticker = str(payload.get("ticker") or payload.get("market_ticker"))
    side_raw = payload.get("taker_outcome_side") or payload.get("taker_side") or payload.get("side")
    side = OutcomeSide(str(side_raw)) if side_raw else OutcomeSide.YES
    yes_price = payload.get("yes_price_dollars") or payload.get("yes_price")
    no_price = payload.get("no_price_dollars") or payload.get("no_price")
    price = _trade_price(side=side, yes_price=yes_price, no_price=no_price, fallback=payload.get("price"))
    quantity = Decimal(str(payload.get("count_fp") or payload.get("quantity") or payload.get("count") or "0"))
    trade_ts = _message_time(payload) or received_at
    return Trade(
        instrument_id=InstrumentId(venue=Venue.KALSHI, market_id=ticker),
        side=side,
        price=price,
        quantity=quantity,
        trade_id=str(payload["trade_id"]) if payload.get("trade_id") is not None else None,
        exchange_ts=trade_ts,
        received_at=received_at,
        aggressor_side=side,
        metadata={"raw_venue": "kalshi"},
    )


def _trade_price(
    *,
    side: OutcomeSide,
    yes_price: object,
    no_price: object,
    fallback: object,
) -> Decimal:
    if side is OutcomeSide.NO:
        if no_price is not None:
            return Decimal(str(no_price))
        if yes_price is not None:
            return Decimal("1") - Decimal(str(yes_price))
    if yes_price is not None:
        return Decimal(str(yes_price))
    if no_price is not None:
        return Decimal("1") - Decimal(str(no_price))
    return Decimal(str(fallback))
