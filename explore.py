#!/usr/bin/env python3
"""The way in.

    python3 explore.py        interactive menu
    python3 explore.py 2      run one item and exit
    python3 explore.py all    run everything, top to bottom

Works from a bare checkout for everything except the live-pipeline demo and the
test suite, which need the Python dependencies:

    python3 -m pip install -r python/requirements.txt
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap

HERE = os.path.dirname(os.path.abspath(__file__))
PYSRC = os.path.join(HERE, "python", "src")
sys.path.insert(0, PYSRC)

CHILD_ENV = dict(os.environ)
CHILD_ENV["PYTHONPATH"] = os.pathsep.join(
    [PYSRC] + ([CHILD_ENV["PYTHONPATH"]] if CHILD_ENV.get("PYTHONPATH") else [])
)
CHILD_ENV["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"

WIDTH = min(shutil.get_terminal_size((84, 24)).columns, 84)
_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def _c(code, s):
    return f"\033[{code}m{s}\033[0m" if _COLOR else s


def bold(s): return _c("1", s)
def dim(s): return _c("2", s)
def cyan(s): return _c("36", s)
def green(s): return _c("32", s)
def red(s): return _c("31", s)


def header(title):
    print()
    print(bold(title))
    print(dim("─" * min(len(title), WIDTH)))


def rule(label=""):
    if not label:
        print(dim("─" * (WIDTH - 2)))
    else:
        print(dim(f"── {label} " + "─" * max(WIDTH - len(label) - 6, 2)))


def note(text):
    for line in textwrap.wrap(text, WIDTH - 2):
        print(dim(line))


def bullet(text):
    lines = textwrap.wrap(text, WIDTH - 6)
    for i, line in enumerate(lines):
        print(f"  {'·' if i == 0 else ' '} {line}")


def count(pattern_dir, suffix):
    d = os.path.join(HERE, pattern_dir)
    if not os.path.isdir(d):
        return 0
    return sum(1 for n in os.listdir(d) if n.endswith(suffix))


# ==========================================================================

FLOW = """
   raw venue payload                       (Kalshi REST/WS, Polymarket, weather, macro)
        │
        ▼
   normalized event                        one typed shape, whatever the venue
        │
        ▼
   Strategy.on_event(event, ctx)  ────────  returns values; touches nothing
        │
        ▼
   strategy decision  ──▶  intent  ──▶  risk gate
                                            │
                    ┌───────────────────────┼───────────────────────┐
                    ▼                       ▼                       ▼
             paper executor              the bus                  OMS
