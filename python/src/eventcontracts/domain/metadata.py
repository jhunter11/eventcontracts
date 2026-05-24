"""Immutable mapping helpers for domain metadata.

Domain objects are frozen dataclasses. Plain ``dict`` fields would still leave
their contents mutable and make otherwise frozen objects unhashable, so metadata
is normalized into this small hashable mapping at construction time.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from typing import Any


class FrozenMap(Mapping[str, Any]):
    """A hashable, recursively frozen string-keyed mapping."""

    __slots__ = ("_hash", "_items")

    def __init__(
        self, values: Mapping[str, Any] | Iterable[tuple[str, Any]] | None = None
    ) -> None:
        raw_items = () if values is None else values.items() if isinstance(values, Mapping) else values
        self._items = tuple(
            sorted(((str(key), freeze_value(value)) for key, value in raw_items), key=lambda item: item[0])
        )
        self._hash = hash(self._items)

    def __getitem__(self, key: str) -> Any:
        for item_key, value in self._items:
            if item_key == key:
                return value
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        return (key for key, _ in self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __hash__(self) -> int:
        return self._hash

    def __repr__(self) -> str:
        return f"FrozenMap({dict(self._items)!r})"

    def items_tuple(self) -> tuple[tuple[str, Any], ...]:
        return self._items


def freeze_value(value: Any) -> Any:
    """Recursively convert mutable containers into hashable equivalents."""

    if isinstance(value, FrozenMap):
        return value
    if isinstance(value, Mapping):
        return FrozenMap(value)
    if isinstance(value, tuple):
        return tuple(freeze_value(item) for item in value)
    if isinstance(value, list):
        return tuple(freeze_value(item) for item in value)
    if isinstance(value, set | frozenset):
        return tuple(sorted((freeze_value(item) for item in value), key=repr))
    return value


def freeze_mapping(values: Mapping[Any, Any] | None) -> FrozenMap:
    return FrozenMap(values)


def thaw_value(value: Any) -> Any:
    """Convert frozen metadata back into JSON-friendly Python containers."""

    if isinstance(value, FrozenMap):
        return {key: thaw_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw_value(item) for item in value]
    return value
