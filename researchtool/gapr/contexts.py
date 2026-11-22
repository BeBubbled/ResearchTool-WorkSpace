"""从引用者的全文里抽出“引用了某篇论文”的句子（S2 引用句的替代）。

两种定位方式：
- html-bib（精确）：arXiv / ar5iv 的 HTML 里，正文引用是指向参考文献条目的链接
  （href="#bib.bibN"）。先在参考文献里按标题找到被引论文的条目，再取所有链接到它的句子。
- pdf-author-year（启发式）：PDF 没有链接，只能找“姓 et al. 年份”“姓 and 姓 年份”。
  数字编号格式（[12]）的论文找不到，会如实注明。
另外可用 --alias 指定方法名（如 PatchTST），提到方法名的句子也算，标为 alias。

抽出的句子来自登记过的全文，所以之后可以被原句核对检查。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from researchtool.gapr.evidence import normalize
from researchtool.gapr.fulltext import FulltextStore, _Html2Text, _looks_like_paper, fetch, html_to_text
from researchtool.gapr.net import FetchError, Http
from researchtool.gapr.paper import Paper

MARK = "\x00"
_MARK_RE = re.compile(r"\x00[^\x00]*\x00")
_ABBREV = ("et al.", "e.g.", "i.e.", "fig.", "figs.", "eq.", "eqs.", "sec.", "tab.", "cf.", "vs.", "resp.",
           "approx.", "no.", "vol.", "pp.", "al.", "etc.")
_REF_HEAD = re.compile(r"\n\s*(?:\d+\s*)?(References|Bibliography|REFERENCES)\s*\n")
MAX_SENT = 900


class _HtmlCites(_Html2Text):
    """在每个指向参考文献条目的链接前插入标记 \\x00bib.bibN\\x00。"""

    def handle_starttag(self, tag, attrs):
        if tag == "a" and not self.skip:
            href = dict(attrs).get("href") or ""
            if href.startswith("#bib."):
                self.out.append(f"{MARK}{href[1:]}{MARK}")
        super().handle_starttag(tag, attrs)


def bib_entries(html: str) -> dict[str, str]:
    out = {}
    for m in re.finditer(r'<li[^>]*\bid="(bib\.bib\d+)"[^>]*>(.*?)</li>', html, re.S):
        out[m.group(1)] = html_to_text(m.group(2))
    return out


def find_bib_ids(entries: dict[str, str], title: str) -> list[str]:
    t = normalize(title)
    if len(t) < 12:
        return []
    probe = t[:50]
    return [bid for bid, txt in entries.items() if probe in normalize(txt)]


def split_sentences(text: str) -> list[str]:
    out = []
    for para in text.split("\n"):
        pieces = re.split(r"(?<=[.!?])\s+(?=[A-Z\[“\"\x00])", para)
        buf = ""
        for p in pieces:
            buf = f"{buf} {p}" if buf else p
            if not buf.lower().rstrip().endswith(_ABBREV):
                out.append(buf.strip())
                buf = ""
        if buf:
            out.append(buf.strip())
    return [s for s in out if s]


def _clean(s: str) -> str:
    return " ".join(_MARK_RE.sub("", s).split())


def _before_references(text: str) -> str:
    ms = list(_REF_HEAD.finditer(text))
    return text[: ms[-1].start()] if ms else text


def from_html(html: str, cited: Paper, aliases: list[str]) -> tuple[list[dict], str]:
    entries = bib_entries(html)
    ids = find_bib_ids(entries, cited.title)
    p = _HtmlCites()
    p.feed(html)
    raw = "\n".join(" ".join(line.split()) for line in "".join(p.out).splitlines() if line.strip())
    body = _before_references(raw)
    found, seen = [], set()
    marks = {f"{MARK}{b}{MARK}" for b in ids}
    alias_re = [re.compile(rf"(?<![\w-]){re.escape(a)}(?![\w-])") for a in aliases if a]
    for s in split_sentences(body):
        if len(s) > MAX_SENT:
            continue
        method = "html-bib" if any(m in s for m in marks) else (
            "alias" if any(r.search(_clean(s)) for r in alias_re) else "")
        c = _clean(s)
        if method and c not in seen:
            seen.add(c)
            found.append({"text": c, "method": method})
    note = "" if ids else "参考文献里没找到被引论文的标题"
    return found, note


def _author_year_re(cited: Paper) -> re.Pattern | None:
    sur = cited.first_surname
    if not sur or not cited.year:
        return None
    years = "|".join(str(y) for y in (cited.year - 1, cited.year, cited.year + 1))
    second = r"(?:\s+et\s+al\.?|\s+(?:and|&)\s+[A-Z][\w'\-]+)?"
    return re.compile(rf"(?<![\w-]){re.escape(sur)}{second}[\s,]*\(?\s*(?:{years})[a-z]?\b")


def from_text(text: str, cited: Paper, aliases: list[str]) -> tuple[list[dict], str]:
    t = re.sub(r"-\n(?=[a-z])", "", text)
    body = _before_references(t)
    joined = re.sub(r"[ \t]*\n[ \t]*(?!\n)", " ", body)  # PDF 断行合并，保留空行作段落
    ay = _author_year_re(cited)
    alias_re = [re.compile(rf"(?<![\w-]){re.escape(a)}(?![\w-])") for a in aliases if a]
    found, seen = [], set()
    for s in split_sentences(joined):
        if len(s) > MAX_SENT:
            continue
        method = "pdf-author-year" if ay and ay.search(s) else (
            "alias" if any(r.search(s) for r in alias_re) else "")
        c = " ".join(s.split())
        if method and c not in seen:
            seen.add(c)
            found.append({"text": c, "method": method})
    note = "" if ay else "被引论文缺作者或年份，只能按方法名匹配"
    if ay and not any(f["method"] == "pdf-author-year" for f in found):
        note = "没找到“作者 年份”式引用（可能是数字编号格式）"
    return found, note


@dataclass
class Extracted:
    citing: Paper
    contexts: list[dict] = field(default_factory=list)
    source: str = ""
    note: str = ""


def extract(http: Http, store: FulltextStore, citing: Paper, cited: Paper,
            aliases: list[str]) -> Extracted:
    """取引用者全文（并登记为证据），再抽出引用被引论文的句子。"""
    key = citing.key
    if not (citing.arxiv_id or citing.acl_id):
        return Extracted(citing, note="没有 arXiv / ACL 全文来源，跳过")
    if citing.arxiv_id:
        for url, method in ((f"https://arxiv.org/html/{citing.arxiv_id}", "arxiv-html"),
                            (f"https://ar5iv.labs.arxiv.org/html/{citing.arxiv_id}", "ar5iv")):
            try:
                html, _ = http.get_text(url)
            except FetchError:
                continue
            if _looks_like_paper(html):
                if not store.rel(key):
                    store.add(key, html_to_text(html), url, method)
                ctx, note = from_html(html, cited, aliases)
                return Extracted(citing, ctx, method, note)
    try:
        text, url, method = fetch(http, key)
    except (FetchError, ValueError, RuntimeError) as e:
        return Extracted(citing, note=f"取全文失败：{getattr(e, 'message', e)}")
    if not store.rel(key):
        store.add(key, text, url, method)
    ctx, note = from_text(text, cited, aliases)
    return Extracted(citing, ctx, method, note)
