"""数据存储目录：把体积大的运行成果放到用户指定的文件夹（例如由 Google Drive 同步的目录）。

只有"成果"类子目录会跟着走；Codex 安装、翻译后端、ResearchStudio 的 Python / LibreOffice 环境、
上传临时文件、用量记录等本机运行时始终留在项目的 .runtime，避免把程序或一次性文件也同步出去。

三层分工：
* 数据目录（会同步）：运行成果 + 共享配置 config/settings.env（API Key、模型偏好等）+ 数据库标识
  researchtool-library.json + 各台电脑的心跳 machines/<机器 ID>.json。
* 项目 .env（不同步）：只留本机专属项，例如 CLI 路径、端口（见 MACHINE_LOCAL_ENV_KEYS）。
* 本机配置（不同步）：数据目录在这台电脑上的路径和它对应的数据库 ID，保存在当前用户的本机配置目录
  （Linux ~/.config/researchtool、macOS ~/Library/Application Support/ResearchTool、
  Windows %LOCALAPPDATA%\\ResearchTool），按「机器 ID + 项目路径」分条记录。

同一个 Google Drive 在不同电脑上挂载路径不同没关系：每台电脑各记自己的路径；启动时再用数据库 ID
核对"这台电脑看到的是不是同一个库"，对不上（登录了别的账号、云盘还没同步下来、路径指向别处）
就退回默认目录并提示，不会把两份数据混在一起。

目录在进程启动时确定一次（各服务在导入时就绑定了路径），因此修改设置后需要重启面板。
如果保存时勾选了"迁移现有数据"，下次启动、在任何服务创建之前，先把旧目录里的子目录移过去。
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

# 旧版本把设置写在项目 .env 里；启动时迁到本机配置后从 .env 删除。
LEGACY_DATA_DIR_ENV_KEY = "RESEARCHTOOL_DATA_DIR"
LEGACY_PENDING_MOVE_ENV_KEY = "RESEARCHTOOL_DATA_DIR_MOVE_FROM"

SHARED_ENV_RELATIVE = Path("config") / "settings.env"
LIBRARY_MARKER = "researchtool-library.json"
MACHINES_DIR = "machines"
ACTIVE_WINDOW_SECONDS = 15 * 60  # 心跳在这段时间内更新过，视为那台电脑正在使用

# 只属于这台电脑的配置：程序路径、端口等。其余配置（API Key、模型偏好……）随数据目录同步。
MACHINE_LOCAL_ENV_KEYS = frozenset({
    "CODEX_BIN", "CODEX_NPM_BIN", "CODEX_UPDATE_INTERVAL_HOURS", "CLAUDE_CODE_CLI",
    "RESEARCHSTUDIO_PYTHON", "RESEARCHSTUDIO_TOOL_PATH", "TRANSLATION_BACKEND_BINARY",
    "WEB_PANEL_PORT", "WEB_PANEL_OPEN_BROWSER", LEGACY_DATA_DIR_ENV_KEY, LEGACY_PENDING_MOVE_ENV_KEY,
})

# 跟随数据目录的子目录（运行成果）。可以是多级路径。
DATA_SUBDIRS = (
    "jobs",
    "reader-cache",
    "research-gaps",
    "gap-survey",
    "paper-revision",
    "revision-compile",
    "branchboard",
    "researchstudio/jobs",
    "prompt-workbench",
)


# -- 机器 ID ---------------------------------------------------------------
def _raw_machine_id() -> str:
    if sys.platform.startswith("linux"):
        for candidate in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
            try:
                text = Path(candidate).read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if text:
                return text
    elif sys.platform == "darwin":
        try:
            output = subprocess.run(["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                                    capture_output=True, text=True, timeout=5).stdout
            for line in output.splitlines():
                if "IOPlatformUUID" in line:
                    return line.split("=", 1)[1].strip().strip('"')
        except (OSError, subprocess.TimeoutExpired):
            pass
    elif os.name == "nt":
        try:
            import winreg  # type: ignore[import-not-found]
            access = winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0)
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography", 0, access) as key:
                return str(winreg.QueryValueEx(key, "MachineGuid")[0])
        except OSError:
            pass
    # 兜底：主机名 + 网卡地址。不如系统 ID 稳定，但足以区分不同电脑。
    return f"{socket.gethostname()}-{uuid.getnode():x}"


def machine_id() -> str:
    """稳定的本机标识（哈希后只取前 16 位，不暴露原始 ID）。"""
    return hashlib.sha256(_raw_machine_id().encode("utf-8")).hexdigest()[:16]


def default_config_path() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")  # Local 不随漫游配置同步
        return base / "ResearchTool" / "machine.json"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "ResearchTool" / "machine.json"
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "researchtool" / "machine.json"


# -- 本机配置 --------------------------------------------------------------
@dataclass
class LocalStore:
    """本机配置文件里属于「这台电脑 × 这个项目」的那一条。"""

    path: Path
    project_root: Path
    machine: str = ""
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @classmethod
    def default(cls, project_root: Path) -> "LocalStore":
        return cls(default_config_path(), project_root, machine_id())

    @property
    def project_key(self) -> str:
        return os.path.normcase(str(self.project_root.resolve()))

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    FIELDS = ("dataDir", "moveFrom", "libraryId", "seededLibrary")

    def read(self) -> dict[str, str]:
        data = self._load()
        entry = data.get("machines", {}).get(self.machine, {}).get("projects", {}).get(self.project_key, {})
        if not isinstance(entry, dict):
            entry = {}
        return {key: str(entry.get(key) or "") for key in self.FIELDS}

    def write(self, **values: str) -> None:
        with self._lock:
            data = self._load()
            machines = data.setdefault("machines", {})
            machine = machines.setdefault(self.machine, {"hostname": socket.gethostname(), "system": platform.system()})
            projects = machine.setdefault("projects", {})
            entry = projects.setdefault(self.project_key, {})
            for key, value in values.items():
                if value:
                    entry[key] = value
                else:
                    entry.pop(key, None)
            if not entry:
                projects.pop(self.project_key, None)
            data["version"] = 1
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(f".{uuid.uuid4().hex}.tmp")
            temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temp, self.path)


@dataclass
class DataDirState:
    default: Path
    active: Path
    configured: str = ""
    warning: str = ""
    migration: dict[str, Any] = field(default_factory=dict)


def _clean(raw: str) -> str:
    return str(raw or "").strip().strip('"').strip("'")


def validate_data_dir(raw: str, default: Path) -> Path:
    """检查用户填写的目录：必须是已存在、可写的绝对路径，且不能落在默认数据目录的成果子目录里。"""
    text = _clean(raw)
    if not text:
        raise ValueError("请填写数据存储目录。")
    if len(text) > 1000:
        raise ValueError("路径太长。")
    path = Path(text).expanduser()
    if not path.is_absolute():
        raise ValueError("请填写绝对路径。")
    if not path.is_dir():
        raise ValueError("该文件夹不存在，请先创建它（或确认云盘已挂载）。")
    resolved = path.resolve()
    default_resolved = default.resolve()
    for name in DATA_SUBDIRS:
        sub = default_resolved / name
        if resolved == sub or sub in resolved.parents:
            raise ValueError("不能把数据目录放在默认数据目录的子目录里。")
    probe = resolved / f".researchtool-write-test-{uuid.uuid4().hex}"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        raise ValueError(f"该文件夹不可写：{exc.strerror or exc.__class__.__name__}") from None
    # 按用户写的样子保存（只规范化 . / ..），不展开符号链接或虚拟盘符：Google Drive 在 Windows 上挂成 G: 之类的虚拟盘。
    return Path(os.path.abspath(path))


def _move_existing(source: Path, target: Path) -> dict[str, Any]:
    moved: list[str] = []
    skipped: list[str] = []
    failed: list[str] = []
    if source.resolve() == target.resolve():
        return {"from": str(source), "moved": moved, "skipped": skipped, "failed": failed}
    for name in DATA_SUBDIRS:
        old = source / name
        if not old.exists():
            continue
        new = target / name
        if new.exists():
            # 目标里已有同名目录（例如另一台电脑同步过来的），不覆盖，留给用户自己合并。
            skipped.append(name)
            continue
        try:
            new.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(old), str(new))
            moved.append(name)
        except OSError as exc:
            failed.append(f"{name}：{exc.strerror or exc.__class__.__name__}")
    return {"from": str(source), "moved": moved, "skipped": skipped, "failed": failed}


def _now() -> float:
    return time.time()


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


# -- 数据库标识与心跳 ------------------------------------------------------
def read_library(data_dir: Path) -> dict[str, Any] | None:
    try:
        data = json.loads((data_dir / LIBRARY_MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("id"), str) and data["id"] else None


def create_library(data_dir: Path, machine: str) -> dict[str, Any]:
    library = {"id": uuid.uuid4().hex, "createdAt": _iso(_now()), "createdBy": socket.gethostname(), "createdByMachine": machine}
    _write_json(data_dir / LIBRARY_MARKER, library)
    return library


def heartbeat(data_dir: Path, store: LocalStore, started_at: float) -> None:
    """每台电脑只写自己的心跳文件，多台电脑同时同步也不会互相冲突。"""
    _write_json(data_dir / MACHINES_DIR / f"{store.machine}.json", {
        "machine": store.machine, "hostname": socket.gethostname(), "system": platform.system(),
        "path": str(data_dir), "project": str(store.project_root), "startedAt": _iso(started_at), "lastSeen": _iso(_now()),
    })


def list_machines(data_dir: Path, current: str) -> list[dict[str, Any]]:
    result = []
    for path in sorted((data_dir / MACHINES_DIR).glob("*.json")):
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
            seen = datetime.fromisoformat(str(item.get("lastSeen"))).timestamp()
        except (OSError, ValueError, TypeError):
            continue
        result.append({
            "machine": str(item.get("machine") or path.stem), "hostname": str(item.get("hostname") or ""),
            "system": str(item.get("system") or ""), "path": str(item.get("path") or ""),
            "lastSeen": str(item.get("lastSeen")), "current": path.stem == current,
            "active": path.stem != current and _now() - seen < ACTIVE_WINDOW_SECONDS,
        })
    return sorted(result, key=lambda item: item["lastSeen"], reverse=True)


def start_heartbeat(data_dir: Path, store: LocalStore, interval: float = 5 * 60) -> threading.Thread:
    started = _now()

    def loop() -> None:
        while True:
            try:
                heartbeat(data_dir, store, started)
            except OSError:
                pass
            time.sleep(interval)

    thread = threading.Thread(target=loop, name="data-dir-heartbeat", daemon=True)
    thread.start()
    return thread


LARGE_FILE_BYTES = 100 * 1024 * 1024
SCAN_TIME_BUDGET_SECONDS = 20.0


def scan_large(data_dir: Path, *, large_file: int = LARGE_FILE_BYTES, budget: float = SCAN_TIME_BUDGET_SECONDS) -> dict[str, Any]:
    """统计数据目录：每个一级子目录的大小、文件数、PDF 数，以及超过阈值的单个文件。"""
    deadline = time.monotonic() + budget
    dirs: dict[str, dict[str, int]] = {}
    big: list[dict[str, Any]] = []
    timed_out = False
    for current, subdirs, files in os.walk(data_dir):
        if time.monotonic() > deadline:
            timed_out = True
            break
        relative = Path(current).relative_to(data_dir)
        top = relative.parts[0] if relative.parts else "."
        bucket = dirs.setdefault(top, {"bytes": 0, "files": 0, "pdfs": 0})
        for name in files:
            try:
                size = (Path(current) / name).stat().st_size
            except OSError:
                continue
            bucket["bytes"] += size
            bucket["files"] += 1
            if name.lower().endswith(".pdf"):
                bucket["pdfs"] += 1
            if size >= large_file:
                big.append({"path": (relative / name).as_posix(), "bytes": size})
    total = sum(item["bytes"] for item in dirs.values())
    return {
        "total": total, "timedOut": timed_out, "largeFileBytes": large_file,
        "dirs": sorted(({"name": key, **value} for key, value in dirs.items()), key=lambda item: item["bytes"], reverse=True),
        "largeFiles": sorted(big, key=lambda item: item["bytes"], reverse=True)[:30],
    }


# -- 共享配置 --------------------------------------------------------------
def shared_env_path(data_dir: Path) -> Path:
    return data_dir / SHARED_ENV_RELATIVE


def is_machine_local(key: str) -> bool:
    return key in MACHINE_LOCAL_ENV_KEYS


def load_shared_env(path: Path) -> None:
    """先于项目 .env 加载，所以共享配置优先；本机专属项即使出现在共享文件里也忽略。"""
    from dotenv import dotenv_values

    if not path.is_file():
        return
    for key, value in dotenv_values(path).items():
        if value is not None and not is_machine_local(key) and key not in os.environ:
            os.environ[key] = value


def move_env_keys(source: Path, target: Path, *, overwrite: bool, backup: bool = False, remove_source: bool = True) -> list[str]:
    """把 source 里可同步的键写到 target（target 已有的键按 overwrite 决定）；remove_source 时再从 source 删除。"""
    from dotenv import dotenv_values, set_key, unset_key

    if not source.is_file():
        return []
    values = {key: value for key, value in dotenv_values(source).items() if value is not None and not is_machine_local(key)}
    if not values:
        return []
    if backup:
        shutil.copy2(source, source.with_name(source.name + ".before-sync"))
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        target.touch(mode=0o600)
    existing = dotenv_values(target)
    moved = []
    for key, value in values.items():
        if overwrite or existing.get(key) is None:
            set_key(str(target), key, value, quote_mode="auto")
            moved.append(key)
        if remove_source:
            unset_key(str(source), key)
    return moved


def adopt_legacy_env(store: LocalStore, env_file: Path, unset: Callable[[str, str], Any]) -> None:
    """把旧版写在项目 .env 里的设置迁到本机配置，然后从 .env 删除，避免它随项目文件夹同步到别的电脑。"""
    from dotenv import dotenv_values

    if not env_file.is_file():
        return
    values = dotenv_values(env_file)
    legacy_dir = _clean(values.get(LEGACY_DATA_DIR_ENV_KEY) or "")
    legacy_move = _clean(values.get(LEGACY_PENDING_MOVE_ENV_KEY) or "")
    if not legacy_dir and not legacy_move:
        return
    if not store.read()["dataDir"] and legacy_dir:
        store.write(dataDir=legacy_dir, moveFrom=legacy_move)
    for key in (LEGACY_DATA_DIR_ENV_KEY, LEGACY_PENDING_MOVE_ENV_KEY):
        if key in values:
            unset(str(env_file), key)
        os.environ.pop(key, None)


def resolve_data_dir(default: Path, store: LocalStore) -> DataDirState:
    """启动时调用一次：决定本次进程使用的数据目录，必要时先执行一次待办的迁移（迁移只做一次）。"""
    saved = store.read()
    configured = _clean(saved["dataDir"])
    state = DataDirState(default=default, active=default, configured=configured)
    if configured:
        path = Path(configured).expanduser()
        library = read_library(path) if path.is_absolute() and path.is_dir() else None
        expected = saved["libraryId"]
        if not (path.is_absolute() and path.is_dir()):
            # 云盘未挂载时不要悄悄在空路径上新建一份数据，回退到默认目录并提示。
            state.warning = f"设置的数据目录不可用（{configured}），本次启动暂时使用默认目录。"
        elif expected and library is None:
            state.warning = (f"数据目录里没找到数据库标识文件 {LIBRARY_MARKER}（云盘可能还没同步完，或路径指向了别的文件夹），"
                             "本次启动暂时使用默认目录。")
        elif expected and library["id"] != expected:
            state.warning = ("数据目录里的数据库和这台电脑上次使用的不是同一个（可能云盘登录了别的账号，或路径指向了别处），"
                             "本次启动暂时使用默认目录。确认无误后在设置里重新保存该目录即可。")
        else:
            state.active = Path(os.path.abspath(path))
            if not expected:
                store.write(libraryId=(library or create_library(state.active, store.machine))["id"])
    pending = _clean(saved["moveFrom"])
    if pending and not state.warning:
        source = Path(pending).expanduser()
        if source.is_dir():
            state.migration = _move_existing(source, state.active)
        else:
            state.migration = {"from": pending, "moved": [], "skipped": [], "failed": ["原目录不存在"]}
        store.write(moveFrom="")
    return state


def status(state: DataDirState, store: LocalStore, shared_env: Path | None = None) -> dict[str, Any]:
    saved = store.read()
    configured = _clean(saved["dataDir"])
    saved_target = str(Path(configured).expanduser()) if configured else str(state.default)
    return {
        "default": str(state.default),
        "active": str(state.active),
        "configured": configured,
        "usingDefault": state.active == state.default,
        "restartRequired": os.path.normcase(saved_target) != os.path.normcase(str(state.active)),
        "pendingMove": _clean(saved["moveFrom"]),
        "warning": state.warning,
        "migration": state.migration,
        "subdirs": list(DATA_SUBDIRS),
        "configFile": str(store.path),
        "machineId": store.machine,
        "sharedEnv": str(shared_env) if shared_env else "",
        "library": read_library(state.active) if state.active != state.default else None,
        "machines": list_machines(state.active, store.machine) if state.active != state.default else [],
    }
