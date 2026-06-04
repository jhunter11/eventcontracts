from __future__ import annotations

import ast
import re
import tomllib
from pathlib import Path

from tests.conftest import REPO_ROOT


def _makefile_parity_dirs() -> set[str]:
    """Parity-case directory basenames referenced by the Makefile ``parity-check``
    target — the cross-language gate CI runs (.github/workflows/quality.yml)."""
    lines = (REPO_ROOT / "Makefile").read_text(encoding="utf-8").splitlines()
    dirs: set[str] = set()
    in_target = False
    for line in lines:
        if line.startswith("parity-check:"):
            in_target = True
            continue
        if in_target:
            if line and not line.startswith((" ", "\t")):
                break  # next target
            dirs.update(re.findall(r"contracts/parity/([A-Za-z0-9_]+)", line))
    return dirs


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
    modules = (
        "crop_drought_yield_reversion",
        "flu_hospitalization_surge",
        "entertainment_box_office",
    )
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


def test_promoted_manifests_are_gated_in_ci_parity_check() -> None:
    """Every promoted strategy must be parity-checked in CI. A promotion manifest
    whose parity directory is not in the Makefile ``parity-check`` target is silent
    cross-language rot — the strategy can be promoted while CI never verifies that
    its Python and Rust implementations still agree."""
    makefile_dirs = _makefile_parity_dirs()
    assert makefile_dirs, "could not parse any parity dirs from the Makefile parity-check target"
    promotion_dir = REPO_ROOT / "configs/promotion"
    for manifest_path in sorted(promotion_dir.glob("*.toml")):
        data = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
        if data.get("promoted") is not True:
            continue
        parity_basename = Path(str(data["parity_cases"])).name
        assert parity_basename in makefile_dirs, (
            f"{manifest_path.name}: parity dir '{parity_basename}' is not gated by the "
            f"Makefile parity-check target (CI cannot catch parity drift). "
            f"CI-gated parity dirs: {sorted(makefile_dirs)}"
        )
