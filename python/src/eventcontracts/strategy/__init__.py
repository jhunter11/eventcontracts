"""Strategy contract: the only surface a researcher needs to implement."""

from eventcontracts.strategy.base import Strategy, StrategyBase, StrategyFactory
from eventcontracts.strategy.context import StrategyContext
from eventcontracts.strategy.lifecycle import VALID_TRANSITIONS, StrategyState
from eventcontracts.strategy.registry import (
    BUILTIN_STRATEGY_PACKAGE,
    ENTRY_POINT_GROUP,
    StrategyRegistry,
    create,
    create_from_spec,
    ensure_registered,
    known,
    load_entry_points,
    load_package_strategies,
    register,
    registry,
)

__all__ = [
    "BUILTIN_STRATEGY_PACKAGE",
    "ENTRY_POINT_GROUP",
    "Strategy",
    "StrategyBase",
    "StrategyContext",
    "StrategyFactory",
    "StrategyRegistry",
    "StrategyState",
    "VALID_TRANSITIONS",
    "create",
    "create_from_spec",
    "ensure_registered",
    "known",
    "load_entry_points",
    "load_package_strategies",
    "register",
    "registry",
]
