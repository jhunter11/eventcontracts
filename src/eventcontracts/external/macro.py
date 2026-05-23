"""Macro reference-data adapters."""

from __future__ import annotations


class MacroReferenceDataClient:
    """Placeholder for BLS, FRED/ALFRED, and policy-rate inputs."""

    def fetch_release_calendar(self) -> None:
        raise NotImplementedError
