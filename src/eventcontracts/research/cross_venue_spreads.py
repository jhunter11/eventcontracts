"""Cross-venue spread normalization research program."""

from __future__ import annotations

from eventcontracts.research.base import ResearchProgram, ResearchResult


class CrossVenueSpreadResearch(ResearchProgram):
    name = "cross_venue_spread_normalization"

    def run(self) -> ResearchResult:
        raise NotImplementedError
