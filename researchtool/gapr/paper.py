"""各数据源统一的论文记录，以及回测的日期防火墙。"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import date


@dataclass
class Paper:
    title: str
    source: str                 # arxiv / s2 / openalex / openreview
    source_id: str
    first_public: str = ""      # YYYY-MM-DD，或只有 YYYY
    date_basis: str = ""        # arXiv v1 / publicationDate / year
    arxiv_id: str = ""
    s2_id: str = ""
    doi: str = ""
    acl_id: str = ""
    venue: str = ""
    year: int | None = None
    citation_count: int | None = None
    abstract: str = ""
    url: str = ""
    authors: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)

    @property
    def first_surname(self) -> str:
        return self.authors[0].split()[-1] if self.authors and self.authors[0].split() else ""

    @property
    def key(self) -> str:
        if self.arxiv_id:
            return f"arXiv:{self.arxiv_id}"
        if self.acl_id:
            return f"ACL:{self.acl_id}"
        if self.s2_id:
            return self.s2_id
        if self.doi:
            return f"DOI:{self.doi}"
        return f"{self.source}:{self.source_id}"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["key"] = self.key
        return d


_ARXIV_ID = re.compile(r"(\d{4}\.\d{4,5}|[a-z\-]+(?:\.[A-Z]{2})?/\d{7})(v\d+)?$", re.I)


def norm_arxiv_id(raw: str) -> str:
    """'arXiv:2211.14730v2' / 'http://arxiv.org/abs/2211.14730v2' -> '2211.14730'"""
    s = raw.strip()
    s = re.sub(r"^(arxiv:)", "", s, flags=re.I)
    s = s.rsplit("/abs/", 1)[-1].rsplit("/pdf/", 1)[-1]
    m = _ARXIV_ID.search(s)
    return m.group(1) if m else s


def after_cutoff(p: Paper, cutoff: date) -> bool | None:
    """True = 晚于截止日；False = 不晚于；None = 判断不了（无日期，或只有同一年份）。"""
    fp = p.first_public.strip()
    if not fp:
        return None
    if len(fp) == 4:
        y = int(fp)
        if y > cutoff.year:
            return True
        if y < cutoff.year:
            return False
        return None
    return date.fromisoformat(fp[:10]) > cutoff


def apply_cutoff(papers: list[Paper], cutoff: date | None) -> tuple[list[Paper], list[Paper]]:
    """回测防火墙：去掉截止日之后公开的论文。判断不了日期的保留，但打上“日期待核”标记。"""
    if cutoff is None:
        return papers, []
    kept, dropped = [], []
    for p in papers:
        r = after_cutoff(p, cutoff)
        if r is True:
            dropped.append(p)
        else:
            if r is None:
                p.flags.append("日期待核")
            kept.append(p)
    return kept, dropped
