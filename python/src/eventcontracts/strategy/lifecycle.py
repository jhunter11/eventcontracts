"""Strategy lifecycle state machine.

The runner owns transitions. Strategies observe their current state through
context but cannot drive transitions directly — they request changes by
emitting decisions (e.g. an ``Alert`` of severity ``ERROR`` may trigger the
runner to drain).
"""

from __future__ import annotations

from enum import Enum


class StrategyState(str, Enum):
    CREATED = "created"
    INITIALIZING = "initializing"
    WARMING_UP = "warming_up"
    READY = "ready"
    RUNNING = "running"
    DRAINING = "draining"
    HALTED = "halted"
    DISPOSED = "disposed"


VALID_TRANSITIONS: dict[StrategyState, frozenset[StrategyState]] = {
    StrategyState.CREATED: frozenset({StrategyState.INITIALIZING, StrategyState.DISPOSED}),
    StrategyState.INITIALIZING: frozenset(
        {StrategyState.WARMING_UP, StrategyState.READY, StrategyState.HALTED}
    ),
    StrategyState.WARMING_UP: frozenset({StrategyState.READY, StrategyState.HALTED}),
    StrategyState.READY: frozenset({StrategyState.RUNNING, StrategyState.HALTED}),
    StrategyState.RUNNING: frozenset(
        {StrategyState.DRAINING, StrategyState.HALTED}
    ),
    StrategyState.DRAINING: frozenset({StrategyState.DISPOSED, StrategyState.HALTED}),
    StrategyState.HALTED: frozenset({StrategyState.DISPOSED}),
    StrategyState.DISPOSED: frozenset(),
}


def can_transition(current: StrategyState, target: StrategyState) -> bool:
    return target in VALID_TRANSITIONS[current]
