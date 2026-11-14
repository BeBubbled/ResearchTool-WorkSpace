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

import hashlib
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
DEFAULT_TITLE = "新聊天"
LEGACY_DEFAULT_TITLE = "未命名对话树"
TITLE_RENAME_AT = 3  # 提出第几个问题时，用前几个问题重新命名一次
TITLE_SYSTEM = (
    "你为一段对话起标题。用户消息里是这段对话到目前为止提出的问题。"
    "只输出标题本身：一行，概括对话主题，中文不超过 16 个字（英文不超过 8 个词），"
    "语言与问题一致；不要引号、书名号、句末标点，也不要任何解释。"
)
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


def questions_of(turn: dict[str, Any]) -> list[dict[str, Any]]:
    """一轮里的问题小框。旧数据只有 question / quote 一个问题，补成同样的结构。"""
    items = turn.get("questions")
    if isinstance(items, list) and items:
        return items
    item: dict[str, Any] = {"id": "q0", "text": turn.get("question", ""), "sourceId": turn.get("parentId")}
    if turn.get("quote"):
        item["quote"] = turn["quote"]
    return [item]


def context_ids(tree: dict[str, Any], turn_id: str | None) -> list[str]:
    """某一轮完成后其会话里「看得到」的轮次：主父链上的每一轮，加上这些轮次拼接进来的其他分支来源。"""
    seen: list[str] = []
    cur = tree["turns"].get(turn_id or "")
    while cur:
        for tid in [cur["id"], *[q.get("sourceId") for q in questions_of(cur)]]:
            if tid and tid in tree["turns"] and tid not in seen:
                seen.append(tid)
        cur = tree["turns"].get(cur.get("parentId") or "")
    return seen


def quote_block(quote: str) -> str:
    return "\n".join("> " + line if line.strip() else ">" for line in quote.strip().splitlines())


def turn_prompt(tree: dict[str, Any], turn: dict[str, Any]) -> str:
    """本轮发给模型的用户消息。会话从主父节点分叉，所以主父链上的内容已在上下文里；
    来自其他分支的被划词轮次不在上下文里，把它们的提问和回答作为引用材料拼在前面（§4 的扩展）。"""
    items = questions_of(turn)
    primary = turn.get("parentId")
    visible = set(context_ids(tree, primary)) if primary else set()
    number = {tid: i + 1 for i, tid in enumerate(tree["order"])}
    injected = []
    for item in items:
        sid = item.get("sourceId")
        if sid and sid not in visible and sid not in injected and sid in tree["turns"]:
            injected.append(sid)
    parts: list[str] = []
    if injected:
        blocks = []
        for sid in injected:
            src = tree["turns"][sid]
            # 来源卡片合并过子树：用合并版（已经吸收了它下面各分支的结论）
            answer = (src.get("merged") or {}).get("markdown") or (src.get("answer") or {}).get("markdown", "")
            if len(answer) > 12000:
                answer = answer[:12000] + "\n…（后略）"
            blocks.append(f"### 第 {number.get(sid, '?')} 轮\n提问：{src.get('question', '')}\n\n回答：\n{answer}")
        parts.append("[以下内容来自其他对话分支，此前的对话里没有它们]\n\n" + "\n\n".join(blocks))
    blocks = []
    for item in items:
        sid, text = item.get("sourceId"), str(item.get("text") or "").strip()
        quote = item.get("quote") or {}
        lines = []
        if quote.get("text"):
            part = "提问" if quote.get("source") == "question" else "回答"
            where = f"上一条{part}" if sid == primary and len(items) == 1 else f"第 {number.get(sid, '?')} 轮的{part}"
            if quote.get("source") == "summary":
                where += "摘要"
            lines.append(f"[针对{where}中的这段内容提问]\n{quote_block(quote['text'])}")
        lines.append(text or "（请结合上面这段内容展开说明。）")
        blocks.append("\n\n".join(lines))
    if len(blocks) == 1:
        parts.append(blocks[0])
    else:
        parts.append(f"我有 {len(blocks)} 个相关的问题，请逐一回答：\n\n" +
                     "\n\n".join(f"## 问题 {i + 1}\n{block}" for i, block in enumerate(blocks)))
    # 主父卡片合并过子树：会话里只有它的原始回答，把合并版作为「以此为准」的材料放在最前面
    merged = (tree["turns"].get(primary or "") or {}).get("merged") or {}
    if merged.get("markdown"):
        text = merged["markdown"]
        if len(text) > 16000:
            text = text[:16000] + "\n…（后略）"
        parts.insert(0, f"[第 {number.get(primary, '?')} 轮的回答之后整合了 {len(merged.get('absorbed') or [])} 个划词分支的结论，"
                        f"下面是整合后的版本；它和你此前的回答不一致的地方，以它为准]\n\n{text}")
    return "\n\n".join(parts)


