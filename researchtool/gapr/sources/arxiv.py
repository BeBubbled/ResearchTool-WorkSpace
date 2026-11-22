"""arXiv API：按日期检索，以及按 id 取首次公开日期（Atom 里的 published = v1 提交时间）。"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import date

from researchtool.gapr.net import Http
from researchtool.gapr.paper import Paper, norm_arxiv_id

API = "https://export.arxiv.org/api/query"
NS = {"a": "http://www.w3.org/2005/Atom", "os": "http://a9.com/-/spec/opensearch/1.1/",
      "arxiv": "http://arxiv.org/schemas/atom"}


def build_query(query: str, date_from: date | None = None, date_to: date | None = None) -> str:
    """普通关键词转成 all:w1 AND all:w2；已含字段前缀（如 ti:、abs:）的原样使用。
    双引号里的短语保留为一个整体。"""
    q = query.strip()
    if not re.search(r"\b(ti|abs|au|all|cat|co|jr|id):", q):
        terms = re.findall(r'"[^"]+"|\S+', q)
        q = " AND ".join(f"all:{t}" for t in terms)
    if date_from or date_to:
        lo = (date_from or date(1991, 1, 1)).strftime("%Y%m%d") + "0000"
        hi = (date_to or date.today()).strftime("%Y%m%d") + "2359"
        q = f"({q}) AND submittedDate:[{lo} TO {hi}]"
    return q


def parse_feed(xml_text: str) -> tuple[list[Paper], int]:
    root = ET.fromstring(xml_text)
    total_el = root.find("os:totalResults", NS)
    total = int(total_el.text) if total_el is not None and total_el.text else 0
    out = []
    for e in root.findall("a:entry", NS):
        raw_id = (e.findtext("a:id", default="", namespaces=NS) or "").strip()
        if "/api/errors" in raw_id:
            raise ValueError("arXiv 返回错误：" + (e.findtext("a:summary", "", NS) or "").strip())
        aid = norm_arxiv_id(raw_id)
        published = (e.findtext("a:published", default="", namespaces=NS) or "")[:10]
        jref = e.findtext("arxiv:journal_ref", default="", namespaces=NS) or ""
        out.append(Paper(
            title=" ".join((e.findtext("a:title", "", NS) or "").split()),
            source="arxiv", source_id=aid, arxiv_id=aid,
            first_public=published, date_basis="arXiv v1" if published else "",
            year=int(published[:4]) if published else None,
            venue=" ".join(jref.split()),
            abstract=" ".join((e.findtext("a:summary", "", NS) or "").split()),
            url=f"https://arxiv.org/abs/{aid}",
            authors=[" ".join((a.findtext("a:name", "", NS) or "").split()) for a in e.findall("a:author", NS)],
        ))
    return out, total


def search(http: Http, query: str, *, limit: int = 20, date_from: date | None = None,
           date_to: date | None = None, sort: str = "relevance") -> tuple[list[Paper], int, bool]:
    params = {"search_query": build_query(query, date_from, date_to), "start": 0,
              "max_results": limit, "sortBy": sort, "sortOrder": "descending"}
    body, cached = http.get_text(API, params)
    papers, total = parse_feed(body)
    return papers, total, cached


def by_ids(http: Http, ids: list[str]) -> list[Paper]:
    """批量取论文（每批 50 个）。用于给其他来源的论文补上 arXiv v1 日期。"""
    ids = [norm_arxiv_id(i) for i in ids if i]
    out: list[Paper] = []
    for i in range(0, len(ids), 50):
        chunk = ids[i:i + 50]
        body, _ = http.get_text(API, {"id_list": ",".join(chunk), "max_results": len(chunk)})
        out.extend(parse_feed(body)[0])
    return out


def resolve_dates(http: Http, papers: list[Paper]) -> None:
    """有 arXiv id 但日期不是 arXiv v1 的论文，改用 arXiv v1 日期（模板全程规则第 2 条）。"""
    need = [p for p in papers if p.arxiv_id and p.date_basis != "arXiv v1"]
    if not need:
        return
    found = {p.arxiv_id: p for p in by_ids(http, [p.arxiv_id for p in need])}
    for p in need:
        a = found.get(norm_arxiv_id(p.arxiv_id))
        if a and a.first_public:
            p.first_public, p.date_basis = a.first_public, "arXiv v1"
