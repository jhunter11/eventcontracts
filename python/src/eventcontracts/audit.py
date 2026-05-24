"""Auditability contracts for cross-layer data handoffs.

The framework intentionally passes typed domain objects between layers, but
every durable or cross-process handoff should also have a small audit record
that can answer:

* where did this object come from?
* what schema/version described it?
* which upstream object caused it?
* what exact canonical bytes were used for parity or replay?

This module is deliberately dependency-free and reusable by ingestion,
normalization, model, gateway, and artifact code.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from eventcontracts.domain.metadata import FrozenMap, freeze_mapping
from eventcontracts.domain.serialization import canonical_sha256
from eventcontracts.domain.validation import require_aware_datetime, require_non_empty


@dataclass(frozen=True)
class AuditStamp:
    """Stable provenance record attached to a cross-layer handoff."""

    object_id: str
    object_kind: str
    schema_version: str
    produced_at: datetime
    producer: str
    canonical_sha256: str
    parent_ids: tuple[str, ...] = ()
    trace_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=FrozenMap)

    def __post_init__(self) -> None:
        require_non_empty(self.object_id, "object_id")
        require_non_empty(self.object_kind, "object_kind")
        require_non_empty(self.schema_version, "schema_version")
        require_aware_datetime(self.produced_at, "produced_at")
        require_non_empty(self.producer, "producer")
        require_non_empty(self.canonical_sha256, "canonical_sha256")
        if len(self.canonical_sha256) != 64:
            raise ValueError("canonical_sha256 must be a hex sha256 digest")
        for parent_id in self.parent_ids:
            require_non_empty(parent_id, "parent_id")
        if self.trace_id is not None:
            require_non_empty(self.trace_id, "trace_id")
        object.__setattr__(self, "parent_ids", tuple(self.parent_ids))
        object.__setattr__(self, "metadata", freeze_mapping(self.metadata))


@dataclass(frozen=True)
class AuditLink:
    """Parent-to-child lineage edge for replay and incident reconstruction."""

    parent_id: str
    child_id: str
    relation: str

    def __post_init__(self) -> None:
        require_non_empty(self.parent_id, "parent_id")
        require_non_empty(self.child_id, "child_id")
        require_non_empty(self.relation, "relation")


class AuditTrail:
    """Append-only audit store boundary."""

    def append_stamp(self, stamp: AuditStamp) -> None:
        raise NotImplementedError

    def append_link(self, link: AuditLink) -> None:
        raise NotImplementedError

    def lineage(self, object_id: str) -> Sequence[AuditLink]:
        raise NotImplementedError

    def get_stamp(self, object_id: str) -> AuditStamp | None:
        raise NotImplementedError


@dataclass(frozen=True)
class AuditValidationIssue:
    """A concrete audit-chain problem found before replay or promotion."""

    object_id: str
    reason: str

    def __post_init__(self) -> None:
        require_non_empty(self.object_id, "object_id")
        require_non_empty(self.reason, "reason")


class AuditTrailValidationError(ValueError):
    """Raised when an audit chain is incomplete or internally inconsistent."""

    def __init__(self, issues: Sequence[AuditValidationIssue]) -> None:
        self.issues = tuple(issues)
        message = "; ".join(
            f"{issue.object_id}: {issue.reason}" for issue in self.issues
        )
        super().__init__(message or "audit trail validation failed")


class InMemoryAuditTrail(AuditTrail):
    """Small deterministic audit store for tests, replay, and local validation."""

    def __init__(self) -> None:
        self._stamps: dict[str, AuditStamp] = {}
        self._links_by_child: dict[str, list[AuditLink]] = {}

    def append_stamp(self, stamp: AuditStamp) -> None:
        current = self._stamps.get(stamp.object_id)
        if current is not None and current != stamp:
            raise ValueError(f"conflicting audit stamp for {stamp.object_id}")
        self._stamps[stamp.object_id] = stamp

    def append_link(self, link: AuditLink) -> None:
        links = self._links_by_child.setdefault(link.child_id, [])
        if link not in links:
            links.append(link)

    def lineage(self, object_id: str) -> Sequence[AuditLink]:
        require_non_empty(object_id, "object_id")
        return tuple(self._links_by_child.get(object_id, ()))

    def get_stamp(self, object_id: str) -> AuditStamp | None:
        require_non_empty(object_id, "object_id")
        return self._stamps.get(object_id)


class AuditTrailValidator:
    """Validate that an object's upstream audit chain is complete and acyclic."""

    def __init__(self, trail: AuditTrail) -> None:
        self.trail = trail

    def validate_complete_chain(self, object_id: str) -> tuple[AuditStamp, ...]:
        """Return upstream stamps if every parent can be explained.

        The check is deliberately strict: `AuditStamp.parent_ids` and explicit
        `AuditLink` records must agree, every parent must have its own stamp,
        and cycles are rejected. This catches the class of failures where a
        model, feature vector, order command, or fill can no longer be traced
        back to exact input data.
        """

        issues: list[AuditValidationIssue] = []
        ordered: list[AuditStamp] = []
        visiting: set[str] = set()
        visited: set[str] = set()

        def walk(current_id: str) -> None:
            if current_id in visiting:
                issues.append(AuditValidationIssue(current_id, "cycle in audit lineage"))
                return
            if current_id in visited:
                return

            stamp = self.trail.get_stamp(current_id)
            if stamp is None:
                issues.append(AuditValidationIssue(current_id, "missing audit stamp"))
                return

            visiting.add(current_id)
            links = tuple(self.trail.lineage(current_id))
            linked_parent_ids = tuple(link.parent_id for link in links)

            missing_links = tuple(
                parent_id
                for parent_id in stamp.parent_ids
                if parent_id not in linked_parent_ids
            )
            for parent_id in missing_links:
                issues.append(
                    AuditValidationIssue(
                        current_id,
                        f"missing explicit link from parent {parent_id}",
                    )
                )

            unexpected_links = tuple(
                parent_id
                for parent_id in linked_parent_ids
                if parent_id not in stamp.parent_ids
            )
            for parent_id in unexpected_links:
                issues.append(
                    AuditValidationIssue(
                        current_id,
                        f"link parent {parent_id} absent from stamp parent_ids",
                    )
                )

            for parent_id in stamp.parent_ids:
                walk(parent_id)

            visiting.remove(current_id)
            visited.add(current_id)
            ordered.append(stamp)

        walk(object_id)
        if issues:
            raise AuditTrailValidationError(issues)
        return tuple(ordered)


def audit_stamp_for(
    value: object,
    *,
    object_id: str,
    object_kind: str,
    schema_version: str,
    produced_at: datetime,
    producer: str,
    parent_ids: Sequence[str] = (),
    trace_id: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> AuditStamp:
    """Build an audit stamp from a canonical domain object."""

    return AuditStamp(
        object_id=object_id,
        object_kind=object_kind,
        schema_version=schema_version,
        produced_at=produced_at,
        producer=producer,
        canonical_sha256=canonical_sha256(value),
        parent_ids=tuple(parent_ids),
        trace_id=trace_id,
        metadata=metadata or {},
    )
