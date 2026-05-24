"""`eventcontracts replay` — stream events from a ParquetEventStore to stdout."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from eventcontracts.domain.events import event_kind
from eventcontracts.domain.serialization import to_primitive
from eventcontracts.replay import NormalizedReplaySource
from eventcontracts.storage import ParquetEventStore


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "replay",
        help="Stream normalized events from a parquet partition tree as JSON.",
    )
    parser.add_argument("--data", type=Path, required=True, help="ParquetEventStore root")
    parser.add_argument("--limit", type=int, default=0, help="Stop after N events (0 = unlimited)")
    parser.add_argument(
        "--kinds",
        nargs="*",
        default=None,
        help="Optional whitelist of event kinds (e.g. trade quote book).",
    )
    parser.set_defaults(handler=_handle)


def _handle(args: argparse.Namespace) -> int:
    store = ParquetEventStore(args.data)
    source = NormalizedReplaySource(store)
    emitted = 0
    for event in source.stream():
        kind = event_kind(event)
        if args.kinds and kind not in args.kinds:
            continue
        print(
            json.dumps(
                {"kind": kind, "payload": to_primitive(event)},
                default=str,
            )
        )
        emitted += 1
        if args.limit and emitted >= args.limit:
            break
    return 0
