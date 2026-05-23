"""Adapters: translate external systems into framework-native types.

Subpackages here own the boundary between the framework and the outside
world: venue clients (REST/WebSocket/FIX), external data providers,
storage backends, and eventual bus/gateway integrations. Strategies and
the runner must not depend on anything in this tree directly; they
interact through the protocols in :mod:`eventcontracts.runner.ports` and
the typed events in :mod:`eventcontracts.domain`.
"""
