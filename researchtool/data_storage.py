"""数据存储目录：把体积大的运行成果放到用户指定的文件夹（例如由 Google Drive 同步的目录）。

只有"成果"类子目录会跟着走；Codex 安装、翻译后端、上传临时文件、用量记录等本机运行时
始终留在项目的 .runtime 下，避免把几百 MB 的程序或一次性的临时文件也同步出去。

目录在进程启动时确定一次（各服务在导入时就绑定了路径），因此修改设置后需要重启面板。
如果保存时勾选了"迁移现有数据"，下次启动、在任何服务创建之前，先把旧目录里的子目录移过去。
"""

from __future__ import annotations

import os
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DATA_DIR_ENV_KEY = "RESEARCHTOOL_DATA_DIR"
# 下次启动时要从哪个目录迁移数据过来；迁移完成后自动清除。
PENDING_MOVE_ENV_KEY = "RESEARCHTOOL_DATA_DIR_MOVE_FROM"

# 跟随数据目录的子目录（运行成果）。
DATA_SUBDIRS = (
    "jobs",
    "reader-cache",
    "research-gaps",
    "gap-survey",
    "paper-revision",
    "revision-compile",
    "branchboard",
    "researchstudio",
    "prompt-workbench",
)


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
    """检查用户填写的目录：必须是已存在、可写的绝对路径，且不能落在项目 .runtime 的数据子目录里。"""
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
        sub = (default_resolved / name)
        if resolved == sub or sub in resolved.parents:
            raise ValueError("不能把数据目录放在默认数据目录的子目录里。")
    probe = resolved / f".researchtool-write-test-{uuid.uuid4().hex}"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        raise ValueError(f"该文件夹不可写：{exc.strerror or exc.__class__.__name__}") from None
    return resolved


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
            shutil.move(str(old), str(new))
            moved.append(name)
        except OSError as exc:
            failed.append(f"{name}：{exc.strerror or exc.__class__.__name__}")
    return {"from": str(source), "moved": moved, "skipped": skipped, "failed": failed}


def resolve_data_dir(default: Path, environ: Any = os.environ) -> DataDirState:
    """启动时调用一次：决定本次进程使用的数据目录，必要时先执行一次待办的迁移。"""
    configured = _clean(environ.get(DATA_DIR_ENV_KEY, ""))
    state = DataDirState(default=default, active=default, configured=configured)
    if configured:
        path = Path(configured).expanduser()
        if path.is_absolute() and path.is_dir():
            state.active = path.resolve()
        else:
            # 云盘未挂载时不要悄悄在空路径上新建一份数据，回退到默认目录并提示。
            state.warning = f"设置的数据目录不可用（{configured}），本次启动暂时使用默认目录。"
    pending = _clean(environ.get(PENDING_MOVE_ENV_KEY, ""))
    if pending and not state.warning:
        source = Path(pending).expanduser()
        if source.is_dir():
            state.migration = _move_existing(source, state.active)
        else:
            state.migration = {"from": pending, "moved": [], "skipped": [], "failed": ["原目录不存在"]}
    return state


def status(state: DataDirState, environ: Any = os.environ) -> dict[str, Any]:
    saved = _clean(environ.get(DATA_DIR_ENV_KEY, ""))
    saved_target = str(Path(saved).expanduser()) if saved else str(state.default)
    return {
        "default": str(state.default),
        "active": str(state.active),
        "configured": saved,
        "usingDefault": state.active == state.default,
        "restartRequired": os.path.normcase(saved_target) != os.path.normcase(str(state.active)),
        "pendingMove": _clean(environ.get(PENDING_MOVE_ENV_KEY, "")),
        "warning": state.warning,
        "migration": state.migration,
        "subdirs": list(DATA_SUBDIRS),
    }
