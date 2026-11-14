"""Locate the Claude Code project a paper lives in, so revision sessions run (and are listed) there."""
from __future__ import annotations

import re
from pathlib import Path


def history_dir(folder: Path) -> Path:
    """Where Claude Code keeps the sessions of a project folder (~/.claude/projects/<encoded path>)."""
    return Path.home() / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(folder))


def find_claude_project(path: str | Path) -> dict | None:
    """Closest ancestor of ``path`` that is a Claude Code project: it has CLAUDE.md, a .claude/
    folder, or existing Claude Code session history. The home directory itself is never chosen
    (~/.claude is the global configuration, not a project)."""
    try:
        start = Path(path).resolve()
    except OSError:
        return None
    start = start if start.is_dir() else start.parent
    home = Path.home().resolve()
    for folder in [start, *start.parents]:
        if folder == home or folder == folder.parent:
            break
        if (folder / "CLAUDE.md").is_file():
            return {"path": str(folder), "reason": "CLAUDE.md"}
        if (folder / ".claude").is_dir():
            return {"path": str(folder), "reason": ".claude"}
        if history_dir(folder).is_dir():
            return {"path": str(folder), "reason": "history"}
    return None
