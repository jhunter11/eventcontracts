"""Inspect event-lake partitions."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from eventcontracts.storage.inspection import inspect_event_lake


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("inspect-data", help="Inspect raw, normalized, and reject partitions.")
    parser.add_argument("--data", type=Path, required=True, help="Event lake root.")
    parser.add_argument("--source", default="*", help="Source to inspect, or '*' for all.")
    parser.set_defaults(handler=_handle_inspect_data)


def _handle_inspect_data(args: argparse.Namespace) -> int:
    print(json.dumps(inspect_event_lake(args.data, source=args.source), indent=2, sort_keys=True))
    return 0
