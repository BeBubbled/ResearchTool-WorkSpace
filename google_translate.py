"""Google Cloud Translation (v2, official API) with sentence alignment through HTML markup.

Alignment strategy
------------------
* Each paragraph run is sent as ONE segment so Google sees the surrounding context, but every
  sentence is wrapped in ``<span id="uN">`` and every protected LaTeX token in
  ``<span translate="no">``. With ``format=html`` Google keeps the tags, moves them to where
  Chinese word order needs them, and leaves ``translate="no"`` content untouched.
* The response is parsed back into one string per sentence. Text that ends up outside every
  sentence span (Google moved words across a boundary) makes that segment ambiguous, so its
  sentences are reported as failed and the caller can retry them one sentence per segment.

The API key travels in the ``X-Goog-Api-Key`` header (never in a URL) and never appears in an
error message.
"""
from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from typing import Callable

import requests

# Cloud Translation – Basic edition (v2) only. Advanced (v3) features such as glossaries,
# custom models or batch jobs are deliberately not used.
ENDPOINT = "https://translation.googleapis.com/language/translate/v2"
MAX_SEGMENTS = 100          # API limit is 128 segments per request
MAX_REQUEST_CHARS = 5000    # Google's recommended maximum request size for v2
VOID_TAGS = {"br", "hr", "img", "wbr", "input", "meta", "link"}


class GoogleTranslateError(ValueError):
    """Safe, user-facing failure (no key, URL or raw provider payload)."""


def segment_html(units: list[tuple[str, str]], token_pattern: re.Pattern) -> str:
    """Wrap sentences (``(local_id, text)``) and their protected tokens for format=html."""
    parts = []
    for local_id, text in units:
        pieces, last = [], 0
        for match in token_pattern.finditer(text):
            pieces.append(html.escape(text[last:match.start()], quote=False))
            pieces.append(f'<span translate="no">{match[0]}</span>')
            last = match.end()
        pieces.append(html.escape(text[last:], quote=False))
        parts.append(f'<span id="{local_id}">{"".join(pieces)}</span>')
    return "".join(parts)


class _SentenceCollector(HTMLParser):
    def __init__(self, ids: set[str]):
        super().__init__(convert_charrefs=True)
        self.ids, self.stack, self.text, self.stray = ids, [], {}, ""

    def handle_starttag(self, tag, attrs):
        if tag in VOID_TAGS:
            return
        element_id = dict(attrs).get("id")
        self.stack.append(element_id if element_id in self.ids else None)

    def handle_startendtag(self, tag, attrs):
        return

    def handle_endtag(self, tag):
        if tag not in VOID_TAGS and self.stack:
            self.stack.pop()

    def handle_data(self, data):
        owner = next((item for item in reversed(self.stack) if item), None)
        if owner is None:
            self.stray += data
        else:
            self.text[owner] = self.text.get(owner, "") + data


def parse_segment(translated: str, ids: list[str]) -> dict[str, str] | None:
    """Return ``{local_id: chinese}``, or None when sentence boundaries were not preserved."""
    collector = _SentenceCollector(set(ids))
    collector.feed(translated)
    collector.close()
    if set(collector.text) != set(ids) or re.sub(r"[\s　，。、；：,.;:!?！？]", "", collector.stray):
        return None
    return {key: re.sub(r"[ \t]+", " ", value).strip() for key, value in collector.text.items()}


class GoogleTranslator:
    def __init__(self, key_getter: Callable[[], str], *, post: Callable[..., requests.Response] = requests.post,
                 timeout: float = 30):
        self.key_getter, self.post, self.timeout = key_getter, post, timeout

    def configured(self) -> bool:
        return bool((self.key_getter() or "").strip())

    def status(self) -> dict:
        return {"configured": self.configured(), "edition": "basic", "endpoint": ENDPOINT}

    def test(self) -> dict:
        """Translate one sentence with the saved key."""
        sample = "The proposed method may improve accuracy on small objects."
        [translated] = self.translate_html([f'<span id="u0">{sample}</span>'])
        parsed = parse_segment(translated, ["u0"]) or {"u0": html.unescape(re.sub(r"<[^>]+>", "", translated))}
        return {"source": sample, "translated": parsed["u0"]}

    def translate_html(self, segments: list[str], key: str | None = None) -> list[str]:
        """Translate up to MAX_SEGMENTS HTML segments EN→zh-CN, preserving order."""
        if not segments:
            return []
        if len(segments) > MAX_SEGMENTS:
            raise GoogleTranslateError("单次 Google 翻译请求的段落过多。")
        api_key = (key if key is not None else self.key_getter() or "").strip()
        if not api_key:
            raise GoogleTranslateError("尚未配置 Google 翻译 API Key。")
        try:
            response = self.post(ENDPOINT, headers={"X-Goog-Api-Key": api_key},
                                 json={"q": segments, "source": "en", "target": "zh-CN", "format": "html"},
                                 timeout=self.timeout)
        except requests.Timeout:
            raise GoogleTranslateError("Google 翻译请求超时，请检查网络或代理。") from None
        except requests.RequestException:
            raise GoogleTranslateError("无法连接 Google 翻译，请检查网络或代理（大陆网络需要代理）。") from None
        if response.status_code != 200:
            raise GoogleTranslateError(self._http_error(response))
        try:
            translations = response.json()["data"]["translations"]
            texts = [item["translatedText"] for item in translations]
        except (ValueError, KeyError, TypeError):
            raise GoogleTranslateError("Google 翻译返回了无法解析的结果。") from None
        if len(texts) != len(segments) or not all(isinstance(t, str) for t in texts):
            raise GoogleTranslateError("Google 翻译返回的段落数量与请求不一致。")
        return texts

    def check(self, key: str) -> None:
        """Validate a key (and that the Cloud Translation API is enabled) with a tiny request."""
        self.translate_html(['<span id="u0">Hello.</span>'], key=key)

    @staticmethod
    def _http_error(response) -> str:
        try:
            error = response.json()["error"]
            reason = " ".join(str(e.get("reason", "")) for e in error.get("errors", []))
            message = str(error.get("message", ""))
        except (ValueError, KeyError, TypeError, AttributeError):
            reason = message = ""
        code = response.status_code
        if code == 400 and ("keyInvalid" in reason or "API key not valid" in message or "API_KEY_INVALID" in message):
            return "Google 翻译 API Key 无效。"
        if code == 403:
            if "rateLimitExceeded" in reason or "userRateLimitExceeded" in reason:
                return "Google 翻译请求过于频繁，请稍后再试。"
            if "dailyLimitExceeded" in reason or "quotaExceeded" in reason:
                return "Google 翻译配额已用尽（检查 Cloud 控制台的配额与结算）。"
            return "Google 翻译拒绝了请求：请确认已在 Cloud 控制台启用 Cloud Translation API、开通结算，且 Key 未限制此 API。"
        if code == 429:
            return "Google 翻译请求过于频繁或配额不足，请稍后再试。"
        if code >= 500:
            return "Google 翻译服务暂时不可用，请稍后再试。"
        return f"Google 翻译请求失败（HTTP {code}）。"
