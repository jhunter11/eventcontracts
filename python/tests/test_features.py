"""Feature schema and store tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from eventcontracts.audit import AuditStamp, audit_stamp_for
from eventcontracts.domain import FeatureSchemaId, FeatureVector
from eventcontracts.domain.events import QuoteEvent
from eventcontracts.domain.ids import EventId
from eventcontracts.domain.models import (
    InstrumentId,
    OrderBookLevel,
    OutcomeSide,
    Quote,
    Venue,
)
from eventcontracts.features import InMemoryFeatureStore
from eventcontracts.features.builders import RollingMidVwapImbalanceBuilder

NOW = datetime(2026, 1, 1, tzinfo=UTC)
INSTR = InstrumentId(venue=Venue.KALSHI, market_id="M-1")


def _vector(name: str, timestamp: datetime) -> FeatureVector:
    return FeatureVector(
        schema_id=FeatureSchemaId("schema-a"),
        schema_version="1",
        instrument_id=None,
        timestamp=timestamp,
        values=((name, 1.0),),
    )


def test_in_memory_feature_store_returns_latest_and_history() -> None:
    first = _vector("x", NOW)
    second = _vector("x", NOW.replace(hour=1))
    store = InMemoryFeatureStore()
    store.write_vector(first, _audit("feature-1", first))
    store.write_vector(second, _audit("feature-2", second))

    assert store.latest(FeatureSchemaId("schema-a"), None) == second
    assert store.history(
        FeatureSchemaId("schema-a"),
        None,
        NOW,
        NOW.replace(hour=1),
    ) == (first, second)
    audit = store.audit_for(second)
    assert audit is not None
    assert audit.object_id == "feature-2"


def test_feature_vector_clamps_probability_semantic_features() -> None:
    with pytest.raises(ValueError, match="probability feature"):
        FeatureVector(
            schema_id=FeatureSchemaId("schema"),
            schema_version="v1",
            instrument_id=None,
            timestamp=NOW,
            values=(("win_probability", 1.1),),
        )

    vector = FeatureVector(
        schema_id=FeatureSchemaId("schema"),
        schema_version="v1",
        instrument_id=None,
        timestamp=NOW,
        values=(("win_probability", 1.0), ("implied_prob_diff", -0.25)),
    )
    assert vector.to_dict()["win_probability"] == 1.0


def test_rolling_builder_state_can_resume_in_a_fresh_instance() -> None:
    builder = RollingMidVwapImbalanceBuilder(
        schema_id=FeatureSchemaId("rolling-a"),
        ewma_half_life_seconds=5,
    )
    first = builder.update(builder.warmup(()), _quote("0.40", "0.60", NOW))

    resumed = RollingMidVwapImbalanceBuilder(
        schema_id=FeatureSchemaId("rolling-a"),
        ewma_half_life_seconds=5,
    )
    second = resumed.update(first, _quote("0.70", "0.90", NOW + timedelta(seconds=5)))

    assert second.vector is not None
    assert second.vector.get("mid_ewma") == pytest.approx(0.65)
    assert second.builder_state


def test_rolling_builder_ewma_uses_elapsed_time_not_tick_count() -> None:
    builder = RollingMidVwapImbalanceBuilder(
        schema_id=FeatureSchemaId("rolling-a"),
        ewma_half_life_seconds=5,
    )
    state = builder.update(builder.warmup(()), _quote("0.40", "0.60", NOW))

    same_instant = builder.update(state, _quote("0.70", "0.90", NOW))
    assert same_instant.vector is not None
    assert same_instant.vector.get("mid_ewma") == pytest.approx(0.50)

    later = builder.update(state, _quote("0.70", "0.90", NOW + timedelta(seconds=5)))
    assert later.vector is not None
    assert later.vector.get("mid_ewma") == pytest.approx(0.65)


def _audit(object_id: str, vector: FeatureVector) -> AuditStamp:
    return audit_stamp_for(
        vector,
        object_id=object_id,
        object_kind="feature_vector",
        schema_version="feature-vector-v1",
        produced_at=vector.timestamp,
        producer="test",
    )


def _quote(bid: str, ask: str, timestamp: datetime) -> QuoteEvent:
    return QuoteEvent(
        event_id=EventId(f"q-{bid}-{ask}-{timestamp.isoformat()}"),
        quote=Quote(
            instrument_id=INSTR,
            side=OutcomeSide.YES,
            bid=OrderBookLevel(price=Decimal(bid), quantity=Decimal("10")),
            ask=OrderBookLevel(price=Decimal(ask), quantity=Decimal("10")),
            exchange_ts=timestamp,
            received_at=timestamp,
        ),
    )
