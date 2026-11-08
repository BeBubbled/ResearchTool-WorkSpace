"""Run single, tool-less text turns through the locally installed, logged-in Claude Code CLI.

Usage is billed to the signed-in Claude plan (Pro/Max), never to an API key: API-key and
third-party-provider variables are stripped from the child environment, and callers should only
submit work when `status()` reports a claude.ai login. Paper text goes through stdin and the
system prompt through a file, so no untrusted text is ever placed on a command line.
"""
from __future__ import annotations

import json
import re
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable

MODELS = {"opus": "Opus · 质量优先", "sonnet": "Sonnet · 更快、更省额度"}
EFFORTS = ("low", "medium", "high", "xhigh", "max")
# Translation is high-volume and mechanical; revision needs the strongest reasoning.
DEFAULTS = {"translate": {"model": "sonnet", "effort": "medium"}, "revise": {"model": "opus", "effort": "high"}}
# Global defaults chosen in 全局设置 → Claude Code (project .env); the recommendation applies when unset.
DEFAULT_ENV = {"translate": ("CLAUDE_CODE_TRANSLATE_MODEL", "CLAUDE_CODE_TRANSLATE_EFFORT"),
               "revise": ("CLAUDE_CODE_REVISE_MODEL", "CLAUDE_CODE_REVISE_EFFORT")}


def current_defaults() -> dict[str, dict[str, str]]:
    result = {}
    for stage, (model_key, effort_key) in DEFAULT_ENV.items():
        model, effort = os.getenv(model_key, "").strip(), os.getenv(effort_key, "").strip()
        result[stage] = {"model": model if model in MODELS else DEFAULTS[stage]["model"],
                         "effort": effort if effort in EFFORTS else DEFAULTS[stage]["effort"]}
    return result


def validate_defaults(data: dict[str, Any]) -> dict[str, str]:
    """Map a settings form ({translateModel, translateEffort, reviseModel, reviseEffort}) to .env keys."""
    values = {}
    for stage, (model_key, effort_key) in DEFAULT_ENV.items():
        model, effort = data.get(f"{stage}Model"), data.get(f"{stage}Effort")
        if model not in MODELS or effort not in EFFORTS:
            raise ValueError("请选择有效的 Claude 模型与推理强度。")
        values[model_key], values[effort_key] = model, effort
    return values
# Variables that would silently switch billing away from the signed-in plan, or make the CLI
# think it is nested inside another Claude Code session.
STRIPPED_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL",
                "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
                "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SIMPLE")
PLAN_AUTH = {"claude.ai"}
# Tools a saved revision session may use inside the paper's project folder: read-only.
SESSION_TOOLS = ("Read", "Grep", "Glob")


class ClaudeSessionMissing(ValueError):
    """A --resume target no longer exists (deleted or pruned); start a new session instead."""


def find_claude_executable(which: Callable[[str], str | None] = shutil.which) -> str | None:
    """Prefer the real binary over npm's .cmd/.ps1 shims so arguments never pass through cmd.exe."""
    override = os.getenv("CLAUDE_CODE_CLI", "").strip()
    if override:
        return override if Path(override).is_file() else None
    found = which("claude")
    candidates = []
    if found:
        path = Path(found)
        candidates += [path.parent / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe", path]
    candidates.append(Path.home() / ".local" / "bin" / ("claude.exe" if os.name == "nt" else "claude"))
    for candidate in candidates:
        if candidate.is_file() and (os.name != "nt" or candidate.suffix.lower() == ".exe"):
            return str(candidate)
    return None


def child_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k.upper() not in STRIPPED_ENV}
    env["PYTHONIOENCODING"] = "utf-8"
    return env


