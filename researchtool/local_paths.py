"""Resolve real local paths for files the browser only exposes by name.

Browsers never reveal a dropped or selected file's absolute path. Because this
panel only serves 127.0.0.1, the backend can do two things instead:

* open the operating system's native file/folder dialog (``pick_path``), and
* find a dropped file on disk from the metadata the browser does expose:
  name, byte size and last-modified time (``locate_file``).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from flask import Blueprint, jsonify, request, send_file

PICK_TIMEOUT_SECONDS = 600
PICKED_TOKEN_TTL_SECONDS = 15 * 60
LOCATE_TIME_BUDGET_SECONDS = 3.0
LOCATE_MAX_DEPTH = 5
LOCATE_MAX_CANDIDATES = 5
MTIME_TOLERANCE_SECONDS = 2.0
SKIPPED_DIR_NAMES = {
    "node_modules", "__pycache__", "$recycle.bin", "system volume information",
    "appdata", "library", "site-packages", "venv", ".venv",
}

_TK_PICKER = r"""
import json, sys, tkinter
from tkinter import filedialog
spec = json.loads(sys.argv[1])
root = tkinter.Tk()
root.withdraw()
root.attributes("-topmost", True)
root.update()
options = {"parent": root, "title": spec.get("title") or ""}
if spec.get("initialDir"):
    options["initialdir"] = spec["initialDir"]
if spec["kind"] == "folder":
    result = filedialog.askdirectory(mustexist=True, **options)
else:
    suffixes = spec.get("suffixes") or []
    types = [("Supported", " ".join("*" + s for s in suffixes))] if suffixes else []
    result = filedialog.askopenfilename(filetypes=types + [("All files", "*.*")], **options)