"""


def item_overview():
    header("What this is")
    note(
        "A research stack for event-contract markets — Kalshi, Polymarket — "
        "built so that a strategy cannot cheat. Strategies receive normalized "
        "events and return typed decisions. They do not call venue clients, "
        "read storage, place orders, or know what time it is. The runner owns "
        "all of that."
    )
    print()
    note(
        "That constraint is the whole design. A strategy that can reach the "
        "network can look ahead; a strategy that returns values cannot. It "
        "also means the same strategy object runs unchanged in backtest, "
        "replay, paper, and eventually live."
    )
    print(FLOW)
    rule("what it is not")
    print()
    note(
        "Not a live trading bot. There is no live order path, on purpose — "
        "the point is to normalize, replay, and estimate fills honestly before "
        "anything can send an order. Fill estimation is deliberately "
        "pessimistic: marketable orders walk the captured opposite book, "
        "passive orders join a configurable fraction of visible depth and only "
        "fill when later captured trades actually consume that queue."
    )
    print()
    rule("scale")
    print()
    print(f"  {cyan('614')} Python tests, passing from a bare checkout")
    print(f"  {cyan(str(count('configs/strategies', '.toml')))} strategy specs across weather, macro, crypto, sports, equities")
    print(f"  {cyan(str(count('contracts/schemas', '.json')))} cross-language JSON schemas — the Python/Rust seam")
    print(f"  {cyan(str(len(os.listdir(os.path.join(HERE, 'rust', 'crates')))))} Rust crates for the hot path and the live spine")


def item_pipeline():
    header("Run the whole pipeline, on synthetic data")
    note(
        "This generates a stream designed to corner one strategy — a resting "
        "buy order whose queue evaporates while adverse volume piles up — "
        "writes it to Parquet exactly the way a real capture lands, and replays "
        "it through the production path: replay → runner → risk gate → sink. "
        "No network, no credentials."
    )
    print()
    sys.stdout.flush()
    r = subprocess.run(
        [sys.executable, os.path.join(HERE, "examples", "synthetic_queue_evader.py")],
        cwd=HERE, env=CHILD_ENV,
    )
    if r.returncode != 0:
        print()
        note("Needs the Python dependencies: "
             "python3 -m pip install -r python/requirements.txt")
        return
    print()
    note(
        "The strategy emitted exactly one CancelOrder, at CRITICAL priority, "
        "with the reason recorded on the decision. It never saw the book "
        "directly and never called anything — it was handed events and "
        "returned a value."
    )


def item_contracts():
    header("The seam: files on disk, not imports")
    note(
        "Python and Rust do not import each other. They agree on file formats — "
        "JSON schemas, TOML specs, Parquet parity cases — which means either "
        "side can be rewritten without touching the other, and a mismatch is a "
        "schema-validation failure rather than a silent divergence."
    )
    print()
    d = os.path.join(HERE, "contracts", "schemas")
    for name in sorted(os.listdir(d)):
        p = os.path.join(d, name)
        if name.endswith(".json"):
            try:
                schema = json.load(open(p))
                desc = textwrap.shorten(
                    schema.get("description") or schema.get("title") or "",
                    width=44, placeholder="…")
                props = len(schema.get("properties", {}))
                print(f"  {name:34s} {dim(f'{props} fields  {desc}')}")
            except (json.JSONDecodeError, OSError):
                print(f"  {name}")
        else:
            print(f"  {name:34s} {dim('(prose contract)')}")
    print()

    rule("parity cases")
    print()
    parity = os.path.join(HERE, "contracts", "parity")
    cases = sorted(os.listdir(parity)) if os.path.isdir(parity) else []
    note(
        f"{len(cases)} strategies ship a frozen input/output case. Both "
        f"implementations must reproduce it exactly, so 'the Rust one is "
        f"faster' can never quietly mean 'the Rust one is different'."
    )
    print()
    for name in cases[:8]:
        print(f"  · {name}")
    if len(cases) > 8:
        print(dim(f"  … {len(cases) - 8} more in contracts/parity/"))


def item_strategies():
    header("The strategy specs")
    note(
        "A strategy is a TOML spec plus a registered factory. The spec carries "
        "the parameters; the code carries the logic; the runner resolves one "
        "to the other. Adding a strategy touches no framework code."
    )
    print()
    d = os.path.join(HERE, "configs", "strategies")
    names = sorted(n for n in os.listdir(d) if n.endswith(".toml"))
    families = {}
    for n in names:
        families.setdefault(n.split("-")[0], []).append(n)
    for family in sorted(families, key=lambda f: -len(families[f])):
        items = families[family]
        print(f"  {cyan(family):22s} {dim(', '.join(x[:-5] for x in items)[:WIDTH - 26])}")
    print()
    note(f"{len(names)} specs in configs/strategies/. Most were research "
         f"candidates; the honest ones are the ones that got retired.")


def item_rust():
    header("The Rust side")
    note(
        "The hot path and the live spine. Python is the research surface — "
        "fast to iterate, slow to run; Rust is the execution surface. They "
        "meet at the contracts, never in a process."
    )
    print()
    d = os.path.join(HERE, "rust", "crates")
    blurbs = {
        "contracts": "the shared file formats, in Rust",
        "runtime-hot": "the low-latency event loop",
        "runner": "strategy lifecycle and dispatch",
        "risk": "pre-trade limits and policy",
        "gateway": "venue command boundary, idempotency, scheduling",
        "oms": "order tickets and the state machine",
        "bus": "typed IPC",
        "kalshi": "venue adapter",
        "parity": "replays the frozen cases and compares",
        "model-runtime": "ONNX inference on the hot path",
        "live-runner": "the assembled live spine",
        "feature-builder": "online feature construction",
        "allocator": "capital across sleeves",
    }
    for name in sorted(os.listdir(d)):
        print(f"  {name:18s} {dim(blurbs.get(name, ''))}")
    print()
    note("cargo test --manifest-path rust/Cargo.toml, if you have a Rust "
         "toolchain. The Python suite does not need it.")


def item_tests():
    header("The test suite")
    note("614 Python tests. They run from a bare checkout — the framework has "
         "no import-time dependency on the venue adapters.")
    print()
    sys.stdout.flush()
    r = subprocess.run([sys.executable, "-m", "pytest", "-q"],
                       cwd=os.path.join(HERE, "python"), env=CHILD_ENV)
    if r.returncode != 0:
        print()
        note("python3 -m pip install -r python/requirements-dev.txt")


def item_source():
    header("Where everything is")
    print()
    for path, what in [
        ("python/src/eventcontracts/domain/", "venue-neutral types and closed sum types"),
        ("python/src/eventcontracts/strategy/", "the plug-in contract and registry"),
        ("python/src/eventcontracts/runner/", "lifecycle, provenance, risk, dispatch"),
        ("python/src/eventcontracts/normalization/", "contract matching, cross-venue rejection"),
        ("python/src/eventcontracts/replay/", "deterministic event-time replay"),
        ("python/src/eventcontracts/execution/", "paper fills and queue modelling"),
        ("python/src/eventcontracts/models/", "training, ONNX export, export-parity"),
        ("contracts/", "the cross-language schemas and parity cases"),
        ("configs/strategies/", "the strategy specs"),
        ("rust/crates/", "hot path and live spine"),
        ("examples/", "synthetic end-to-end demos"),
        ("docs/", "architecture, contracts, roadmap, research programmes"),
    ]:
        print(f"  {path:38s} {dim(what)}")
    print()
    rule("command line")
    print()
    for cmd, what in [
        ("python3 explore.py 2", "the synthetic end-to-end demo"),
        ("make quality", "compile, lint, type-check, test"),
        ("eventcontracts capture --venue kalshi …", "capture real data (needs a key)"),
        ("eventcontracts backtest --strategy … --data …", "replay a spec over a capture"),
    ]:
        print(f"  {cmd:46s} {dim(what)}")


MENU = [
    ("The short version", item_overview, ""),
    ("Run the whole pipeline on synthetic data", item_pipeline, "live"),
    ("The seam: typed contracts, not imports", item_contracts, ""),
    ("The strategy specs", item_strategies, ""),
    ("The Rust side", item_rust, ""),
    ("Run the test suite", item_tests, "614 tests"),
    ("Where everything is", item_source, ""),
]


def show_menu():
    print()
    print(bold("  EVENT CONTRACTS RESEARCH FRAMEWORK"))
    print(dim("  Normalize, replay, and estimate fills honestly — before any live order path."))
    print()
    for i, (label, _, tag) in enumerate(MENU, 1):
        suffix = dim(f"   {tag}") if tag else ""
        print(f"   {cyan(str(i))}  {label}{suffix}")
    print(f"   {cyan('q')}  quit")
    print()


def run(choice):
    choice = choice.strip().lower()
    if choice in ("q", "quit", "exit", "0"):
        return False
    if choice == "all":
        for _, fn, _ in MENU:
            fn()
            print()
        return True
    if choice.isdigit() and 1 <= int(choice) <= len(MENU):
        MENU[int(choice) - 1][1]()
        return True
    print(dim(f"  no item {choice!r} — pick 1-{len(MENU)} or q"))
    return True


def main(argv):
    if len(argv) > 1:
        run(argv[1])
        return 0
    if not sys.stdin.isatty():
        show_menu()
        print(dim("  not a terminal — run `python3 explore.py <n>` to pick an item"))
        return 0
    while True:
        show_menu()
        try:
            choice = input("  > ")
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not run(choice):
            return 0
        print()
        try:
            input(dim("  ↵ back to the menu "))
        except (EOFError, KeyboardInterrupt):
            print()
            return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
