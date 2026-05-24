"""File-backed StateStore for strategy snapshots.

Strategies persist their internal state between runs via the
``snapshot()`` / ``restore()`` hooks on the Strategy protocol. The
runner writes snapshots through a :class:`StateStore` implementation —
this one stores them as opaque bytes under a directory tree keyed by
strategy id.

The format is binary (whatever the strategy chose to emit) wrapped in
no envelope: a snapshot is exactly one file per strategy. Multi-version
storage is left for the (future) artifact bundle exporter; here we
keep it simple.
"""

from __future__ import annotations

from pathlib import Path

from eventcontracts.domain.ids import StrategyId


class FileStateStore:
    """Implements :class:`eventcontracts.runner.ports.StateStore`.

    Snapshots live at ``<root>/<strategy_id>.snapshot``. Reads return
    ``None`` when no snapshot exists yet, so first runs do not need
    special-case logic.
    """

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, strategy_id: StrategyId) -> Path:
        # Strategy ids can include path-unsafe characters in theory; the
        # framework enforces non-empty strings, so a simple replace is
        # enough for the slugs used in practice.
        slug = str(strategy_id).replace("/", "_")
        return self.root / f"{slug}.snapshot"

    def save(self, strategy_id: StrategyId, state: bytes) -> None:
        path = self._path(strategy_id)
        # Write to a temp file then rename for atomic publish on
        # POSIX. Avoids torn snapshots if the process dies mid-write.
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(state)
        tmp.replace(path)

    def load(self, strategy_id: StrategyId) -> bytes | None:
        path = self._path(strategy_id)
        if not path.exists():
            return None
        return path.read_bytes()

    def clear(self, strategy_id: StrategyId) -> None:
        path = self._path(strategy_id)
        if path.exists():
            path.unlink()
