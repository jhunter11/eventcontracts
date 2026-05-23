"""Strategy registry.

Each strategy registers itself by name. The runner resolves a ``StrategySpec``
to a concrete strategy through the registry, so adding a new strategy is one
file plus one ``@register("name")`` decorator — no other code paths change.

Strategies shipped in this package register via eager import (see
``eventcontracts.plugins.strategies``). External packages can attach
strategies through the ``eventcontracts.strategies`` entry-point group;
call :func:`load_entry_points` once at startup to import them.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from importlib import metadata

from eventcontracts.domain.spec import StrategySpec
from eventcontracts.strategy.base import Strategy, StrategyFactory

ENTRY_POINT_GROUP = "eventcontracts.strategies"


class StrategyRegistry:
    def __init__(self) -> None:
        self._factories: dict[str, StrategyFactory] = {}

    def register(self, name: str, factory: StrategyFactory) -> None:
        if name in self._factories:
            raise ValueError(f"strategy already registered: {name}")
        self._factories[name] = factory

    def create(self, name: str, spec: StrategySpec) -> Strategy:
        if name not in self._factories:
            raise KeyError(
                f"strategy not registered: {name} (known: {sorted(self._factories)})"
            )
        return self._factories[name](spec)

    def known(self) -> tuple[str, ...]:
        return tuple(sorted(self._factories))

    def clear(self) -> None:
        self._factories.clear()


registry = StrategyRegistry()


def register(name: str) -> Callable[[StrategyFactory], StrategyFactory]:
    def decorator(factory: StrategyFactory) -> StrategyFactory:
        registry.register(name, factory)
        return factory

    return decorator


def create(name: str, spec: StrategySpec) -> Strategy:
    return registry.create(name, spec)


def known() -> tuple[str, ...]:
    return registry.known()


def load_entry_points() -> tuple[str, ...]:
    """Import every module advertised under ``eventcontracts.strategies``.

    Importing the module triggers any ``@register(...)`` decorators it
    defines. Returns the names of the entry points that were loaded so
    callers can log or diff against the installed set.
    """

    loaded: list[str] = []
    for ep in metadata.entry_points(group=ENTRY_POINT_GROUP):
        importlib.import_module(ep.value)
        loaded.append(ep.name)
    return tuple(loaded)
