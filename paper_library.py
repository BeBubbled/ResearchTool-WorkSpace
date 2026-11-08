"""One paper library for the whole client, built on the reader's local cache.

Every paper opened in 文献精读 is stored once in the reader cache, keyed by a content
fingerprint (the "cache key"). This module is the single way other modules reach those
papers: Research Gaps imports the richest text artifact, ResearchStudio takes the original
PDF, and the library page lists everything with where it is used. Served at /api/library.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable

CACHE_KEY = re.compile(r"[0-9a-f]{64}")


def valid_key(cache_key: Any) -> bool:
    return isinstance(cache_key, str) and bool(CACHE_KEY.fullmatch(cache_key))


class PaperLibrary:
    def __init__(
        self,
        *,
        list_entries: Callable[[], list[dict[str, Any]]],
        get_entry: Callable[[str], dict[str, Any] | None],
        cached_file: Callable[[str, str, str], Path | None],
        raw_manifest: Callable[[str], dict[str, Any]] = lambda key: {},
        usage: Callable[[], dict[str, dict[str, list[dict[str, str]]]]] | None = None,
        notes: Callable[[str], list[dict[str, Any]]] = lambda key: [],
        analyses: Callable[[str], list[dict[str, Any]]] = lambda key: [],
        studio_jobs: Callable[[], list[dict[str, Any]]] = lambda: [],
    ) -> None:
        """
        list_entries / get_entry: reader-cache library entries (see web_panel.reader_cache_library_entry).
        cached_file(cache_key, storage, filename): a file inside one cache entry, or None.
        raw_manifest(cache_key): the cache manifest, for older entries that predate "document".
        usage(): {module: {cache_key: [{id, name}]}} — where each paper is referenced.
        notes(cache_key) / analyses(cache_key): reading notes and Three-Pass snapshots of one paper.
        studio_jobs(): ResearchStudio jobs; the workspace keeps those made from this paper.
        """
        self.list_entries = list_entries
        self.get_entry = get_entry
        self.cached_file = cached_file
        self.raw_manifest = raw_manifest
        self.usage = usage or (lambda: {})
        self.notes = notes
        self.analyses = analyses
        self.studio_jobs = studio_jobs

    # ---- sources other modules consume ----
    def analysis_source(self, cache_key: str) -> dict[str, Any] | None:
        """Richest durable text artifact (OCR HTML, Markdown, or the PDF) for analysis."""
        entry = self.get_entry(cache_key) if valid_key(cache_key) else None
        if not entry:
            return None
        storage = str(entry.get("storageRoot") or "")
        filename = entry.get("documentFilename") if entry.get("renderKind") == "html" else entry.get("sourceFilename")
        if not storage or not filename:
            return None
        path = self.cached_file(cache_key, storage, str(filename))
        return {"entry": entry, "path": str(path)} if path else None

    def pdf_source(self, cache_key: str) -> Path | None:
        """The original PDF of a library paper, if one was imported."""
        if not valid_key(cache_key):
            return None
        manifest = self.raw_manifest(cache_key) or {}
        for section in (manifest.get("document") or {}, manifest.get("ocr") or {}):
            if not isinstance(section, dict):
                continue
            filename = str(section.get("sourceFilename") or section.get("sourceName") or "")
            if Path(filename).suffix.lower() != ".pdf":
                continue
            for storage in ("source", "ocr"):
                path = self.cached_file(cache_key, storage, filename)
                if path:
                    return path
        return None

    # ---- the library listing ----
    def _public(self, entry: dict[str, Any], usage: dict[str, dict[str, list[dict[str, str]]]]) -> dict[str, Any]:
        key = entry["id"]
        has_pdf = self.pdf_source(key) is not None
        used = {module: refs.get(key, []) for module, refs in usage.items() if refs.get(key)}
        return {
            "id": key,
            "title": entry.get("title") or "阅读文档",
            "sourceName": entry.get("sourceName") or "",
            "sourceType": entry.get("sourceType") or "",
            "renderKind": entry.get("renderKind") or "",
            "useOcr": bool(entry.get("useOcr")),
            "hasPdf": has_pdf,
            "hasTranslation": bool(entry.get("hasTranslation")),
            "updatedAt": entry.get("updatedAt") or 0,
            "usedBy": used,
            "links": {
                "workspace": f"/library/{key}",
                "reader": f"/reader?library={key}",
                **({"studio": f"/researchstudio?paperId={key}"} if has_pdf else {}),
                "gaps": f"/research-gaps?libraryPaper={key}",
            },
        }

    def documents(self, *, query: str = "", pdf_only: bool = False) -> list[dict[str, Any]]:
        try:
            usage = self.usage()
        except Exception:  # usage is a hint; never block the listing
            usage = {}
        items = [self._public(entry, usage) for entry in self.list_entries() if valid_key(entry.get("id"))]
        if pdf_only:
            items = [item for item in items if item["hasPdf"]]
        needle = query.strip().casefold()
        if needle:
            items = [item for item in items if needle in f"{item['title']}\n{item['sourceName']}".casefold()]
        return items

    def document(self, cache_key: str) -> dict[str, Any] | None:
        entry = self.get_entry(cache_key) if valid_key(cache_key) else None
        if not entry:
            return None
        try:
            usage = self.usage()
        except Exception:
            usage = {}
        return self._public(entry, usage)

    # ---- one paper's workspace ----
    @staticmethod
    def _safe(call: Callable[[], Any], fallback: Any) -> Any:
        try:
            return call()
        except Exception:  # one module failing must not hide the rest of the workspace
            return fallback

    def workspace(self, cache_key: str) -> dict[str, Any] | None:
        """Everything the client knows about one paper: files, notes, Three-Pass, projects, Studio jobs."""
        document = self.document(cache_key)
        if not document:
            return None
        notes = [note for note in self._safe(lambda: self.notes(cache_key), []) if isinstance(note, dict)]
        notes.sort(key=lambda note: float(note.get("updatedAt") or note.get("createdAt") or 0), reverse=True)
        analyses = [_analysis(item) for item in self._safe(lambda: self.analyses(cache_key), []) if isinstance(item, dict)]
        studio = [_studio_job(job) for job in self._safe(self.studio_jobs, [])
                  if isinstance(job, dict) and job.get("paperId") == cache_key]
        files = [{"kind": "pdf", "label": "原始 PDF", "url": f"/api/library/{cache_key}/pdf"}] if document["hasPdf"] else []
        return {
            "document": document,
            "files": files,
            "notes": [_note(note) for note in notes],
            "analyses": analyses,
            "gaps": document["usedBy"].get("gaps", []),
            "studio": studio,
        }


def _text(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _note(note: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": _text(note.get("id"), 80),
        "kind": "answer" if note.get("kind") == "answer" else "selection",
        "section": _text(note.get("section"), 200),
        "action": _text(note.get("action"), 120),
        "question": _text(note.get("question"), 2000),
        "selection": _text(note.get("selection"), 4000),
        "text": _text(note.get("text"), 30000),
        "createdAt": note.get("createdAt") or 0,
    }


def _analysis(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": _text(item.get("id"), 64),
        "status": _text(item.get("status"), 40),
        "phaseLabel": _text(item.get("phaseLabel"), 80),
        "backend": _text(item.get("backend"), 20),
        "model": _text(item.get("model"), 120),
        "language": _text(item.get("language"), 20),
        "createdAt": item.get("createdAt") or 0,
        "reportUrl": item.get("reportUrl") if item.get("reportReady") else None,
    }


def _studio_job(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": _text(job.get("id"), 64),
        "label": _text(job.get("label") or job.get("module"), 80),
        "status": _text(job.get("status"), 40),
        "error": _text(job.get("error"), 400),
        "createdAt": job.get("createdAt") or 0,
    }
