"""Local usage ledger: what each engine was asked to do, so cost is visible in one place.

Every record is one JSON line in .runtime/usage/usage.jsonl:
  {"t": epoch, "engine": "api"|"codex"|"claude"|"mathpix"|"google", "feature": "...",
   "label": "preset or model", "calls": n, "inputTokens": n, "outputTokens": n, "pages": n,
   "characters": n, "reused": n}
Nothing secret is written (no prompts, no keys). API token counts come from the provider's
own `usage` field when it returns one; Codex and Claude runs are counted as submissions only,
because their plans report quota, not tokens. Served at GET /api/usage?days=30.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

ENGINES = ("api", "codex", "claude", "mathpix", "google")
ENGINE_LABELS = {"api": "API 预设", "codex": "Codex", "claude": "Claude Code", "mathpix": "Mathpix OCR", "google": "Google 翻译"}
FEATURE_LABELS = {
    "three_pass": "精读 Three-Pass", "reader_question": "精读划词解读", "reader_translation": "精读翻译",
    "document_translate": "文档翻译", "pdf_ocr_translate": "论文 PDF OCR", "markdown_repair": "Markdown 修复",
    "reader_ocr": "精读 OCR 导入", "gaps": "研究空白地图", "studio": "ResearchStudio", "revision": "双语改稿",
    "llm": "其他 LLM 调用", "llm_test": "预设测试",
}
NUMBER_FIELDS = ("calls", "inputTokens", "outputTokens", "pages", "characters", "reused")
MAX_BYTES = 8 * 1024 * 1024


class UsageLedger:
    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else None
        self.lock = threading.Lock()

    def configure(self, path: Path) -> None:
        self.path = Path(path)

    def record(self, engine: str, feature: str, *, label: str = "", **numbers: Any) -> None:
        if self.path is None or engine not in ENGINES:
            return
        entry: dict[str, Any] = {"t": round(time.time(), 3), "engine": engine, "feature": str(feature or "llm")[:60]}
        if label:
            entry["label"] = str(label)[:120]
        for name in NUMBER_FIELDS:
            value = numbers.get(name, 1 if name == "calls" else 0)
            try:
                value = max(0, int(value or 0))
            except (TypeError, ValueError):
                value = 0
            if value:
                entry[name] = value
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        try:
            with self.lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                if self.path.exists() and self.path.stat().st_size > MAX_BYTES:
                    self.path.replace(self.path.with_suffix(".1.jsonl"))
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(line)
        except OSError:
            pass  # usage tracking must never break the request it measures

    def entries(self, since: float = 0) -> list[dict[str, Any]]:
        if self.path is None:
            return []
        result = []
        for path in (self.path.with_suffix(".1.jsonl"), self.path):
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for line in lines:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if isinstance(entry, dict) and float(entry.get("t") or 0) >= since:
                    result.append(entry)
        return result

    def summary(self, days: int = 30, now: float | None = None) -> dict[str, Any]:
        days = max(1, min(366, int(days)))
        now = time.time() if now is None else now
        since = now - days * 86400
        engines: dict[str, dict[str, Any]] = {}
        daily: dict[str, dict[str, int]] = {}
        for entry in self.entries(since):
            engine = entry.get("engine")
            if engine not in ENGINES:
                continue
            bucket = engines.setdefault(engine, {"engine": engine, "label": ENGINE_LABELS[engine], **{k: 0 for k in NUMBER_FIELDS}, "features": {}})
            feature = str(entry.get("feature") or "llm")
            feature_bucket = bucket["features"].setdefault(feature, {"feature": feature, "label": FEATURE_LABELS.get(feature, feature), **{k: 0 for k in NUMBER_FIELDS}})
            for name in NUMBER_FIELDS:
                value = int(entry.get(name) or 0)
                bucket[name] += value
                feature_bucket[name] += value
            day = time.strftime("%Y-%m-%d", time.localtime(float(entry["t"])))
            daily.setdefault(day, {k: 0 for k in ENGINES})[engine] += int(entry.get("calls") or 0)
        ordered = []
        for engine in ENGINES:
            if engine in engines:
                bucket = engines[engine]
                bucket["features"] = sorted(bucket["features"].values(), key=lambda item: -item["calls"])
                ordered.append(bucket)
        return {"days": days, "since": since, "engines": ordered, "daily": [{"day": day, **counts} for day, counts in sorted(daily.items())]}


def tokens_from_response(response: Any) -> tuple[int, int]:
    """(input, output) tokens from an OpenAI-style chat or responses result, when present."""
    usage = response.get("usage") if isinstance(response, dict) else getattr(response, "usage", None)
    if usage is None:
        return 0, 0
    read = (lambda name: usage.get(name)) if isinstance(usage, dict) else (lambda name: getattr(usage, name, None))
    input_tokens = read("prompt_tokens") or read("input_tokens") or 0
    output_tokens = read("completion_tokens") or read("output_tokens") or 0
    try:
        return int(input_tokens), int(output_tokens)
    except (TypeError, ValueError):
        return 0, 0


LEDGER = UsageLedger()


def record(engine: str, feature: str, **values: Any) -> None:
    LEDGER.record(engine, feature, **values)


def record_response(feature: str, response: Any, *, label: str = "") -> None:
    input_tokens, output_tokens = tokens_from_response(response)
    LEDGER.record("api", feature, label=label, inputTokens=input_tokens, outputTokens=output_tokens)
