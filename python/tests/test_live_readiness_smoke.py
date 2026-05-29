"""V6-S2 gate: live-promotable strategies must produce >=1 risk-APPROVED intent.

Replays each strategy's parity stream through the real runner + risk gate. This
catches the silent "emits orders, dispatches nothing" failure (classically
``missing_market_snapshot`` when an order fires on an External event without
attached BBO evidence) that ``parity_check`` cannot see because it only compares
strategy decisions, never the risk verdict.

A regression that drops the cached-snapshot attach in flu/crop would make them
0-approved here and fail this gate.
"""

from __future__ import annotations

import pytest

from eventcontracts.cli.strategy_smoke import SmokeResult, run_no_trade_smoke
from tests.conftest import REPO_ROOT

PROMOTABLE: tuple[tuple[str, str, str], ...] = (
    (
        "flu_hospitalization_surge",
        "configs/strategies/flu-hospitalization-surge.toml",
        "contracts/parity/flu_hospitalization_surge",
    ),
    (
        "crop_drought_yield_reversion",
        "configs/strategies/crop-drought-yield-reversion.toml",
        "contracts/parity/crop_drought_yield_reversion",
    ),
    (
        "sports_tennis_xgboost",
        "configs/strategies/sports-tennis-xgboost.toml",
        "contracts/parity/sports_tennis_xgboost",
    ),
)


@pytest.mark.parametrize(
    ("name", "spec", "parity"), PROMOTABLE, ids=[p[0] for p in PROMOTABLE]
)
def test_promotable_strategy_produces_approved_intent(name: str, spec: str, parity: str) -> None:
    result = run_no_trade_smoke(REPO_ROOT / spec, REPO_ROOT / parity)
    assert result.ok, f"{name}: {result.summary()}"
    assert result.intents_approved >= 1


def test_smoke_flags_all_rejected_as_not_ok() -> None:
    result = SmokeResult(
        orders_emitted=3,
        intents_approved=0,
        intents_rejected=3,
        rejection_reasons={"missing_market_snapshot": 3},
    )
    assert not result.ok
    assert result.dominant_reason() == "missing_market_snapshot"
    assert "ALL were risk-rejected" in result.summary()


def test_smoke_flags_no_orders_as_not_ok() -> None:
    result = SmokeResult()
    assert not result.ok
    assert "no order intents" in result.summary()
