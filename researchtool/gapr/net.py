"""HTTP：磁盘缓存 + 按主机限速 + 429/5xx 退避重试。只用标准库。

缓存永不过期（保证可复现）；需要最新结果时传 refresh=True。
缓存键只含 URL 和查询参数，不含请求头，所以 API key 不会落盘。
"""

from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

USER_AGENT = "gapr/0.1 (research-gap survey tool)"

# 各主机两次请求之间的最小间隔（秒）
MIN_INTERVAL = {
    "export.arxiv.org": 3.1,
    "arxiv.org": 1.0,
    "api.semanticscholar.org": 1.1,
    "api.openalex.org": 0.2,
    "api2.openreview.net": 1.0,
}
class FetchError(Exception):
    def __init__(self, url: str, status: int | None, message: str):
        super().__init__(f"{status or ''} {message}".strip())
        self.url = url
        self.status = status
        self.message = message


class _Throttle:
    """按主机限速。有缓存目录时把上次请求时间写进文件，
    这样连续多次命令行调用（每次都是新进程）之间也遵守间隔。"""

    def __init__(self, state_file: Path | None):
        self.state_file = state_file
        self.mem: dict[str, float] = {}

    def _load(self) -> dict[str, float]:
        if self.state_file and self.state_file.exists():
            try:
                return {**json.loads(self.state_file.read_text(encoding="utf-8")), **self.mem}
            except (ValueError, OSError):
                pass
        return dict(self.mem)

    def wait(self, host: str) -> None:
        state = self._load()
        gap = MIN_INTERVAL.get(host, 0.5)
        wait = state.get(host, 0.0) + gap - time.time()
        if wait > 0:
            time.sleep(wait)
        state[host] = self.mem[host] = time.time()
        if self.state_file:
            try:
                self.state_file.parent.mkdir(parents=True, exist_ok=True)
                self.state_file.write_text(json.dumps(state), encoding="utf-8")
            except OSError:
                pass


def build_url(url: str, params: dict | None) -> str:
    if not params:
        return url
    clean = {k: v for k, v in params.items() if v not in (None, "")}
    return url + "?" + urllib.parse.urlencode(clean, doseq=True)


class Http:
    def __init__(self, cache_dir: Path | None = None, *, refresh: bool = False, retries: int = 4):
        self.cache_dir = Path(cache_dir) / "http" if cache_dir else None
        self.refresh = refresh
        self.retries = retries
        self.throttle = _Throttle(Path(cache_dir) / "throttle.json" if cache_dir else None)

    def _cache_path(self, full_url: str) -> Path | None:
        if not self.cache_dir:
            return None
        return self.cache_dir / (hashlib.sha256(full_url.encode()).hexdigest()[:32] + ".json")

    def _download(self, full: str, headers: dict | None) -> bytes:
        host = urllib.parse.urlparse(full).hostname or ""
        req_headers = {"User-Agent": USER_AGENT, **(headers or {})}
        delay = 5.0
        for attempt in range(self.retries + 1):
            self.throttle.wait(host)
            req = urllib.request.Request(full, headers=req_headers)
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    return resp.read()
            except urllib.error.HTTPError as e:
                text = e.read().decode("utf-8", errors="replace")[:500]
                if e.code in (429, 500, 502, 503, 504) and attempt < self.retries:
                    ra = e.headers.get("Retry-After")
                    time.sleep(float(ra) if ra and ra.isdigit() else delay)
                    delay *= 2
                    continue
                raise FetchError(full, e.code, _explain(host, e.code, text)) from None
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
                if attempt < self.retries:
                    time.sleep(delay)
                    delay *= 2
                    continue
                reason = getattr(e, "reason", None) or e
                raise FetchError(full, None, f"网络错误（重试 {self.retries} 次后放弃）：{reason}") from None
        raise FetchError(full, None, "重试次数用尽")

    def get_text(self, url: str, params: dict | None = None, headers: dict | None = None,
                 secret_params: dict | None = None) -> tuple[str, bool]:
        """返回 (响应正文, 是否来自缓存)。
        secret_params（如 API key）只加在实际请求上，不进缓存键、不写进缓存文件。"""
        full = build_url(url, params)
        cp = self._cache_path(full)
        if cp and cp.exists() and not self.refresh:
            return json.loads(cp.read_text(encoding="utf-8"))["body"], True
        real = full
        if secret_params:
            sep = "&" if "?" in full else "?"
            real = full + sep + urllib.parse.urlencode({k: v for k, v in secret_params.items() if v})
        try:
            body = self._download(real, headers).decode("utf-8", errors="replace")
        except FetchError as e:
            e.url = full  # 不把 key 带进错误信息
            raise
        if cp:
            cp.parent.mkdir(parents=True, exist_ok=True)
            cp.write_text(json.dumps({"url": full, "fetched_at": datetime.now().isoformat(timespec="seconds"),
                                      "body": body}, ensure_ascii=False), encoding="utf-8")
        return body, False

    def get_bytes(self, url: str, headers: dict | None = None) -> tuple[bytes, bool]:
        """二进制下载（PDF），缓存为 .bin。"""
        cp = self._cache_path(url)
        bp = cp.with_suffix(".bin") if cp else None
        if bp and bp.exists() and not self.refresh:
            return bp.read_bytes(), True
        data = self._download(url, headers)
        if bp:
            bp.parent.mkdir(parents=True, exist_ok=True)
            bp.write_bytes(data)
        return data, False

    def get_json(self, url: str, params: dict | None = None, headers: dict | None = None,
                 secret_params: dict | None = None) -> tuple[dict, bool]:
        body, cached = self.get_text(url, params, headers, secret_params)
        return json.loads(body), cached


def _explain(host: str, code: int, text: str) -> str:
    if host == "api.semanticscholar.org" and code == 429:
        return "Semantic Scholar 限流（没有 API key 时与所有人共用额度）。设置环境变量 S2_API_KEY 后重试。"
    if host == "api2.openreview.net" and "ChallengeRequired" in text:
        return ("OpenReview 要求人机验证，匿名接口取不到这条内容。"
                "可在浏览器里手动打开论坛页查看，或设置环境变量 OPENREVIEW_TOKEN。")
    if host == "api.openalex.org" and code in (402, 429):
        return "OpenAlex 额度用完或限流。设置环境变量 OPENALEX_API_KEY（免费 key：https://openalex.org/settings/api）。"
    return text.strip() or "请求失败"
