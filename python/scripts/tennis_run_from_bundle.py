"""Plane B (execution) launcher: run the live-runner from a pipeline bundle.

Reads a bundle produced by ``tennis_pipeline.py`` (``snapshots.jsonl`` +
``manifest.json``) and constructs the Rust live-runner invocation — the tickers,
the snapshot file, the promoted model bundle, and the schema-version pin — so the
execution host needs no hand-assembled command.

SAFETY: defaults to OBSERVE mode (no ``--live-submit``: subscribe + score + show
would-be intents, place nothing). Real money requires BOTH ``--live-submit`` here
AND answering the runner's own ``Type yes to proceed`` prompt (pass ``--execute``
to actually spawn the runner; without it this only prints the command). A
freshness gate refuses a stale bundle or one whose matches have all started.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _load_manifest(bundle: Path) -> dict:
    mf = bundle / "manifest.json"
    if not mf.exists():
        raise SystemExit(f"no manifest.json in {bundle}")
    return json.loads(mf.read_text(encoding="utf-8"))


def _freshness_ok(manifest: dict, max_age_min: int, include_started: bool) -> tuple[bool, str]:
    now = datetime.now(UTC)
    gen = manifest.get("generated_at")
    if gen:
        age_min = (now - datetime.fromisoformat(gen.replace("Z", "+00:00"))).total_seconds() / 60.0
        if age_min > max_age_min:
            return False, f"bundle is {age_min:.0f} min old (> --max-age-min {max_age_min})"
    if not include_started:
        commences = [
            datetime.fromisoformat(m["commence_time"].replace("Z", "+00:00"))
            for m in manifest.get("matches", [])
            if m.get("commence_time")
        ]
        if commences and all(c <= now for c in commences):
            return False, "every match in the bundle has already started"
    return True, "ok"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bundle", type=Path, required=True, help="bundle dir (snapshots.jsonl + manifest.json)")
    ap.add_argument("--sleeve-spec", type=Path, default=ROOT / "configs/sleeves/sports-tennis-kalshi-live-a.toml")
    ap.add_argument("--strategy-spec", type=Path, default=ROOT / "configs/strategies/sports-tennis-xgboost.toml")
    ap.add_argument("--artifacts-root", type=Path, default=ROOT / "artifacts/tennis_xgboost/bundles")
    ap.add_argument("--duration-secs", type=int, default=3600)
    ap.add_argument("--max-live-orders", type=int, default=1)
    ap.add_argument("--max-age-min", type=int, default=60, help="refuse a bundle older than this")
    ap.add_argument("--include-started", action="store_true")
    ap.add_argument("--live-submit", action="store_true", help="enable real submission (runner still prompts yes)")
    ap.add_argument("--execute", action="store_true", help="actually spawn the runner (default: just print)")
    ap.add_argument("--runner-bin", default="", help="path to a prebuilt live-runner; default uses cargo run")
    args = ap.parse_args()

    manifest = _load_manifest(args.bundle)
    tickers = manifest.get("tickers", [])
    if not tickers:
        raise SystemExit("manifest has no tickers")
    ok, why = _freshness_ok(manifest, args.max_age_min, args.include_started)
    if not ok:
        raise SystemExit(f"REFUSING: {why} (override with --include-started / --max-age-min)")

    bundle_name = manifest.get("model", {}).get("bundle", "")
    artifact = args.artifacts_root / bundle_name
    if not artifact.exists():
        raise SystemExit(f"model bundle not found: {artifact}")
    schema_v = str(manifest.get("model", {}).get("expect_tennis_schema_version", "2"))
    snapshots = args.bundle / "snapshots.jsonl"

    if args.runner_bin:
        cmd = [args.runner_bin]
    else:
        cmd = ["cargo", "run", "-p", "eventcontracts-live-runner", "--manifest-path",
               str(ROOT / "rust" / "Cargo.toml"), "--"]
    cmd += [
        "--strategy-spec", str(args.strategy_spec),
        "--sleeve-spec", str(args.sleeve_spec),
        "--tennis-artifact", str(artifact),
        "--tennis-snapshots-jsonl", str(snapshots),
        "--expect-tennis-schema-version", schema_v,
        "--duration-secs", str(args.duration_secs),
        "--reconcile-on-start",
        "--reconcile-report", str(args.bundle / "reconcile.json"),
        "--metrics-snapshot-file", str(args.bundle / "metrics.txt"),
        "--tickers", *tickers,
    ]
    if args.live_submit:
        cmd += ["--live-submit", "--max-live-orders", str(args.max_live_orders)]

    mode = "LIVE-SUBMIT" if args.live_submit else "OBSERVE (no orders)"
    print(f"mode: {mode}")
    print(f"bundle: {args.bundle}  matches: {len(tickers)}  earliest_commence: {manifest.get('earliest_commence')}")
    print("command:")
    print("  " + " ".join(cmd))
    if not args.execute:
        print("\n(dry print only — add --execute to spawn the runner; KALSHI_ENV must be set in this shell)")
        return 0
    print("\nspawning runner (it will prompt before any live submit)...")
    return subprocess.run(cmd, cwd=str(ROOT)).returncode  # noqa: S603


if __name__ == "__main__":
    raise SystemExit(main())
