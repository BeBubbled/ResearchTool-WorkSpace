"""Extra actions for 双语论文改稿 that live outside the revision service itself.

  compile check   copy the paper's local project folder (images, .cls, .bib…) into a scratch
                  directory, overlay the selected revision's .tex/.bib, run latexmk (or
                  pdflatex/xelatex) without shell-escape, and report errors with file:line.
  git write-back  write the selected revision's files into the original folder, only when that
                  folder is a git working copy and every target file is unmodified in git, so
                  `git diff` shows exactly the change and `git checkout -- <file>` undoes it.
                  Nothing is committed.

Served under /api/paper-revision-tools/…; the page add-on is static/paper_revision_extras.js.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from flask import Blueprint, jsonify, request, send_file

COPY_LIMIT_BYTES = 300 * 1024 * 1024
COMPILE_TIMEOUT = 240
SKIP_DIRS = {".git", ".svn", "node_modules", "__pycache__", ".venv", "venv", ".runtime"}
ERROR_LINE = re.compile(r"^(?:! .+|.+?:\d+: .+)$")
WARNING_LINE = re.compile(r"(LaTeX Warning: .+|Package \S+ Warning: .+|Overfull \\hbox.+|Underfull \\hbox.+)")


def _engine() -> list[str] | None:
    if shutil.which("latexmk"):
        return ["latexmk", "-pdf", "-interaction=nonstopmode", "-halt-on-error", "-file-line-error", "-no-shell-escape"]
    for name in ("pdflatex", "xelatex"):
        if shutil.which(name):
            return [name, "-interaction=nonstopmode", "-halt-on-error", "-file-line-error", "-no-shell-escape"]
    return None


def safe_relative(name: str) -> PurePosixPath:
    path = PurePosixPath(str(name).replace("\\", "/"))
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts) or ":" in str(path):
        raise ValueError(f"文件路径不安全：{name}")
    return path


def main_file(files: list[dict[str, Any]], origin: dict[str, Any]) -> str:
    if origin.get("main"):
        return str(origin["main"])
    for item in files:
        if item["name"].lower().endswith(".tex") and "\\documentclass" in item["text"]:
            return item["name"]
    raise ValueError("找不到包含 \\documentclass 的主 .tex 文件。")


def parse_log(text: str, limit: int = 30) -> tuple[list[str], list[str]]:
    lines = text.splitlines()
    errors, warnings = [], []
    for index, line in enumerate(lines):
        if ERROR_LINE.match(line) and not line.startswith("Latexmk:"):
            context = " ".join(part.strip() for part in lines[index + 1:index + 3] if part.strip().startswith("l."))
            errors.append((line + (f"  ({context})" if context else ""))[:400])
        else:
            match = WARNING_LINE.search(line)
            if match:
                warnings.append(match.group(1)[:300])
    unique = lambda items: list(dict.fromkeys(items))[:limit]
    return unique(errors), unique(warnings)


def copy_project_tree(source: Path, target: Path) -> int:
    """Copy the paper folder (assets the export does not include); returns bytes copied."""
    total = 0
    for path in source.rglob("*"):
        relative = path.relative_to(source)
        if any(part in SKIP_DIRS or part.startswith(".") for part in relative.parts) or path.is_symlink():
            continue
        if path.is_dir():
            continue
        size = path.stat().st_size
        total += size
        if total > COPY_LIMIT_BYTES:
            raise ValueError("论文目录超过 300 MB，编译检查不复制这么大的目录。")
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
    return total


def git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=30)


class RevisionTools:
    def __init__(self, work_root: Path, *, read: Callable[[str], dict[str, Any]],
                 files: Callable[[str, int | None], list[dict[str, Any]]]) -> None:
        """read(pid) → the project record; files(pid, revision) → [{name, text}] for that revision."""
        self.work_root = Path(work_root)
        self.read = read
        self.files = files
        self.lock = threading.Lock()
        self.results: dict[str, dict[str, Any]] = {}

    def _project(self, pid: str, revision: Any) -> tuple[dict[str, Any], list[dict[str, Any]], int]:
        project = self.read(pid)
        revision = project["revision"] if revision in (None, "") else int(revision)
        return project, self.files(pid, revision), revision

    # ---- compile check ----
    def compile_check(self, pid: str, revision: Any = None) -> dict[str, Any]:
        command = _engine()
        if not command:
            raise RuntimeError("没有找到 latexmk、pdflatex 或 xelatex。请安装 TeX Live 或 MiKTeX 并加入 PATH。")
        project, files, revision = self._project(pid, revision)
        origin = project.get("origin") or {}
        check_id = uuid.uuid4().hex
        work = self.work_root / pid / check_id
        work.mkdir(parents=True)
        copied_assets = False
        root = Path(origin["root"]) if origin.get("root") else None
        if root and root.is_dir():
            copy_project_tree(root, work)
            copied_assets = True
        for item in files:
            destination = work / Path(*safe_relative(item["name"]).parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(item["text"].lstrip("﻿"), encoding="utf-8", newline="")
        main = main_file(files, origin)
        main_path = work / Path(*safe_relative(main).parts)
        started = time.time()
        try:
            completed = subprocess.run([*command, main_path.name], cwd=main_path.parent, capture_output=True, text=True,
                                       encoding="utf-8", errors="replace", timeout=COMPILE_TIMEOUT)
            timed_out = False
        except subprocess.TimeoutExpired as exc:
            completed, timed_out = exc, True
        log_path = main_path.with_suffix(".log")
        log_text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.is_file() else ""
        output = (getattr(completed, "stdout", "") or "") + (getattr(completed, "stderr", "") or "")
        errors, warnings = parse_log(log_text or output)
        pdf = main_path.with_suffix(".pdf")
        ok = not timed_out and getattr(completed, "returncode", 1) == 0 and pdf.is_file()
        if timed_out:
            errors.insert(0, f"编译超过 {COMPILE_TIMEOUT} 秒，已停止。")
        elif not ok and not errors:
            errors.append((output.strip().splitlines() or ["编译失败，没有生成 PDF。"])[-1][:400])
        result = {
            "id": check_id, "ok": ok, "revision": revision, "engine": command[0], "main": main,
            "copiedAssets": copied_assets, "seconds": round(time.time() - started, 1),
            "errors": errors, "warnings": warnings,
            "undefinedReferences": sum(1 for line in warnings if re.search(r"(Reference|Citation) `[^']+' .*undefined", line)),
            "pdfUrl": f"/api/paper-revision-tools/compile/{check_id}/pdf" if pdf.is_file() else None,
            "logUrl": f"/api/paper-revision-tools/compile/{check_id}/log" if log_path.is_file() else None,
        }
        with self.lock:
            self.results[check_id] = {"pdf": pdf if pdf.is_file() else None, "log": log_path if log_path.is_file() else None, "at": time.time()}
            for old in [key for key, value in self.results.items() if time.time() - value["at"] > 86400]:
                self.results.pop(old, None)
        return result

    def compile_file(self, check_id: str, kind: str) -> Path | None:
        with self.lock:
            entry = self.results.get(check_id)
        path = entry.get(kind) if entry else None
        return path if path and path.is_file() else None

    # ---- git write-back ----
    def write_back_plan(self, pid: str, revision: Any = None) -> dict[str, Any]:
        project, files, revision = self._project(pid, revision)
        origin = project.get("origin") or {}
        if not origin.get("root") or not Path(origin["root"]).is_dir():
            raise ValueError("只有从本机目录导入的论文才能写回；上传或粘贴导入的论文请使用导出 ZIP。")
        if not shutil.which("git"):
            raise RuntimeError("没有找到 git。")
        root = Path(origin["root"]).resolve()
        top = git(root, "rev-parse", "--show-toplevel")
        if top.returncode != 0:
            raise ValueError("论文目录不是 git 工作副本。写回只在 git 管理的目录里进行，便于用 git 查看和撤销。")
        changes, blocked = [], []
        for item in files:
            relative = safe_relative(item["name"])
            target = (root / Path(*relative.parts)).resolve()
            if root not in target.parents:
                raise ValueError(f"文件超出论文目录：{item['name']}")
            text = item["text"]
            current = target.read_text(encoding="utf-8-sig") if target.is_file() else None
            if current is not None and current.replace("\r\n", "\n") == text.lstrip("﻿").replace("\r\n", "\n"):
                continue
            status = git(root, "status", "--porcelain", "--", str(target)).stdout.strip()
            if status:
                blocked.append({"name": item["name"], "status": status[:2].strip() or "?"})
            else:
                changes.append({"name": item["name"], "new": current is None})
        return {"revision": revision, "root": str(root), "changes": changes, "blocked": blocked,
                "branch": git(root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()}

    def write_back(self, pid: str, revision: Any = None) -> dict[str, Any]:
        plan = self.write_back_plan(pid, revision)
        if plan["blocked"]:
            names = "、".join(item["name"] for item in plan["blocked"])
            raise ValueError(f"这些文件在 git 中有未提交的改动，为避免覆盖没有写回：{names}。请先提交或暂存后再试。")
        _, files, _ = self._project(pid, plan["revision"])
        wanted = {item["name"] for item in plan["changes"]}
        root = Path(plan["root"])
        written = []
        for item in files:
            if item["name"] not in wanted:
                continue
            target = root / Path(*safe_relative(item["name"]).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            staging = target.with_name(f".{target.name}.revision-tmp")
            staging.write_text(item["text"], encoding="utf-8", newline="")
            staging.replace(target)
            written.append(item["name"])
        return {**plan, "written": written,
                "undo": "git -C \"{}\" checkout -- {}".format(plan["root"], " ".join(f'"{name}"' for name in written)) if written else ""}


def create_revision_tools_blueprint(tools: RevisionTools) -> Blueprint:
    bp = Blueprint("paper_revision_tools", __name__, url_prefix="/api/paper-revision-tools")

    def fail(exc: Exception, status: int = 400):
        return jsonify({"error": str(exc)[:1000]}), status

    @bp.post("/projects/<pid>/compile-check")
    def compile_check(pid: str):
        data = request.get_json(silent=True) or {}
        try:
            return jsonify(tools.compile_check(pid, data.get("revision")))
        except KeyError as exc:
            return fail(exc, 404)
        except RuntimeError as exc:
            return fail(exc, 503)
        except (ValueError, OSError) as exc:
            return fail(exc)

    @bp.get("/compile/<check_id>/<kind>")
    def compile_file(check_id: str, kind: str):
        if kind not in {"pdf", "log"} or not re.fullmatch(r"[0-9a-f]{32}", check_id):
            return fail(ValueError("不存在。"), 404)
        path = tools.compile_file(check_id, kind)
        if not path:
            return fail(ValueError("编译结果已过期，请重新检查。"), 404)
        return send_file(path, mimetype="application/pdf" if kind == "pdf" else "text/plain; charset=utf-8",
                         as_attachment=kind == "log", download_name=path.name)

    @bp.post("/projects/<pid>/write-back")
    def write_back(pid: str):
        data = request.get_json(silent=True) or {}
        try:
            if data.get("confirm") is not True:
                return jsonify(tools.write_back_plan(pid, data.get("revision")))
            return jsonify(tools.write_back(pid, data.get("revision")))
        except KeyError as exc:
            return fail(exc, 404)
        except RuntimeError as exc:
            return fail(exc, 503)
        except (ValueError, OSError) as exc:
            return fail(exc, 409)

    return bp
