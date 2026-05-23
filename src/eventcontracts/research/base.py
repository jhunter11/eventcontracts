"""Research program interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class ResearchResult:
    program_name: str
    metrics: dict[str, float]
    artifacts: dict[str, str]
    notes: tuple[str, ...] = ()


class ResearchProgram(ABC):
    name: str

    @abstractmethod
    def run(self) -> ResearchResult:
        """Run the research program and return reproducible result metadata."""
