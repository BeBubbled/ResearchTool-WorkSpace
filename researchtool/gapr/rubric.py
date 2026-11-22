"""规则文件冻结：落实模板全程规则第 4 条“先定规则再看结果”。

- gapr freeze：在 matrix.csv 还没填任何格子时，对 rubric/ 下所有文件算哈希，
  写入 rubric.lock.json。矩阵已填过格子时拒绝冻结，除非 --force 并写明理由；
  强制冻结会留下一条历史记录，报告里要如实列出。
- gapr gaps 等计算命令运行前调用 verify()：规则文件与冻结时不一致就拒绝计算。

哈希前先把 CRLF 统一成 LF，避免 git 在 Windows 上转换换行导致误报。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

from researchtool.gapr.matrix import matrix_filled_cells

LOCK_NAME = "rubric.lock.json"


class RubricError(Exception):
    pass


def _digest(path: Path) -> str:
    data = path.read_bytes().replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest()


def _snapshot(rubric_dir: Path) -> dict[str, str]:
    if not rubric_dir.is_dir():
        raise RubricError(f"{rubric_dir} 不存在")
    return {p.relative_to(rubric_dir).as_posix(): _digest(p)
            for p in sorted(rubric_dir.rglob("*")) if p.is_file()}


def freeze(root: Path, *, force: bool = False, reason: str = "") -> dict:
    root = Path(root)
    lock_path = root / LOCK_NAME
    filled = matrix_filled_cells(root / "matrix.csv")
    old = json.loads(lock_path.read_text(encoding="utf-8")) if lock_path.exists() else None
    if filled and not force:
        raise RubricError(
            f"matrix.csv 已经填了 {filled} 个格子。看到结果之后再改规则违反“先定规则再看结果”。"
            " 确实必须改时用 --force --reason \"…\"，这条修改会写进报告。")
    if force and not reason:
        raise RubricError("--force 必须同时给 --reason")
    attrs = root / "rubric" / "attributes.csv"
    from researchtool.gapr import schema  # 避免循环导入
    if not schema.read_rows(attrs):
        raise RubricError("rubric/attributes.csv 还没有任何属性，不能冻结")
    snap = _snapshot(root / "rubric")
    now = datetime.now().isoformat(timespec="seconds")
    history = (old or {}).get("history", [])
    history.append({"at": now, "files": snap, "forced": bool(force),
                    "reason": reason, "matrix_cells_filled": filled})
    lock = {"frozen_at": now, "files": snap, "history": history}
    lock_path.write_text(json.dumps(lock, ensure_ascii=False, indent=2), encoding="utf-8")
    return lock


def verify(root: Path) -> dict:
    root = Path(root)
    lock_path = root / LOCK_NAME
    if not lock_path.exists():
        raise RubricError("规则文件还没冻结：先在确认点 2 运行 gapr freeze")
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    now = _snapshot(root / "rubric")
    frozen = lock["files"]
    changed = sorted(k for k in frozen.keys() & now.keys() if frozen[k] != now[k])
    added = sorted(now.keys() - frozen.keys())
    removed = sorted(frozen.keys() - now.keys())
    if changed or added or removed:
        parts = []
        if changed:
            parts.append("被修改：" + "、".join(changed))
        if added:
            parts.append("新增：" + "、".join(added))
        if removed:
            parts.append("删除：" + "、".join(removed))
        raise RubricError("规则文件在冻结后发生变化（" + "；".join(parts) + "）。"
                          " 恢复原文件，或用 gapr freeze --force --reason 记录这次修改。")
    return lock


def forced_changes(root: Path) -> list[dict]:
    lock_path = Path(root) / LOCK_NAME
    if not lock_path.exists():
        return []
    return [h for h in json.loads(lock_path.read_text(encoding="utf-8")).get("history", []) if h.get("forced")]
