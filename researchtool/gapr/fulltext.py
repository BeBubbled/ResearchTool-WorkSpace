"""论文全文：下载、转成纯文本、登记为证据，以及按论文核对原句。

来源顺序：
  arXiv:ID  → arxiv.org/html/ID → ar5iv.labs.arxiv.org/html/ID → arxiv.org/pdf/ID（pdftotext）
  ACL:ID    → aclanthology.org/ID.pdf（pdftotext）
  其他       → 需要用户手动导入（gapr fulltext --import 文件 --url 出处）
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from html.parser import HTMLParser
from pathlib import Path

from researchtool.gapr.evidence import Evidence, normalize, quote_in
from researchtool.gapr.net import FetchError, Http
from researchtool.gapr.paper import norm_arxiv_id

KIND = "fulltext"
_SKIP_TAGS = {"script", "style", "noscript", "head", "nav", "footer", "button", "svg"}
_BLOCK_TAGS = {"p", "div", "section", "h1", "h2", "h3", "h4", "h5", "h6", "li", "tr", "br",
               "figcaption", "caption", "table", "blockquote", "article"}


class _Html2Text(HTMLParser):
    """LaTeXML / ar5iv 页面转纯文本；<math> 用 alttext 代替，避免 MathML 噪声。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0
        self.math = 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self.skip += 1
        elif tag == "math":
            self.math += 1
            alt = dict(attrs).get("alttext")
            if alt and not self.skip and self.math == 1:
                self.out.append(f" {alt} ")
        elif tag in _BLOCK_TAGS:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS and self.skip:
            self.skip -= 1
        elif tag == "math" and self.math:
            self.math -= 1
        elif tag in _BLOCK_TAGS:
            self.out.append("\n")

    def handle_data(self, data):
        if not self.skip and not self.math:
            self.out.append(data.replace("\n", " "))  # 源码里的换行不是段落边界


def html_to_text(html: str) -> str:
    p = _Html2Text()
    p.feed(html)
    lines = [" ".join(line.split()) for line in "".join(p.out).splitlines()]
    return "\n".join(line for line in lines if line)


def pdf_to_text(data: bytes) -> str:
    exe = shutil.which("pdftotext")
    for cand in (r"C:\Program Files\Git\ucrt64\bin\pdftotext.exe", r"C:\Program Files\Git\mingw64\bin\pdftotext.exe"):
        if not exe and Path(cand).exists():  # Git for Windows 自带 poppler
            exe = cand
    if not exe:
        raise RuntimeError("没有找到 pdftotext（poppler）。安装后重试，或用 --import 手动导入全文。")
    with tempfile.TemporaryDirectory() as d:
        pdf = Path(d) / "paper.pdf"
        pdf.write_bytes(data)
        out = Path(d) / "paper.txt"
        subprocess.run([exe, "-enc", "UTF-8", str(pdf), str(out)], check=True,
                       capture_output=True, timeout=120)
        return out.read_text(encoding="utf-8", errors="replace")


def _looks_like_paper(html: str) -> bool:
    return "ltx_document" in html or "ltx_page_main" in html


def fetch(http: Http, key: str) -> tuple[str, str, str]:
    """返回 (纯文本, 来源网址, 获取方式)。"""
    k = key.strip()
    low = k.lower()
    tried: list[str] = []
    if low.startswith("arxiv:"):
        aid = norm_arxiv_id(k)
        for url, method in ((f"https://arxiv.org/html/{aid}", "arxiv-html"),
                            (f"https://ar5iv.labs.arxiv.org/html/{aid}", "ar5iv")):
            try:
                html, _ = http.get_text(url)
            except FetchError as e:
                tried.append(f"{method}: {e.message[:80]}")
                continue
            if _looks_like_paper(html):
                return html_to_text(html), url, method
            tried.append(f"{method}: 不是论文正文页")
        url = f"https://arxiv.org/pdf/{aid}"
        data, _ = http.get_bytes(url)
        return pdf_to_text(data), url, "arxiv-pdf"
    if low.startswith("acl:"):
        url = f"https://aclanthology.org/{k[4:]}.pdf"
        data, _ = http.get_bytes(url)
        return pdf_to_text(data), url, "acl-pdf"
    raise ValueError(f"{key}：只支持自动获取 arXiv:… 和 ACL:… 的全文，其他请用 --import 手动导入"
                     + (f"（已尝试：{'；'.join(tried)}）" if tried else ""))


class FulltextStore:
    def __init__(self, project_root: Path):
        self.ev = Evidence(project_root)
        self._cache: dict[str, str | None] = {}

    @staticmethod
    def canonical(key: str) -> str:
        k = key.strip()
        if k.lower().startswith("arxiv:"):
            return "arXiv:" + norm_arxiv_id(k)
        if k.lower().startswith("acl:"):
            return "ACL:" + k[4:]
        return k

    def rel(self, key: str) -> str | None:
        name = self.canonical(key)
        for rel, info in self.ev.entries(KIND).items():
            if info.get("name") == name:
                return rel
        return None

    def add(self, key: str, text: str, source_url: str, method: str) -> Path:
        self._cache.pop(self.canonical(key), None)
        return self.ev.save(KIND, self.canonical(key), text, source_url=source_url, method=method)

    def method(self, key: str) -> str:
        rel = self.rel(key)
        return self.ev.manifest().get(rel, {}).get("method", "") if rel else ""

    def norm_text(self, key: str) -> str | None:
        """已登记且未被改动的全文（规范化后）；没有则 None。"""
        name = self.canonical(key)
        if name not in self._cache:
            rel = self.rel(name)
            data = self.ev.verified(rel) if rel else None
            self._cache[name] = normalize(data.decode("utf-8", errors="replace")) if data else None
        return self._cache[name]

    def check_quote(self, key: str, quote: str) -> tuple[bool | None, str]:
        """True=找到；False=全文里没有；None=没有可用全文。"""
        t = self.norm_text(key)
        if t is None:
            return None, f"{self.canonical(key)} 没有登记的全文（先运行 gapr fulltext）"
        return quote_in(t, quote)


def context_pool(project_root: Path, kind: str) -> list[tuple[str, str]]:
    """从登记过的引用句（citations）或审稿意见（reviews）证据里取出 (文件, 规范化文本) 列表。"""
    import json
    ev = Evidence(project_root)
    out = []
    for rel in ev.entries(kind):
        data = ev.verified(rel)
        if data is None:
            continue
        items = json.loads(data.decode("utf-8"))
        for it in items:
            if kind == "citations":
                texts = it.get("contexts") or []
            else:
                texts = list((it.get("fields") or {}).values())
            out.extend((rel, normalize(t)) for t in texts)
    return out


def check_in_pool(pool: list[tuple[str, str]], quote: str) -> tuple[bool | None, str]:
    if not pool:
        return None, "还没有登记过的证据文件"
    for rel, t in pool:
        ok, _ = quote_in(t, quote)
        if ok:
            return True, rel
    ok, why = quote_in("", quote)
    return False, why if "太短" in why else "登记过的证据里找不到这段文字"
