"""One status model for every AI engine the client can use.

Claude Code (plan login), Codex (ChatGPT login), Google Translate (API key) and the
saved OpenAI-compatible API presets each report status in their own shape. This
module turns them into one public, non-secret shape so every page (reader, revision,
prompts, settings) can show the same engine/model/effort picker and the same
"ready / not ready" hints. Served at GET /api/engines.
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

ENGINE_LABELS = {
    "claude": "Claude Code",
    "codex": "Codex",
    "google": "Google 翻译",
    "api": "API 预设",
}
SETTINGS_TABS = {"claude": "claude", "codex": "codex", "google": "google", "api": "llm"}


def normalize_claude(status: dict[str, Any] | None) -> dict[str, Any]:
    status = status or {}
    ready = bool(status.get("plan"))
    return {
        "id": "claude",
        "label": ENGINE_LABELS["claude"],
        "ready": ready,
        "available": bool(status.get("available")),
        "plan": ready,
        "subscriptionType": str(status.get("subscriptionType") or ""),
        "error": "" if ready else str(status.get("error") or "Claude Code 未登录 Claude 账号。"),
        "models": [m for m in status.get("models") or [] if isinstance(m, dict) and m.get("id")],
        "efforts": [str(e) for e in status.get("efforts") or []],
        "defaults": status.get("defaults") or {},
        "settingsTab": SETTINGS_TABS["claude"],
    }


def normalize_codex(status: dict[str, Any] | None) -> dict[str, Any]:
    status = status or {}
    ready = bool(status.get("chatgptAuthenticated") and status.get("defaultModel"))
    if ready:
        error = ""
    elif not status.get("chatgptAuthenticated"):
        error = str(status.get("error") or "Codex 尚未通过 ChatGPT 登录。")
    else:
        error = "Codex 没有可用的默认模型。"
    return {
        "id": "codex",
        "label": ENGINE_LABELS["codex"],
        "ready": ready,
        "planType": str(status.get("planType") or ""),
        "error": error,
        "defaultModel": status.get("defaultModel") or "",
        "defaultEffort": status.get("defaultReasoningEffort") or "",
        "models": [
            {
                "id": m.get("id"),
                "label": m.get("displayName") or m.get("id"),
                "efforts": [str(e) for e in m.get("supportedEfforts") or []],
            }
            for m in status.get("models") or []
            if isinstance(m, dict) and m.get("id")
        ],
        "settingsTab": SETTINGS_TABS["codex"],
    }


def normalize_google(status: dict[str, Any] | None) -> dict[str, Any]:
    status = status or {}
    ready = bool(status.get("configured"))
    return {
        "id": "google",
        "label": ENGINE_LABELS["google"],
        "ready": ready,
        "configured": ready,
        "edition": status.get("edition") or "basic",
        "error": "" if ready else "尚未配置 Google 翻译 API Key。",
        "settingsTab": SETTINGS_TABS["google"],
    }


def normalize_api(presets: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Public preset fields only: never the API key."""
    result = []
    for preset in presets or []:
        if not isinstance(preset, dict) or not preset.get("id"):
            continue
        result.append({
            "id": preset["id"],
            "name": preset.get("name") or preset["id"],
            "model": preset.get("model", ""),
            "protocol": preset.get("protocol") or "auto",
            "concurrency": preset.get("concurrency") or 1,
            "contextWindow": preset.get("contextWindow"),
        })
    return result


class EngineCatalog:
    """Collects status from each engine (in parallel, each failure isolated)."""

    def __init__(
        self,
        *,
        claude_status: Callable[..., dict[str, Any]] | None = None,
        codex_status: Callable[..., dict[str, Any]] | None = None,
        google_status: Callable[[], dict[str, Any]] | None = None,
        llm_presets: Callable[[], list[dict[str, Any]]] | None = None,
        ttl: float = 5.0,
    ) -> None:
        self.claude_status = claude_status
        self.codex_status = codex_status
        self.google_status = google_status
        self.llm_presets = llm_presets
        self.ttl = ttl
        self._lock = threading.Lock()
        self._cached: tuple[float, dict[str, Any]] | None = None

    @staticmethod
    def _safe(call: Callable[[], Any], fallback: Any) -> Any:
        try:
            return call()
        except Exception as exc:  # one broken engine must not hide the others
            if isinstance(fallback, dict):
                return {**fallback, "error": str(exc)[:300]}
            return fallback

    def snapshot(self, refresh: bool = False) -> dict[str, Any]:
        with self._lock:
            if not refresh and self._cached and time.monotonic() - self._cached[0] < self.ttl:
                return self._cached[1]
        calls = {
            "claude": (lambda: self.claude_status(refresh=refresh)) if self.claude_status else (lambda: {}),
            "codex": (lambda: self.codex_status(refresh=refresh)) if self.codex_status else (lambda: {}),
            "google": self.google_status or (lambda: {}),
            "api": self.llm_presets or (lambda: []),
        }
        fallbacks = {"claude": {}, "codex": {}, "google": {}, "api": []}
        with ThreadPoolExecutor(max_workers=4, thread_name_prefix="engine-status") as pool:
            futures = {key: pool.submit(self._safe, call, fallbacks[key]) for key, call in calls.items()}
            raw = {key: future.result() for key, future in futures.items()}
        result = {
            "claude": normalize_claude(raw["claude"]),
            "codex": normalize_codex(raw["codex"]),
            "google": normalize_google(raw["google"]),
            "api": normalize_api(raw["api"]),
        }
        result["ready"] = [key for key in ("claude", "codex", "google") if result[key]["ready"]] + (["api"] if result["api"] else [])
        result["checkedAt"] = time.time()
        with self._lock:
            self._cached = (time.monotonic(), result)
        return result

    def invalidate(self) -> None:
        with self._lock:
            self._cached = None
