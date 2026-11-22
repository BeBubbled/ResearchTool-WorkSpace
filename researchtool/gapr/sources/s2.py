"""Semantic Scholar Graph API：检索、单篇、参考文献、被引列表（含引用上下文）。

论文 id 可以写成 S2 paperId，或 arXiv:2211.14730、ACL:2020.acl-main.1、DOI:…、CorpusId:…
设置环境变量 S2_API_KEY 后自动带上；没有 key 时与所有人共用额度，很容易 429。
"""

from __future__ import annotations

import os
from datetime import date

from researchtool.gapr.net import Http
from researchtool.gapr.paper import Paper, norm_arxiv_id

BASE = "https://api.semanticscholar.org/graph/v1"
FIELDS = "paperId,title,abstract,year,publicationDate,venue,externalIds,citationCount,url,authors"
CITING_FIELDS = ("contexts,intents,isInfluential,paperId,title,year,publicationDate,"
                 "venue,externalIds,citationCount,authors")


def _headers() -> dict:
    # 面板里叫 SEMANTIC_SCHOLAR_API_KEY；单独用命令行时也认 S2_API_KEY
    key = (os.environ.get("S2_API_KEY") or os.environ.get("SEMANTIC_SCHOLAR_API_KEY") or "").strip()
    return {"x-api-key": key} if key else {}


def has_key() -> bool:
    return bool(_headers())


def to_paper(d: dict) -> Paper:
    ext = d.get("externalIds") or {}
    pub = d.get("publicationDate") or ""
    year = d.get("year")
    p = Paper(
        title=" ".join((d.get("title") or "").split()),
        source="s2", source_id=d.get("paperId") or "", s2_id=d.get("paperId") or "",
        arxiv_id=norm_arxiv_id(ext["ArXiv"]) if ext.get("ArXiv") else "",
        doi=ext.get("DOI") or "", acl_id=ext.get("ACL") or "",
        venue=d.get("venue") or "", year=year,
        citation_count=d.get("citationCount"), abstract=d.get("abstract") or "",
        url=d.get("url") or "",
        authors=[a.get("name") or "" for a in d.get("authors") or []],
    )
    if pub:
        p.first_public, p.date_basis = pub, "publicationDate"
    elif year:
        p.first_public, p.date_basis = str(year), "year"
    return p


def _date_range(date_from: date | None, date_to: date | None) -> str:
    if not (date_from or date_to):
        return ""
    return f"{date_from.isoformat() if date_from else ''}:{date_to.isoformat() if date_to else ''}"


def search(http: Http, query: str, *, limit: int = 20, date_from: date | None = None,
           date_to: date | None = None) -> tuple[list[Paper], int, bool]:
    params = {"query": query, "limit": min(limit, 100), "fields": FIELDS,
              "publicationDateOrYear": _date_range(date_from, date_to)}
    d, cached = http.get_json(f"{BASE}/paper/search", params, _headers())
    return [to_paper(x) for x in d.get("data") or []], int(d.get("total") or 0), cached


def paper(http: Http, pid: str) -> Paper:
    d, _ = http.get_json(f"{BASE}/paper/{pid}", {"fields": FIELDS}, _headers())
    return to_paper(d)


def _paged(http: Http, url: str, fields: str, max_items: int) -> tuple[list[dict], bool]:
    out, offset, all_cached = [], 0, True
    while len(out) < max_items:
        page = min(1000, max_items - len(out))
        d, cached = http.get_json(url, {"fields": fields, "limit": page, "offset": offset}, _headers())
        all_cached &= cached
        data = d.get("data") or []
        out.extend(data)
        if "next" not in d or not data:
            break
        offset = d["next"]
    return out, all_cached


def citations(http: Http, pid: str, *, max_items: int = 2000) -> tuple[list[dict], bool]:
    """被引列表。每项：{paper: Paper, contexts: [str], intents: [str], influential: bool}"""
    raw, cached = _paged(http, f"{BASE}/paper/{pid}/citations", CITING_FIELDS, max_items)
    return [{"paper": to_paper(x.get("citingPaper") or {}), "contexts": x.get("contexts") or [],
             "intents": x.get("intents") or [], "influential": bool(x.get("isInfluential"))}
            for x in raw if x.get("citingPaper", {}).get("paperId")], cached


def references(http: Http, pid: str, *, max_items: int = 1000) -> tuple[list[dict], bool]:
    raw, cached = _paged(http, f"{BASE}/paper/{pid}/references", CITING_FIELDS, max_items)
    return [{"paper": to_paper(x.get("citedPaper") or {}), "contexts": x.get("contexts") or [],
             "intents": x.get("intents") or [], "influential": bool(x.get("isInfluential"))}
            for x in raw if (x.get("citedPaper") or {}).get("paperId")], cached


def critique_contexts(contexts: list[str], keywords: list[str]) -> list[str]:
    """第 2 步“他人指出”：只保留含批评词的引用句。"""
    kws = [k.lower() for k in keywords]
    return [c for c in contexts if any(k in c.lower() for k in kws)]
