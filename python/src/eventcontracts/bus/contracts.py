"""Bus and IPC boundaries."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime

from eventcontracts.audit import AuditStamp
from eventcontracts.domain.decisions import IntentEnvelope
from eventcontracts.domain.events import NormalizedEvent
from eventcontracts.domain.metadata import FrozenMap, freeze_mapping
from eventcontracts.domain.validation import require_aware_datetime, require_non_empty

TopicName = str


@dataclass(frozen=True)
class TopicSpec:
    name: TopicName
    schema_version: str
    description: str

    def __post_init__(self) -> None:
        require_non_empty(self.name, "topic name")
        require_non_empty(self.schema_version, "schema_version")


@dataclass(frozen=True)
class BusMessage:
    topic: TopicName
    key: str
    payload: bytes
    schema_version: str
    published_at: datetime
    audit: AuditStamp
    headers: Mapping[str, str] = field(default_factory=FrozenMap)

    def __post_init__(self) -> None:
        require_non_empty(self.topic, "topic")
        require_non_empty(self.key, "key")
        require_non_empty(self.schema_version, "schema_version")
        require_aware_datetime(self.published_at, "published_at")
        object.__setattr__(self, "headers", freeze_mapping(self.headers))


class MessageCodec:
    """Serialize domain messages for cross-process topics."""

    def encode_event(self, event: NormalizedEvent, topic: TopicSpec) -> BusMessage:
        raise NotImplementedError

    def encode_intent(self, envelope: IntentEnvelope, topic: TopicSpec) -> BusMessage:
        raise NotImplementedError

    def decode_event(self, message: BusMessage) -> NormalizedEvent:
        raise NotImplementedError

    def decode_intent(self, message: BusMessage) -> IntentEnvelope:
        raise NotImplementedError


class BusPublisher:
    """Publish canonical messages to IPC topics."""

    def publish_event(self, event: NormalizedEvent) -> None:
        raise NotImplementedError

    def publish_intent(self, envelope: IntentEnvelope) -> None:
        raise NotImplementedError

    def publish_many(self, messages: Iterable[BusMessage]) -> int:
        raise NotImplementedError


class BusSubscriber:
    """Subscribe to topic streams and manage acknowledgements."""

    def subscribe(self, topic: TopicName) -> Iterator[BusMessage]:
        raise NotImplementedError

    def ack(self, message: BusMessage) -> None:
        raise NotImplementedError

    def nack(self, message: BusMessage, reason: str) -> None:
        raise NotImplementedError


class SchemaRegistry:
    """Registry for topic schema versions."""

    def register_topic(self, topic: TopicSpec) -> None:
        raise NotImplementedError

    def get_topic(self, name: TopicName) -> TopicSpec:
        raise NotImplementedError

    def validate(self, message: BusMessage) -> None:
        raise NotImplementedError
