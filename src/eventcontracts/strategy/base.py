"""The ``Strategy`` contract.

This is the only surface a researcher implements. Everything else — data
capture, normalization, replay, risk, OMS, settlement, observability — is
the runner's responsibility.

The contract is intentionally small:

* ``on_init``      — one-time setup with access to the context.
* ``on_event``     — called for every normalized event matching the spec's
                     subscription. Returns a sequence of decisions.
* ``on_shutdown``  — clean exit hook for releasing any external state.
* ``snapshot`` / ``restore`` — opaque bytes for restartable runners.

Strategies are pure with respect to the framework: they take ``(event, ctx)``
and return decisions. They do not call venue clients, the bus, or storage
directly. That keeps backtests, paper, and live identical at the strategy
boundary.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import Callable, Protocol, runtime_checkable

from eventcontracts.domain.decisions import StrategyDecision
from eventcontracts.domain.events import NormalizedEvent
from eventcontracts.domain.spec import StrategySpec
from eventcontracts.strategy.context import StrategyContext


@runtime_checkable
class Strategy(Protocol):
    spec: StrategySpec

    def on_init(self, ctx: StrategyContext) -> None: ...

    def on_event(
        self, event: NormalizedEvent, ctx: StrategyContext
    ) -> Sequence[StrategyDecision]: ...

    def on_shutdown(self, ctx: StrategyContext) -> None: ...

    def snapshot(self) -> bytes: ...

    def restore(self, state: bytes) -> None: ...


class StrategyBase(ABC):
    """Default implementations for everything except ``on_event``.

    Concrete strategies override only what they need. ``on_event`` is the
    only required method.
    """

    def __init__(self, spec: StrategySpec) -> None:
        self.spec = spec

    def on_init(self, ctx: StrategyContext) -> None:
        return None

    @abstractmethod
    def on_event(
        self, event: NormalizedEvent, ctx: StrategyContext
    ) -> Sequence[StrategyDecision]: ...

    def on_shutdown(self, ctx: StrategyContext) -> None:
        return None

    def snapshot(self) -> bytes:
        return b""

    def restore(self, state: bytes) -> None:
        return None


StrategyFactory = Callable[[StrategySpec], Strategy]
