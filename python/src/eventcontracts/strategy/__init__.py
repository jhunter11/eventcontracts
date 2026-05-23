"""Strategy contract: the only surface a researcher needs to implement."""

from eventcontracts.strategy.base import Strategy, StrategyBase, StrategyFactory
from eventcontracts.strategy.context import StrategyContext
from eventcontracts.strategy.lifecycle import VALID_TRANSITIONS, StrategyState
from eventcontracts.strategy.registry import (
    ENTRY_POINT_GROUP,
    StrategyRegistry,
    create,
    known,
    load_entry_points,
    register,
    registry,
)

__all__ = [
    "ENTRY_POINT_GROUP",
    "Strategy",
    "StrategyBase",
    "StrategyContext",
    "StrategyFactory",
    "StrategyRegistry",
    "StrategyState",
    "VALID_TRANSITIONS",
    "create",
    "known",
    "load_entry_points",
    "register",
    "registry",
]
