"""对话树（Branchboard）：把本机 Claude Code 会话渲染成一棵二维对话树。

每一轮（Turn）= 一次提问 + 回答，是树上的一个节点：
- follow（继续提问）：在父节点回答之后接着问；
- highlight（划词提问）：针对父节点回答里选中的一段原文提问。

上下文语义：一轮能看到的只有「根 → 父节点」这条祖先路径。实现方式是每个子节点都用
``claude -p --resume <父节点 session> --fork-session`` 从父节点完成时的会话分叉出新会话，
父会话本身从不被续写，所以兄弟分支、主线之间互不可见。分支只隔离对话，不隔离工作目录。

整棵树存成 ``{工作目录}/.branchboard/{treeId}.json``；本机索引（树 → 工作目录）放在运行目录。
Claude Code 以 ``--input-format stream-json`` 运行，权限请求通过 ``--permission-prompt-tool stdio``
回传给本服务，再由前端批准 / 拒绝。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from flask import Blueprint, Response, jsonify, request, stream_with_context

from claude_code_cli import EFFORTS, MODELS, child_env, find_claude_executable

TREE_DIR = ".branchboard"
PERMISSION_MODES = {"default": "每次询问", "acceptEdits": "自动批准编辑", "plan": "计划模式"}
# 这些工具在无界面的 -p 模式里没有意义（需要交互式回答），直接禁用。
DISALLOWED_TOOLS = ("AskUserQuestion",)
EDIT_TOOLS = {"Write": "file_path", "Edit": "file_path", "MultiEdit": "file_path", "NotebookEdit": "notebook_path"}
COMMAND_TOOLS = {"Bash", "PowerShell"}
HIGHLIGHT_HEADER = "[针对上一条回答中的这段内容提问]"
SUMMARY_MIN_CHARS = 360
TREE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
SESSION_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
SUMMARY_SYSTEM = (
    "你负责为一段对话回答写卡片摘要。把回答压缩成 3–6 条要点，每条不超过 40 个字，"
    "使用与回答相同的语言，保留关键结论、数字和专有名词。只输出一个 JSON 字符串数组，不要输出其他内容。"
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def highlight_message(quote: str, question: str) -> str:
    """划词提问发给模型的用户消息：引用块 + 问题（§4）。"""
    quoted = "\n".join("> " + line if line.strip() else ">" for line in quote.strip().splitlines())
    return f"{HIGHLIGHT_HEADER}\n{quoted}\n\n{question.strip()}"


def turn_prompt(turn: dict[str, Any]) -> str:
    quote = (turn.get("quote") or {}).get("text")
    if turn.get("kind") == "highlight" and quote:
        return highlight_message(quote, turn["question"])
    return turn["question"]


def parse_summary(text: str) -> list[str]:
    """从摘要模型输出里取出要点列表；容忍 ```json 包裹或逐行列表。"""
    text = (text or "").strip()
    match = re.search(r"\[[\s\S]*\]", text)
    if match:
        try:
            items = json.loads(match.group(0))
            if isinstance(items, list):
                return [str(item).strip() for item in items if str(item).strip()][:6]
        except ValueError:
            pass
    lines = [re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", line).strip() for line in text.splitlines()]
    return [line for line in lines if line][:6]


def new_answer() -> dict[str, Any]:
    return {"status": "pending", "markdown": "", "summary": None, "toolEvents": [], "filesChanged": [],
            "ranCommands": False, "error": "", "statusLine": ""}


def public_turn(turn: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in turn.items() if not key.startswith("_")}


class EventHub:
    """每棵树一条事件流（SSE）。事件带递增序号，断线重连时可以从某个序号之后继续。"""

    def __init__(self, keep: int = 400):
        self._lock = threading.Condition()
        self._seq = 0
        self._events: dict[str, deque] = {}
        self._keep = keep

    @property
    def seq(self) -> int:
        with self._lock:
            return self._seq

    def publish(self, tree_id: str, kind: str, data: dict[str, Any]) -> None:
        with self._lock:
            self._seq += 1
            self._events.setdefault(tree_id, deque(maxlen=self._keep)).append((self._seq, kind, data))
            self._lock.notify_all()

    def since(self, tree_id: str, after: int) -> list[tuple[int, str, dict[str, Any]]]:
        with self._lock:
            return [event for event in self._events.get(tree_id, ()) if event[0] > after]

    def wait(self, tree_id: str, after: int, timeout: float) -> list[tuple[int, str, dict[str, Any]]]:
        deadline = time.monotonic() + timeout
        with self._lock:
            while True:
                events = [event for event in self._events.get(tree_id, ()) if event[0] > after]
                remaining = deadline - time.monotonic()
                if events or remaining <= 0:
                    return events
                self._lock.wait(remaining)


class TreeStore:
    """整棵树一个 JSON 文件，放在工作目录的 .branchboard/ 下；本机索引记录 treeId → 工作目录。"""

    def __init__(self, runtime_dir: Path):
        self.runtime_dir = Path(runtime_dir)
        self.index_path = self.runtime_dir / "index.json"
        self._lock = threading.RLock()

    def _read_index(self) -> dict[str, dict[str, Any]]:
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write_index(self, index: dict[str, Any]) -> None:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.index_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.index_path)

    @staticmethod
    def tree_path(cwd: str, tree_id: str) -> Path:
        return Path(cwd) / TREE_DIR / f"{tree_id}.json"

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            index = self._read_index()
        items = [{"id": tree_id, **meta, "missing": not self.tree_path(meta.get("cwd", ""), tree_id).is_file()}
                 for tree_id, meta in index.items() if TREE_ID_RE.match(tree_id)]
        return sorted(items, key=lambda item: item.get("updatedAt", ""), reverse=True)

    def load(self, tree_id: str) -> dict[str, Any]:
        if not TREE_ID_RE.match(tree_id or ""):
            raise KeyError("对话树不存在。")
        with self._lock:
            meta = self._read_index().get(tree_id)
        if not meta:
            raise KeyError("对话树不存在。")
        path = self.tree_path(meta["cwd"], tree_id)
        try:
            tree = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise KeyError(f"对话树文件已不存在：{path}") from None
        except ValueError:
            raise ValueError(f"对话树文件已损坏：{path}") from None
        tree.setdefault("turns", {})
        tree.setdefault("order", [])
        tree.setdefault("settings", {})
        return tree

    def save(self, tree: dict[str, Any]) -> None:
        tree["updatedAt"] = now_iso()
        data = {**tree, "turns": {tid: public_turn(turn) for tid, turn in tree["turns"].items()}}
        for turn in data["turns"].values():
            turn.get("answer", {}).pop("statusLine", None)
            turn.pop("permissions", None)
        path = self.tree_path(tree["cwd"], tree["id"])
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, path)
            index = self._read_index()
            index[tree["id"]] = {"cwd": tree["cwd"], "title": tree["title"], "updatedAt": tree["updatedAt"],
                                 "turnCount": len(tree["turns"])}
            self._write_index(index)

    def forget(self, tree_id: str, *, delete_file: bool) -> None:
        with self._lock:
            index = self._read_index()
            meta = index.pop(tree_id, None)
            if meta is None:
                raise KeyError("对话树不存在。")
            self._write_index(index)
        if delete_file:
            try:
                self.tree_path(meta["cwd"], tree_id).unlink()
            except OSError:
                pass


class TurnRun:
    """一轮正在运行的 Claude Code 进程：流式解析输出，把权限请求交给前端。"""

    def __init__(self, service: "BranchboardService", tree: dict[str, Any], turn: dict[str, Any]):
        self.service, self.tree, self.turn = service, tree, turn
        self.proc: subprocess.Popen | None = None
        self.write_lock = threading.Lock()
        self.pending: dict[str, dict[str, Any]] = {}
        self.interrupted = False
        self.tool_index: dict[str, dict[str, Any]] = {}
        self.last_publish = 0.0

    # ---- 进程 I/O ----
    def send(self, payload: dict[str, Any]) -> None:
        with self.write_lock:
            if not self.proc or not self.proc.stdin or self.proc.stdin.closed:
                return
            try:
                self.proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
                self.proc.stdin.flush()
            except (OSError, ValueError):
                pass

    def close_stdin(self) -> None:
        with self.write_lock:
            try:
                if self.proc and self.proc.stdin and not self.proc.stdin.closed:
                    self.proc.stdin.close()
            except OSError:
                pass

    def args(self, exe: str) -> list[str]:
        settings = self.tree.get("settings") or {}
        args = [exe, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
                "--include-partial-messages", "--permission-prompt-tool", "stdio",
                "--disallowedTools", *DISALLOWED_TOOLS]
        if settings.get("model") in MODELS:
            args += ["--model", settings["model"]]
        if settings.get("effort") in EFFORTS:
            args += ["--effort", settings["effort"]]
        mode = settings.get("permissionMode")
        if mode in ("acceptEdits", "plan"):
            args += ["--permission-mode", mode]
        parent = self.tree["turns"].get(self.turn.get("parentId") or "")
        if parent:
            # 每个子节点都从父节点的会话分叉；父会话从不被续写，兄弟分支互不污染。
            args += ["--resume", parent["sessionId"], "--fork-session"]
        return args

    def run(self) -> None:
        answer = self.turn["answer"]
        exe = self.service.finder()
        if not exe:
            self.fail("未找到 Claude Code CLI。请先安装（npm i -g @anthropic-ai/claude-code）并登录。")
            return
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        try:
            self.proc = self.service.popen(self.args(exe), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                                           cwd=self.tree["cwd"], env=child_env(), creationflags=flags)
        except OSError as exc:
            self.fail(f"无法启动 Claude Code CLI：{exc}")
            return
        stderr: list[str] = []

        def drain_stderr(stream=self.proc.stderr):
            try:
                stderr.extend(stream)
            finally:
                getattr(stream, "close", lambda: None)()
        stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
        stderr_thread.start()
        answer["status"] = "streaming"
        answer["statusLine"] = "正在启动 Claude Code…"
        self.publish(force=True)
        self.send({"type": "control_request", "request_id": "init", "request": {"subtype": "initialize", "hooks": None}})
        self.send({"type": "user", "message": {"role": "user", "content": turn_prompt(self.turn)},
                   "parent_tool_use_id": None, "session_id": "default"})
        result = None
        try:
            for line in self.proc.stdout:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                if event.get("type") == "result":
                    result = event
                    self.close_stdin()
                    continue
                with self.service._lock:  # 与快照 / 权限回复互斥，避免序列化时字典被改动
                    self.handle(event)
            self.proc.wait()
        except Exception as exc:  # noqa: BLE001 - 解析异常也要落盘为错误状态
            self.proc.kill()
            self.fail(f"读取 Claude Code 输出失败：{exc}")
            return
        finally:
            self.pending.clear()
            self.turn["permissions"] = []
            self.close_stdin()
            getattr(self.proc.stdout, "close", lambda: None)()
        stderr_thread.join(2)
        self.finish(result, "".join(stderr))

    # ---- 事件解析 ----
    def handle(self, event: dict[str, Any]) -> None:
        kind = event.get("type")
        answer = self.turn["answer"]
        if kind == "system" and event.get("subtype") == "init" and event.get("session_id"):
            self.turn["sessionId"] = event["session_id"]
            answer["statusLine"] = "思考中…"
            self.publish()
        elif kind == "stream_event":
            inner = event.get("event") or {}
            if event.get("parent_tool_use_id"):
                return  # 子代理内部的流不进入主回答
            if inner.get("type") == "content_block_start":
                block = inner.get("content_block") or {}
                if block.get("type") == "text":
                    if answer["markdown"] and not answer["markdown"].endswith("\n\n"):
                        answer["markdown"] = answer["markdown"].rstrip("\n") + "\n\n"
                    answer["statusLine"] = "正在回答…"
                elif block.get("type") == "thinking":
                    answer["statusLine"] = "思考中…"
                elif block.get("type") in ("tool_use", "server_tool_use"):
                    answer["statusLine"] = f"调用工具：{block.get('name', '')}"
                self.publish()
            elif inner.get("type") == "content_block_delta":
                delta = inner.get("delta") or {}
                if delta.get("type") == "text_delta":
                    answer["markdown"] += delta.get("text", "")
                    self.publish()
        elif kind == "assistant" and not event.get("parent_tool_use_id"):
            for block in (event.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    self.add_tool(block)
        elif kind == "user" and not event.get("parent_tool_use_id"):
            content = (event.get("message") or {}).get("content")
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    self.tool_result(block)
        elif kind == "control_request":
            self.permission_request(event)

    def add_tool(self, block: dict[str, Any]) -> None:
        name, data = str(block.get("name") or ""), block.get("input") or {}
        tool = {"id": block.get("id"), "name": name, "input": data, "outputPreview": "", "ts": now_iso()}
        self.turn["answer"]["toolEvents"].append(tool)
        if tool["id"]:
            self.tool_index[tool["id"]] = tool
        self.turn["answer"]["statusLine"] = f"调用工具：{name} {describe_tool_input(name, data)}".strip()
        if name in COMMAND_TOOLS:
            self.turn["answer"]["ranCommands"] = True
        self.publish(force=True)

    def tool_result(self, block: dict[str, Any]) -> None:
        tool = self.tool_index.get(block.get("tool_use_id") or "")
        if not tool:
            return
        content = block.get("content")
        if isinstance(content, list):
            content = "\n".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
        tool["outputPreview"] = str(content or "")[:1200]
        tool["isError"] = bool(block.get("is_error"))
        key = EDIT_TOOLS.get(tool["name"])
        if key and not tool["isError"] and isinstance(tool["input"], dict) and tool["input"].get(key):
            path = relative_path(str(tool["input"][key]), self.tree["cwd"])
            if path not in self.turn["answer"]["filesChanged"]:
                self.turn["answer"]["filesChanged"].append(path)
        self.turn["answer"]["statusLine"] = "思考中…"
        self.publish(force=True)

    def permission_request(self, event: dict[str, Any]) -> None:
        req = event.get("request") or {}
        request_id = str(event.get("request_id") or "")
        if req.get("subtype") != "can_use_tool" or not request_id:
            if request_id:  # 未知的控制请求：明确回绝，避免 CLI 一直等待
                self.send({"type": "control_response", "response": {"subtype": "error", "request_id": request_id,
                                                                     "error": "Branchboard 不支持该请求。"}})
            return
        item = {"requestId": request_id, "tool": str(req.get("tool_name") or ""), "input": req.get("input") or {},
                "description": str(req.get("description") or ""), "toolUseId": req.get("tool_use_id")}
        self.pending[request_id] = item
        self.turn["permissions"] = list(self.pending.values())
        self.turn["answer"]["statusLine"] = f"等待批准：{item['tool']}"
        self.publish(force=True)

    def reply_permission(self, request_id: str, allow: bool) -> None:
        item = self.pending.pop(request_id, None)
        if not item:
            raise KeyError("权限请求已失效。")
        if allow:
            response = {"behavior": "allow", "updatedInput": item["input"]}
        else:
            response = {"behavior": "deny", "message": "用户在 Branchboard 中拒绝了这次操作。"}
        self.send({"type": "control_response",
                   "response": {"subtype": "success", "request_id": request_id, "response": response}})
        tool = self.tool_index.get(item.get("toolUseId") or "")
        if tool is None:
            tool = next((t for t in reversed(self.turn["answer"]["toolEvents"])
                         if t["name"] == item["tool"] and "approved" not in t), None)
        if tool is not None:
            tool["approved"] = allow
        self.turn["permissions"] = list(self.pending.values())
        self.turn["answer"]["statusLine"] = "思考中…" if allow else "已拒绝，等待 Claude 继续…"
        self.publish(force=True)

    def interrupt(self) -> None:
        self.interrupted = True
        for request_id in list(self.pending):
            self.reply_permission(request_id, False)
        self.send({"type": "control_request", "request_id": f"int-{uuid.uuid4().hex[:8]}",
                   "request": {"subtype": "interrupt"}})
        proc = self.proc

        def kill_later():
            time.sleep(6)
            if proc and proc.poll() is None:
                proc.kill()
        threading.Thread(target=kill_later, daemon=True).start()

    # ---- 收尾 ----
    def publish(self, force: bool = False) -> None:
        now = time.monotonic()
        if force or now - self.last_publish > 0.12:
            self.last_publish = now
            self.service.publish_turn(self.tree, self.turn)

    def fail(self, message: str) -> None:
        answer = self.turn["answer"]
        with self.service._lock:
            answer.update(status="error", error=message, statusLine="")
            self.turn["permissions"] = []
        self.service.turn_finished(self.tree, self.turn)

    def finish(self, result: dict[str, Any] | None, stderr: str) -> None:
        with self.service._lock:
            self._finish(result, stderr)
        self.service.turn_finished(self.tree, self.turn)

    def _finish(self, result: dict[str, Any] | None, stderr: str) -> None:
        answer = self.turn["answer"]
        answer["statusLine"] = ""
        if result and result.get("session_id"):
            self.turn["sessionId"] = result["session_id"]
        if self.interrupted:
            answer["status"] = "interrupted"
        elif result is None:
            answer.update(status="error", error=friendly_error(stderr) or "Claude Code 中途退出，没有返回结果。")
        elif result.get("is_error") or result.get("subtype") not in (None, "success"):
            detail = str(result.get("result") or result.get("subtype") or "")
            answer.update(status="error", error=friendly_error(detail) or "Claude Code 执行失败。")
        else:
            answer["status"] = "done"
            if not answer["markdown"].strip() and isinstance(result.get("result"), str):
                answer["markdown"] = result["result"]


def describe_tool_input(name: str, data: Any) -> str:
    if not isinstance(data, dict):
        return ""
    for key in ("file_path", "notebook_path", "path", "pattern", "command", "url", "query", "description"):
        if data.get(key):
            text = str(data[key]).replace("\n", " ")
            return text if len(text) <= 80 else text[:77] + "…"
    return ""


def relative_path(path: str, cwd: str) -> str:
    try:
        return Path(path).resolve().relative_to(Path(cwd).resolve()).as_posix()
    except (ValueError, OSError):
        return path


def friendly_error(detail: str) -> str:
    lowered = (detail or "").lower()
    if not lowered.strip():
        return ""
    if "no conversation found" in lowered:
        return "父节点对应的 Claude Code 会话已不存在（可能被清理），无法从这里继续。"
    if "limit" in lowered and ("usage" in lowered or "rate" in lowered or "reached" in lowered):
        return "Claude 套餐额度已用尽或触发速率限制，请稍后再试。"
    if "login" in lowered or "log in" in lowered or "authenticat" in lowered or "oauth" in lowered:
        return "Claude Code 登录已失效，请在终端运行 claude 重新登录。"
    return "Claude Code：" + detail.strip()[:400]


class BranchboardService:
    def __init__(self, runtime_dir: Path, *, finder: Callable[[], str | None] = find_claude_executable,
                 popen: Callable[..., Any] = subprocess.Popen,
                 summarize: Callable[[str], list[str]] | None = None,
                 claude_status: Callable[[], dict[str, Any]] | None = None):
        self.store = TreeStore(Path(runtime_dir))
        self.hub = EventHub()
        self.finder, self.popen, self.summarize, self.claude_status = finder, popen, summarize, claude_status
        self._lock = threading.RLock()
        self._trees: dict[str, dict[str, Any]] = {}
        self._runs: dict[str, TurnRun] = {}

    # ---- 树 ----
    def tree(self, tree_id: str) -> dict[str, Any]:
        with self._lock:
            tree = self._trees.get(tree_id)
            if tree is None:
                tree = self.store.load(tree_id)
                for turn in tree["turns"].values():  # 上次运行时被打断的轮次
                    if turn.get("answer", {}).get("status") in ("pending", "streaming"):
                        turn["answer"]["status"] = "interrupted"
                self._trees[tree_id] = tree
            return tree

    def snapshot(self, tree_id: str) -> dict[str, Any]:
        with self._lock:
            tree = self.tree(tree_id)
            return json.loads(json.dumps({**tree, "turns": {k: public_turn(v) for k, v in tree["turns"].items()},
                                          "seq": self.hub.seq}))

    def list_trees(self) -> list[dict[str, Any]]:
        return self.store.list()

    def create_tree(self, title: str, cwd: str, settings: dict[str, Any] | None = None) -> dict[str, Any]:
        title = (title or "").strip()[:120] or "未命名对话树"
        cwd = (cwd or "").strip().strip('"')
        if not cwd or not Path(cwd).is_dir():
            raise ValueError("请选择一个存在的工作目录。")
        cwd = str(Path(cwd).resolve())
        tree = {"id": uuid.uuid4().hex, "title": title, "cwd": cwd, "rootId": None, "turns": {}, "order": [],
                "createdAt": now_iso(), "settings": clean_settings(settings or {})}
        with self._lock:
            self.store.save(tree)
            self._trees[tree["id"]] = tree
        return self.snapshot(tree["id"])

    def update_tree(self, tree_id: str, data: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            tree = self.tree(tree_id)
            if "title" in data:
                tree["title"] = str(data["title"] or "").strip()[:120] or tree["title"]
            if isinstance(data.get("settings"), dict):
                tree["settings"] = clean_settings({**tree.get("settings", {}), **data["settings"]})
            self.store.save(tree)
            self.hub.publish(tree_id, "tree", {"title": tree["title"], "settings": tree["settings"]})
        return self.snapshot(tree_id)

    def delete_tree(self, tree_id: str, *, delete_file: bool = True) -> None:
        with self._lock:
            if any(run.tree["id"] == tree_id for run in self._runs.values()):
                raise ValueError("这棵树还有正在运行的轮次，请先中断。")
            self._trees.pop(tree_id, None)
            self.store.forget(tree_id, delete_file=delete_file)

    # ---- 轮次 ----
    def create_turn(self, tree_id: str, data: dict[str, Any]) -> dict[str, Any]:
        question = str(data.get("question") or "").strip()
        if not question:
            raise ValueError("问题不能为空。")
        kind = data.get("kind")
        parent_id = data.get("parentId") or None
        with self._lock:
            tree = self.tree(tree_id)
            if parent_id is None:
                if tree["rootId"]:
                    raise ValueError("这棵树已经有起点了，请从某个回答继续提问。")
                kind = "root"
            else:
                parent = tree["turns"].get(parent_id)
                if not parent:
                    raise KeyError("父节点不存在。")
                if kind not in ("follow", "highlight"):
                    raise ValueError("轮次类型无效。")
                if not parent.get("sessionId") or parent["answer"]["status"] not in ("done", "interrupted"):
                    raise ValueError("父节点的回答还没完成，暂时不能从它继续。")
            turn: dict[str, Any] = {"id": uuid.uuid4().hex, "treeId": tree_id, "parentId": parent_id, "kind": kind,
                                    "createdAt": now_iso(), "question": question[:20000], "answer": new_answer(),
                                    "permissions": []}
            if kind == "highlight":
                quote = data.get("quote") or {}
                text = str(quote.get("text") or "").strip()
                if not text:
                    raise ValueError("划词提问需要选中一段原文。")
                turn["quote"] = {"text": text[:4000], "source": "summary" if quote.get("source") == "summary" else "full"}
                for key in ("start", "end"):
                    if isinstance(quote.get(key), int) and quote[key] >= 0:
                        turn["quote"][key] = quote[key]
                siblings = [t for t in tree["turns"].values() if t.get("parentId") == parent_id and t["kind"] == "highlight"]
                turn["layout"] = {"dir": 1 if len(siblings) % 2 == 0 else -1}
            if kind == "follow" and isinstance(data.get("dir"), int) and data["dir"] in (1, -1):
                turn["layout"] = {"dir": data["dir"]}
            tree["turns"][turn["id"]] = turn
            tree["order"].append(turn["id"])
            if kind == "root":
                tree["rootId"] = turn["id"]
                if tree["title"] == "未命名对话树":
                    tree["title"] = question.splitlines()[0][:60]
            self.store.save(tree)
            self.hub.publish(tree_id, "turn.created", public_turn(turn))
            self._start(tree, turn)
        return public_turn(turn)

    def retry_turn(self, tree_id: str, turn_id: str) -> dict[str, Any]:
        with self._lock:
            tree = self.tree(tree_id)
            turn = tree["turns"].get(turn_id)
            if not turn:
                raise KeyError("轮次不存在。")
            if turn_id in self._runs:
                raise ValueError("这一轮正在运行。")
            if any(t.get("parentId") == turn_id for t in tree["turns"].values()):
                raise ValueError("这一轮已经有后续提问，不能原地重试。")
            turn["answer"] = new_answer()
            turn.pop("sessionId", None)
            self._start(tree, turn)
        return public_turn(turn)

    def _start(self, tree: dict[str, Any], turn: dict[str, Any]) -> None:
        run = TurnRun(self, tree, turn)
        self._runs[turn["id"]] = run
        threading.Thread(target=run.run, name=f"branchboard-{turn['id'][:8]}", daemon=True).start()

    def interrupt(self, tree_id: str, turn_id: str) -> None:
        run = self._runs.get(turn_id)
        if not run or run.tree["id"] != tree_id:
            raise KeyError("这一轮没有在运行。")
        with self._lock:
            run.interrupt()

    def reply_permission(self, tree_id: str, turn_id: str, request_id: str, allow: bool) -> None:
        run = self._runs.get(turn_id)
        if not run or run.tree["id"] != tree_id:
            raise KeyError("这一轮没有在运行。")
        with self._lock:
            run.reply_permission(request_id, bool(allow))

    def publish_turn(self, tree: dict[str, Any], turn: dict[str, Any]) -> None:
        with self._lock:
            data = json.loads(json.dumps(public_turn(turn)))
        self.hub.publish(tree["id"], "turn", data)

    def turn_finished(self, tree: dict[str, Any], turn: dict[str, Any]) -> None:
        with self._lock:
            self._runs.pop(turn["id"], None)
            turn["permissions"] = []
            self.store.save(tree)
        self.publish_turn(tree, turn)
        answer = turn["answer"]
        if answer["status"] == "done" and self.summarize and len(answer["markdown"]) >= SUMMARY_MIN_CHARS:
            threading.Thread(target=self._summarize, args=(tree, turn), daemon=True).start()

    def _summarize(self, tree: dict[str, Any], turn: dict[str, Any]) -> None:
        try:
            summary = self.summarize(turn["answer"]["markdown"])
        except Exception:  # noqa: BLE001 - 摘要失败时卡片显示全文前几行
            return
        if not summary:
            return
        with self._lock:
            turn["answer"]["summary"] = summary
            self.store.save(tree)
        self.hub.publish(tree["id"], "turn.summary", {"turnId": turn["id"], "summary": summary})

    def events(self, tree_id: str, after: int) -> Iterator[str]:
        self.tree(tree_id)  # 不存在时抛 KeyError
        yield "retry: 2000\n\n"
        while True:
            events = self.hub.wait(tree_id, after, 15)
            if not events:
                yield ": keep-alive\n\n"
                continue
            for seq, kind, data in events:
                after = seq
                yield f"id: {seq}\nevent: {kind}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def clean_settings(settings: dict[str, Any]) -> dict[str, str]:
    return {"model": settings.get("model") if settings.get("model") in MODELS else "",
            "effort": settings.get("effort") if settings.get("effort") in EFFORTS else "",
            "permissionMode": settings.get("permissionMode") if settings.get("permissionMode") in PERMISSION_MODES else "default"}


def claude_summarizer(runner: Any) -> Callable[[str], list[str]]:
    """用一次轻量、无工具、不落盘的调用生成卡片摘要；不进入任何分支的上下文。"""
    def summarize(markdown: str) -> list[str]:
        text = runner.run([{"role": "system", "content": SUMMARY_SYSTEM},
                           {"role": "user", "content": markdown[:24000]}], "sonnet", "low")
        return parse_summary(text)
    return summarize


def create_branchboard_blueprint(service: BranchboardService) -> Blueprint:
    bp = Blueprint("branchboard_api", __name__, url_prefix="/api/branchboard")

    def body() -> dict[str, Any]:
        value = request.get_json(silent=True)
        return value if isinstance(value, dict) else {}

    def error(exc: Exception):
        status = 404 if isinstance(exc, KeyError) else 400
        message = exc.args[0] if exc.args else "请求失败。"
        return jsonify({"error": str(message)}), status

    @bp.get("/config")
    def config():
        claude = None
        if service.claude_status:
            try:
                claude = service.claude_status()
            except Exception as exc:  # noqa: BLE001
                claude = {"available": False, "error": str(exc)}
        return jsonify({"claude": claude, "models": [{"id": k, "label": v} for k, v in MODELS.items()],
                        "efforts": list(EFFORTS),
                        "permissionModes": [{"id": k, "label": v} for k, v in PERMISSION_MODES.items()]})

    @bp.get("/trees")
    def trees():
        return jsonify({"trees": service.list_trees()})

    @bp.post("/trees")
    def create_tree():
        data = body()
        try:
            return jsonify({"tree": service.create_tree(data.get("title"), data.get("cwd"), data.get("settings"))}), 201
        except (KeyError, ValueError, OSError) as exc:
            return error(exc)

    @bp.get("/trees/<tree_id>")
    def get_tree(tree_id: str):
        try:
            return jsonify({"tree": service.snapshot(tree_id)})
        except (KeyError, ValueError) as exc:
            return error(exc)

    @bp.patch("/trees/<tree_id>")
    def patch_tree(tree_id: str):
        try:
            return jsonify({"tree": service.update_tree(tree_id, body())})
        except (KeyError, ValueError, OSError) as exc:
            return error(exc)

    @bp.delete("/trees/<tree_id>")
    def delete_tree(tree_id: str):
        try:
            service.delete_tree(tree_id, delete_file=request.args.get("keepFile") != "1")
            return jsonify({"ok": True})
        except (KeyError, ValueError) as exc:
            return error(exc)

    @bp.post("/trees/<tree_id>/turns")
    def create_turn(tree_id: str):
        try:
            return jsonify({"turn": service.create_turn(tree_id, body())}), 201
        except (KeyError, ValueError, OSError) as exc:
            return error(exc)

    @bp.post("/trees/<tree_id>/turns/<turn_id>/retry")
    def retry_turn(tree_id: str, turn_id: str):
        try:
            return jsonify({"turn": service.retry_turn(tree_id, turn_id)})
        except (KeyError, ValueError) as exc:
            return error(exc)

    @bp.post("/trees/<tree_id>/turns/<turn_id>/interrupt")
    def interrupt(tree_id: str, turn_id: str):
        try:
            service.interrupt(tree_id, turn_id)
            return jsonify({"ok": True})
        except (KeyError, ValueError) as exc:
            return error(exc)

    @bp.post("/trees/<tree_id>/turns/<turn_id>/permissions/<request_id>")
    def permission(tree_id: str, turn_id: str, request_id: str):
        try:
            service.reply_permission(tree_id, turn_id, request_id, bool(body().get("allow")))
            return jsonify({"ok": True})
        except (KeyError, ValueError) as exc:
            return error(exc)

    @bp.get("/trees/<tree_id>/events")
    def events(tree_id: str):
        try:
            after = max(0, int(request.headers.get("Last-Event-ID") or request.args.get("after") or 0))
        except ValueError:
            after = 0
        try:
            stream = service.events(tree_id, after)
            first = next(stream)
        except (KeyError, ValueError) as exc:
            return error(exc)

        def generate():
            yield first
            yield from stream
        response = Response(stream_with_context(generate()), mimetype="text/event-stream")
        response.headers["Cache-Control"] = "no-cache"
        response.headers["X-Accel-Buffering"] = "no"
        return response

    return bp
