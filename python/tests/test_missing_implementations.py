"""Inventory intentional scaffold boundaries.

This test makes unfinished interfaces explicit. When a concrete implementation
lands, update the count here as part of the same change.
"""

from __future__ import annotations

from tests.conftest import REPO_ROOT

EXPECTED_NOT_IMPLEMENTED = {
    "src/eventcontracts/adapters/venues/kalshi/client.py": 2,
    "src/eventcontracts/adapters/venues/polymarket/client.py": 4,
    "src/eventcontracts/allocation/capital.py": 6,
    "src/eventcontracts/audit.py": 4,
    "src/eventcontracts/bus/contracts.py": 13,
    "src/eventcontracts/execution/simulator.py": 1,
    "src/eventcontracts/external/base.py": 3,
    "src/eventcontracts/features/pipeline.py": 7,
    "src/eventcontracts/gateway/base.py": 14,
    "src/eventcontracts/ledger/accounting.py": 12,
    "src/eventcontracts/markets/detection.py": 4,
    "src/eventcontracts/models/pipeline.py": 9,
    "src/eventcontracts/normalization/contracts.py": 1,
    "src/eventcontracts/normalization/cross_venue.py": 1,
    "src/eventcontracts/observability/telemetry.py": 10,
    "src/eventcontracts/oms/state.py": 11,
    "src/eventcontracts/replay/engine.py": 1,
}


def test_not_implemented_inventory_is_explicit() -> None:
    src = REPO_ROOT / "python" / "src"
    actual = {}
    for path in sorted(src.rglob("*.py")):
        count = path.read_text().count("raise NotImplementedError")
        if count:
            actual[path.relative_to(REPO_ROOT / "python").as_posix()] = count

    assert actual == EXPECTED_NOT_IMPLEMENTED
