"""Search everything the client has written down, not just paper titles.

Sources (each a callable, so one failing module never hides the rest):
  papers      library titles / file names
  notes       reader notes: selection, prompt, question and answer text
  analyses    Three-Pass Markdown (pass1-3 and report)
  comments    bilingual revision comments and the quoted text they anchor to
  gaps        research-gap titles and descriptions
  prompts     Prompt workbench templates
Served at GET /api/search?q=; the command palette shows the results.
"""
from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Any, Callable, Iterable

MAX_FILE_CHARS = 400_000
KIND_LABELS = {
    "paper": "论文", "note": "笔记", "analysis": "Three-Pass", "comment": "改稿批注", "gap": "研究空白", "prompt": "Prompt 模板",
}
_SPACE = re.compile(r"\s+")


def normalize(text: Any) -> str:
    return _SPACE.sub(" ", str(text or "")).strip()


def snippet(text: Any, needle: str, width: int = 90) -> str:
    """A short window of `text` around the first match of `needle` (case-insensitive)."""
    flat = normalize(text)
    if not flat:
        return ""
    index = flat.casefold().find(needle.casefold()) if needle else -1
    if index < 0:
        return flat[:width] + ("…" if len(flat) > width else "")
    start = max(0, index - width // 3)
    end = min(len(flat), start + width)
    return ("…" if start else "") + flat[start:end] + ("…" if end < len(flat) else "")


def matches(needle: str, *fields: Any) -> bool:
    folded = needle.casefold()
    return any(folded in normalize(field).casefold() for field in fields if field)


class FileTextCache:
    """Read Markdown reports once per modification time, keeping a folded copy for matching."""

    def __init__(self) -> None:
        self.cache: dict[str, tuple[float, str, str]] = {}
        self.lock = threading.Lock()

    def read(self, path: Path) -> str:
        return self.read_entry(path)[0]

    def read_entry(self, path: Path) -> tuple[str, str]:
        """(text, normalized casefolded text): every search scans the folded copy instead of re-folding."""
        try:
            stat = path.stat()
        except OSError:
            return "", ""
        key = str(path)
        with self.lock:
            cached = self.cache.get(key)
            if cached and cached[0] == stat.st_mtime:
                return cached[1], cached[2]
        try:
            text = path.read_text(encoding="utf-8", errors="replace")[:MAX_FILE_CHARS]
        except OSError:
            return "", ""
        folded = normalize(text).casefold()
        with self.lock:
            if len(self.cache) > 2000:
                self.cache.clear()
            self.cache[key] = (stat.st_mtime, text, folded)
        return text, folded


class GlobalSearch:
    def __init__(
        self,
        *,
        papers: Callable[[], list[dict[str, Any]]],
        notes: Callable[[str], list[dict[str, Any]]],
        analysis_files: Callable[[str], Iterable[tuple[str, Path]]],
        revision_projects: Callable[[], Iterable[dict[str, Any]]] = lambda: [],
        gaps: Callable[[str], Iterable[dict[str, Any]]] = lambda needle: [],
        prompts: Callable[[], Iterable[dict[str, Any]]] = lambda: [],
    ) -> None:
        """
        papers(): library documents ({id, title, sourceName}).
        notes(cache_key): reader notes of one paper.
        analysis_files(cache_key): (analysis id, Markdown path) pairs of one paper's Three-Pass runs.
        revision_projects(): full revision projects ({id, title, comments: [{id, text, anchors: [{quote}]}]}).
        gaps(needle): research gaps already filtered by the store ({id, projectId, canonicalTitle, description}).
        prompts(): Prompt templates ({id, title, content, category}).
        """
        self.papers = papers
        self.notes = notes
        self.analysis_files = analysis_files
        self.revision_projects = revision_projects
        self.gaps = gaps
        self.prompts = prompts
        self.files = FileTextCache()

    @staticmethod
    def _safe(call: Callable[[], Any]) -> list[Any]:
        try:
            return list(call() or [])
        except Exception:
            return []

    def search(self, query: str, *, limit: int = 40, per_kind: int = 12) -> list[dict[str, Any]]:
        needle = normalize(query)[:200]
        if len(needle) < 2:
            return []
        results: dict[str, list[dict[str, Any]]] = {kind: [] for kind in KIND_LABELS}
        folded_needle = needle.casefold()

        def add(kind: str, **item: Any) -> None:
            bucket = results[kind]
            if len(bucket) < per_kind:
                bucket.append({"kind": kind, "kindLabel": KIND_LABELS[kind], **item})

        papers = [paper for paper in self._safe(self.papers) if isinstance(paper, dict) and paper.get("id")]
        for paper in papers:
            key, title = paper["id"], paper.get("title") or "阅读文档"
            if matches(needle, title, paper.get("sourceName")):
                add("paper", title=title, snippet=paper.get("sourceName") or "", url=f"/reader?library={key}")
            for note in self._safe(lambda: self.notes(key)):
                if not isinstance(note, dict) or len(results["note"]) >= per_kind:
                    break
                fields = (note.get("selection"), note.get("text"), note.get("question"), note.get("action"))
                if matches(needle, *fields):
                    hit = next((field for field in fields if field and matches(needle, field)), "")
                    add("note", title=f"{note.get('action') or '笔记'} · {title}", snippet=snippet(hit, needle),
                        url=f"/reader?library={key}&inspector=notes&note={note.get('id') or ''}")
            if len(results["analysis"]) < per_kind:
                for analysis_id, path in self._safe(lambda: self.analysis_files(key)):
                    text, folded = self.files.read_entry(Path(path))
                    if text and folded_needle in folded:
                        add("analysis", title=f"{Path(path).stem} · {title}", snippet=snippet(text, needle),
                            url=f"/reader?library={key}&inspector=three-pass&analysis={analysis_id}")
                        break
        for project in self._safe(self.revision_projects):
            if not isinstance(project, dict):
                continue
            for comment in project.get("comments") or []:
                quotes = " ".join(str(anchor.get("quote") or "") for anchor in comment.get("anchors") or [] if isinstance(anchor, dict))
                if matches(needle, comment.get("text"), quotes):
                    add("comment", title=f"批注 · {project.get('title') or '论文'}",
                        snippet=snippet(comment.get("text") if matches(needle, comment.get("text")) else quotes, needle),
                        url=f"/paper-revision?project={project.get('id')}&comment={comment.get('id')}")
        for gap in self._safe(lambda: self.gaps(needle)):
            if isinstance(gap, dict) and matches(needle, gap.get("canonicalTitle"), gap.get("description")):
                add("gap", title=gap.get("canonicalTitle") or "研究空白", snippet=snippet(gap.get("description"), needle),
                    url=f"/research-gaps?project={gap.get('projectId')}&gap={gap.get('id')}")
        for template in self._safe(self.prompts):
            if isinstance(template, dict) and matches(needle, template.get("title"), template.get("content")):
                add("prompt", title=template.get("title") or "模板", snippet=snippet(template.get("content"), needle),
                    url=f"/prompts?template={template.get('id')}")
        ordered = [item for kind in KIND_LABELS for item in results[kind]]
        return ordered[:limit]
