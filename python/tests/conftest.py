"""Shared pytest configuration.

Provides a ``REPO_ROOT`` constant so tests can reference repo-relative
paths (e.g. ``configs/...``) without depending on pytest's working
directory.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
"""Filesystem path to the repository root (parent of ``python/``)."""