def subtree_members(tree: dict[str, Any], root_id: str) -> list[str]:
    """子树成员（与前端 subtreeMembers 一致）：从 root_id 上的划词分支出发，按创建顺序收进所有来源都已在子树里的卡片；
    root_id 自己的普通追问（主线上的新问题）不算，同时挂在子树外卡片上的卡片也不算。"""
    inside, members = {root_id}, []
    for tid in tree["order"]:
        turn = tree["turns"].get(tid)
        if not turn or tid == root_id:
            continue
        sources = list(dict.fromkeys(q.get("sourceId") for q in questions_of(turn) if q.get("sourceId")))
        if not sources or not all(s in inside for s in sources):
            continue
        if root_id in sources and not any(q.get("sourceId") == root_id and (q.get("quote") or {}).get("text")
                                          for q in questions_of(turn)):
            continue
        inside.add(tid)
        members.append(tid)
    return members


def absorbed_by(tree: dict[str, Any]) -> dict[str, str]:
    """被合并收起的卡片 → 收起它的那张卡片。"""
    owner: dict[str, str] = {}
    for tid in tree["order"]:
        for member in ((tree["turns"].get(tid) or {}).get("merged") or {}).get("absorbed") or []:
            owner.setdefault(member, tid)
    return owner


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


def fallback_title(question: str) -> str:
    """模型标题生成之前（或不可用时）先用第一个问题的首行。"""
    lines = [line.strip() for line in (question or "").splitlines() if line.strip()]
    return lines[0][:60] if lines else DEFAULT_TITLE


def clean_title(text: str) -> str:
    """模型输出 → 标题：取第一行，去掉引号、Markdown 标记和句末标点。"""
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if not lines:
        return ""
    first = re.sub(r"<[^>]{0,20}>", "", lines[0])  # 模型偶尔带出 </br> 之类的标签
    title = re.sub(r"^(标题|title)\s*[:：]\s*", "", first.lstrip("#*- ").strip(), flags=re.I)
    quotes = "\"'“”‘’「」『』《》`* "
    title = title.strip(quotes).rstrip("。.!！?？;；,，").strip(quotes)  # 「标题」。 和 「标题。」 都要处理
    return title[:60]


def question_count(tree: dict[str, Any]) -> int:
    return sum(len(questions_of(tree["turns"][tid])) for tid in tree["order"] if tid in tree["turns"])


