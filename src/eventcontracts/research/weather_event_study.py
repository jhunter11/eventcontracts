"""Kalshi weather event-study research program."""

from __future__ import annotations

from eventcontracts.research.base import ResearchProgram, ResearchResult


class KalshiWeatherEventStudy(ResearchProgram):
    name = "kalshi_weather_event_study"

    def run(self) -> ResearchResult:
        raise NotImplementedError
