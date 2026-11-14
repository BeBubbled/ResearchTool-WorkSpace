"""BibTeX for papers: online lookup with fallbacks, and a local entry when offline.

Order of sources for a title:
  1. Crossref (bibliographic search, then DOI content negotiation for BibTeX)
  2. Semantic Scholar (title match; its citationStyles.bibtex)
  3. a minimal local @misc entry built from what we know (title, authors, year)
A result is accepted only when its title closely matches the requested one, so a search
miss never silently returns a different paper.
"""
from __future__ import annotations

import difflib
import re
import unicodedata
from typing import Any, Callable

import requests

USER_AGENT = "ResearchToolkit/1.0 (local research toolbox)"
TIMEOUT = 15
TITLE_MATCH = 0.86
_NON_WORD = re.compile(r"[^0-9a-z]+")


def title_key(title: Any) -> str:
    text = unicodedata.normalize("NFKD", str(title or "")).encode("ascii", "ignore").decode("ascii").lower()
    return _NON_WORD.sub(" ", text).strip()


def titles_match(left: Any, right: Any, threshold: float = TITLE_MATCH) -> bool:
    a, b = title_key(left), title_key(right)
    if not a or not b:
        return False
    return a == b or difflib.SequenceMatcher(None, a, b).ratio() >= threshold


def citation_key(title: Any, authors: Any = None, year: Any = None) -> str:
    surname = ""
    if isinstance(authors, (list, tuple)) and authors:
        first = str(authors[0]).strip()
        surname = first.split(",")[0].split()[-1] if first else ""
    elif isinstance(authors, str) and authors.strip():
        surname = authors.split(" and ")[0].split(",")[0].split()[-1]
    words = [word for word in title_key(title).split() if len(word) > 3 and word not in {"with", "from", "that", "this", "towards", "using"}]
    key = f"{title_key(surname).replace(' ', '')}{year or ''}{words[0] if words else 'paper'}"
    return key or "paper"


def _escape(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")


def local_bibtex(title: str, authors: Any = None, year: Any = None, *, note: str = "") -> str:
    """A minimal entry when no online source matched; clearly marked for manual completion."""
    names = " and ".join(str(name) for name in authors) if isinstance(authors, (list, tuple)) else str(authors or "")
    fields = [f"  title = {{{_escape(title)}}}"]
    if names.strip():
        fields.append(f"  author = {{{_escape(names)}}}")
    if year:
        fields.append(f"  year = {{{_escape(str(year))}}}")
    fields.append(f"  note = {{{_escape(note or '本地生成，未在线核实，请补全')}}}")
    return "@misc{" + citation_key(title, authors, year) + ",\n" + ",\n".join(fields) + "\n}"


def plain_citation(title: str, authors: Any = None, year: Any = None, venue: str = "") -> str:
    names = list(authors) if isinstance(authors, (list, tuple)) else [str(authors)] if authors else []
    author_text = ", ".join(names[:3]) + (" et al." if len(names) > 3 else "")
    parts = [part for part in (author_text, f"({year})" if year else "", f"{title}.", venue) if part]
    return " ".join(parts).strip()


class CitationResolver:
    def __init__(self, get: Callable[..., Any] | None = None, semantic_scholar_key: Callable[[], str] = lambda: "") -> None:
        self.get = get or requests.get
        self.semantic_scholar_key = semantic_scholar_key

    def _crossref(self, title: str) -> dict[str, Any] | None:
        response = self.get(
            "https://api.crossref.org/works",
            params={"query.bibliographic": title, "rows": 3, "select": "DOI,title,author,issued"},
            headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT,
        )
        if response.status_code != 200:
            return None
        for item in (response.json().get("message") or {}).get("items") or []:
            found = (item.get("title") or [""])[0]
            if titles_match(found, title) and item.get("DOI"):
                bib = self.get(
                    f"https://doi.org/{item['DOI']}",
                    headers={"Accept": "application/x-bibtex; charset=utf-8", "User-Agent": USER_AGENT}, timeout=TIMEOUT,
                )
                if bib.status_code == 200 and bib.text.strip().startswith("@"):
                    return {"bibtex": bib.text.strip(), "source": "crossref", "doi": item["DOI"], "title": found}
        return None

    def _semantic_scholar(self, title: str) -> dict[str, Any] | None:
        headers = {"User-Agent": USER_AGENT}
        key = self.semantic_scholar_key()
        if key:
            headers["x-api-key"] = key
        response = self.get(
            "https://api.semanticscholar.org/graph/v1/paper/search/match",
            params={"query": title, "fields": "title,citationStyles,externalIds,year,authors"}, headers=headers, timeout=TIMEOUT,
        )
        if response.status_code != 200:
            return None
        for item in response.json().get("data") or []:
            bibtex = ((item.get("citationStyles") or {}).get("bibtex") or "").strip()
            if titles_match(item.get("title"), title) and bibtex.startswith("@"):
                return {"bibtex": bibtex, "source": "semantic_scholar", "doi": (item.get("externalIds") or {}).get("DOI"), "title": item.get("title")}
        return None

    def resolve(self, title: str, *, authors: Any = None, year: Any = None, online: bool = True) -> dict[str, Any]:
        title = str(title or "").strip()
        if not title:
            raise ValueError("缺少论文标题。")
        errors = []
        if online:
            for source in (self._crossref, self._semantic_scholar):
                try:
                    found = source(title)
                except (requests.RequestException, ValueError) as exc:
                    errors.append(f"{source.__name__.strip('_')}: {exc.__class__.__name__}")
                    continue
                if found:
                    return {**found, "verified": True}
        return {
            "bibtex": local_bibtex(title, authors, year), "source": "local", "verified": False, "title": title,
            "errors": errors,
        }
