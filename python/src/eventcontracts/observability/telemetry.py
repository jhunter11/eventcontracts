"""Observability boundary scaffolds."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from eventcontracts.audit import AuditStamp
from eventcontracts.domain.metadata import FrozenMap, freeze_mapping
from eventcontracts.domain.validation import require_aware_datetime, require_non_empty


class HealthStatus(str, Enum):
    OK = "ok"
    DEGRADED = "degraded"
    FAILING = "failing"


@dataclass(frozen=True)
class AlertEvent:
    name: str
    severity: str
    message: str
    occurred_at: datetime
    audit: AuditStamp
    labels: Mapping[str, str] = field(default_factory=FrozenMap)

    def __post_init__(self) -> None:
        require_non_empty(self.name, "name")
        require_non_empty(self.severity, "severity")
        require_non_empty(self.message, "message")
        require_aware_datetime(self.occurred_at, "occurred_at")
        object.__setattr__(self, "labels", freeze_mapping(self.labels))


@dataclass(frozen=True)
class TraceSpan:
    trace_id: str
    span_id: str
    name: str
    started_at: datetime
    ended_at: datetime | None = None

    def __post_init__(self) -> None:
        require_non_empty(self.trace_id, "trace_id")
        require_non_empty(self.span_id, "span_id")
        require_non_empty(self.name, "name")
        require_aware_datetime(self.started_at, "started_at")
        if self.ended_at is not None:
            require_aware_datetime(self.ended_at, "ended_at")


class StructuredLogger:
    """Emit structured logs with domain provenance."""

    def info(self, event: str, **fields: object) -> None:
        raise NotImplementedError

    def warning(self, event: str, **fields: object) -> None:
        raise NotImplementedError

    def error(self, event: str, **fields: object) -> None:
        raise NotImplementedError


class MetricsRecorder:
    """Record counters, gauges, and latency histograms."""

    def increment(self, name: str, *, labels: Mapping[str, str] | None = None) -> None:
        raise NotImplementedError

    def gauge(
        self, name: str, value: float, *, labels: Mapping[str, str] | None = None
    ) -> None:
        raise NotImplementedError

    def observe(
        self, name: str, value: float, *, labels: Mapping[str, str] | None = None
    ) -> None:
        raise NotImplementedError


class Tracer:
    """Create trace spans around cross-layer handoffs."""

    def start_span(self, name: str) -> TraceSpan:
        raise NotImplementedError

    def end_span(self, span: TraceSpan) -> TraceSpan:
        raise NotImplementedError


class HealthCheck:
    """Report health for capture, replay, runner, gateway, and storage services."""

    def check(self) -> HealthStatus:
        raise NotImplementedError

    def details(self) -> Mapping[str, str]:
        raise NotImplementedError
