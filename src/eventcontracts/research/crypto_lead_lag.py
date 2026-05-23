"""Polymarket crypto lead-lag research program."""

from __future__ import annotations

from eventcontracts.research.base import ResearchProgram, ResearchResult


class PolymarketCryptoLeadLag(ResearchProgram):
    name = "polymarket_crypto_lead_lag"

    def run(self) -> ResearchResult:
        raise NotImplementedError
