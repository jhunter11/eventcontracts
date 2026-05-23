"""Command-line entry points for framework maintenance tasks."""

from __future__ import annotations

import argparse
from pathlib import Path
from pprint import pprint

from eventcontracts.config import (
    load_sleeve_spec,
    load_storage_config,
    load_strategy_spec,
    load_toml,
    load_venue_config,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="eventcontracts")
    subparsers = parser.add_subparsers(dest="command", required=True)

    check_config = subparsers.add_parser("check-config", help="Load and print a TOML config.")
    check_config.add_argument("path", type=Path)

    validate_config = subparsers.add_parser(
        "validate-config", help="Validate a TOML config against a known schema."
    )
    validate_config.add_argument("kind", choices=("strategy", "sleeve", "storage", "venue"))
    validate_config.add_argument("path", type=Path)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "check-config":
        pprint(load_toml(args.path))
        return 0

    if args.command == "validate-config":
        loaders = {
            "strategy": load_strategy_spec,
            "sleeve": load_sleeve_spec,
            "storage": load_storage_config,
            "venue": load_venue_config,
        }
        pprint(loaders[args.kind](args.path))
        return 0

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
