"""Raw-envelope to strategy-event normalization pipeline."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass

from eventcontracts.domain.events import NormalizedEvent
from eventcontracts.storage.interfaces import (
    EventEnvelope,
    EventStore,
    NormalizedEventStore,
)


NormalizeFn = Callable[[EventEnvelope], NormalizedEvent]
NormalizerKey = tuple[str, str]


@dataclass(frozen=True)
class NormalizationResult:
    """Audit record for one raw-to-normalized handoff."""

    raw: EventEnvelope
    normalized: NormalizedEvent | None
    accepted: bool
    reasons: tuple[str, ...] = ()


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
        return NormalizationResult(raw=raw, normalized=handler(raw), accepted=True)


class NormalizationPipeline:
    """Move raw persisted envelopes into the normalized event store."""

    def __init__(
        self,
        raw_store: EventStore,
        normalized_store: NormalizedEventStore,
        normalizer: EventNormalizer,
    ) -> None:
        self.raw_store = raw_store
        self.normalized_store = normalized_store
        self.normalizer = normalizer

    def run(self, *, source: str = "*") -> tuple[NormalizationResult, ...]:
        results: list[NormalizationResult] = []
        for raw in self.raw_store.read(source):
            result = self.normalizer.normalize(raw)
            results.append(result)
            if result.normalized is not None:
                self.normalized_store.append_normalized(result.normalized)
        return tuple(results)


def normalize_all(
    envelopes: Iterable[EventEnvelope],
    normalizer: EventNormalizer,
) -> tuple[NormalizationResult, ...]:
    """Pure helper for tests and offline research notebooks."""

    return tuple(normalizer.normalize(envelope) for envelope in envelopes)
