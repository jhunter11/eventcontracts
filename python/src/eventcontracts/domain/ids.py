"""Typed identifiers used across the framework.

NewType wrappers keep IDs from being silently swapped for each other while
remaining cheap string values that can cross process and language boundaries.
"""

from __future__ import annotations

from typing import NewType

StrategyId = NewType("StrategyId", str)
SleeveId = NewType("SleeveId", str)
RunId = NewType("RunId", str)

ClientOrderId = NewType("ClientOrderId", str)
VenueOrderId = NewType("VenueOrderId", str)
FillId = NewType("FillId", str)

CorrelationId = NewType("CorrelationId", str)
EventId = NewType("EventId", str)

ModelName = NewType("ModelName", str)
ModelVersion = NewType("ModelVersion", str)

FeatureSchemaId = NewType("FeatureSchemaId", str)