root.destroy()
sys.stdout.write(json.dumps({"path": result or ""}))
"""


class PickerUnavailable(RuntimeError):
    pass


def _normalize_suffixes(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    result = []
    for value in values[:20]:
        text = str(value).strip().lower()
        if text and len(text) <= 16:
            result.append(text if text.startswith(".") else f".{text}")
    return result


def _osascript_picker(kind: str, title: str, suffixes: list[str]) -> str:
    prompt = json.dumps(title or ("选择文件夹" if kind == "folder" else "选择文件"))
    if kind == "folder":
        script = f"POSIX path of (choose folder with prompt {prompt})"
    else:
        types = ""
        if suffixes:
            types = " of type {" + ", ".join(json.dumps(s.lstrip(".")) for s in suffixes) + "}"
        script = f"POSIX path of (choose file with prompt {prompt}{types})"
    completed = subprocess.run(
        ["osascript", "-e", script], capture_output=True, text=True, timeout=PICK_TIMEOUT_SECONDS
    )
    if completed.returncode != 0:
        # User cancellation exits with -128; treat every failure as no selection
        # unless osascript itself is missing (raised as FileNotFoundError).
        return ""
    return completed.stdout.strip()


def _tk_picker(kind: str, title: str, suffixes: list[str], initial_dir: str) -> str:
    spec = json.dumps({"kind": kind, "title": title, "suffixes": suffixes, "initialDir": initial_dir})
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    completed = subprocess.run(
        [sys.executable, "-I", "-c", _TK_PICKER, spec],
        capture_output=True, text=True, encoding="utf-8", timeout=PICK_TIMEOUT_SECONDS,
        creationflags=creationflags,
    )
    if completed.returncode != 0:
        raise PickerUnavailable((completed.stderr or "无法打开系统文件对话框。").strip()[-500:])
    try:
        return str(json.loads(completed.stdout or "{}").get("path") or "")
    except json.JSONDecodeError as exc:
        raise PickerUnavailable("系统文件对话框返回了无效结果。") from exc


def run_native_picker(kind: str, title: str = "", suffixes: list[str] | None = None, initial_dir: str = "") -> str:
    """Show a native dialog in a child process and return the chosen path or ''."""
    suffixes = suffixes or []
    if sys.platform == "darwin":
        try:
            return _osascript_picker(kind, title, suffixes)
        except FileNotFoundError:
            pass
    return _tk_picker(kind, title, suffixes, initial_dir)


def _default_search_roots() -> list[Path]:
    home = Path.home()
    names = ["Downloads", "Desktop", "Documents"]
    roots = [home / name for name in names]
    for variable in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        base = os.environ.get(variable)
        if base:
            roots.extend(Path(base) / name for name in ("Desktop", "Documents"))
            roots.append(Path(base))
    roots.append(home)
    return roots


@dataclass
class LocalPathService:
    state_file: Path | None = None
    picker: Callable[..., str] = run_native_picker
    search_roots: Callable[[], list[Path]] = _default_search_roots
    recent_limit: int = 20
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _picker_lock: threading.Lock = field(default_factory=threading.Lock)
    _picked: dict[str, tuple[Path, float]] = field(default_factory=dict)
    _recent: deque = field(default_factory=deque)

    def __post_init__(self) -> None:
        self._recent = deque(maxlen=self.recent_limit)
        if self.state_file and self.state_file.is_file():
            try:
                saved = json.loads(self.state_file.read_text(encoding="utf-8"))
                for item in saved.get("recentDirs", [])[: self.recent_limit]:
                    self._recent.append(str(item))
            except (OSError, ValueError, AttributeError):
                pass

    # -- recent directories -------------------------------------------------
    def remember_dir(self, directory: Path) -> None:
        text = str(directory)
        with self._lock:
            if text in self._recent:
                self._recent.remove(text)
            self._recent.appendleft(text)
            snapshot = list(self._recent)
        if self.state_file:
            try:
                self.state_file.parent.mkdir(parents=True, exist_ok=True)
                self.state_file.write_text(json.dumps({"recentDirs": snapshot}, ensure_ascii=False), encoding="utf-8")
            except OSError:
                pass

    def recent_dirs(self) -> list[Path]:
        with self._lock:
            return [Path(item) for item in self._recent]

    # -- native picker ------------------------------------------------------
    def pick(self, kind: str, title: str = "", suffixes: list[str] | None = None) -> dict[str, Any]:
        if kind not in {"file", "folder"}:
            raise ValueError("kind must be 'file' or 'folder'.")
        if not self._picker_lock.acquire(blocking=False):
            raise ValueError("已有一个文件选择窗口打开，请先完成或取消它。")
        try:
            recent = self.recent_dirs()
            initial = next((str(item) for item in recent if item.is_dir()), "")
            raw = self.picker(kind, title, suffixes or [], initial)
        finally:
            self._picker_lock.release()
        if not raw:
            return {"cancelled": True}
        path = Path(raw).expanduser()
        if kind == "folder":
            if not path.is_dir():
                raise ValueError("所选文件夹不存在。")
            self.remember_dir(path)
            return {"path": str(path)}
        if not path.is_file():
            raise ValueError("所选文件不存在。")
        if suffixes and path.suffix.lower() not in suffixes:
            raise ValueError(f"请选择 {' / '.join(suffixes)} 文件。")
        self.remember_dir(path.parent)
        token = uuid.uuid4().hex
        now = time.monotonic()
        with self._lock:
            self._picked = {key: value for key, value in self._picked.items() if now - value[1] < PICKED_TOKEN_TTL_SECONDS}
            self._picked[token] = (path, now)
        stat = path.stat()
        return {"path": str(path), "name": path.name, "size": stat.st_size, "token": token}

    def picked_file(self, token: str) -> Path | None:
        with self._lock:
            item = self._picked.get(token)
        if not item or time.monotonic() - item[1] >= PICKED_TOKEN_TTL_SECONDS or not item[0].is_file():
            return None
        return item[0]

    # -- locate by metadata -------------------------------------------------
    def locate(self, name: str, size: int, last_modified_ms: float | None = None) -> dict[str, Any]:
        name = Path(str(name or "")).name
        if not name or name in {".", ".."}:
            raise ValueError("缺少文件名。")
        if not isinstance(size, int) or size < 0:
            raise ValueError("文件大小无效。")
        target_mtime = float(last_modified_ms) / 1000 if last_modified_ms else None
        deadline = time.monotonic() + LOCATE_TIME_BUDGET_SECONDS
        exact: list[Path] = []
        loose: list[Path] = []
        seen_dirs: set[str] = set()

        def consider(candidate: Path) -> None:
            try:
                stat = candidate.stat()
            except OSError:
                return
            if stat.st_size != size or not candidate.is_file():
                return
            bucket = exact if target_mtime is None or abs(stat.st_mtime - target_mtime) <= MTIME_TOLERANCE_SECONDS else loose
            resolved = candidate.resolve()
            if resolved not in exact and resolved not in loose:
                bucket.append(resolved)

        # Recently used folders are checked directly first: cheap and most likely.
        for directory in self.recent_dirs():
            consider(directory / name)
        timed_out = False
        if not exact:
            for root in self._unique_roots():
                if time.monotonic() > deadline:
                    timed_out = True
                    break
                timed_out = self._walk(root, name, consider, deadline, seen_dirs, root_depth=0 if root != Path.home() else LOCATE_MAX_DEPTH - 1) or timed_out
                if len(exact) > LOCATE_MAX_CANDIDATES:
                    break
        matches = exact or loose
        result: dict[str, Any] = {"path": None, "candidates": [str(item) for item in matches[:LOCATE_MAX_CANDIDATES]], "timedOut": timed_out}
        if len(exact) == 1:
            result["path"] = str(exact[0])
            self.remember_dir(exact[0].parent)
        return result

    def _unique_roots(self) -> Iterable[Path]:
        seen: set[str] = set()
        for root in [*self.recent_dirs(), *self.search_roots()]:
            key = os.path.normcase(str(root))
            if key in seen or not root.is_dir():
                continue
            seen.add(key)
            yield root

    def _walk(self, root: Path, name: str, consider: Callable[[Path], None], deadline: float, seen: set[str], root_depth: int) -> bool:
        stack = [(root, root_depth)]
        lowered = name.lower()
        while stack:
            if time.monotonic() > deadline:
                return True
            directory, depth = stack.pop()
            key = os.path.normcase(str(directory))
            if key in seen:
                continue
            seen.add(key)
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                child = entry.name.lower()
                                if depth + 1 < LOCATE_MAX_DEPTH and not child.startswith(".") and child not in SKIPPED_DIR_NAMES:
                                    stack.append((Path(entry.path), depth + 1))
                            elif entry.name.lower() == lowered:
                                consider(Path(entry.path))
                        except OSError:
                            continue
            except OSError:
                continue
        return False


def create_local_paths_blueprint(service: LocalPathService) -> Blueprint:
    bp = Blueprint("local_paths", __name__)

    @bp.post("/api/local-paths/pick")
    def pick():
        payload = request.get_json(silent=True) or {}
        try:
            result = service.pick(
                str(payload.get("kind") or "file"),
                str(payload.get("title") or "")[:200],
                _normalize_suffixes(payload.get("suffixes")),
            )
        except PickerUnavailable as exc:
            return jsonify(error=f"无法打开系统文件对话框：{exc}"), 503
        except subprocess.TimeoutExpired:
            return jsonify(error="文件选择超时。"), 504
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        return jsonify(result)

    @bp.get("/api/local-paths/picked/<token>")
    def picked(token: str):
        path = service.picked_file(token)
        if path is None:
            return jsonify(error="所选文件已失效，请重新选择。"), 404
        return send_file(path, as_attachment=True, download_name=path.name)

    @bp.post("/api/local-paths/locate")
    def locate():
        payload = request.get_json(silent=True) or {}
        size = payload.get("size")
        modified = payload.get("lastModified")
        if not isinstance(modified, (int, float)) or isinstance(modified, bool):
            modified = None
        try:
            result = service.locate(str(payload.get("name") or ""), size if isinstance(size, int) and not isinstance(size, bool) else -1, modified)
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        return jsonify(result)

    return bp
