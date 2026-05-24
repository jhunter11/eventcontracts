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
import pkgutil
from collections.abc import Callable
from importlib import metadata

from eventcontracts.domain.spec import StrategySpec
from eventcontracts.strategy.base import Strategy, StrategyFactory

ENTRY_POINT_GROUP = "eventcontracts.strategies"
BUILTIN_STRATEGY_PACKAGE = "eventcontracts.plugins.strategies"
BUILTIN_STRATEGIES: dict[str, str] = {
    "example_threshold": "eventcontracts.plugins.strategies.example_threshold",
}


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


def load_package_strategies(package_name: str = BUILTIN_STRATEGY_PACKAGE) -> tuple[str, ...]:
    """Import every strategy module in an installed package.

    This makes repository-local strategies plug-and-play: add a module under
    ``eventcontracts.plugins.strategies``, register a factory with
    ``@register("name")``, and startup discovery imports it without requiring a
    central list edit.
    """

    package = importlib.import_module(package_name)
    package_path = getattr(package, "__path__", None)
    if package_path is None:
        return ()

    loaded: list[str] = []
    for module_info in pkgutil.iter_modules(package_path):
        if module_info.ispkg or module_info.name.startswith("_"):
            continue
        module_name = f"{package_name}.{module_info.name}"
        importlib.import_module(module_name)
        loaded.append(module_info.name)
    return tuple(sorted(loaded))


def load_entry_points() -> tuple[str, ...]:
    """Import built-in and installed modules advertised as strategies.

    Importing the module triggers any ``@register(...)`` decorators it
    defines. Returns the names of the entry points that were loaded so
    callers can log or diff against the installed set.
    """

    loaded: list[str] = []
    for name in load_package_strategies():
        if name not in loaded:
            loaded.append(name)
    for name, module in BUILTIN_STRATEGIES.items():
        importlib.import_module(module)
        if name not in loaded:
            loaded.append(name)
    for ep in metadata.entry_points(group=ENTRY_POINT_GROUP):
        importlib.import_module(ep.value)
        if ep.name not in loaded:
            loaded.append(ep.name)
    return tuple(loaded)


def ensure_registered(name: str) -> None:
    """Load known strategy plug-ins and fail clearly if `name` is unavailable."""

    if name not in registry.known():
        load_entry_points()
    if name not in registry.known():
        raise KeyError(
            f"strategy not registered: {name}; add a module under "
            f"{BUILTIN_STRATEGY_PACKAGE} with @register({name!r}) or install an "
            f"{ENTRY_POINT_GROUP} entry point"
        )


def create_from_spec(spec: StrategySpec) -> Strategy:
    """Create a strategy after loading local and installed plug-ins."""

    ensure_registered(spec.name)
    return create(spec.name, spec)
