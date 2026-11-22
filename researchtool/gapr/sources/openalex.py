"""OpenAlex：检索、单篇、被引列表（正向追踪）、参考文献（向前补漏）。不提供引用句。

API key：设置环境变量 OPENALEX_API_KEY 后自动带上（作为 api_key 参数，不进缓存）。
没有 key 时按 IP 共享每日免费额度（2026-11 实测的说明），申请：https://openalex.org/settings/api
"""

from __future__ import annotations

import os
from datetime import date

from researchtool.gapr.net import Http
from researchtool.gapr.paper import Paper, norm_arxiv_id

BASE = "https://api.openalex.org"
SELECT = ("id,display_name,title,publication_date,publication_year,ids,doi,locations,"
          "primary_location,cited_by_count,abstract_inverted_index,authorships")
ACL_DOI = "10.18653/v1/"


def _secret() -> dict:
    key = os.environ.get("OPENALEX_API_KEY", "").strip()
    return {"api_key": key} if key else {}


def has_key() -> bool:
    return bool(_secret())


def _abstract(inv: dict | None) -> str:
    if not inv:
        return ""
    pos = sorted((i, w) for w, idx in inv.items() for i in idx)
    return " ".join(w for _, w in pos)


def to_paper(d: dict) -> Paper:
    ids = d.get("ids") or {}
    doi = (ids.get("doi") or d.get("doi") or "").replace("https://doi.org/", "")
    arxiv = ""
    for loc in d.get("locations") or []:
        u = (loc.get("landing_page_url") or "")
        if "arxiv.org/abs/" in u:
            arxiv = norm_arxiv_id(u)
            break
    if not arxiv and doi.lower().startswith("10.48550/arxiv."):
        arxiv = norm_arxiv_id(doi[len("10.48550/arxiv."):])
    acl = doi[len(ACL_DOI):] if doi.lower().startswith(ACL_DOI) else ""
    src = ((d.get("primary_location") or {}).get("source") or {})
    pub = d.get("publication_date") or ""
    return Paper(
        title=" ".join((d.get("title") or d.get("display_name") or "").split()),
        source="openalex", source_id=(d.get("id") or "").rsplit("/", 1)[-1],
        first_public=pub, date_basis="publicationDate" if pub else "",
        arxiv_id=arxiv, doi=doi, acl_id=acl, venue=src.get("display_name") or "",
        year=d.get("publication_year"), citation_count=d.get("cited_by_count"),
        abstract=_abstract(d.get("abstract_inverted_index")),
        url=d.get("doi") or d.get("id") or "",
        authors=[((a.get("author") or {}).get("display_name") or "") for a in d.get("authorships") or []],
    )


def search(http: Http, query: str, *, limit: int = 20, date_from: date | None = None,
           date_to: date | None = None) -> tuple[list[Paper], int, bool]:
    filters = []
    if date_from:
        filters.append(f"from_publication_date:{date_from.isoformat()}")
    if date_to:
        filters.append(f"to_publication_date:{date_to.isoformat()}")
    params = {"search": query, "per-page": min(limit, 200), "filter": ",".join(filters), "select": SELECT}
    d, cached = http.get_json(f"{BASE}/works", params, secret_params=_secret())
    return [to_paper(x) for x in d.get("results") or []], int((d.get("meta") or {}).get("count") or 0), cached


def work_path(key: str) -> str:
    """arXiv:… / ACL:… / DOI:… / W… 转成 OpenAlex 的单篇路径。"""
    k = key.strip()
    low = k.lower()
    if low.startswith("arxiv:"):
        return f"doi:10.48550/arXiv.{norm_arxiv_id(k)}"
    if low.startswith("acl:"):
        return f"doi:{ACL_DOI}{k[4:]}"
    if low.startswith("doi:"):
        return "doi:" + k[4:]
    if low.startswith("openalex:"):
        return k.split(":", 1)[1]
    if k[:1] in "Ww" and k[1:].isdigit():
        return k.upper()
    raise ValueError(f"{key}：OpenAlex 只认 arXiv:… / ACL:… / DOI:… / W 开头的 id")


def work(http: Http, key: str) -> tuple[Paper, list[str]]:
    """单篇论文，以及它的参考文献 id 列表（referenced_works）。"""
    d, _ = http.get_json(f"{BASE}/works/{work_path(key)}", {"select": SELECT + ",referenced_works"},
                         secret_params=_secret())
    refs = [(r or "").rsplit("/", 1)[-1] for r in d.get("referenced_works") or []]
    return to_paper(d), refs


def citations(http: Http, key: str, *, max_items: int = 2000) -> tuple[list[dict], bool]:
    """被引列表，按被引数从高到低。格式与 s2.citations 一致（contexts 为空）。"""
    p, _ = work(http, key)
    out, all_cached = [], True
    # 没有 key 时不允许 cursor 翻页（2026-11 实测），改用页码；按被引排序，前几页就够挑选
    cursor, page = ("*", None) if has_key() else (None, 1)
    while len(out) < max_items:
        params = {"filter": f"cites:{p.source_id}", "per-page": 200,
                  "sort": "cited_by_count:desc", "select": SELECT}
        if cursor:
            params["cursor"] = cursor
        else:
            params["page"] = page
        d, cached = http.get_json(f"{BASE}/works", params, secret_params=_secret())
        all_cached &= cached
        res = d.get("results") or []
        out.extend(res)
        if not res or len(res) < 200:
            break
        if cursor:
            cursor = (d.get("meta") or {}).get("next_cursor")
            if not cursor:
                break
        else:
            page += 1
    return [{"paper": to_paper(x), "contexts": [], "intents": [], "influential": False}
            for x in out[:max_items]], all_cached


def references(http: Http, key: str, *, max_items: int = 1000) -> tuple[list[dict], bool]:
    _, refs = work(http, key)
    refs = refs[:max_items]
    out, all_cached = [], True
    for i in range(0, len(refs), 50):
        chunk = refs[i:i + 50]
        params = {"filter": "openalex_id:" + "|".join(chunk), "per-page": 50, "select": SELECT}
        d, cached = http.get_json(f"{BASE}/works", params, secret_params=_secret())
        all_cached &= cached
        out.extend(d.get("results") or [])
    return [{"paper": to_paper(x), "contexts": [], "intents": [], "influential": False} for x in out], all_cached
