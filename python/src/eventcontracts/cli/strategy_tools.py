"""Strategy scaffolding and promotion checks."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

from eventcontracts.config import load_strategy_spec

# Archetypes the scaffolder can stamp out. Not all are promotable yet.
KNOWN_ARCHETYPES = {"threshold", "external_edge", "model_edge", "scalper", "arb"}
# Archetypes that have a real Rust runtime in `rust/crates/runner/src/registry.rs`
# (`ThresholdStrategy` and the config-only `ExternalEdgeStrategy`). A strategy
# tagged with any other archetype cannot be instantiated by the live Rust runner,
# so it is not promotable. Keep in sync with `registry.rs::instantiate`.
RUST_IMPLEMENTED_ARCHETYPES = {"threshold", "external_edge"}


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    new_parser = subparsers.add_parser("new-strategy", help="Scaffold a strategy config set.")
    new_parser.add_argument("name")
    new_parser.add_argument("--archetype", required=True, choices=sorted(KNOWN_ARCHETYPES))
    new_parser.add_argument("--root", type=Path, default=Path("."))
    new_parser.set_defaults(handler=_handle_new_strategy)

    verify_parser = subparsers.add_parser(
        "verify-strategy",
        help="Verify a strategy is promotable: parity cases exist, archetype/name "
        "is instantiable in both languages, and parity_check passes.",
    )
    verify_parser.add_argument("name")
    verify_parser.add_argument("--root", type=Path, default=Path("."))
    verify_parser.add_argument(
        "--skip-parity",
        action="store_true",
        help="Skip the Rust parity_check run (structural checks only). The full "
        "promotion gate must NOT skip it.",
    )
    verify_parser.add_argument(
        "--skip-smoke",
        action="store_true",
        help="Skip the no-trade smoke (>=1 risk-approved intent). The full "
        "promotion gate must NOT skip it.",
    )
    verify_parser.set_defaults(handler=_handle_verify_strategy)


def _handle_new_strategy(args: argparse.Namespace) -> int:
    root = args.root
    name = _slug(args.name)
    archetype = str(args.archetype)
    strategy_name = name.replace("-", "_")

    strategy_path = root / "configs" / "strategies" / f"{name}.toml"
    sleeve_path = root / "configs" / "sleeves" / f"{name}-kalshi-paper-a.toml"
    parity_dir = root / "contracts" / "parity" / strategy_name
    if strategy_path.exists() or sleeve_path.exists() or parity_dir.exists():
        raise FileExistsError(f"strategy scaffold already exists for {name}")

    strategy_path.parent.mkdir(parents=True, exist_ok=True)
    sleeve_path.parent.mkdir(parents=True, exist_ok=True)
    parity_dir.mkdir(parents=True, exist_ok=True)
    strategy_path.write_text(_strategy_toml(name, strategy_name, archetype), encoding="utf-8")
    sleeve_path.write_text(_sleeve_toml(name, strategy_name), encoding="utf-8")
    (parity_dir / ".gitkeep").write_text("", encoding="utf-8")
    print(f"created strategy scaffold: {name}")
    return 0


def _handle_verify_strategy(args: argparse.Namespace) -> int:
    root = args.root
    name = _slug(args.name)
    strategy_path = root / "configs" / "strategies" / f"{name}.toml"
    spec = load_strategy_spec(strategy_path)
    parity_dir = root / "contracts" / "parity" / str(spec.name)
    archetype = str(spec.tags.get("archetype") or spec.parameters.get("archetype") or "")
    issues: list[str] = []

    # 1) Real parity coverage: a directory is not enough — promotion requires at
    #    least one parity case file, or there is no cross-language guarantee.
    if not parity_dir.is_dir():
        issues.append(f"missing parity directory: {parity_dir}")
    elif not sorted(parity_dir.glob("*.json")):
        issues.append(
            f"no parity case files (*.json) in {parity_dir}; a strategy cannot be "
            "promoted to live without cross-language parity coverage"
        )

    # 2) Promotability: an archetype must have a real Rust runtime; a named
    #    strategy must be registered in Python (the Rust side is proven by the
    #    parity_check run below).
    if archetype:
        if archetype not in KNOWN_ARCHETYPES:
            issues.append(f"unknown archetype: {archetype}")
        elif archetype not in RUST_IMPLEMENTED_ARCHETYPES:
            issues.append(
                f"archetype '{archetype}' has no Rust runtime "
                f"(implemented: {sorted(RUST_IMPLEMENTED_ARCHETYPES)}); not promotable to live"
            )
    else:
        try:
            from eventcontracts.strategy.registry import ensure_registered

            ensure_registered(spec.name)
        except Exception as exc:  # noqa: BLE001
            issues.append(f"strategy is neither registered (Python) nor archetyped: {exc}")

    # 3) Cross-language parity must actually pass (this also proves the strategy
    #    instantiates in the Rust runner). Skipped only for fast structural
    #    checks; the full promotion gate must run it.
    if not args.skip_parity and not issues:
        ok, detail = _run_parity_check(root, strategy_path, parity_dir)
        if not ok:
            issues.append(f"parity_check failed (cross-language gate): {detail}")

    # 4) No-trade smoke (V6-S2): replay the parity stream through the real runner
    #    + risk gate and require >=1 risk-APPROVED intent. Catches the silent
    #    failure where a strategy emits orders that are all risk-rejected (e.g.
    #    missing_market_snapshot) and dispatches nothing live.
    if not args.skip_smoke and not issues:
        from eventcontracts.cli.strategy_smoke import run_no_trade_smoke

        smoke = run_no_trade_smoke(strategy_path, parity_dir)
        if not smoke.ok:
            issues.append(f"no-trade smoke failed (V6-S2): {smoke.summary()}")

    if issues:
        for issue in issues:
            print(f"FAIL {issue}")
        return 1
    print(f"OK strategy promotable: {spec.name}")
    return 0


def _run_parity_check(root: Path, strategy_path: Path, parity_dir: Path) -> tuple[bool, str]:
    """Run the Rust `parity_check` binary for one strategy. Returns (ok, detail).

    A missing cargo toolchain is a *failure* for a promotion gate — the operator
    must verify cross-language parity in an environment that can build Rust.
    """

    cmd = [
        "cargo",
        "run",
        "--quiet",
        "--manifest-path",
        str((root / "rust" / "Cargo.toml").resolve()),
        "-p",
        "eventcontracts-parity",
        "--bin",
        "parity_check",
        "--",
        "--strategy-spec",
        str(strategy_path.resolve()),
        "--cases",
        str(parity_dir.resolve()),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return False, "cargo not found on PATH; required to verify cross-language parity"
    if result.returncode != 0:
        detail = (result.stdout + result.stderr).strip()
        return False, detail[-500:] if detail else f"exit code {result.returncode}"
    return True, "ok"


def _slug(value: str) -> str:
    slug = value.strip().lower().replace("_", "-")
    if not slug or any(ch for ch in slug if not (ch.isalnum() or ch == "-")):
        raise ValueError("strategy name must contain only letters, numbers, '-' or '_'")
    return slug


def _strategy_toml(name: str, strategy_name: str, archetype: str) -> str:
    return f"""strategy_id = "{name}-v1"
name = "{strategy_name}"
version = "0.1.0"
description = "Scaffolded {archetype} strategy."
feature_schema_id = "{strategy_name}_features"

[subscription]
venues = ["kalshi"]
instrument_patterns = ["KX*"]
event_kinds = ["quote", "external"]
external_sources = ["example-signal"]

[default_execution_priority]
tier = "relaxed"
max_delay_ms = 5000
expires_after_ms = 300000
reason = "scaffold default"

[parameters]
signal_source = "example-signal"
min_edge_bps = "100"
size = "1"

[tags]
archetype = "{archetype}"
mode = "paper"
"""


def _sleeve_toml(name: str, strategy_name: str) -> str:
    return f"""sleeve_id = "{name}-kalshi-paper-a"
strategy_id = "{name}-v1"
strategy_version = "0.1.0"
venue = "kalshi"
capital_allocation = "1000"
currency = "USD"

[risk]
max_order_notional = "25"
max_position_notional = "100"
max_daily_loss = "25"
max_open_orders = 5
max_gross_exposure = "250"
currency = "USD"
max_market_data_age_ms = 1000
max_spread = "0.50"
max_order_lifetime_ms = 300000

[tags]
mode = "paper"
strategy_name = "{strategy_name}"
"""
