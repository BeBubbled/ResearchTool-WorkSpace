"""OpenReview API v2：按标题找论坛，取审稿意见里的 Weaknesses。

现状（2026-11 实测）：匿名访问时，/notes/search 可用；/notes?forum=… 会被
人机验证拦下（ChallengeRequiredError）。本工具不绕过验证：设置了环境变量
OPENREVIEW_TOKEN 就带上，否则报错并给出论坛网址，由人在浏览器里查看。
另外，ACL Rolling Review 的审稿意见大多不公开，这一来源主要覆盖 ICLR / NeurIPS 等。
"""

from __future__ import annotations

import os

from researchtool.gapr.net import Http

BASE = "https://api2.openreview.net"
WEAKNESS_FIELDS = ("weaknesses", "weakness", "limitations", "main_review", "review",
                   "summary_of_the_review", "strengths_and_weaknesses")


def _headers() -> dict:
    tok = os.environ.get("OPENREVIEW_TOKEN", "").strip()
    return {"Authorization": f"Bearer {tok}"} if tok else {}


def _val(content: dict, key: str) -> str:
    v = content.get(key)
    if isinstance(v, dict):
        v = v.get("value")
    return v if isinstance(v, str) else ""


def search(http: Http, term: str, *, limit: int = 10) -> tuple[list[dict], int, bool]:
    params = {"term": term, "type": "terms", "content": "title", "source": "forum", "limit": limit}
    d, cached = http.get_json(f"{BASE}/notes/search", params, _headers())
    out = [{"forum": n.get("forum") or n.get("id"),
            "title": _val(n.get("content", {}), "title"),
            "venue": _val(n.get("content", {}), "venueid") or _val(n.get("content", {}), "venue"),
            "url": f"https://openreview.net/forum?id={n.get('forum') or n.get('id')}"}
           for n in d.get("notes") or []]
    return out, int(d.get("count") or len(out)), cached


def reviews(http: Http, forum_id: str) -> tuple[list[dict], bool]:
    d, cached = http.get_json(f"{BASE}/notes", {"forum": forum_id, "limit": 1000}, _headers())
    out = []
    for n in d.get("notes") or []:
        invs = n.get("invitations") or [n.get("invitation") or ""]
        if not any(i.endswith("Official_Review") or i.endswith("/Review") for i in invs):
            continue
        c = n.get("content") or {}
        fields = {k: _val(c, k) for k in WEAKNESS_FIELDS if _val(c, k)}
        out.append({"id": n.get("id"), "rating": _val(c, "rating"), "fields": fields,
                    "url": f"https://openreview.net/forum?id={forum_id}&noteId={n.get('id')}"})
    return out, cached
