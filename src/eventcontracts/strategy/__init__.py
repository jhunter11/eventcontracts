"""Strategy contract: the only surface a researcher needs to implement."""

from eventcontracts.strategy.base import Strategy, StrategyBase, StrategyFactory
from eventcontracts.strategy.context import StrategyContext
from eventcontracts.strategy.lifecycle import VALID_TRANSITIONS, StrategyState
from eventcontracts.strategy.registry import (
    StrategyRegistry,
    create,
    known,
    register,
    registry,
)

__all__ = [
    "Strategy",
    "StrategyBase",
    "StrategyContext",
    "StrategyFactory",
    "StrategyRegistry",
    "StrategyState",
    "VALID_TRANSITIONS",
    "create",
    "known",
    "register",
    "registry",
]
