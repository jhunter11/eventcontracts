"""Top-level CLI dispatcher.

Subcommands live in sibling modules in this package and register
themselves with :func:`build_parser` via a small declarative list.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from pathlib import Path
from pprint import pprint
from typing import cast

from eventcontracts.cli import backtest as backtest_cmd
from eventcontracts.cli import capture as capture_cmd
from eventcontracts.cli import capture_weather as capture_weather_cmd
from eventcontracts.cli import gen_windows as gen_windows_cmd
from eventcontracts.cli import inspect_data as inspect_data_cmd
from eventcontracts.cli import live_paper as live_paper_cmd
from eventcontracts.cli import normalize as normalize_cmd
from eventcontracts.cli import rank as rank_cmd
from eventcontracts.cli import replay as replay_cmd
from eventcontracts.cli import sports_golf as sports_golf_cmd
from eventcontracts.cli import sweep as sweep_cmd
from eventcontracts.cli import train as train_cmd
from eventcontracts.cli import validate_bundle as validate_bundle_cmd
from eventcontracts.cli import weather as weather_cmd
from eventcontracts.config import (
    load_sleeve_spec,
    load_storage_config,
    load_strategy_spec,
    load_toml,
    load_venue_config,
)

CommandHandler = Callable[[argparse.Namespace], int]


def _register_check_config(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("check-config", help="Load and print a TOML config.")
    parser.add_argument("path", type=Path)
    parser.set_defaults(handler=_handle_check_config)


def _handle_check_config(args: argparse.Namespace) -> int:
    pprint(load_toml(args.path))
    return 0


def _register_validate_config(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "validate-config", help="Validate a TOML config against a known schema."
    )
    parser.add_argument("kind", choices=("strategy", "sleeve", "storage", "venue"))
    parser.add_argument("path", type=Path)
    parser.set_defaults(handler=_handle_validate_config)


def _handle_validate_config(args: argparse.Namespace) -> int:
    loaders = {
        "strategy": load_strategy_spec,
        "sleeve": load_sleeve_spec,
        "storage": load_storage_config,
        "venue": load_venue_config,
    }
    pprint(loaders[args.kind](args.path))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="eventcontracts")
    subparsers = parser.add_subparsers(dest="command", required=True)

    _register_check_config(subparsers)
    _register_validate_config(subparsers)
    backtest_cmd.register(subparsers)
    capture_cmd.register(subparsers)
    capture_weather_cmd.register(subparsers)
    live_paper_cmd.register(subparsers)
    normalize_cmd.register(subparsers)
    inspect_data_cmd.register(subparsers)
    replay_cmd.register(subparsers)
    sweep_cmd.register(subparsers)
    gen_windows_cmd.register(subparsers)
    rank_cmd.register(subparsers)
    train_cmd.register(subparsers)
    sports_golf_cmd.register(subparsers)
    weather_cmd.register(subparsers)
    validate_bundle_cmd.register(subparsers)

    return parser


def main(argv: list[str] | None = None) -> int:
    from eventcontracts.env import load_default_env

    load_default_env()
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = cast(CommandHandler, args.handler)
    return handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