class ClaudeCodeRunner:
    def __init__(self, *, finder: Callable[[], str | None] = find_claude_executable,
                 runner: Callable[..., Any] = subprocess.run, popen: Callable[..., Any] = subprocess.Popen,
                 timeout: float = 900, status_ttl: float = 60):
        self.finder, self.runner, self.timeout, self.status_ttl = finder, runner, timeout, status_ttl
        self.popen = popen
        self._status: tuple[float, dict[str, Any]] | None = None
        self._lock = threading.Lock()

    def _call(self, args: list[str], *, input_text: str = "", cwd: str | None = None, timeout: float):
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        return self.runner(args, input=input_text, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, cwd=cwd, env=child_env(), creationflags=flags)

    def status(self, *, refresh: bool = False) -> dict[str, Any]:
        """Public, non-identifying login summary (no email or organisation)."""
        with self._lock:
            if not refresh and self._status and time.monotonic() - self._status[0] < self.status_ttl:
                return {**self._status[1], "defaults": current_defaults()}
        exe = self.finder()
        result: dict[str, Any] = {"available": bool(exe), "loggedIn": False, "plan": False,
                                  "subscriptionType": "", "models": [{"id": k, "label": v} for k, v in MODELS.items()],
                                  "efforts": list(EFFORTS), "recommended": DEFAULTS, "version": "",
                                  "cliPath": exe or ""}
        if not exe:
            result["error"] = "未找到 Claude Code CLI。请先安装（npm i -g @anthropic-ai/claude-code）后重试。"
        else:
            try:
                version = self._call([exe, "--version"], timeout=30).stdout.strip()
                result["version"] = version.split(" ")[0][:40]
                proc = self._call([exe, "auth", "status", "--json"], timeout=30)
                data = json.loads(proc.stdout or "{}")
                result.update(loggedIn=bool(data.get("loggedIn")), authMethod=str(data.get("authMethod") or ""),
                              subscriptionType=str(data.get("subscriptionType") or ""))
                result["plan"] = result["loggedIn"] and result["authMethod"] in PLAN_AUTH
                if not result["loggedIn"]:
                    result["error"] = "Claude Code 尚未登录。请在终端运行 claude 并用 Claude 账号登录。"
                elif not result["plan"]:
                    result["error"] = "Claude Code 当前不是 Claude 账号登录（可能是 API Key），不会使用套餐额度。"
            except (OSError, ValueError, subprocess.SubprocessError):
                result["error"] = "无法读取 Claude Code 登录状态。"
        with self._lock:
            self._status = (time.monotonic(), result)
        return {**result, "defaults": current_defaults()}

    def test(self) -> dict[str, Any]:
        """A tiny real request (Sonnet, low effort) proving login, plan billing and CLI flags work."""
        started = time.monotonic()
        reply = self.run([{"role": "system", "content": "Reply with exactly: OK"},
                          {"role": "user", "content": "ping"}], "sonnet", "low")
        return {"reply": reply.strip()[:40], "seconds": round(time.monotonic() - started, 1)}

    @staticmethod
    def _split(messages: list[dict[str, str]]) -> tuple[str, str]:
        system = "\n\n".join(m["content"] for m in messages if m.get("role") == "system" and m.get("content"))
        user = "\n\n".join(m["content"] for m in messages if m.get("role") != "system" and m.get("content"))
        if not user.strip():
            raise ValueError("Claude 请求不能为空。")
        return system, user

    def _invoke(self, args: list[str], user: str, cwd: str) -> dict[str, Any]:
        try:
            proc = self._call(args, input_text=user, cwd=cwd, timeout=self.timeout)
        except subprocess.TimeoutExpired:
            raise ValueError(f"Claude Code 超过 {int(self.timeout)} 秒未返回，本轮未应用。") from None
        except OSError:
            raise ValueError("无法启动 Claude Code CLI。") from None
        try:
            data = json.loads((proc.stdout or "").strip().splitlines()[-1])
        except (ValueError, IndexError):
            data = None
        if not isinstance(data, dict):
            detail = (proc.stderr or proc.stdout or "").strip()
            if "no conversation found" in detail.lower():
                raise ClaudeSessionMissing("Claude Code 会话已不存在。")
            raise ValueError(self._friendly(detail) or "Claude Code 没有返回可解析的结果。")
        text = data.get("result")
        if data.get("is_error") or data.get("subtype") not in {None, "success"} or not isinstance(text, str):
            detail = str(text or data.get("subtype") or "")
            if "no conversation found" in detail.lower():
                raise ClaudeSessionMissing("Claude Code 会话已不存在。")
            raise ValueError(self._friendly(detail) or "Claude Code 执行失败。")
        return data

    def _stream(self, args: list[str], user: str, cwd: str, on_text: Callable[[str], None]) -> dict[str, Any]:
        """Like _invoke, but with --output-format stream-json: ``on_text`` receives the reply text
        accumulated so far after every streamed fragment. An exception raised by ``on_text`` (e.g. the
        job was cancelled) stops the CLI and propagates."""
        args = [a if a != "json" else "stream-json" for a in args] + ["--verbose", "--include-partial-messages"]
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        try:
            proc = self.popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                              encoding="utf-8", errors="replace", cwd=cwd, env=child_env(), creationflags=flags)
        except OSError:
            raise ValueError("无法启动 Claude Code CLI。") from None
        timer = threading.Timer(self.timeout, proc.kill)
        timer.start()
        text, final = "", None
        try:
            proc.stdin.write(user)
            proc.stdin.close()
            for line in proc.stdout:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                inner = event.get("event") or {}
                delta = inner.get("delta") or {}
                if event.get("type") == "stream_event" and delta.get("type") == "text_delta":
                    text += delta.get("text", "")
                    on_text(text)
                elif event.get("type") == "result":
                    final = event
            proc.wait()
        except BaseException:
            proc.kill()
            raise
        finally:
            timer.cancel()
        if final is None:  # killed by the timeout, or the CLI exited early (login, quota, bad flags…)
            detail = (proc.stderr.read() if proc.stderr else "").strip()
            raise ValueError(self._friendly(detail) if detail else
                             f"Claude Code 超过 {int(self.timeout)} 秒未返回或中途退出，本轮未应用。")
        result = final.get("result")
        if final.get("is_error") or final.get("subtype") not in {None, "success"} or not isinstance(result, str):
            raise ValueError(self._friendly(str(result or final.get("subtype") or "")) or "Claude Code 执行失败。")
        return final

    def run(self, messages: list[dict[str, str]], model: str, effort: str = "high",
            on_text: Callable[[str], None] | None = None) -> str:
        """Return the final text of one tool-less, unsaved turn. Raises ValueError with a safe Chinese message.
        With ``on_text`` the reply is streamed and reported as it grows."""
        if model not in MODELS or effort not in EFFORTS:
            raise ValueError("Claude 模型或推理强度无效。")
        system, user = self._split(messages)
        exe = self.finder()
        if not exe:
            raise ValueError("未找到 Claude Code CLI，请先安装并登录。")
        with tempfile.TemporaryDirectory(prefix="claude-text-turn-") as folder:
            # An empty working directory: no project CLAUDE.md, settings or files are in reach.
            system_file = Path(folder) / "system.txt"
            system_file.write_text(system + "\nDo not use tools. Return only the requested output.", encoding="utf-8")
            mcp_file = Path(folder) / "mcp.json"
            mcp_file.write_text('{"mcpServers": {}}', encoding="utf-8")
            args = [exe, "-p", "--output-format", "json", "--model", model, "--effort", effort,
                    "--tools", "", "--strict-mcp-config", "--mcp-config", str(mcp_file),
                    "--no-session-persistence", "--system-prompt-file", str(system_file)]
            if on_text is not None:
                return self._stream(args, user, folder, on_text)["result"]
            return self._invoke(args, user, folder)["result"]

    def run_session(self, messages: list[dict[str, str]], model: str, effort: str, *, session_id: str,
                    resume: bool, cwd: str, name: str = "") -> str:
        """One turn of a saved Claude Code session in a project folder.

        The session is created with ``--session-id`` (first round, named ``name``) or continued with
        ``--resume``; it is persisted like any Claude Code conversation in that folder, so it can be
        reopened with ``claude --resume`` there. The project's CLAUDE.md applies; only the read-only
        tools Read/Grep/Glob are available (no shell, no edits, no MCP servers). Raises
        ClaudeSessionMissing when a resumed session no longer exists."""
        if model not in MODELS or effort not in EFFORTS:
            raise ValueError("Claude 模型或推理强度无效。")
        if not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", session_id or ""):
            raise ValueError("Claude 会话 ID 无效。")
        if not Path(cwd).is_dir():
            raise ValueError("Claude Code 项目目录不存在。")
        system, user = self._split(messages)
        exe = self.finder()
        if not exe:
            raise ValueError("未找到 Claude Code CLI，请先安装并登录。")
        with tempfile.TemporaryDirectory(prefix="claude-session-turn-") as folder:
            system_file = Path(folder) / "system.txt"  # kept outside the project folder
            system_file.write_text(system, encoding="utf-8")
            mcp_file = Path(folder) / "mcp.json"
            mcp_file.write_text('{"mcpServers": {}}', encoding="utf-8")
            args = [exe, "-p", "--output-format", "json", "--model", model, "--effort", effort,
                    "--tools", ",".join(SESSION_TOOLS), "--allowedTools", *SESSION_TOOLS,
                    "--strict-mcp-config", "--mcp-config", str(mcp_file),
                    "--append-system-prompt-file", str(system_file)]
            args += ["--resume", session_id] if resume else ["--session-id", session_id] + (["--name", name[:80]] if name else [])
            data = self._invoke(args, user, cwd)
        if data.get("session_id") and data["session_id"] != session_id:
            raise ValueError("Claude Code 返回了不同的会话 ID，本轮未应用。")
        return data["result"]

    @staticmethod
    def _friendly(detail: str) -> str:
        lowered = detail.lower()
        if "limit" in lowered and ("usage" in lowered or "rate" in lowered or "reached" in lowered):
            return "Claude 套餐额度已用尽或触发速率限制，请稍后再试（与 claude.ai / Claude Code 共用额度）。"
        if "login" in lowered or "log in" in lowered or "authenticat" in lowered or "oauth" in lowered:
            return "Claude Code 登录已失效，请在终端运行 claude 重新登录。"
        return ("Claude Code：" + detail[:300]) if detail else ""
