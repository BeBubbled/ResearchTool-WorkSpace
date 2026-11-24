"""演化树调研：把 researchtool/gapr（问题驱动的方法演化树）接进面板。

面板只做三件事：
- 管理调研项目（新建、查看 project.toml 与各个 CSV）；
- 显示 gapr 的逐步检查结果和确认点状态；
- 在子进程里运行 gapr 的检索 / 全文 / 原句核对等命令，输出实时回显。

确认点（gapr confirm）故意不在面板里提供：它只能由用户本人在自己的终端里运行，
面板只显示要运行的命令。这样模型无论直接调命令还是调面板接口，都没法代替用户确认。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
from typing import Any, Callable
import uuid

from flask import Blueprint, jsonify, request

PROJECT_ID_RE = re.compile(r"^[\w一-鿿][\w一-鿿-]{0,59}$")
MAX_OUTPUT_CHARS = 400_000
MAX_TABLE_ROWS = 2000
MAX_JOBS_KEPT = 40

# 面板里可以看的文件：CSV 按表格显示，其余按文本显示。
VIEWABLE_FILES = [
    ("project.toml", "第 0 步 · 输入"),
    ("families.csv", "第 1 步 · 方法族"),
    ("nodes.csv", "第 1 步 · 节点"),
    ("neighbor_terms.csv", "第 1 步 · 术语对照"),
    ("defects.csv", "第 2 步 · 缺陷"),
    ("edges.csv", "第 3 步 · 演化边"),
    ("forks.csv", "第 3 步 · 分歧点"),
    ("open_problems.csv", "第 3 步 · 开放问题"),
    ("rubric/attributes.csv", "第 4 步 · 属性"),
    ("rubric/incompatible.csv", "第 4 步 · 不相容"),
    ("rubric/scoring.md", "第 4 步 · 打分标准"),
    ("rubric/backtest_rules.md", "第 4 步 · 回测判定"),
    ("matrix.csv", "第 4 步 · 属性矩阵"),
    ("matrix_evidence.csv", "第 4 步 · 矩阵依据"),
    ("gaps_mechanical.csv", "第 5 步 · 机械候选"),
    ("gaps.csv", "第 5–7 步 · 候选空白"),
    ("filtered.csv", "第 5 步 · 过滤掉的"),
    ("search_log.csv", "全程 · 检索记录"),
    ("decisions.md", "自动模式 · Claude 的决定与理由"),
    ("report.md", "报告"),
]
VIEWABLE = {name for name, _ in VIEWABLE_FILES}

SEARCH_TOOLS = ("s2", "openalex", "arxiv", "openreview")
LINK_SOURCES = ("auto", "s2", "openalex")
SEARCH_KINDS = ("phrasing", "forward", "venue", "cross")

# 面板能运行的命令。positional 依次放在项目目录之后；options 为 字段 → (命令行参数, 类型)。
# 类型：str / int / date / bool / list（逗号分隔，展开成多次同名参数）/ ("choice", 可选值)
COMMANDS: dict[str, dict[str, Any]] = {
    "search": {
        "label": "检索论文",
        "positional": ["query"],
        "options": {
            "tool": ("--tool", ("choice", SEARCH_TOOLS)),
            "limit": ("--limit", "int"),
            "from": ("--from", "date"),
            "to": ("--to", "date"),
            "sort": ("--sort", ("choice", ("relevance", "date"))),
            "for": ("--for", "str"),
            "kind": ("--kind", ("choice", SEARCH_KINDS)),
            "postCutoff": ("--post-cutoff", "bool"),
            "refresh": ("--refresh", "bool"),
        },
    },
    "paper": {"label": "单篇信息", "positional": ["id"], "options": {"refresh": ("--refresh", "bool")}},
    "fulltext": {"label": "下载全文", "positional": ["key"], "options": {"refresh": ("--refresh", "bool")}},
    "quote": {"label": "核对原句", "positional": ["key", "text"], "options": {}},
    "citations": {
        "label": "正向追踪（被引）",
        "positional": ["id"],
        "options": {
            "source": ("--source", ("choice", LINK_SOURCES)),
            "max": ("--max", "int"),
            "show": ("--show", "int"),
            "for": ("--for", "str"),
            "critique": ("--critique", "bool"),
            "postCutoff": ("--post-cutoff", "bool"),
            "refresh": ("--refresh", "bool"),
        },
    },
    "references": {
        "label": "向前补漏（参考文献）",
        "positional": ["id"],
        "options": {
            "source": ("--source", ("choice", LINK_SOURCES)),
            "max": ("--max", "int"),
            "show": ("--show", "int"),
            "refresh": ("--refresh", "bool"),
        },
    },
    "contexts": {
        "label": "他人指出（引用句）",
        "positional": ["id"],
        "options": {
            "top": ("--top", "int"),
            "pool": ("--pool", "int"),
            "alias": ("--alias", "list"),
            "show": ("--show", "int"),
            "for": ("--for", "str"),
            "source": ("--source", ("choice", LINK_SOURCES)),
            "postCutoff": ("--post-cutoff", "bool"),
            "refresh": ("--refresh", "bool"),
        },
    },
    "reviews": {"label": "审稿意见", "positional": ["forum"], "options": {"refresh": ("--refresh", "bool")}},
    "matrix-template": {"label": "生成空矩阵", "positional": [], "options": {}},
    "gaps": {"label": "列出候选空白", "positional": [], "options": {}},
}


POSITIONAL_LABELS = {"query": "检索词", "id": "论文 ID", "key": "论文 ID", "text": "原句", "forum": "OpenReview 论坛 ID"}


def gapr_available() -> tuple[bool, str]:
    """gapr 用到 Python 3.11 的 tomllib；面板本身支持 3.10，所以这里单独检查。"""
    if sys.version_info < (3, 11):
        return False, f"演化树调研需要 Python 3.11 或更高版本，当前是 {sys.version.split()[0]}。"
    return True, ""


@dataclass
class Job:
    id: str
    project: str
    command: str
    argv: list[str]
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    returncode: int | None = None
    output: str = ""
    cancelled: bool = False
    process: subprocess.Popen | None = None

    @property
    def running(self) -> bool:
        return self.finished_at is None

    def public(self, *, with_output: bool = True) -> dict[str, Any]:
        data = {
            "id": self.id, "project": self.project, "command": self.command,
            "label": COMMANDS[self.command]["label"], "display": "gapr " + " ".join(_display_arg(a) for a in self.argv),
            "startedAt": self.started_at, "finishedAt": self.finished_at,
            "running": self.running, "returncode": self.returncode, "cancelled": self.cancelled,
        }
        if with_output:
            data["output"] = self.output
        return data


def _display_arg(value: str) -> str:
    return f'"{value}"' if (not value or any(c.isspace() for c in value)) else value


class GapSurveyService:
    def __init__(self, root: Path, *, workdir: Path, python: str | None = None,
                 env: Callable[[], dict[str, str]] | None = None, claude: Any = None):
        self.root = Path(root)
        self.workdir = Path(workdir)
        self.python = python or sys.executable
        self.env = env or (lambda: dict(os.environ))
        self.jobs: dict[str, Job] = {}
        self.lock = threading.Lock()
        # 自动模式：状态存在项目目录之外（root 的上一级 auto/），Claude 会话改不到
        from researchtool.gap_survey_auto import AutoRunner
        self.auto = AutoRunner(self.root.parent / "auto", workdir=self.workdir, runner=claude,
                               project_dir=self.project_dir, python=self.python) if claude else None

    # ------------------------------------------------------------ 项目

    def project_dir(self, project_id: str) -> Path:
        if not PROJECT_ID_RE.fullmatch(project_id or ""):
            raise ValueError("项目名只能用中文、字母、数字、下划线和连字符，最多 60 个字符。")
        path = self.root / project_id
        if not (path / "project.toml").is_file():
            raise KeyError(f"没有名为 {project_id} 的调研项目。")
        return path

    def _gapr(self):
        ok, why = gapr_available()
        if not ok:
            raise RuntimeError(why)
        from researchtool.gapr import checks, confirm, project  # 延迟导入：Python 3.10 下面板其余功能照常可用
        return checks, confirm, project

    def list_projects(self) -> list[dict[str, Any]]:
        if not self.root.is_dir():
            return []
        _, _, prj = self._gapr()
        items = []
        for path in sorted(self.root.iterdir()):
            if not (path / "project.toml").is_file() or not PROJECT_ID_RE.fullmatch(path.name):
                continue
            try:
                p = prj.load(path)
                info = {"domain": p.domain, "mode": p.mode, "cutoff": str(p.cutoff or ""), "scale": p.scale}
            except (ValueError, OSError) as exc:
                info = {"error": str(exc)}
            items.append({"id": path.name, "updatedAt": _latest_mtime(path), **info})
        items.sort(key=lambda item: -item["updatedAt"])
        return items

    def create(self, data: dict[str, Any]) -> dict[str, Any]:
        _, _, prj = self._gapr()
        project_id = str(data.get("id") or "").strip()
        if not PROJECT_ID_RE.fullmatch(project_id):
            raise ValueError("项目名只能用中文、字母、数字、下划线和连字符，最多 60 个字符。")
        mode = str(data.get("mode") or "survey")
        if mode not in ("survey", "backtest"):
            raise ValueError("模式只能是正式调研或回测。")
        scale = str(data.get("scale") or "quick")
        if scale not in ("mini", "quick", "full"):
            raise ValueError("规模只能是 mini、quick 或 full。")
        start = _optional_date(data.get("start"), "起始时间")
        cutoff = _optional_date(data.get("cutoff"), "截止日")
        if mode == "backtest" and not cutoff:
            raise ValueError("回测项目必须填写截止日。")
        domain = _plain_text(data.get("domain"), "领域", 200)
        audience = _plain_text(data.get("audience"), "读者", 60)
        path = self.root / project_id
        if path.exists():
            raise ValueError(f"已经有名为 {project_id} 的项目。")
        self.root.mkdir(parents=True, exist_ok=True)
        prj.init(path, domain=domain, mode=mode, start=start, cutoff=cutoff, scale=scale, audience=audience)
        if data.get("auto") and self.auto:
            self.auto.enable(project_id, True)
        extra = {key: data.get(key) for key in ("include", "exclude") if str(data.get(key) or "").strip()}
        if extra:
            return self.update(project_id, extra)
        return self.overview(project_id)

    def cutoff_lock(self, path: Path, mode: str) -> str:
        """回测的截止日决定日期防火墙：检索过或确认过节点之后就不能再改，否则之前的检索结果会失真。"""
        if mode != "backtest":
            return ""
        _, confirm, _ = self._gapr()
        if "1" in confirm.load(path):
            return "确认点 1 已确认，回测截止日不能再改。"
        log = path / "search_log.csv"
        if log.is_file() and _count_rows(log):
            return "已经按这个截止日检索过（search_log.csv 有记录），回测截止日不能再改。"
        return ""

    def update(self, project_id: str, data: dict[str, Any]) -> dict[str, Any]:
        """改 project.toml 里第 0 步的输入，保留文件里的注释和其他设置。模式（正式调研 / 回测）不能改。"""
        _, _, prj = self._gapr()
        path = self.project_dir(project_id)
        target = path / "project.toml"
        text = target.read_text(encoding="utf-8")
        current = tomllib_loads(text).get("project", {})
        mode = current.get("mode", "survey")
        values: dict[str, str] = {}
        if "domain" in data:
            values["domain"] = _plain_text(data.get("domain"), "领域", 200)
        if "audience" in data:
            values["audience"] = _plain_text(data.get("audience"), "读者", 60)
        for key, label in (("include", "包含"), ("exclude", "排除")):
            if key in data:
                values[key] = _plain_text(data.get(key), label, 500)
        if "start" in data:
            values["start"] = _optional_date(data.get("start"), "起始时间")
        if "scale" in data:
            scale = str(data.get("scale") or "")
            if scale not in ("mini", "quick", "full"):
                raise ValueError("规模只能是 mini、quick 或 full。")
            values["scale"] = scale
        if "mode" in data and str(data.get("mode")) != mode:
            raise ValueError("项目建好后不能切换正式调研 / 回测，请新建一个项目。")
        if "cutoff" in data:
            cutoff = _optional_date(data.get("cutoff"), "截止日")
            if cutoff != str(current.get("cutoff") or ""):
                if mode == "backtest" and not cutoff:
                    raise ValueError("回测项目必须填写截止日。")
                locked = self.cutoff_lock(path, mode)
                if locked:
                    raise ValueError(locked)
                values["cutoff"] = cutoff or date.today().isoformat()
        if values:
            updated = _set_toml_values(text, "project", values)
            tomllib_loads(updated)  # 写坏了就不落盘
            temp = target.with_suffix(".toml.tmp")
            temp.write_text(updated, encoding="utf-8")
            os.replace(temp, target)
            try:
                prj.load(path)
            except ValueError:
                target.write_text(text, encoding="utf-8")
                raise
        return self.overview(project_id)

    def overview(self, project_id: str) -> dict[str, Any]:
        checks, confirm, prj = self._gapr()
        path = self.project_dir(project_id)
        info: dict[str, Any] = {"id": project_id, "path": str(path.resolve())}
        local_root = os.environ.get("GAPR_LOCAL_ROOT", "").strip()
        if local_root:  # 数据目录在云盘里时，HTTP 缓存和批量原始文件放本机，这里告诉用户放哪
            info["localDir"] = str(Path(local_root) / path.resolve().name)
        try:
            p = prj.load(path)
        except (ValueError, OSError) as exc:
            return {**info, "error": f"project.toml 读取失败：{exc}", "steps": [], "files": self._files(path)}
        raw = tomllib_loads((path / "project.toml").read_text(encoding="utf-8")).get("project", {})
        info.update({
            "domain": p.domain, "mode": p.mode, "start": p.start, "cutoff": str(p.cutoff or ""),
            "scale": p.scale, "audience": p.audience,
            "include": str(raw.get("include") or ""), "exclude": str(raw.get("exclude") or ""),
            "cutoffLocked": self.cutoff_lock(path, p.mode),
        })
        steps = []
        for res in checks.run(p):
            steps.append({
                "step": res.step, "title": checks.STEP_TITLES[res.step], "started": res.started,
                "passed": res.passed,
                "issues": [{"level": "error" if i.level == checks.ERROR else "warn", "where": i.where, "message": i.msg}
                           for i in res.issues],
            })
        confirmed = confirm.load(path)
        points = []
        for point, title in (("1", "第 1 步之后：节点清单与术语对照表"), ("2", "第 4 步归纳属性之后：属性与相容性表（确认后冻结）")):
            record = confirmed.get(point) or {}
            points.append({"point": point, "title": title, "confirmedAt": record.get("at", ""),
                           "note": record.get("note", ""), "command": self.confirm_command(path, point),
                           "byClaude": record.get("by") == "claude-auto", "review": record.get("review", "")})
        candidates = confirmed.get("candidates") or {}
        info.update({
            "steps": steps,
            "confirmations": points,
            "candidatesLockedAt": candidates.get("at", ""),
            "files": self._files(path),
            "running": self.running_job(project_id),
            "auto": self.auto_summary(project_id),
        })
        return info

    def auto_summary(self, project_id: str) -> dict[str, Any]:
        if not self.auto:
            return {"available": False, "enabled": False, "running": False}
        state = self.auto.load(project_id)
        return {"available": True, "enabled": state["enabled"], "running": self.auto.is_running(project_id),
                "status": state["status"]}

    def confirm_command(self, path: Path, point: str) -> str:
        """给用户在自己的 PowerShell 里运行的确认命令（面板不代跑）。"""
        python = Path(self.python)
        return (f'cd "{self.workdir}"; & "{python}" -m researchtool.gapr confirm "{path.resolve()}" {point}')

    def _files(self, path: Path) -> list[dict[str, Any]]:
        out = []
        for name, label in VIEWABLE_FILES:
            target = path / name
            if not target.is_file():
                continue
            rows = None
            if name.endswith(".csv"):
                rows = _count_rows(target)
            out.append({"name": name, "label": label, "rows": rows, "updatedAt": target.stat().st_mtime})
        return out

    def read_file(self, project_id: str, name: str) -> dict[str, Any]:
        path = self.project_dir(project_id)
        if name not in VIEWABLE:
            raise ValueError("这个文件不能在面板里查看。")
        target = path / name
        if not target.is_file():
            raise KeyError(f"{name} 不存在。")
        if not name.endswith(".csv"):
            return {"name": name, "kind": "text", "text": target.read_text(encoding="utf-8", errors="replace")}
        from researchtool.gapr import schema
        import csv
        with target.open(encoding=schema.ENCODING_READ, newline="") as handle:
            reader = csv.reader(handle)
            columns = next(reader, [])
            rows, total = [], 0
            for row in reader:
                total += 1
                if len(rows) < MAX_TABLE_ROWS:
                    rows.append(row)
        return {"name": name, "kind": "table", "columns": columns, "rows": rows, "total": total,
                "truncated": total > len(rows)}

    # ------------------------------------------------------------ 命令

    def build_argv(self, project_dir: Path, command: str, args: dict[str, Any]) -> list[str]:
        spec = COMMANDS.get(command)
        if not spec:
            raise ValueError("面板不支持这个命令。确认点请在你自己的终端里运行。")
        argv = [command, str(project_dir.resolve())]
        options: list[str] = []
        for key, (flag, kind) in spec["options"].items():
            value = args.get(key)
            if value in (None, "", False, []):
                continue
            if kind == "bool":
                if value is True:
                    options.append(flag)
                continue
            if kind == "int":
                try:
                    number = int(value)
                except (TypeError, ValueError):
                    raise ValueError(f"{flag} 需要整数。") from None
                if not 0 < number <= 100_000:
                    raise ValueError(f"{flag} 超出范围。")
                options.append(f"{flag}={number}")
            elif kind == "date":
                options.append(f"{flag}={_optional_date(value, flag)}")
            elif kind == "list":
                items = value if isinstance(value, list) else re.split(r"[,，;；]", str(value))
                for item in items:
                    item = _plain_text(item, flag, 200)
                    if item:
                        options.append(f"{flag}={item}")
            elif isinstance(kind, tuple) and kind[0] == "choice":
                if value not in kind[1]:
                    raise ValueError(f"{flag} 只能是 {' / '.join(kind[1])}。")
                options.append(f"{flag}={value}")
            else:
                options.append(f"{flag}={_plain_text(value, flag, 500)}")
        positionals = []
        for key in spec["positional"]:
            label = POSITIONAL_LABELS.get(key, key)
            value = _plain_text(args.get(key), label, 4000)
            if not value:
                raise ValueError(f"请填写{label}。")
            if value.startswith("-"):
                raise ValueError(f"{label}不能以 - 开头。")
            positionals.append(value)
        return [*argv, *positionals, *options]

    def running_job(self, project_id: str) -> dict[str, Any] | None:
        with self.lock:
            job = next((j for j in self.jobs.values() if j.project == project_id and j.running), None)
            return job.public(with_output=False) if job else None

    def start(self, project_id: str, command: str, args: dict[str, Any]) -> dict[str, Any]:
        self._gapr()
        path = self.project_dir(project_id)
        argv = self.build_argv(path, command, args if isinstance(args, dict) else {})
        if self.auto and self.auto.is_running(project_id):
            raise ValueError("自动运行进行中，先停止自动运行再手动运行命令。")
        with self.lock:
            if any(j.project == project_id and j.running for j in self.jobs.values()):
                raise ValueError("这个项目已有命令在运行，等它结束或先停止它。")
            job = Job(id=uuid.uuid4().hex[:12], project=project_id, command=command, argv=argv)
            self.jobs[job.id] = job
            self._trim()
        env = self.env()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        try:
            job.process = subprocess.Popen(
                [self.python, "-m", "researchtool.gapr", *argv], cwd=str(self.workdir), env=env,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            job.output = f"无法启动：{exc}"
            job.returncode, job.finished_at = -1, time.time()
            return job.public()
        threading.Thread(target=self._pump, args=(job,), daemon=True).start()
        return job.public()

    def _pump(self, job: Job) -> None:
        proc = job.process
        assert proc is not None and proc.stdout is not None
        for raw in iter(proc.stdout.readline, b""):
            text = raw.decode("utf-8", errors="replace").replace("\r\n", "\n")
            with self.lock:
                job.output = (job.output + text)[-MAX_OUTPUT_CHARS:]
        proc.stdout.close()
        proc.wait()
        with self.lock:
            job.returncode = proc.returncode
            job.finished_at = time.time()

    def _trim(self) -> None:
        finished = sorted((j for j in self.jobs.values() if not j.running), key=lambda j: j.started_at)
        for job in finished[: max(0, len(self.jobs) - MAX_JOBS_KEPT)]:
            self.jobs.pop(job.id, None)

    def job(self, job_id: str) -> dict[str, Any]:
        with self.lock:
            job = self.jobs.get(job_id)
            if not job:
                raise KeyError("没有这条运行记录（面板重启后会清空）。")
            return job.public()

    def list_jobs(self, project_id: str) -> list[dict[str, Any]]:
        with self.lock:
            jobs = [j.public(with_output=False) for j in self.jobs.values() if j.project == project_id]
        return sorted(jobs, key=lambda j: -j["startedAt"])

    def cancel(self, job_id: str) -> dict[str, Any]:
        with self.lock:
            job = self.jobs.get(job_id)
            if not job:
                raise KeyError("没有这条运行记录。")
            if job.running and job.process:
                job.cancelled = True
                job.process.kill()
        return self.job(job_id)


def tomllib_loads(text: str) -> dict[str, Any]:
    import tomllib  # Python 3.11+；调用前已由 _gapr() 检查过版本
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"project.toml 格式错误：{exc}") from None


_TOML_VALUE_RE = r'("(?:[^"\\\n]|\\.)*"|\'[^\'\n]*\')'


def _set_toml_values(text: str, section: str, values: dict[str, str]) -> str:
    """只替换 [section] 里 key = "…" 的值，注释和其他行原样保留；缺的键插在节标题后面。
    值已由 _plain_text / _optional_date 排除了引号、反斜杠和控制字符，可以直接放进 TOML 基本字符串。"""
    lines = text.splitlines(keepends=True)
    current, header_at, done = None, None, set()
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and not stripped.startswith("[["):
            current = stripped.strip("[] \t")
            if current == section:
                header_at = i
            continue
        if current != section:
            continue
        body = line.rstrip("\r\n")
        for key, value in values.items():
            m = re.match(rf"^(\s*{re.escape(key)}\s*=\s*){_TOML_VALUE_RE}(.*)$", body)
            if m and key not in done:
                lines[i] = f'{m.group(1)}"{value}"{m.group(3)}{line[len(body):]}'
                done.add(key)
    missing = [k for k in values if k not in done]
    if missing:
        if header_at is None:
            raise ValueError(f"project.toml 里没有 [{section}] 一节。")
        newline = "\r\n" if "\r\n" in text else "\n"
        lines[header_at + 1:header_at + 1] = [f'{key} = "{values[key]}"{newline}' for key in missing]
    return "".join(lines)


def _latest_mtime(path: Path) -> float:
    times = [f.stat().st_mtime for f in path.glob("*") if f.is_file()]
    return max(times, default=path.stat().st_mtime)


def _count_rows(path: Path) -> int:
    with path.open(encoding="utf-8-sig", errors="replace", newline="") as handle:
        import csv
        return max(0, sum(1 for _ in csv.reader(handle)) - 1)


def _optional_date(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        raise ValueError(f"{label} 要写成 YYYY-MM-DD。") from None


def _plain_text(value: Any, label: str, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) > limit:
        raise ValueError(f"{label} 太长（最多 {limit} 个字符）。")
    if any(ord(c) < 32 and c not in "\t" for c in text):
        raise ValueError(f"{label} 不能包含换行或控制字符。")
    if label in ("领域", "读者", "包含", "排除") and ('"' in text or "\\" in text):
        raise ValueError(f"{label} 不能包含双引号或反斜杠。")
    return text


def create_gap_survey_blueprint(service: GapSurveyService) -> Blueprint:
    bp = Blueprint("gap_survey_api", __name__, url_prefix="/api/gap-survey")

    @bp.before_request
    def require_marker():
        if request.method != "GET" and request.headers.get("X-Gap-Survey") != "1":
            return jsonify({"error": "缺少演化树调研请求标识。"}), 403
        return None

    def body() -> dict[str, Any]:
        value = request.get_json(silent=True)
        return value if isinstance(value, dict) else {}

    def error(exc: Exception):
        status = 404 if isinstance(exc, KeyError) else 400
        message = exc.args[0] if exc.args else "请求失败。"
        return jsonify({"error": str(message)}), status

    @bp.get("/status")
    def status():
        ok, why = gapr_available()
        return jsonify({"available": ok, "reason": why, "root": str(service.root.resolve()),
                        "openalexKey": bool(os.environ.get("OPENALEX_API_KEY", "").strip()),
                        "s2Key": bool((os.environ.get("S2_API_KEY") or os.environ.get("SEMANTIC_SCHOLAR_API_KEY") or "").strip()),
                        "openreviewToken": bool(os.environ.get("OPENREVIEW_TOKEN", "").strip()),
                        "commands": [{"name": name, "label": spec["label"]} for name, spec in COMMANDS.items()]})

    @bp.get("/guide")
    def guide():
        """方法说明（唯一的流程说明）：docs/gap-research.md。"""
        path = service.workdir / "docs" / "gap-research.md"
        try:
            return jsonify({"name": "docs/gap-research.md", "text": path.read_text(encoding="utf-8")})
        except OSError:
            return jsonify({"error": "找不到 docs/gap-research.md。"}), 404

    @bp.get("/projects")
    def projects():
        try:
            return jsonify({"projects": service.list_projects()})
        except (RuntimeError, OSError) as exc:
            return error(exc)

    @bp.post("/projects")
    def create_project():
        try:
            return jsonify({"project": service.create(body())})
        except (ValueError, RuntimeError, OSError) as exc:
            return error(exc)

    @bp.get("/projects/<project_id>")
    def project(project_id: str):
        try:
            return jsonify({"project": service.overview(project_id)})
        except (KeyError, ValueError, RuntimeError, OSError) as exc:
            return error(exc)

    @bp.put("/projects/<project_id>")
    def update_project(project_id: str):
        try:
            return jsonify({"project": service.update(project_id, body())})
        except (KeyError, ValueError, RuntimeError, OSError) as exc:
            return error(exc)

    def auto_or_error():
        if not service.auto:
            raise ValueError("面板没有配置 Claude Code，自动模式不可用。")
        return service.auto

    @bp.get("/projects/<project_id>/auto")
    def auto_state(project_id: str):
        try:
            service.project_dir(project_id)
            return jsonify({"auto": auto_or_error().public(project_id)})
        except (KeyError, ValueError, RuntimeError, OSError) as exc:
            return error(exc)

    @bp.post("/projects/<project_id>/auto/enable")
    def auto_enable(project_id: str):
        try:
            service.project_dir(project_id)
            return jsonify({"auto": auto_or_error().enable(project_id, bool(body().get("enabled", True)))})
        except (KeyError, ValueError, RuntimeError, OSError) as exc:
            return error(exc)

    @bp.post("/projects/<project_id>/auto/start")
    def auto_start(project_id: str):
        data = body()
        if data.get("acknowledged") is not True:
            return jsonify({"error": "自动运行会消耗 Claude 套餐额度，请先勾选确认。"}), 400
        try:
            service.project_dir(project_id)
            if service.running_job(project_id):
                raise ValueError("这个项目有命令正在运行，等它结束再开始自动运行。")
            return jsonify({"auto": auto_or_error().start(project_id, model=str(data.get("model") or "opus"),
                                                          effort=str(data.get("effort") or "high"))})
        except (KeyError, ValueError, RuntimeError, OSError) as exc:
            return error(exc)

    @bp.post("/projects/<project_id>/auto/stop")
    def auto_stop(project_id: str):
        try:
            service.project_dir(project_id)
            return jsonify({"auto": auto_or_error().stop(project_id)})
        except (KeyError, ValueError, RuntimeError, OSError) as exc:
            return error(exc)

    @bp.get("/projects/<project_id>/file")
    def project_file(project_id: str):
        try:
            return jsonify(service.read_file(project_id, request.args.get("name", "")))
        except (KeyError, ValueError, RuntimeError, OSError) as exc:
            return error(exc)

    @bp.get("/projects/<project_id>/jobs")
    def jobs(project_id: str):
        return jsonify({"jobs": service.list_jobs(project_id)})

    @bp.post("/projects/<project_id>/run")
    def run(project_id: str):
        data = body()
        try:
            return jsonify({"job": service.start(project_id, str(data.get("command") or ""), data.get("args") or {})})
        except (KeyError, ValueError, RuntimeError, OSError) as exc:
            return error(exc)

    @bp.get("/jobs/<job_id>")
    def job(job_id: str):
        try:
            return jsonify({"job": service.job(job_id)})
        except KeyError as exc:
            return error(exc)

    @bp.post("/jobs/<job_id>/cancel")
    def cancel(job_id: str):
        try:
            return jsonify({"job": service.cancel(job_id)})
        except KeyError as exc:
            return error(exc)

    return bp