def question_texts(tree: dict[str, Any], limit: int) -> list[str]:
    """按提问顺序取前 limit 个问题的文字；纯划词引用（没写问题）用引用的原文代替。"""
    out: list[str] = []
    for tid in tree["order"]:
        for q in questions_of(tree["turns"].get(tid) or {}):
            text = str(q.get("text") or "").strip() or str((q.get("quote") or {}).get("text") or "").strip()
            if text:
                out.append(text)
            if len(out) >= limit:
                return out
    return out


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
                                 "turnCount": len(tree["turns"]), "createdAt": tree.get("createdAt", "")}
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
        with self.service._lock:
            prompt = turn_prompt(self.tree, self.turn)
        self.send({"type": "user", "message": {"role": "user", "content": prompt},
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
                 claude_status: Callable[[], dict[str, Any]] | None = None,
                 complete: Callable[..., str] | None = None,
                 titler: Callable[[list[str]], str] | None = None,
                 default_cwd: Path | str | None = None):
        self.store = TreeStore(Path(runtime_dir))
        # 新聊天不选目录时用的工作目录；第一次用到才创建
        self.default_cwd = Path(default_cwd) if default_cwd else Path(runtime_dir) / "chats"
        # 自动命名：titler(前几个问题) -> 标题；没有时用第一个问题的首行
        self.titler = titler
        self.hub = EventHub()
        self.finder, self.popen, self.summarize, self.claude_status = finder, popen, summarize, claude_status
        # 完整版的模型辅助（分类、改写修正段落）：complete(messages, model, effort) -> 文本；无工具、不落盘
        self.complete = complete
        self._lock = threading.RLock()
        self._trees: dict[str, dict[str, Any]] = {}
        self._runs: dict[str, TurnRun] = {}

    # ---- 树 ----
    def tree(self, tree_id: str) -> dict[str, Any]:
        with self._lock:
            tree = self._trees.get(tree_id)
            if tree is None:
                tree = self.store.load(tree_id)
                for turn in tree["turns"].values():
                    turn["questions"] = questions_of(turn)  # 旧数据：单个问题补成问题列表
                    if turn.get("answer", {}).get("status") in ("pending", "streaming"):  # 上次运行时被打断
                        turn["answer"]["status"] = "interrupted"
                if "titleSource" not in tree:  # 旧数据：标题还是占位或首问首行的算自动命名，其余算用户起的
                    root = tree["turns"].get(tree.get("rootId") or "") or {}
                    auto = tree.get("title") in (DEFAULT_TITLE, LEGACY_DEFAULT_TITLE, fallback_title(root.get("question", "")))
                    tree["titleSource"] = "auto" if auto else "manual"
                self._trees[tree_id] = tree
            return tree

    def snapshot(self, tree_id: str) -> dict[str, Any]:
        with self._lock:
            tree = self.tree(tree_id)
            data = {k: v for k, v in tree.items() if k != "fullVersionCache"}
            return json.loads(json.dumps({**data, "turns": {k: public_turn(v) for k, v in tree["turns"].items()},
                                          "seq": self.hub.seq}))

    def list_trees(self) -> list[dict[str, Any]]:
        return self.store.list()

    def create_tree(self, title: str, cwd: str, settings: dict[str, Any] | None = None) -> dict[str, Any]:
        title = (title or "").strip()[:120]
        cwd = (cwd or "").strip().strip('"')
        if not cwd:  # 新聊天没选目录：用默认工作目录
            self.default_cwd.mkdir(parents=True, exist_ok=True)
            cwd = str(self.default_cwd)
        if not Path(cwd).is_dir():
            raise ValueError("请选择一个存在的工作目录。")
        cwd = str(Path(cwd).resolve())
        tree = {"id": uuid.uuid4().hex, "title": title or DEFAULT_TITLE, "titleSource": "manual" if title else "auto",
                "cwd": cwd, "rootId": None, "turns": {}, "order": [],
                "createdAt": now_iso(), "settings": clean_settings(settings or {})}
        with self._lock:
            self.store.save(tree)
            self._trees[tree["id"]] = tree
        return self.snapshot(tree["id"])

    def update_tree(self, tree_id: str, data: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            tree = self.tree(tree_id)
            title = str(data.get("title") or "").strip()[:120]
            if title:  # 用户亲手改的名字，之后不再自动改
                tree["title"], tree["titleSource"] = title, "manual"
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
    @staticmethod
    def _requested_questions(data: dict[str, Any]) -> list[dict[str, Any]]:
        """请求里的问题小框；兼容旧格式 {parentId, kind, question, quote}。"""
        items = data.get("questions")
        if not isinstance(items, list):
            item = {"sourceId": data.get("parentId") or None, "text": data.get("question")}
            if data.get("kind") == "highlight":
                item["quote"] = data.get("quote") or {}
                if not str(item["quote"].get("text") or "").strip():
                    raise ValueError("划词提问需要选中一段原文。")
            items = [item]
        if not items or len(items) > 20:
            raise ValueError("一次最多提 20 个问题。")
        clean = []
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                raise ValueError("问题格式无效。")
            entry: dict[str, Any] = {"id": f"q{index}", "text": str(item.get("text") or "").strip()[:20000],
                                     "sourceId": item.get("sourceId") or None}
            quote = item.get("quote") or {}
            if isinstance(quote, dict) and str(quote.get("text") or "").strip():
                entry["quote"] = {"text": str(quote["text"]).strip()[:4000],
                                  "source": quote.get("source") if quote.get("source") in ("summary", "question") else "full"}
                for key in ("start", "end"):
                    if isinstance(quote.get(key), int) and quote[key] >= 0:
                        entry["quote"][key] = quote[key]
            clean.append(entry)
        if not any(entry["text"] for entry in clean):
            raise ValueError("问题不能为空。")
        return clean

    def create_turn(self, tree_id: str, data: dict[str, Any]) -> dict[str, Any]:
        questions = self._requested_questions(data)
        with self._lock:
            tree = self.tree(tree_id)
            asked_before = question_count(tree)
            sources = list(dict.fromkeys(q["sourceId"] for q in questions if q["sourceId"]))
            if not sources:
                if tree["rootId"]:
                    raise ValueError("这棵树已经有起点了，请从某个回答继续提问。")
                if len(questions) > 1 or any(q.get("quote") for q in questions):
                    raise ValueError("起点只能是一个普通问题。")
                kind, parent_id = "root", None
            else:
                if any(not q["sourceId"] for q in questions):
                    raise ValueError("每个问题都要指明来自哪张卡片。")
                owners = absorbed_by(tree)
                for sid in sources:
                    if sid in owners:
                        raise ValueError(f"第 {tree['order'].index(sid) + 1} 轮已并入第 {tree['order'].index(owners[sid]) + 1} 轮，"
                                         "请在合并后的卡片上提问，或先取消合并。")
                    src = tree["turns"].get(sid)
                    if not src:
                        raise KeyError("被提问的卡片不存在。")
                    if src["answer"]["status"] not in ("done", "interrupted"):
                        raise ValueError(f"第 {tree['order'].index(sid) + 1} 轮的回答还没完成，暂时不能从它提问。")
                # 主父节点：最近创建的来源，它的会话上下文最完整；其余来源以引用材料的形式拼进本轮消息
                parent_id = max(sources, key=tree["order"].index)
                if not tree["turns"][parent_id].get("sessionId"):
                    raise ValueError("父节点没有可继续的 Claude Code 会话。")
                if len(questions) > 1:
                    kind = "multi"
                else:
                    kind = "highlight" if questions[0].get("quote") else "follow"
            turn: dict[str, Any] = {"id": uuid.uuid4().hex, "treeId": tree_id, "parentId": parent_id, "kind": kind,
                                    "createdAt": now_iso(), "questions": questions,
                                    "question": "\n\n".join(q["text"] for q in questions if q["text"])[:20000],
                                    "answer": new_answer(), "permissions": []}
            if kind == "highlight":
                turn["quote"] = questions[0]["quote"]
            if parent_id:
                # 和父节点同一行的只有它的第一个普通追问；其余（划词、多问题、第二个追问）各开新行，
                # 方向按 下、上 交替并持久化，之后插入新分支不会让旧分支翻转。
                children = [t for t in tree["turns"].values() if t.get("parentId") == parent_id]
                first_follow = kind == "follow" and not any(t["kind"] == "follow" for t in children)
                if not first_follow:
                    branches = [t for t in children if (t.get("layout") or {}).get("dir")]
                    turn["layout"] = {"dir": data["dir"] if data.get("dir") in (1, -1) else (1 if len(branches) % 2 == 0 else -1)}
            tree["turns"][turn["id"]] = turn
            tree["order"].append(turn["id"])
            if kind == "root":
                tree["rootId"] = turn["id"]
            # 自动命名：提出第一个问题后命名，提出第 TITLE_RENAME_AT 个问题时按前几个问题更新一次
            asked = asked_before + len(questions)
            retitle = tree.get("titleSource") == "auto" and (asked_before == 0 or asked_before < TITLE_RENAME_AT <= asked)
            if retitle and asked_before == 0:
                tree["title"] = fallback_title(turn["question"])  # 模型标题回来之前先顶上
                self.hub.publish(tree_id, "tree", {"title": tree["title"], "settings": tree["settings"]})
            self.store.save(tree)
            self.hub.publish(tree_id, "turn.created", public_turn(turn))
            self._start(tree, turn)
            if retitle and self.titler:
                threading.Thread(target=self._retitle, args=(tree, question_texts(tree, TITLE_RENAME_AT)),
                                 name=f"branchboard-title-{tree_id[:8]}", daemon=True).start()
        return public_turn(turn)

    def _retitle(self, tree: dict[str, Any], questions: list[str]) -> None:
        try:
            title = clean_title(self.titler(questions))
        except Exception:  # noqa: BLE001 - 命名失败就保留现有标题
            return
        if not title:
            return
        with self._lock:
            # 期间被用户改了名、或这棵树已被删除：不再覆盖
            if tree.get("titleSource") != "auto" or self._trees.get(tree["id"]) is not tree:
                return
            tree["title"] = title
            self.store.save(tree)
            self.hub.publish(tree["id"], "tree", {"title": title, "settings": tree["settings"]})

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

    # ---- 完整版 / 子树合并：模型辅助（可选，只有用户点按钮才调用） ----
    def _fv_node(self, tree: dict[str, Any], turn_id: str, node_id: Any) -> str:
        """被处理的卡片必须是 turn_id 本身，或它的子树成员。"""
        node_id = str(node_id or turn_id)
        if node_id != turn_id and node_id not in subtree_members(tree, turn_id):
            raise ValueError("这张卡片不在要合并的子树里。")
        return node_id

    def _fv_branches(self, tree: dict[str, Any], turn_id: str, refs: list[Any]) -> list[dict[str, Any]]:
        """校验前端给的分支引用 {childId, qi[, childText]}：必须是划在 turn_id 上、且已完成的问题小框。
        childText 是前端算好的「这个分支合并了它自己的子分支之后」的内容；没有时用原始回答。"""
        turn = tree["turns"].get(turn_id)
        if not turn:
            raise KeyError("轮次不存在。")
        if not turn["answer"].get("markdown"):
            raise ValueError("这一轮还没有回答。")
        out = []
        for ref in refs:
            if not isinstance(ref, dict):
                raise ValueError("分支格式无效。")
            child = tree["turns"].get(str(ref.get("childId") or ""))
            qs = questions_of(child) if child else []
            qi = ref.get("qi")
            if not child or not isinstance(qi, int) or not 0 <= qi < len(qs):
                raise KeyError("分支不存在。")
            q = qs[qi]
            if q.get("sourceId") != turn_id or not (q.get("quote") or {}).get("text"):
                raise ValueError("这个分支不是划在这一轮上的。")
            if child["answer"].get("status") != "done":
                raise ValueError("分支的回答还没完成。")
            answer = str(ref.get("childText") or "") or child["answer"].get("markdown", "")
            out.append({"key": ref.get("key") or f"{child['id']}:{qi}", "n": tree["order"].index(child["id"]) + 1,
                        "question": q.get("text") or "", "quote": q["quote"]["text"],
                        "answer": answer if len(answer) <= 6000 else answer[:6000] + "\n…（后略）"})
        return out

    def full_version_classify(self, tree_id: str, turn_id: str, refs: list[Any]) -> dict[str, str]:
        """一次调用给子树里所有划词分支分类：correct / explain / extend。refs: [{key, nodeId?, childId, qi}]。"""
        if not self.complete:
            raise RuntimeError("没有可用的 Claude Code，无法用模型分类。")
        if not isinstance(refs, list) or not refs or len(refs) > 60:
            raise ValueError("分支数量无效。")
        with self._lock:
            tree = self.tree(tree_id)
            by_node: dict[str, list[Any]] = {}
            for ref in refs:
                node = self._fv_node(tree, turn_id, (ref or {}).get("nodeId") if isinstance(ref, dict) else None)
                by_node.setdefault(node, []).append(ref)
            branches = [b for node, group in by_node.items() for b in self._fv_branches(tree, node, group)]
        listing = "\n\n".join(
            f"### {b['key']}\n被划中的原文：{b['quote']}\n追问：{b['question'] or '（未写，只就这段展开）'}\n"
            f"追问的回答（节选）：{b['answer'][:600]}" for b in branches)
        text = self.complete([{"role": "system", "content": FV_CLASSIFY_SYSTEM},
                              {"role": "user", "content": listing}], "sonnet", "low")
        result = parse_json_object(text)
        allowed = {b["key"] for b in branches}
        return {k: v for k, v in result.items() if k in allowed and v in ("correct", "explain", "extend")}

    def full_version_rewrite(self, tree_id: str, turn_id: str, groups: list[Any]) -> dict[str, Any]:
        """逐段改写含「修正」分支的段落：每段一次调用，只输出改好的这一段。结果按内容缓存在树文件里。
        groups: [{nodeId?, block, paragraph, items: [{childId, qi, childText?}]}]；前端应当先发最深的一层。"""
        if not self.complete:
            raise RuntimeError("没有可用的 Claude Code，无法用模型改写。")
        if not isinstance(groups, list) or not groups or len(groups) > 12:
            raise ValueError("一次最多改写 12 段。")
        jobs = []
        with self._lock:
            tree = self.tree(tree_id)
            cache = tree.setdefault("fullVersionCache", {})
            for group in groups:
                group = group if isinstance(group, dict) else {}
                node = self._fv_node(tree, turn_id, group.get("nodeId"))
                source = (tree["turns"].get(node) or {}).get("answer", {}).get("markdown", "")
                paragraph = str(group.get("paragraph") or "")
                if not paragraph.strip() or paragraph not in source:
                    raise ValueError("段落与原回答不一致，请刷新后重试。")
                branches = self._fv_branches(tree, node, group.get("items") or [])
                if not branches:
                    raise ValueError("这一段没有需要修正的分支。")
                key = hashlib.sha256(json.dumps([paragraph, [[b["question"], b["quote"], b["answer"]] for b in branches]],
                                                ensure_ascii=False).encode("utf-8")).hexdigest()[:32]
                jobs.append({"nodeId": node, "block": group.get("block"), "paragraph": paragraph, "branches": branches,
                             "key": key, "cached": cache.get(key)})
        rewrites, called = [], 0
        for job in jobs:
            text = job["cached"]
            if not text:
                prompt = [f"## 原段落\n{job['paragraph']}"]
                for b in job["branches"]:
                    prompt.append(f"## 修正信息（第 {b['n']} 轮）\n被划中的原文：{b['quote']}\n追问：{b['question'] or '（未写）'}\n"
                                  f"追问得到的回答：\n{b['answer']}")
                text = self.complete([{"role": "system", "content": FV_REWRITE_SYSTEM},
                                      {"role": "user", "content": "\n\n".join(prompt)}], "sonnet", "medium")
                text = re.sub(r"^```(?:markdown|md)?\s*\n|\n?```\s*$", "", (text or "").strip()).strip()
                if not text:
                    raise ValueError("模型没有返回改写结果。")
                called += 1
                with self._lock:
                    cache[job["key"]] = text
                    while len(cache) > 200:
                        cache.pop(next(iter(cache)))
            rewrites.append({"nodeId": job["nodeId"], "block": job["block"], "text": text})
        if called:
            with self._lock:
                self.store.save(tree)
        return {"rewrites": rewrites, "called": called, "cached": len(jobs) - called}

    # ---- 子树合并：把合并版写回卡片，并把并进来的卡片收起 ----
    def merge_subtree(self, tree_id: str, turn_id: str, data: dict[str, Any]) -> dict[str, Any]:
        markdown = str(data.get("markdown") or "").strip()
        absorbed = data.get("absorbed") or []
        if not markdown:
            raise ValueError("合并版是空的。")
        if len(markdown) > 300_000:
            raise ValueError("合并版太长了。")
        if not isinstance(absorbed, list) or not all(isinstance(x, str) for x in absorbed):
            raise ValueError("被合并的卡片列表无效。")
        with self._lock:
            tree = self.tree(tree_id)
            turn = tree["turns"].get(turn_id)
            if not turn:
                raise KeyError("轮次不存在。")
            if turn["answer"].get("status") != "done":
                raise ValueError("这一轮的回答还没完成。")
            if turn_id in absorbed_by(tree):
                raise ValueError("这张卡片自己已经并入了别的卡片。")
            members = subtree_members(tree, turn_id)
            if not set(absorbed) <= set(members):
                raise ValueError("只能合并这张卡片下面的子树。")
            if any(tid in self._runs for tid in absorbed):
                raise ValueError("子树里还有正在回答的卡片，等它完成再合并。")
            stats = data.get("stats") if isinstance(data.get("stats"), dict) else {}
            turn["merged"] = {"markdown": markdown, "absorbed": [tid for tid in tree["order"] if tid in set(absorbed)],
                              "stats": {k: v for k, v in stats.items() if isinstance(v, int)}, "mergedAt": now_iso()}
            # 子树里原先的合并（嵌套）已经包含在这次的结果里，一并解除，避免一张卡片被两处收起
            for tid in absorbed:
                tree["turns"][tid].pop("merged", None)
            self.store.save(tree)
            changed = [turn, *[tree["turns"][tid] for tid in absorbed]]
        for t in changed:
            self.publish_turn(tree, t)
        return public_turn(turn)

    def unmerge_subtree(self, tree_id: str, turn_id: str) -> dict[str, Any]:
        with self._lock:
            tree = self.tree(tree_id)
            turn = tree["turns"].get(turn_id)
            if not turn:
                raise KeyError("轮次不存在。")
            if not turn.pop("merged", None):
                raise ValueError("这张卡片没有合并过。")
            self.store.save(tree)
        self.publish_turn(tree, turn)
        return public_turn(turn)


FV_CLASSIFY_SYSTEM = (
    "你在整理一段对话：用户在一条回答里划中某段原文继续追问，得到了若干分支。判断每个分支对原回答起什么作用：\n"
    "- correct：推翻、限定或修正了被划中的说法（原文说得不准、过强、过时或有误）；\n"
    "- explain：解释概念、给出处或数字、补充定义，不改变原说法；\n"
    "- extend：展开到原回答之外的新话题。\n"
    "只输出一个 JSON 对象，键是每个分支标题（### 后面的字符串），值是 correct / explain / extend 之一，不要输出其他内容。"
)
FV_REWRITE_SYSTEM = (
    "你在修订一篇回答。给你原回答中的一段，以及针对其中被划中原文的追问和回答（修正信息）。"
    "改写这一段，使其中与被划中原文相关的说法吸收修正信息；与之无关的句子保持原样。"
    "保持原文的语言、语气和 Markdown 格式，长度不超过原段落的 1.5 倍，不要提到「追问」「修正」「上一轮」。"
    "只输出改写后的这一段，不要解释。"
)


def parse_json_object(text: str) -> dict[str, Any]:
    """从模型输出里取出第一个 JSON 对象；容忍 ```json 包裹。"""
    match = re.search(r"\{[\s\S]*\}", text or "")
    if not match:
        return {}
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


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


def claude_titler(runner: Any) -> Callable[[list[str]], str]:
    """用一次轻量、无工具、不落盘的调用给对话起标题。"""
    def title(questions: list[str]) -> str:
        user = "\n\n".join(f"问题 {i + 1}：{q[:1500]}" for i, q in enumerate(questions))
        return runner.run([{"role": "system", "content": TITLE_SYSTEM}, {"role": "user", "content": user}], "sonnet", "low")
    return title


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
                        "efforts": list(EFFORTS), "defaultCwd": str(service.default_cwd),
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

    @bp.post("/trees/<tree_id>/turns/<turn_id>/full-version/classify")
    def full_version_classify(tree_id: str, turn_id: str):
        try:
            return jsonify({"categories": service.full_version_classify(tree_id, turn_id, body().get("items"))})
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 503
        except (KeyError, ValueError) as exc:
            return error(exc)

    @bp.post("/trees/<tree_id>/turns/<turn_id>/full-version/rewrite")
    def full_version_rewrite(tree_id: str, turn_id: str):
        try:
            return jsonify(service.full_version_rewrite(tree_id, turn_id, body().get("groups")))
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 503
        except (KeyError, ValueError) as exc:
            return error(exc)

    @bp.post("/trees/<tree_id>/turns/<turn_id>/merge")
    def merge_subtree(tree_id: str, turn_id: str):
        try:
            return jsonify({"turn": service.merge_subtree(tree_id, turn_id, body())})
        except (KeyError, ValueError) as exc:
            return error(exc)

    @bp.delete("/trees/<tree_id>/turns/<turn_id>/merge")
    def unmerge_subtree(tree_id: str, turn_id: str):
        try:
            return jsonify({"turn": service.unmerge_subtree(tree_id, turn_id)})
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
