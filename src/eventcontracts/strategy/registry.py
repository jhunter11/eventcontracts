"""Strategy registry.

Each strategy registers itself by name. The runner resolves a ``StrategySpec``
to a concrete strategy through the registry, so adding a new strategy is one
file plus one ``@register("name")`` decorator — no other code paths change.
"""

from __future__ import annotations

from collections.abc import Callable

from eventcontracts.domain.spec import StrategySpec
from eventcontracts.strategy.base import Strategy, StrategyFactory


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
