"""Feature schema and store tests."""

from __future__ import annotations

from datetime import UTC, datetime

from eventcontracts.audit import AuditStamp, audit_stamp_for
from eventcontracts.domain import FeatureSchemaId, FeatureVector
from eventcontracts.features import InMemoryFeatureStore

NOW = datetime(2026, 1, 1, tzinfo=UTC)


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


def _audit(object_id: str, vector: FeatureVector) -> AuditStamp:
    return audit_stamp_for(
        vector,
        object_id=object_id,
        object_kind="feature_vector",
        schema_version="feature-vector-v1",
        produced_at=vector.timestamp,
        producer="test",
    )
