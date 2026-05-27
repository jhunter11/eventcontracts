"""Kalshi Phase 2 capture tests with recorded payloads only."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

from eventcontracts.adapters.venues.kalshi import KalshiPublicClient, KalshiWebSocketClient
from eventcontracts.cli.capture import capture_kalshi_fixture, capture_kalshi_rest_polls, resolve_kalshi_tickers
from eventcontracts.cli.main import main as cli
from eventcontracts.config import load_strategy_spec
from eventcontracts.domain import LifecycleEvent, OrderBookEvent, QuoteEvent, TradeEvent
from eventcontracts.domain.models import InstrumentId, Venue
from eventcontracts.ingestion import SubscriptionPlanner
from eventcontracts.normalization import EventNormalizer, kalshi_normalizers
from eventcontracts.storage import EventEnvelope, ParquetEventStore
from tests.conftest import REPO_ROOT

FIXTURES = REPO_ROOT / "python/tests/fixtures/kalshi"


def test_rest_client_paginates_and_maps_market_book_and_trades() -> None:
    async def run() -> None:
        client = _mock_rest_client()

        markets = await client.list_markets()
        book = await client.get_order_book(
            InstrumentId(venue=Venue.KALSHI, market_id="KXHIGHNY-26MAY24-B75")
        )
        trades = await client.get_recent_trades("KXHIGHNY-26MAY24-B75")

        assert [market.instrument_id.market_id for market in markets] == [
            "KXHIGHNY-26MAY24-B75",
            "KXFED-26JUN-T4.50",
        ]
        assert book.yes_bids[0].price == Decimal("0.4500")
        assert book.yes_asks[0].price == Decimal("0.4900")
        assert trades[0].trade_id == "trade-rest-1"
        assert trades[0].price == Decimal("0.4700")

    asyncio.run(run())


def test_rest_client_maps_authenticated_balance_to_cash_balance() -> None:
    async def run() -> None:
        client = _mock_rest_client()

        balance = await client.get_cash_balance(currency="USD", subaccount=2)

        assert balance.currency == "USD"
        assert balance.available == Decimal("123.45")
        assert balance.total == Decimal("123.45")
        assert balance.held_for_orders == Decimal("0")
        assert balance.updated_at == datetime(2026, 5, 27, 10, 0, tzinfo=UTC)

    asyncio.run(run())


def test_resolve_kalshi_tickers_unions_exact_and_glob_patterns() -> None:
    async def run() -> None:
        client = _mock_rest_client()

        tickers = await resolve_kalshi_tickers(
            client,
            patterns=("KXHIGHNY-*", "KXEXACT-1"),
            status="open",
        )

        assert tickers == ("KXEXACT-1", "KXHIGHNY-26MAY24-B75")

    asyncio.run(run())


def test_websocket_subscribe_command_and_sequence_gap_detection() -> None:
    client = KalshiWebSocketClient()
    command = client.subscribe_command(("ticker", "trade"), ("KXHIGHNY-26MAY24-B75",))

    assert command["cmd"] == "subscribe"
    assert command["params"]["channels"] == ["ticker", "trade"]
    assert command["params"]["market_tickers"] == ["KXHIGHNY-26MAY24-B75"]

    first = client.message_to_envelope(
        {
            "type": "ticker",
            "sid": 1,
            "seq": 1,
            "msg": {
                "market_ticker": "KXHIGHNY-26MAY24-B75",
                "yes_bid_dollars": "0.45",
                "yes_ask_dollars": "0.53",
                "ts_ms": 1779631200000,
            },
        },
        received_at=datetime(2026, 5, 24, 12, 0, tzinfo=UTC),
    )
    second = client.message_to_envelope(
        {
            "type": "ticker",
            "sid": 1,
            "seq": 3,
            "msg": {
                "market_ticker": "KXHIGHNY-26MAY24-B75",
                "yes_bid_dollars": "0.46",
                "yes_ask_dollars": "0.54",
                "ts_ms": 1779631201000,
            },
        },
        received_at=datetime(2026, 5, 24, 12, 0, 1, tzinfo=UTC),
    )

    assert first is not None
    assert second is not None
    assert second.metadata["sequence_gap"] is True
    assert second.metadata["expected_sequence"] == 2
    assert second.metadata["actual_sequence"] == 3


def test_websocket_stream_reconnects_after_connection_failure() -> None:
    async def run() -> None:
        connector = _FailOnceConnector()
        client = KalshiWebSocketClient(
            connector=connector,
            base_backoff_seconds=0,
            max_backoff_seconds=0,
        )

        envelopes = [
            envelope
            async for envelope in client.stream(
                channels=("ticker",),
                market_tickers=("KXHIGHNY-26MAY24-B75",),
                max_messages=1,
            )
        ]

        assert connector.calls == 2
        assert len(envelopes) == 1
        assert envelopes[0].channel == "ticker"

    asyncio.run(run())


def test_subscription_planner_unions_active_strategy_patterns() -> None:
    specs = [
        load_strategy_spec(REPO_ROOT / "configs/strategies/weather-temperature-arbitrage.toml"),
        load_strategy_spec(REPO_ROOT / "configs/strategies/macro-cpi-predictor.toml"),
    ]

    plan = SubscriptionPlanner().plan(tuple(specs))

    assert plan.venues == (Venue.KALSHI,)
    assert "KXHIGH*" in plan.instrument_patterns
    assert "KXCPI*" in plan.instrument_patterns
    assert "quote" in plan.event_kinds
    assert "open-meteo" in plan.external_sources


def test_kalshi_normalizer_handles_recorded_ws_payloads() -> None:
    client = KalshiWebSocketClient()
    normalizer = EventNormalizer(kalshi_normalizers())
    envelopes = _recorded_ws_envelopes(client)

    results = [normalizer.normalize(envelope) for envelope in envelopes]
    events = [result.normalized for result in results if result.normalized is not None]

    assert all(result.accepted for result in results)
    assert any(isinstance(event, QuoteEvent) for event in events)
    assert any(isinstance(event, TradeEvent) for event in events)
    assert any(isinstance(event, LifecycleEvent) for event in events)
    book_events = [event for event in events if isinstance(event, OrderBookEvent)]
    assert len(book_events) == 2
    assert book_events[-1].book.yes_bids[0].quantity == Decimal("17.00")


def test_capture_fixture_writes_raw_parquet(tmp_path: Path) -> None:
    count = capture_kalshi_fixture(FIXTURES / "ws_messages.jsonl", tmp_path)

    store = ParquetEventStore(tmp_path)
    envelopes = list(store.read(source="kalshi-ws"))

    assert count == 5
    assert len(envelopes) == 5
    assert (tmp_path / "raw" / "venue=kalshi" / "source=kalshi-ws").exists()


def test_rest_poll_capture_records_books_and_dedupes_recent_trades(tmp_path: Path) -> None:
    async def run() -> None:
        store = ParquetEventStore(tmp_path)
        count = await capture_kalshi_rest_polls(
            _mock_rest_client(),
            store=store,
            tickers=("KXHIGHNY-26MAY24-B75",),
            max_polls=2,
            poll_interval_seconds=0,
            trades_limit=100,
        )

        envelopes = list(store.read(source="kalshi-rest"))

        assert count == 3
        assert [event.channel for event in envelopes].count("book") == 2
        assert [event.channel for event in envelopes].count("trade") == 1
        assert [event.metadata["poll_index"] for event in envelopes if event.channel == "book"] == [0, 1]
        assert any(
            event.metadata.get("trade_key") == "KXHIGHNY-26MAY24-B75:trade_id:trade-rest-1"
            for event in envelopes
        )

    asyncio.run(run())


def test_rest_poll_capture_normalizes_for_backtest_replay(
    capsys: Any,
    tmp_path: Path,
) -> None:
    async def capture() -> None:
        await capture_kalshi_rest_polls(
            _mock_rest_client(),
            store=ParquetEventStore(tmp_path),
            tickers=("KXHIGHNY-26MAY24-B75",),
            max_polls=2,
            poll_interval_seconds=0,
            trades_limit=100,
        )

    asyncio.run(capture())

    rc = cli(["normalize", "--data", str(tmp_path), "--source", "kalshi-rest", "--normalizer", "kalshi"])

    assert rc == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["processed"] == 3
    assert summary["accepted"] == 3
    events = list(ParquetEventStore(tmp_path).read_normalized())
    assert len(events) == 3
    assert sum(isinstance(event, OrderBookEvent) for event in events) == 2
    assert sum(isinstance(event, TradeEvent) for event in events) == 1


def test_capture_cli_fixture_mode_writes_parquet(
    capsys: Any,
    tmp_path: Path,
) -> None:
    rc = cli(
        [
            "capture",
            "--venue",
            "kalshi",
            "--fixture-jsonl",
            str(FIXTURES / "ws_messages.jsonl"),
            "--out",
            str(tmp_path),
        ]
    )

    assert rc == 0
    assert "captured" in capsys.readouterr().out
    assert len(list(ParquetEventStore(tmp_path).read(source="kalshi-ws"))) == 5


def test_normalize_cli_converts_captured_kalshi_raw_to_normalized(
    capsys: Any,
    tmp_path: Path,
) -> None:
    capture_kalshi_fixture(FIXTURES / "ws_messages.jsonl", tmp_path)

    rc = cli(
        [
            "normalize",
            "--data",
            str(tmp_path),
            "--source",
            "kalshi-ws",
            "--normalizer",
            "kalshi",
        ]
    )

    assert rc == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["processed"] == 5
    assert summary["accepted"] == 5
    assert summary["rejected"] == 0
    assert len(list(ParquetEventStore(tmp_path).read_normalized())) == 5


def test_capture_cli_fixture_can_normalize_and_write_manifest(
    capsys: Any,
    tmp_path: Path,
) -> None:
    rc = cli(
        [
            "capture",
            "--venue",
            "kalshi",
            "--fixture-jsonl",
            str(FIXTURES / "ws_messages.jsonl"),
            "--out",
            str(tmp_path),
            "--normalize",
        ]
    )

    assert rc == 0
    assert "manifest" in capsys.readouterr().out
    assert len(list(ParquetEventStore(tmp_path).read(source="kalshi-ws"))) == 5
    assert len(list(ParquetEventStore(tmp_path).read_normalized())) == 5
    manifests = tuple((tmp_path / "manifests").glob("capture-*.json"))
    assert len(manifests) == 1
    manifest = json.loads(manifests[0].read_text())
    assert manifest["captured"] == 5
    assert manifest["started_at"]
    assert manifest["ended_at"]
    assert manifest["code_version"]
    assert manifest["output_root"] == str(tmp_path)
    assert manifest["normalization"]["accepted"] == 5


def test_normalize_cli_persists_reject_partition(
    capsys: Any,
    tmp_path: Path,
) -> None:
    store = ParquetEventStore(tmp_path)
    store.append(
        EventEnvelope(
            venue=Venue.KALSHI,
            source="kalshi-ws",
            channel="unknown_channel",
            received_at=datetime(2026, 5, 24, 12, 0, tzinfo=UTC),
            exchange_ts=None,
            payload={"market_ticker": "KXUNKNOWN-1"},
            schema_version="kalshi-ws-v1",
        )
    )
    store.flush()

    rc = cli(
        [
            "normalize",
            "--data",
            str(tmp_path),
            "--source",
            "kalshi-ws",
            "--normalizer",
            "kalshi",
        ]
    )

    assert rc == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["processed"] == 1
    assert summary["accepted"] == 0
    assert summary["rejected"] == 1
    rejects = list(ParquetEventStore(tmp_path).read_normalization_rejects(source="kalshi-ws"))
    assert len(rejects) == 1
    assert rejects[0].raw.channel == "unknown_channel"
    assert "no normalizer" in rejects[0].reasons[0]
    assert (tmp_path / "normalization_rejects" / "venue=kalshi" / "source=kalshi-ws").exists()


def test_inspect_data_cli_reports_raw_normalized_and_reject_counts(
    capsys: Any,
    tmp_path: Path,
) -> None:
    capture_kalshi_fixture(FIXTURES / "ws_messages.jsonl", tmp_path)
    cli(["normalize", "--data", str(tmp_path), "--source", "kalshi-ws", "--normalizer", "kalshi"])
    capsys.readouterr()

    rc = cli(["inspect-data", "--data", str(tmp_path), "--source", "kalshi-ws"])

    assert rc == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["raw_count"] == 5
    assert summary["normalized_count"] == 5
    assert summary["reject_count"] == 0
    assert summary["raw_by_source"] == {"kalshi-ws": 5}
    assert summary["normalized_by_kind"]["book"] == 2


def _recorded_ws_envelopes(client: KalshiWebSocketClient) -> list[Any]:
    envelopes: list[Any] = []
    for line in (FIXTURES / "ws_messages.jsonl").read_text().splitlines():
        envelope = client.message_to_envelope(json.loads(line))
        if envelope is not None:
            envelopes.append(envelope)
    return envelopes


def _mock_rest_client() -> KalshiPublicClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/portfolio/balance"):
            assert request.url.params.get("subaccount") == "2"
            return httpx.Response(
                200,
                json={
                    "balance": 12345,
                    "balance_dollars": "123.45",
                    "portfolio_value": 14567,
                    "updated_ts": 1779876000,
                },
            )
        if request.url.path.endswith("/markets") and request.url.params.get("cursor") == "next-page":
            return httpx.Response(200, json=_fixture_json("rest_markets_page_2.json"))
        if request.url.path.endswith("/markets"):
            return httpx.Response(200, json=_fixture_json("rest_markets_page_1.json"))
        if request.url.path.endswith("/orderbook"):
            return httpx.Response(200, json=_fixture_json("rest_orderbook.json"))
        if request.url.path.endswith("/markets/trades"):
            return httpx.Response(200, json=_fixture_json("rest_trades.json"))
        return httpx.Response(404, json={"error": "not found"})

    return KalshiPublicClient(
        base_url="https://example.test/trade-api/v2",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


def _fixture_json(name: str) -> dict[str, Any]:
    loaded = json.loads((FIXTURES / name).read_text())
    if not isinstance(loaded, dict):
        raise TypeError(f"fixture must be a JSON object: {name}")
    return loaded


class _FailOnceConnector:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, url: str, headers: Mapping[str, str]) -> Any:
        self.calls += 1
        if self.calls == 1:
            return _FailingWebSocketContext()
        return _WebSocketContext(
            (
                json.dumps(
                    {
                        "type": "ticker",
                        "sid": 1,
                        "seq": 1,
                        "msg": {
                            "market_ticker": "KXHIGHNY-26MAY24-B75",
                            "yes_bid_dollars": "0.45",
                            "yes_ask_dollars": "0.53",
                            "ts_ms": 1779631200000,
                        },
                    }
                ),
            )
        )


class _FailingWebSocketContext:
    async def __aenter__(self) -> Any:
        raise RuntimeError("connect failed")

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None


class _WebSocketContext:
    def __init__(self, messages: tuple[str, ...]) -> None:
        self.websocket = _FakeWebSocket(messages)

    async def __aenter__(self) -> Any:
        return self.websocket

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None


class _FakeWebSocket:
    def __init__(self, messages: tuple[str, ...]) -> None:
        self.messages = list(messages)
        self.sent: list[str] = []

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def recv(self) -> str:
        if not self.messages:
            raise RuntimeError("no more messages")
        return self.messages.pop(0)
