"""证据文件与来源清单。

gapr 抓下来的论文全文、引用句、审稿意见都存在 <project>/evidence/ 下，
每写一个文件就在 evidence/manifest.json 里记下：来源网址、获取方式、时间、sha256。
核对原句时只认清单里登记过、且内容哈希没变的文件，手改过的文件不算证据。
用户手动导入的全文（例如只有扫描版的论文）登记为 method=manual，报告里会单独列出。
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import datetime
from pathlib import Path

MANIFEST = "manifest.json"


def safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Evidence:
    def __init__(self, project_root: Path):
        self.dir = Path(project_root) / "evidence"

    # ---- 清单 ----
    def _manifest_path(self) -> Path:
        return self.dir / MANIFEST

    def manifest(self) -> dict:
        p = self._manifest_path()
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}

    def _write_manifest(self, m: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self._manifest_path().write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8")

    def save(self, kind: str, name: str, content: str | object, *, source_url: str = "",
             method: str = "", ext: str | None = None) -> Path:
        """写证据文件并登记。content 为 str 时按文本存，否则存 JSON。"""
        if isinstance(content, str):
            data, ext = content.encode("utf-8"), ext or ".txt"
        else:
            data, ext = json.dumps(content, ensure_ascii=False, indent=1).encode("utf-8"), ext or ".json"
        rel = f"{kind}/{safe_name(name)}{ext}"
        path = self.dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        m = self.manifest()
        m[rel] = {"kind": kind, "name": name, "source_url": source_url, "method": method,
                  "sha256": _sha(data), "saved_at": datetime.now().isoformat(timespec="seconds")}
        self._write_manifest(m)
        return path

    def verified(self, rel: str) -> bytes | None:
        """清单里登记过、且哈希未变时返回内容；否则 None。"""
        info = self.manifest().get(rel)
        path = self.dir / rel
        if not info or not path.exists():
            return None
        data = path.read_bytes()
        return data if _sha(data) == info["sha256"] else None

    def tampered(self) -> list[str]:
        """登记过但内容被改动或删除的文件。"""
        out = []
        for rel, info in self.manifest().items():
            p = self.dir / rel
            if not p.exists() or _sha(p.read_bytes()) != info["sha256"]:
                out.append(rel)
        return out

    def entries(self, kind: str) -> dict[str, dict]:
        return {k: v for k, v in self.manifest().items() if v.get("kind") == kind}


# ---------------------------------------------------------------- 原句匹配

_TRANS = str.maketrans({"­": "", "‐": "-", "‑": "-", "–": "-", "—": "-",
                        "‘": "'", "’": "'", "“": '"', "”": '"'})
_SPLIT = re.compile(r"\.\.\.|…|\[\.\.\.\]")
MIN_QUOTE_CHARS = 20


def normalize(text: str) -> str:
    """只保留字母和数字并转小写：不受空格、换行、标点、连字符断行、连字（ﬁ）影响。"""
    t = unicodedata.normalize("NFKC", text).translate(_TRANS).lower()
    t = re.sub(r"-\s*\n\s*", "", t)  # PDF 断行连字符
    return "".join(ch for ch in t if ch.isalnum())


def quote_in(norm_text: str, quote: str) -> tuple[bool, str]:
    """原句（可用 … 省略中间部分）是否按顺序出现在已规范化的全文里。"""
    segs = [normalize(s) for s in _SPLIT.split(quote)]
    segs = [s for s in segs if s]
    if not segs:
        return False, "原句为空"
    if sum(len(s) for s in segs) < MIN_QUOTE_CHARS:
        return False, f"原句太短（规范化后少于 {MIN_QUOTE_CHARS} 个字母数字），无法可靠核对"
    pos = 0
    for s in segs:
        i = norm_text.find(s, pos)
        if i < 0:
            return False, "原文中找不到这段文字"
        pos = i + len(s)
    return True, ""
