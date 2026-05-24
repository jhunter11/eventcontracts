"""Subscription planning from active strategy specs."""

from __future__ import annotations

from dataclasses import dataclass

from eventcontracts.domain.models import Venue
from eventcontracts.domain.spec import StrategySpec


@dataclass(frozen=True)
class EventSubscriptionPlan:
    venues: tuple[Venue, ...]
    instrument_patterns: tuple[str, ...]
    event_kinds: tuple[str, ...]
    external_sources: tuple[str, ...]


class SubscriptionPlanner:
    """Union active strategy subscriptions into one capture request."""

    def plan(self, specs: list[StrategySpec] | tuple[StrategySpec, ...]) -> EventSubscriptionPlan:
        venues: set[Venue] = set()
        patterns: set[str] = set()
        event_kinds: set[str] = set()
        external_sources: set[str] = set()
        for spec in specs:
            venues.update(spec.subscription.venues)
            patterns.update(spec.subscription.instrument_patterns)
            event_kinds.update(spec.subscription.event_kinds)
            external_sources.update(spec.subscription.external_sources)
        return EventSubscriptionPlan(
            venues=tuple(sorted(venues, key=lambda venue: venue.value)),
            instrument_patterns=tuple(sorted(patterns)),
            event_kinds=tuple(sorted(event_kinds)),
            external_sources=tuple(sorted(external_sources)),
        )
