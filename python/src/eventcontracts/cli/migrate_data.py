"""`eventcontracts migrate-data` — upgrade legacy parquet partitions in place.

A schema-version bump must never brick previously-captured data. Readers are
version-tolerant (they upcast legacy files on read), but this command stamps the
current schema marker permanently so the lake is uniform and the strict
integrity check applies going forward. Idempotent.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from eventcontracts.storage.parquet_store import migrate_event_lake


def register(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "migrate-data",
        help="Upgrade legacy/older parquet partitions to the current schema version.",
    )
    parser.add_argument("--data", type=Path, required=True, help="Event lake root.")
    parser.set_defaults(handler=_handle)


def _handle(args: argparse.Namespace) -> int:
    counts = migrate_event_lake(args.data)
    print(json.dumps({"migrated": counts}, indent=2, sort_keys=True))
    return 0
