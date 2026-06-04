from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from eventcontracts.research.ledger import read_jsonl, stable_hash, write_jsonl
from eventcontracts.research.timing import (
    EdgeEvaluation,
    MarketQuoteSnapshot,
    ModelValuation,
    SourceStamp,
    age_ms,
    is_stale,
)

NOW = datetime(2026, 6, 2, 12, 0, tzinfo=UTC)


def test_age_and_stale_source_stamp() -> None:
    source_ts = NOW - timedelta(milliseconds=250)

    stamp = SourceStamp.from_timestamps(
        source="fixture",
        source_ts=source_ts,
        received_at=NOW,
        max_age_ms=200,
        sequence="42",
    )

    assert age_ms(source_ts, NOW) == pytest.approx(250)
    assert is_stale(stamp.raw_age_ms, 200)
    assert stamp.stale
    assert stamp.stale_reason == "age_ms>200"


def test_market_quote_snapshot_mid_and_spread() -> None:
    quote = MarketQuoteSnapshot(
        venue="kalshi",
        market_id="KXDEMO",
        ticker="KXDEMO",
        received_at=NOW,
        yes_bid=Decimal("0.42"),
        yes_ask=Decimal("0.47"),
    )

    assert quote.yes_mid == Decimal("0.445")
    assert quote.yes_spread == Decimal("0.05")


def test_model_valuation_and_edge_round_trip(tmp_path: Path) -> None:
    valuation = ModelValuation(
        model_id="demo",
        schema_version="demo-v1",
        market_id="KXDEMO",
        as_of=NOW,
        fair_yes=Decimal("0.62"),
        fair_no=Decimal("0.38"),
        confidence=Decimal("0.70"),
        feature_hash=stable_hash({"x": 1}),
        feature_payload={"x": 1},
    )
    edge = EdgeEvaluation(
        market_id="KXDEMO",
        as_of=NOW,
        side="YES",
        fair_price=Decimal("0.62"),
        executable_price=Decimal("0.55"),
        raw_edge=Decimal("0.07"),
        fee=Decimal("0.02"),
        spread_cost=Decimal("0.01"),
        net_edge=Decimal("0.04"),
        candidate=True,
        reason="edge_after_costs",
    )

    path = tmp_path / "ledger.jsonl"
    write_jsonl(path, [valuation, edge])
    rows = read_jsonl(path)

    assert rows[0]["fair_yes"] == "0.62"
    assert rows[0]["as_of"] == NOW.isoformat()
    assert rows[1]["candidate"] is True
    assert rows[1]["net_edge"] == "0.04"


def test_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        SourceStamp(source="bad", source_ts=None, received_at=datetime(2026, 6, 2, 12, 0))
