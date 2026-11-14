"""Local prompt library and short-lived, capability-protected browser handoffs."""

from __future__ import annotations

import base64
import io
import json
import re
import secrets
import shutil
import threading
import time
import uuid
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

from flask import Blueprint, abort, jsonify, render_template, request, send_file


MAX_PROMPT = 100_000
MAX_FILES = 20
MAX_BYTES = 100 * 1024 * 1024
CHUNK_BYTES = 512 * 1024
TRANSFER_TTL = 60 * 60
DEFAULT_TEMPLATES = [
    {"id": "learning_mental_models", "title": "核心思维模型", "category": "三问学习法", "content": "这个领域的专家共同使用的五个核心思维模型是什么？"},
    {"id": "learning_expert_disagreements", "title": "专家根本分歧", "category": "三问学习法", "content": "这个领域的专家在哪三个问题上存在根本性分歧？\n请分别说明各方最有力的论据。"},
    {"id": "learning_understanding_questions", "title": "理解诊断问题", "category": "三问学习法", "content": "请生成十个问题，用于区分：\n一个人是真正深入理解了这个学科，\n还是只是记住了一些知识点。"},
    {"id": "summary", "title": "论文总结", "category": "科研阅读", "content": "请阅读所附材料，总结研究问题、核心方法、主要结果和局限。区分作者的主张与实验支持的结论，并标明对应章节、页码或图表。"},
    {"id": "mechanism", "title": "方法与机制", "category": "科研阅读", "content": "请逐步解释方法的输入、输出、关键模块及其关系。给出一个具体例子，说明方法为什么有效，并列出成立所需的假设。"},
    {"id": "math", "title": "公式推导", "category": "科研阅读", "content": "请解释材料中的关键公式，列出符号含义、维度和条件，并从已给前提逐行推导。对省略步骤或无法确认的假设明确说明。"},
    {"id": "review", "title": "批判性审查", "category": "科研阅读", "content": "请审查证据是否支持结论，分析实验对照、潜在混杂、适用边界和失败情形，并提出可验证的改进实验。"},
    {"id": "translate", "title": "学术翻译", "category": "写作表达", "content": "请将材料忠实翻译为简体中文，保留公式、代码、引用编号与专有名词，统一术语，不增加原文没有的信息。"},
    {"id": "polish", "title": "润色表达", "category": "写作表达", "content": "请改进文字的清晰度、逻辑和准确性，保留原意与事实。先给修改后的版本，再简要说明关键修改。"},
    {"id": "implement", "title": "实现方案", "category": "编程开发", "content": "请将需求拆解为可执行的实现步骤，说明接口、数据结构、关键边界条件和验证方法；需要代码时提供可运行的最小实现。"},
    {"id": "format", "title": "结构化回答", "category": "输出要求", "content": "请使用中文回答，先给结论，再解释依据。必要时使用表格、示例和 LaTeX 公式；明确区分事实、推断与尚不确定的内容。"},
]

# 文献精读的划选动作。它们是 Prompt 库里的内置模板（id 前缀 "reader:"）：可以在 Prompt 工作台里
# 改名称和内容（保存为本机覆盖），删除即恢复默认；精读页的划选按钮和 AI 请求都读这里。
READER_ACTION_CATEGORY = "精读划选动作"
READER_ACTION_PREFIX = "reader:"
READER_ACTION_DEFAULTS = {
    "translate": ("翻译选区", "忠实翻译为简体中文，保留 LaTeX、变量名、引用编号、代码和专有名词；必要时在译文后用一句话说明关键术语。"),
    "explain": ("解释选区", "先给一句结论，再逐句解释术语、变量和逻辑；优先给机器学习论文中的具体含义。"),
    "three_pass": ("Three-Pass 解读", (
        "在一次回答中完成三个层次的精简阅读，不要拆成多次请求。"
        "严格使用 `## Pass 1 · 快速定位`、`## Pass 2 · 结构与证据`、`## Pass 3 · 深读与批判` 三个小标题。"
        "Pass 1 只说明选区主题、目的及其在章节中的位置，不超过 120 个汉字；"
        "Pass 2 连接核心主张、方法步骤、公式/证据与必要假设，不超过 220 个汉字；"
        "Pass 3 指出深层含义、潜在缺口、失效条件或复现问题，不超过 260 个汉字。"
        "总回答不超过 700 个汉字；每层最多 3 个要点，不复述同一内容。"
    )),
    "formula": ("解释公式", "列出所有符号及张量形状/取值域，说明每一项的作用、输入输出、假设和训练/推理时的意义。"),
    "geometry": ("解释公式的几何意义", "明确向量/参数/概率分布所处的空间，说明方向、距离、角度、投影、流形或优化几何；没有严格几何解释时要直说并给直觉。"),
    "derivation": ("推导这一步", "从可见的前提开始，逐行给出代数、概率或微积分变形；指出使用的恒等式、近似或条件，不能凭空补全未给出的前提。"),
    "summary": ("总结选区和相邻上下文", "说明问题、方法、关键机制、结论以及它在整篇论文中的作用。"),
    "intuition": ("给出直觉解释", "使用一个小型数值、二维几何或训练过程例子，但不要改变原公式或暗示未证实结论。"),
    "assumptions": ("批判性阅读", "识别显式/隐式假设、潜在失效情形、计算代价、数据分布要求与需要实验验证的主张。"),
    "implementation": ("转换为实现视角", "说明张量形状、伪代码步骤、数值稳定性、常用默认值和在 PyTorch/JAX 中容易出错的位置。"),
}


