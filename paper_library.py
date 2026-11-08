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
    ) -> None:
        """
        list_entries / get_entry: reader-cache library entries (see web_panel.reader_cache_library_entry).
        cached_file(cache_key, storage, filename): a file inside one cache entry, or None.
        raw_manifest(cache_key): the cache manifest, for older entries that predate "document".
        usage(): {module: {cache_key: [{id, name}]}} — where each paper is referenced.
        """
        self.list_entries = list_entries
        self.get_entry = get_entry
        self.cached_file = cached_file
        self.raw_manifest = raw_manifest
        self.usage = usage or (lambda: {})

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
