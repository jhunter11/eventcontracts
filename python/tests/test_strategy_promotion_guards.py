from __future__ import annotations

import ast
import tomllib

from tests.conftest import REPO_ROOT


def test_promoted_strategies_do_not_emit_market_orders() -> None:
    strategy_dir = REPO_ROOT / "python/src/eventcontracts/plugins/strategies"
    offenders: list[str] = []
    for path in strategy_dir.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and node.attr == "MARKET"
                and isinstance(node.value, ast.Name)
                and node.value.id == "OrderType"
            ):
                offenders.append(f"{path}:{node.lineno}")

    assert offenders == []


def test_promotable_mid_pricing_strategies_discretize_with_shared_helpers() -> None:
    """V6-C3: strategies that derive a limit price from the book mid and run on
    the live Rust runner (``external_edge`` archetype) must discretise via the
    shared ``strategy.pricing`` helpers — never ad-hoc ``round()``/``_clip``,
    which either leak edge or emit a sub-cent price the venue rejects.

    Add any new promotable mid-pricing strategy to ``modules`` below.
    """
    strategy_dir = REPO_ROOT / "python/src/eventcontracts/plugins/strategies"
    modules = ("crop_drought_yield_reversion", "flu_hospitalization_surge")
    for module in modules:
        tree = ast.parse(
            (strategy_dir / f"{module}.py").read_text(encoding="utf-8"), filename=module
        )
        uses_helper = False
        offenders: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id in {"buy_limit_from_fair", "sell_limit_from_fair"}:
                    uses_helper = True
                if node.func.id in {"round", "_clip"}:
                    offenders.append(f"{module}:{node.lineno}:{node.func.id}()")
            if isinstance(node, ast.FunctionDef) and node.name == "_clip":
                offenders.append(f"{module}:{node.lineno}:def _clip")

        assert uses_helper, f"{module} must discretise prices via strategy.pricing helpers"
        assert offenders == [], f"ad-hoc price rounding in {module}: {offenders}"


def test_promotion_manifests_require_nonempty_parity() -> None:
    promotion_dir = REPO_ROOT / "configs/promotion"
    manifests = sorted(promotion_dir.glob("*.toml"))

    assert manifests
    for manifest_path in manifests:
        data = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
        assert data.get("promoted") is True
        strategy_spec = (manifest_path.parent / str(data["strategy_spec"])).resolve()
        parity_dir = (manifest_path.parent / str(data["parity_cases"])).resolve()

        assert strategy_spec.is_file(), manifest_path.name
        assert parity_dir.is_dir(), manifest_path.name
        assert any(parity_dir.glob("*.json")), manifest_path.name