class PromptWorkbench:
    def __init__(self, root: Path, extension_dir: Path):
        self.root = Path(root)
        self.extension_dir = Path(extension_dir)
        self.lock = threading.RLock()
        self.transfers: dict[str, dict] = {}

    def templates(self):
        path = self.root / "templates.json"
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        return [dict(item) for item in DEFAULT_TEMPLATES]

    def save_templates(self, items):
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / "templates.json"
        staging = path.with_suffix(".tmp")
        staging.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
        staging.replace(path)

    # ---- built-in reader actions (with local overrides) ----
    def _action_overrides(self) -> dict:
        path = self.root / "reader_actions.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except (OSError, ValueError):
            data = {}
        return data if isinstance(data, dict) else {}

    def _save_action_overrides(self, overrides: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / "reader_actions.json"
        staging = path.with_suffix(".tmp")
        staging.write_text(json.dumps(overrides, ensure_ascii=False, indent=2), encoding="utf-8")
        staging.replace(path)

    def reader_actions(self) -> list[dict]:
        """Every reader action as a library template: builtin, editable, resettable."""
        overrides = self._action_overrides()
        items = []
        for action, (title, content) in READER_ACTION_DEFAULTS.items():
            custom = overrides.get(action) if isinstance(overrides.get(action), dict) else {}
            items.append({
                "id": READER_ACTION_PREFIX + action, "action": action,
                "title": custom.get("title") or title, "category": READER_ACTION_CATEGORY,
                "content": custom.get("content") or content,
                "builtin": True, "scope": "reader", "customized": bool(custom),
            })
        return items

    def reader_action(self, action: str) -> dict | None:
        return next((item for item in self.reader_actions() if item["action"] == action), None)

    def set_reader_action(self, action: str, title: str, content: str) -> dict:
        default_title, default_content = READER_ACTION_DEFAULTS[action]
        overrides = self._action_overrides()
        custom = {key: value for key, value, default in (("title", title, default_title), ("content", content, default_content)) if value != default}
        if custom:
            overrides[action] = custom
        else:
            overrides.pop(action, None)
        self._save_action_overrides(overrides)
        return self.reader_action(action)

    def reset_reader_action(self, action: str) -> dict:
        overrides = self._action_overrides()
        overrides.pop(action, None)
        self._save_action_overrides(overrides)
        return self.reader_action(action)

    def library(self) -> list[dict]:
        return [*self.templates(), *self.reader_actions()]

    def cleanup(self):
        """Called on workbench access; abandoned uploads also expire after restart."""
        cutoff = time.time() - TRANSFER_TTL
        with self.lock:
            for key, item in list(self.transfers.items()):
                if item["created"] < cutoff:
                    self.remove_files(key)
                    del self.transfers[key]
            folder = self.root / "transfers"
            if folder.exists():
                for path in folder.iterdir():
                    if re.fullmatch(r"[0-9a-f]{32}", path.name) and path.is_dir() and path.stat().st_mtime < cutoff:
                        shutil.rmtree(path)

    def remove_files(self, transfer_id):
        # Only server-generated IDs can identify a directory beneath this root.
        if re.fullmatch(r"[0-9a-f]{32}", transfer_id):
            shutil.rmtree(self.root / "transfers" / transfer_id, ignore_errors=True)


def local_request_error(*, allow_extension_transfers: bool = False) -> str | None:
    """Check the local Host and browser request origin before any route runs."""
    if urlsplit(request.host_url).hostname not in {"localhost", "127.0.0.1", "::1"}:
        return "仅接受本机访问。"
    origin = request.headers.get("Origin")
    is_extension_transfer = bool(
        allow_extension_transfers
        and request.path.startswith("/api/prompts/transfers/")
        and origin
        and re.fullmatch(r"chrome-extension://[a-p]{32}", origin)
    )
    if origin is not None and origin != request.host_url.rstrip("/") and not is_extension_transfer:
        return "不允许跨站访问。"
    if request.headers.get("Sec-Fetch-Site") == "cross-site" and not is_extension_transfer:
        return "不允许跨站访问。"
    return None


def create_prompt_blueprint(service: PromptWorkbench):
    bp = Blueprint("prompt_workbench", __name__)

    @bp.before_request
    def local_only():
        error = local_request_error(allow_extension_transfers=True)
        if error:
            abort(403, description=error)
        if request.method in {"POST", "PUT", "DELETE"} and request.headers.get("X-Prompt-Workbench") != "1":
            abort(403, description="缺少工作台请求标识。")
        limit = MAX_BYTES + 2 * 1024 * 1024 if request.path == "/api/prompts/transfers" else 512 * 1024
        if request.content_length and request.content_length > limit:
            abort(413, description="请求过大，附件总大小最多为 100 MB。")
        service.cleanup()

    @bp.after_request
    def private_response(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @bp.errorhandler(400)
    @bp.errorhandler(403)
    @bp.errorhandler(404)
    @bp.errorhandler(409)
    @bp.errorhandler(413)
    def api_error(error):
        return jsonify(error=error.description), error.code

    def json_object():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            abort(400, description="请提供 JSON 对象。")
        return data

    def template_fields(data):
        item = {}
        for field, limit in (("title", 80), ("category", 40), ("content", MAX_PROMPT)):
            value = data.get(field, "通用" if field == "category" else "")
            if not isinstance(value, str) or not value.strip() or len(value) > limit:
                abort(400, description=f"{field} 不能为空且不能超过 {limit} 字符。")
            item[field] = value.strip() if field != "content" else value
        return item

    def authorized_transfer(transfer_id):
        item = service.transfers.get(transfer_id)
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        if not item or not secrets.compare_digest(item["token"], token):
            abort(404, description="传输不存在或已过期，请重新提交。")
        return item

    def public_transfer(item):
        return {key: item[key] for key in ("id", "prompt", "files", "status", "message", "created")}

    @bp.get("/prompts")
    def page():
        return render_template("prompts.html")

    @bp.get("/api/prompts/templates")
    def list_templates():
        with service.lock:
            return jsonify(templates=service.library(), maxFiles=MAX_FILES, maxBytes=MAX_BYTES, maxPrompt=MAX_PROMPT)

    def builtin_action(template_id):
        if not template_id.startswith(READER_ACTION_PREFIX):
            return None
        action = template_id[len(READER_ACTION_PREFIX):]
        if action not in READER_ACTION_DEFAULTS:
            abort(404, description="模板不存在。")
        return action

    @bp.post("/api/prompts/templates")
    def add_template():
        item = {"id": uuid.uuid4().hex, **template_fields(json_object())}
        with service.lock:
            items = service.templates()
            if len(items) >= 200:
                abort(400, description="最多保存 200 个模板。")
            service.save_templates([*items, item])
        return jsonify(template=item), 201

    @bp.put("/api/prompts/templates/<template_id>")
    def edit_template(template_id):
        data = json_object()
        action = builtin_action(template_id)
        if action:  # builtin: name and content are editable, category is fixed
            fields = template_fields({**data, "category": READER_ACTION_CATEGORY})
            if len(fields["content"]) > 4000:
                abort(400, description="精读动作的内容不能超过 4000 字符。")
            with service.lock:
                return jsonify(template=service.set_reader_action(action, fields["title"], fields["content"]))
        fields = template_fields(data)
        with service.lock:
            items = service.templates()
            item = next((row for row in items if row["id"] == template_id), None)
            if not item:
                abort(404, description="模板不存在。")
            item.update(fields)
            service.save_templates(items)
        return jsonify(template=item)

    @bp.delete("/api/prompts/templates/<template_id>")
    def delete_template(template_id):
        action = builtin_action(template_id)
        if action:  # builtins are never removed; deleting resets them to the default text
            with service.lock:
                return jsonify(ok=True, template=service.reset_reader_action(action))
        with service.lock:
            items = service.templates()
            if not any(row["id"] == template_id for row in items):
                abort(404, description="模板不存在。")
            service.save_templates([row for row in items if row["id"] != template_id])
        return jsonify(ok=True)

    @bp.get("/api/prompts/extension.zip")
    def extension_zip():
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            for filename in ("manifest.json", "background.js", "workbench.js", "chatgpt.js", "README.md"):
                archive.write(service.extension_dir / filename, f"research-prompt-bridge/{filename}")
        output.seek(0)
        return send_file(output, mimetype="application/zip", as_attachment=True, download_name="research-prompt-bridge.zip")

    @bp.post("/api/prompts/transfers")
    def create_transfer():
        prompt = request.form.get("prompt", "")
        if not prompt.strip() or len(prompt) > MAX_PROMPT:
            abort(400, description=f"请输入 Prompt（最多 {MAX_PROMPT:,} 字符）。")
        uploads = request.files.getlist("files")
        if len(uploads) > MAX_FILES:
            abort(400, description=f"最多添加 {MAX_FILES} 个附件。")
        with service.lock:
            if sum(row["status"] not in {"completed", "cancelled", "error"} for row in service.transfers.values()) >= 5:
                abort(409, description="待处理提交过多，请先完成或取消之前的提交。")
            transfer_id = uuid.uuid4().hex
            folder = service.root / "transfers" / transfer_id
            folder.mkdir(parents=True)
            files = []
            total = 0
            try:
                for index, upload in enumerate(uploads):
                    name = (upload.filename or "attachment").replace("\\", "/").rsplit("/", 1)[-1]
                    name = "".join(c for c in name if ord(c) >= 32)[:240] or "attachment"
                    size = 0
                    with (folder / str(index)).open("wb") as handle:
                        while chunk := upload.stream.read(CHUNK_BYTES):
                            size += len(chunk)
                            total += len(chunk)
                            if total > MAX_BYTES:
                                abort(413, description="附件总大小最多为 100 MB。")
                            handle.write(chunk)
                    files.append({"index": index, "name": name, "size": size, "type": upload.mimetype or "application/octet-stream"})
                item = {"id": transfer_id, "token": secrets.token_urlsafe(32), "prompt": prompt, "files": files,
                        "status": "pending", "message": "等待浏览器扩展打开 ChatGPT。", "created": time.time()}
                service.transfers[transfer_id] = item
            except BaseException:
                service.remove_files(transfer_id)
                raise
        return jsonify(id=transfer_id, token=item["token"]), 201

    @bp.get("/api/prompts/transfers/<transfer_id>")
    def get_transfer(transfer_id):
        with service.lock:
            return jsonify(public_transfer(authorized_transfer(transfer_id)))

    @bp.get("/api/prompts/transfers/<transfer_id>/files/<int:index>")
    def file_chunk(transfer_id, index):
        with service.lock:
            item = authorized_transfer(transfer_id)
            if item["status"] in {"completed", "cancelled", "error"} or index >= len(item["files"]):
                abort(404, description="附件不存在或已清理。")
            offset = request.args.get("offset", "0")
            if not re.fullmatch(r"[0-9]{1,12}", offset) or int(offset) > item["files"][index]["size"]:
                abort(400, description="附件读取位置无效。")
            with (service.root / "transfers" / transfer_id / str(index)).open("rb") as handle:
                handle.seek(int(offset))
                chunk = handle.read(CHUNK_BYTES)
            return jsonify(data=base64.b64encode(chunk).decode("ascii"), nextOffset=int(offset) + len(chunk),
                           done=int(offset) + len(chunk) >= item["files"][index]["size"])

    @bp.post("/api/prompts/transfers/<transfer_id>/status")
    def update_transfer(transfer_id):
        data = json_object()
        status, message = data.get("status"), data.get("message", "")
        if status not in {"opening", "waiting", "filling", "uploading", "completed", "error", "cancelled"} or not isinstance(message, str) or len(message) > 500:
            abort(400, description="传输状态无效。")
        with service.lock:
            item = authorized_transfer(transfer_id)
            if item["status"] not in {"completed", "cancelled", "error"}:
                item.update(status=status, message=message)
                if status in {"completed", "cancelled", "error"}:
                    service.remove_files(transfer_id)
            return jsonify(public_transfer(item))

    return bp
