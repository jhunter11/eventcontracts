"""Audit chain validation tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from eventcontracts.audit import (
    AuditLink,
    AuditTrailValidationError,
    AuditTrailValidator,
    InMemoryAuditTrail,
    audit_stamp_for,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def test_audit_validator_accepts_complete_parent_chain() -> None:
    raw = audit_stamp_for(
        {"payload": {"price": "0.42"}},
        object_id="raw-1",
        object_kind="raw_envelope",
        schema_version="raw-event-v1",
        produced_at=NOW,
        producer="capture.fixture",
    )
    normalized = audit_stamp_for(
        {"kind": "trade", "price": "0.42"},
        object_id="norm-1",
        object_kind="normalized_event",
        schema_version="normalized-event-v1",
        produced_at=NOW,
        producer="normalizer.fixture",
        parent_ids=("raw-1",),
    )

    trail = InMemoryAuditTrail()
    trail.append_stamp(raw)
    trail.append_stamp(normalized)
    trail.append_link(AuditLink(parent_id="raw-1", child_id="norm-1", relation="normalized_from"))

    chain = AuditTrailValidator(trail).validate_complete_chain("norm-1")

    assert tuple(stamp.object_id for stamp in chain) == ("raw-1", "norm-1")


def test_audit_validator_rejects_missing_parent_link() -> None:
    raw = audit_stamp_for(
        {"payload": {"price": "0.42"}},
        object_id="raw-1",
        object_kind="raw_envelope",
        schema_version="raw-event-v1",
        produced_at=NOW,
        producer="capture.fixture",
    )
    normalized = audit_stamp_for(
        {"kind": "trade", "price": "0.42"},
        object_id="norm-1",
        object_kind="normalized_event",
        schema_version="normalized-event-v1",
        produced_at=NOW,
        producer="normalizer.fixture",
        parent_ids=("raw-1",),
    )

    trail = InMemoryAuditTrail()
    trail.append_stamp(raw)
    trail.append_stamp(normalized)

    with pytest.raises(AuditTrailValidationError, match="missing explicit link"):
        AuditTrailValidator(trail).validate_complete_chain("norm-1")


def test_audit_validator_rejects_missing_parent_stamp() -> None:
    normalized = audit_stamp_for(
        {"kind": "trade", "price": "0.42"},
        object_id="norm-1",
        object_kind="normalized_event",
        schema_version="normalized-event-v1",
        produced_at=NOW,
        producer="normalizer.fixture",
        parent_ids=("raw-1",),
    )

    trail = InMemoryAuditTrail()
    trail.append_stamp(normalized)
    trail.append_link(AuditLink(parent_id="raw-1", child_id="norm-1", relation="normalized_from"))

    with pytest.raises(AuditTrailValidationError, match="missing audit stamp"):
        AuditTrailValidator(trail).validate_complete_chain("norm-1")


def test_audit_validator_rejects_parent_produced_after_child() -> None:
    raw = audit_stamp_for(
        {"payload": {"price": "0.42"}},
        object_id="raw-1",
        object_kind="raw_envelope",
        schema_version="raw-event-v1",
        produced_at=NOW + timedelta(seconds=1),
        producer="capture.fixture",
    )
    normalized = audit_stamp_for(
        {"kind": "trade", "price": "0.42"},
        object_id="norm-1",
        object_kind="normalized_event",
        schema_version="normalized-event-v1",
        produced_at=NOW,
        producer="normalizer.fixture",
        parent_ids=("raw-1",),
    )

    trail = InMemoryAuditTrail()
    trail.append_stamp(raw)
    trail.append_stamp(normalized)
    trail.append_link(AuditLink(parent_id="raw-1", child_id="norm-1", relation="normalized_from"))

    with pytest.raises(AuditTrailValidationError, match="produced after child"):
        AuditTrailValidator(trail).validate_complete_chain("norm-1")
