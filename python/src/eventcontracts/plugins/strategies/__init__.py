"""Concrete strategy implementations.

Each module here defines one strategy and registers it via ``@register``.
Importing this package eagerly imports every strategy module so the registry
is populated as a side effect.
"""

from eventcontracts.plugins.strategies import example_threshold

__all__ = ["example_threshold"]
