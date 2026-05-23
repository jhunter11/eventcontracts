"""Contract normalization boundaries."""

from __future__ import annotations

from dataclasses import dataclass, field

from eventcontracts.domain.models import InstrumentId


@dataclass(frozen=True)
class ContractMatchCandidate:
    left: InstrumentId
    right: InstrumentId
    score: float
    evidence: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ContractMatchDecision:
    candidate: ContractMatchCandidate
    accepted: bool
    reasons: tuple[str, ...]


class ContractNormalizer:
    """Accepts or rejects cross-venue contract matches with explicit reasons."""

    def evaluate(self, candidate: ContractMatchCandidate) -> ContractMatchDecision:
        raise NotImplementedError
