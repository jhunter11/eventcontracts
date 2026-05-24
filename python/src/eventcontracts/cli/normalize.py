"""Normalize raw event-lake envelopes into replay-ready events."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from eventcontracts.normalization import BASIC_NORMALIZERS, EventNormalizer, NormalizationPipeline, kalshi_normalizers
from eventcontracts.normalization.pipeline import NormalizeFn, NormalizerKey
from eventcontracts.storage import ParquetEventStore


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("normalize", help="Normalize raw event-lake envelopes.")
    parser.add_argument("--data", type=Path, required=True, help="Event lake root.")
    parser.add_argument("--source", default="*", help="Raw source to normalize, or '*' for all.")
    parser.add_argument("--normalizer", choices=("basic", "kalshi"), default="kalshi")
    parser.set_defaults(handler=_handle_normalize)


def _handle_normalize(args: argparse.Namespace) -> int:
    summary = normalize_event_lake(args.data, source=args.source, normalizer_name=args.normalizer)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


def normalize_event_lake(
    root: Path | str,
    *,
    source: str = "*",
    normalizer_name: str = "kalshi",
) -> dict[str, Any]:
    """Normalize persisted raw envelopes and return an auditable run summary."""

    store = ParquetEventStore(root)
    pipeline = NormalizationPipeline(
        raw_store=store,
        normalized_store=store,
        normalizer=EventNormalizer(_handlers_for(normalizer_name)),
        reject_store=store,
    )
    results = pipeline.run(source=source)
    store.flush()

    accepted = sum(1 for result in results if result.accepted)
    rejected = len(results) - accepted
    return {
        "data": str(root),
        "source": source,
        "normalizer": normalizer_name,
        "processed": len(results),
        "accepted": accepted,
        "rejected": rejected,
        "accepted_by_channel": dict(
            sorted(Counter(result.raw.channel for result in results if result.accepted).items())
        ),
        "rejected_by_channel": dict(
            sorted(Counter(result.raw.channel for result in results if not result.accepted).items())
        ),
        "rejected_by_reason": dict(
            sorted(Counter(reason for result in results if not result.accepted for reason in result.reasons).items())
        ),
    }


def _handlers_for(name: str) -> Mapping[NormalizerKey, NormalizeFn]:
    if name == "basic":
        return BASIC_NORMALIZERS
    if name == "kalshi":
        return kalshi_normalizers()
    raise ValueError(f"unsupported normalizer: {name}")
