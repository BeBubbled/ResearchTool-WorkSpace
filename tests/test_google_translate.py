"""Google Cloud Translation adapter: HTML sentence alignment and safe errors. No network calls."""
import json
import re
import unittest

import requests

from google_translate import ENDPOINT, GoogleTranslateError, GoogleTranslator, parse_segment, segment_html

TOKEN = re.compile(r"⟦L\d{6}⟧")


class FakeResponse:
    def __init__(self, status, payload):
        self.status_code, self.payload = status, payload

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class AlignmentTests(unittest.TestCase):
    def test_segment_wraps_sentences_and_protects_tokens(self):
        html = segment_html([("u0", "A < B & C ⟦L000001⟧ holds. "), ("u1", "Then ⟦L000002⟧.")], TOKEN)
        self.assertEqual(html, '<span id="u0">A &lt; B &amp; C <span translate="no">⟦L000001⟧</span> holds. </span>'
                               '<span id="u1">Then <span translate="no">⟦L000002⟧</span>.</span>')

    def test_parse_keeps_reordered_tokens_and_unescapes(self):
        translated = ('<span id="u0"><span translate="no">⟦L000001⟧</span> 中 A &lt; B 成立。</span> '
                      '<span id="u1">然后<span translate="no">⟦L000002⟧</span>。</span>')
        self.assertEqual(parse_segment(translated, ["u0", "u1"]),
                         {"u0": "⟦L000001⟧ 中 A < B 成立。", "u1": "然后⟦L000002⟧。"})

    def test_parse_rejects_moved_text_and_missing_sentences(self):
        self.assertIsNone(parse_segment('<span id="u0">甲</span>漂移的文字<span id="u1">乙</span>', ["u0", "u1"]))
        self.assertIsNone(parse_segment('<span id="u0">甲乙合并</span>', ["u0", "u1"]))
        self.assertEqual(parse_segment('<span id="u0">甲</span>，<br><span id="u1">乙</span>', ["u0", "u1"]),
                         {"u0": "甲", "u1": "乙"})


class TranslatorTests(unittest.TestCase):
    def translator(self, response=None, exc=None):
        calls = []
        def post(url, **kwargs):
            calls.append({"url": url, **kwargs})
            if exc:
                raise exc
            return response
        return GoogleTranslator(lambda: "secret-key-123", post=post), calls

    def test_request_uses_header_key_html_format_and_preserves_order(self):
        translator, calls = self.translator(FakeResponse(200, {"data": {"translations": [
            {"translatedText": "一"}, {"translatedText": "二"}]}}))
        self.assertEqual(translator.translate_html(["one", "two"]), ["一", "二"])
        call = calls[0]
        self.assertEqual(call["url"], ENDPOINT)
        self.assertNotIn("secret-key-123", call["url"])
        self.assertEqual(call["headers"], {"X-Goog-Api-Key": "secret-key-123"})
        self.assertEqual(call["json"], {"q": ["one", "two"], "source": "en", "target": "zh-CN", "format": "html"})

    def test_errors_are_specific_and_never_leak_the_key(self):
        cases = [
            (FakeResponse(400, {"error": {"message": "API key not valid. Please pass a valid API key.",
                                          "errors": [{"reason": "badRequest"}]}}), "Key 无效"),
            (FakeResponse(403, {"error": {"errors": [{"reason": "accessNotConfigured"}]}}), "启用 Cloud Translation API"),
            (FakeResponse(403, {"error": {"errors": [{"reason": "dailyLimitExceeded"}]}}), "配额"),
            (FakeResponse(429, {}), "频繁"),
            (FakeResponse(503, ValueError()), "暂时不可用"),
            (FakeResponse(200, {"data": {"translations": [{"translatedText": "一"}]}}), "数量"),
        ]
        for response, expected in cases:
            translator, _ = self.translator(response)
            with self.assertRaisesRegex(GoogleTranslateError, expected) as raised:
                translator.translate_html(["one", "two"])
            self.assertNotIn("secret-key-123", str(raised.exception))
        translator, _ = self.translator(exc=requests.ConnectionError("https://x?key=secret-key-123"))
        with self.assertRaisesRegex(GoogleTranslateError, "代理") as raised:
            translator.translate_html(["one"])
        self.assertNotIn("secret-key-123", str(raised.exception))

    def test_missing_key_and_check(self):
        translator = GoogleTranslator(lambda: "", post=lambda *a, **k: self.fail("must not call"))
        self.assertFalse(translator.configured())
        with self.assertRaisesRegex(GoogleTranslateError, "尚未配置"):
            translator.translate_html(["one"])
        translator, calls = self.translator(FakeResponse(200, {"data": {"translations": [{"translatedText": "你好。"}]}}))
        translator.check("other-key")
        self.assertEqual(calls[0]["headers"], {"X-Goog-Api-Key": "other-key"})


if __name__ == "__main__":
    unittest.main()
