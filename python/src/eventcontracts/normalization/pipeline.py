"""Raw-envelope to strategy-event normalization pipeline."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass

from eventcontracts.domain.events import NormalizedEvent
from eventcontracts.domain.serialization import canonical_sha256
from eventcontracts.storage.interfaces import (
    EventEnvelope,
    EventStore,
    NormalizationReject,
    NormalizationRejectStore,
    NormalizedEventStore,
)

NormalizedBatch = NormalizedEvent | tuple[NormalizedEvent, ...]
NormalizeFn = Callable[[EventEnvelope], NormalizedBatch]
NormalizerKey = tuple[str, str]


@dataclass(frozen=True)
class NormalizationResult:
    """Audit record for one raw-to-normalized handoff."""

    raw: EventEnvelope
    normalized: NormalizedEvent | None
    accepted: bool
    reasons: tuple[str, ...] = ()
    normalized_events: tuple[NormalizedEvent, ...] = ()

    def __post_init__(self) -> None:
        if self.normalized_events:
            return
        if self.normalized is not None:
            object.__setattr__(self, "normalized_events", (self.normalized,))


class EventNormalizer:
    """Dispatch raw envelopes to schema/channel-specific parser functions.

    Handoff:
    ``storage.EventEnvelope`` comes from the raw store.
    A registered parser converts it into a ``domain.NormalizedEvent`` variant.
    The normalized event is what strategies see during replay/live operation.
    """

    def __init__(self, handlers: Mapping[NormalizerKey, NormalizeFn]) -> None:
        self.handlers = dict(handlers)

    def normalize(self, raw: EventEnvelope) -> NormalizationResult:
        key = (raw.schema_version, raw.channel)
        handler = self.handlers.get(key)
        if handler is None:
            return NormalizationResult(
                raw=raw,
                normalized=None,
                accepted=False,
                reasons=(f"no normalizer for {key}",),
            )
        try:
            normalized = handler(raw)
        except Exception as exc:
            return NormalizationResult(
                raw=raw,
                normalized=None,
                accepted=False,
                reasons=(f"normalization failed: {type(exc).__name__}: {exc}",),
            )
        events = normalized if isinstance(normalized, tuple) else (normalized,)
        if not events:
            return NormalizationResult(
                raw=raw,
                normalized=None,
                accepted=False,
                reasons=("normalizer emitted no events",),
            )
        return NormalizationResult(
            raw=raw,
            normalized=events[0],
            accepted=True,
            normalized_events=events,
        )


class NormalizationPipeline:
    """Move raw persisted envelopes into the normalized event store."""

    def __init__(
        self,
        raw_store: EventStore,
        normalized_store: NormalizedEventStore,
        normalizer: EventNormalizer,
        reject_store: NormalizationRejectStore | None = None,
    ) -> None:
        self.raw_store = raw_store
        self.normalized_store = normalized_store
        self.normalizer = normalizer
        self.reject_store = reject_store

    def run(self, *, source: str = "*") -> tuple[NormalizationResult, ...]:
        results: list[NormalizationResult] = []
        for raw in self.raw_store.read(source):
            result = self.normalizer.normalize(raw)
            results.append(result)
            if result.normalized_events:
                for event in result.normalized_events:
                    self.normalized_store.append_normalized(event)
            elif self.reject_store is not None:
                self.reject_store.append_normalization_reject(
                    NormalizationReject(
                        raw=raw,
                        reasons=result.reasons,
                        raw_sha256=canonical_sha256(raw),
                    )
                )
        return tuple(results)


def normalize_all(
    envelopes: Iterable[EventEnvelope],
    normalizer: EventNormalizer,
) -> tuple[NormalizationResult, ...]:
    """Pure helper for tests and offline research notebooks."""

    return tuple(normalizer.normalize(envelope) for envelope in envelopes)
