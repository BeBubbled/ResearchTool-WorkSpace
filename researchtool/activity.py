"""One activity feed for every long-running job in the client.

Each module keeps its own job records (toolbox jobs, Three-Pass analyses, ResearchStudio,
Research Gap analysis/import jobs, bilingual revision rounds). This module turns them into
one shape so the client shell can show running counts on the rail and notify when a job
finishes: {id, module, title, state, status, createdAt, finishedAt, url}.

state is one of: active, done, warning, failed, cancelled.
Served at GET /api/activity; the sources are passed in as callables so a failing module
never hides the others.
"""
from __future__ import annotations

from typing import Any, Callable, Iterable

ACTIVE_STATUSES = {"queued", "running", "cancelling", "processing", "pending", "scanning", "extracting", "starting"}
WARNING_STATUSES = {"completed_with_warnings", "incomplete", "paused"}
FAILED_STATUSES = {"failed", "error", "interrupted"}
CANCELLED_STATUSES = {"cancelled", "canceled", "stopped"}
MODULES = ("toolbox", "reader", "studio", "gaps", "revision")


def activity_state(status: Any, *, busy: bool = False) -> str:
    value = str(status or "").strip().lower()
    if busy or value in ACTIVE_STATUSES:
        return "active"
    if value in WARNING_STATUSES:
        return "warning"
    if value in FAILED_STATUSES:
        return "failed"
    if value in CANCELLED_STATUSES:
        return "cancelled"
    return "done"


def activity_item(
    *, id: str, module: str, title: str, status: Any, url: str = "",
    created_at: Any = None, finished_at: Any = None, busy: bool = False, detail: str = "",
) -> dict[str, Any]:
    state = activity_state(status, busy=busy)
    created = _time(created_at)
    finished = _time(finished_at) if state != "active" else None
    return {
        "id": str(id), "module": module if module in MODULES else "toolbox", "title": str(title or "任务")[:160],
        "state": state, "status": str(status or ""), "detail": str(detail or "")[:200],
        "createdAt": created, "finishedAt": finished or (created if state != "active" else None), "url": url,
    }


def _time(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def collect(sources: Iterable[Callable[[], Iterable[dict[str, Any]]]], *, limit: int = 60) -> dict[str, Any]:
    """Run every source, newest first; active jobs always sort before finished ones."""
    items: list[dict[str, Any]] = []
    errors = 0
    for source in sources:
        try:
            items.extend(item for item in source() if isinstance(item, dict))
        except Exception:  # one module failing must not hide the others
            errors += 1
    items.sort(key=lambda item: (item["state"] != "active", -(item.get("finishedAt") or item.get("createdAt") or 0)))
    items = items[:limit]
    counts = {module: 0 for module in MODULES}
    for item in items:
        if item["state"] == "active":
            counts[item["module"]] = counts.get(item["module"], 0) + 1
    return {"items": items, "active": counts, "activeTotal": sum(counts.values()), "sourceErrors": errors}
