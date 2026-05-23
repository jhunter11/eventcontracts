"""Ingestion pipeline boundaries."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from eventcontracts.domain.models import Venue


@dataclass(frozen=True)
class IngestionJob:
    name: str
    venue: Venue | None
    source: str
    starts_at: datetime | None = None
    ends_at: datetime | None = None


class IngestionPipeline:
    """Runs raw capture jobs and writes raw event envelopes."""

    def run(self, job: IngestionJob) -> None:
        raise NotImplementedError
