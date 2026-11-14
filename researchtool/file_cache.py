"""Small in-memory caches for files the panel re-reads on every request.

The library listing, global search and paper workspace read every paper's manifest,
reader state and OCR HTML on each call. These files change only when the panel writes
them, so results are kept per file and reused until the file's signature changes:
(inode, size, mtime_ns). Writers that go through web_panel.write_durable_text also
invalidate explicitly, which covers two writes landing in the same timestamp tick.
"""
from __future__ import annotations

import copy
import json
import os
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable

MISSING = object()


def signature(path: Path) -> tuple[int, int, int] | None:
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return (stat.st_ino, stat.st_size, stat.st_mtime_ns)


class StatCache:
    """Values derived from one file, recomputed when the file's signature changes."""

    def __init__(self, max_entries: int = 4096) -> None:
        self.max_entries = max_entries
        self.entries: OrderedDict[tuple[str, str], tuple[tuple[int, int, int], Any]] = OrderedDict()
        self.lock = threading.Lock()

    def get(self, path: Path, compute: Callable[[Path], Any], *, kind: str = "") -> Any:
        """compute(path) once per file version; None/MISSING results are never cached for missing files."""
        sig = signature(path)
        if sig is None:
            return compute(path)
        key = (kind, os.path.normcase(str(path)))
        with self.lock:
            cached = self.entries.get(key)
            if cached and cached[0] == sig:
                self.entries.move_to_end(key)
                return cached[1]
        value = compute(path)
        # A file rewritten while compute() ran keeps the older signature out of the cache.
        if signature(path) == sig:
            with self.lock:
                self.entries[key] = (sig, value)
                self.entries.move_to_end(key)
                while len(self.entries) > self.max_entries:
                    self.entries.popitem(last=False)
        return value

    def read_json(self, path: Path) -> Any:
        """Parsed JSON (a private deep copy, so callers may mutate it), or MISSING when unreadable."""
        value = self.get(path, _load_json, kind="json")
        return MISSING if value is MISSING else copy.deepcopy(value)

    def invalidate(self, path: Path) -> None:
        normalized = os.path.normcase(str(path))
        with self.lock:
            for key in [key for key in self.entries if key[1] == normalized]:
                del self.entries[key]

    def clear(self) -> None:
        with self.lock:
            self.entries.clear()


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return MISSING


FILES = StatCache()
