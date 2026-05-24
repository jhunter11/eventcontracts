"""Import coverage for broad system-boundary scaffolds."""

from __future__ import annotations


def test_new_scaffold_packages_import() -> None:
    import eventcontracts.allocation as allocation
    import eventcontracts.artifacts as artifacts
    import eventcontracts.bus as bus
    import eventcontracts.contracts as contracts
    import eventcontracts.external as external
    import eventcontracts.features as features
    import eventcontracts.gateway as gateway
    import eventcontracts.ledger as ledger
    import eventcontracts.markets as markets
    import eventcontracts.models as models
    import eventcontracts.observability as observability
    import eventcontracts.oms as oms

    # Reach into each module so import-only smoke is meaningful and mypy
    # doesn't flag the imported callables as "always truthy in bool context".
    assert allocation.Allocator.__name__ == "Allocator"
    assert artifacts.ArtifactBundle.__name__ == "ArtifactBundle"
    assert bus.BusMessage.__name__ == "BusMessage"
    assert contracts.validate_contract.__name__ == "validate_contract"
    assert external.ExternalObservation.__name__ == "ExternalObservation"
    assert features.InMemoryFeatureStore.__name__ == "InMemoryFeatureStore"
    assert gateway.DryRunVenueGateway.__name__ == "DryRunVenueGateway"
    assert ledger.LedgerStore.__name__ == "LedgerStore"
    assert markets.SubscriptionMarketDetector.__name__ == "SubscriptionMarketDetector"
    assert models.ModelTrainer.__name__ == "ModelTrainer"
    assert observability.HealthStatus.__name__ == "HealthStatus"
    assert oms.OrderStateMachine.__name__ == "OrderStateMachine"
