"""Cross-venue normalization workflows."""

from __future__ import annotations

from eventcontracts.normalization.contracts import ContractMatchCandidate, ContractMatchDecision


class CrossVenueSpreadUniverseBuilder:
    """Builds an auditable universe of accepted and rejected spread candidates."""

    def build(self) -> list[ContractMatchDecision]:
        raise NotImplementedError

    def reject(self, candidate: ContractMatchCandidate, *reasons: str) -> ContractMatchDecision:
        return ContractMatchDecision(candidate=candidate, accepted=False, reasons=tuple(reasons))
