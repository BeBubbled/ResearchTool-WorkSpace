"""确认点：只能由用户本人在交互式终端里确认，模型代跑会被拒绝。

- 确认点 1（第 1 步之后）：节点清单 + 邻近领域术语对照表。
  记录节点 id 集合和术语表的快照；之后增删节点或改术语表，检查会要求重新确认。
- 确认点 2（第 4 步归纳属性之后、填格子之前）：属性定义 + 相容性表。
  确认时冻结 rubric/（见 rubric.py）。
- 候选锁定（仅回测）：第 5 步结束、开始查截止日之后的历史之前，锁定候选清单。
  这一步不需要人确认，由第一次越过截止日的检索自动触发。

防代跑：要求标准输入是交互式终端（isatty）。Claude Code 的命令工具不是终端，
所以模型直接运行 gapr confirm 会被拒绝。项目的 .claude/settings.json 里另有
deny 规则作为第二道保险。
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Callable

from researchtool.gapr import rubric, schema

FILE = "confirmations.json"


class ConfirmError(Exception):
    pass


def _hash(obj) -> str:
    return hashlib.sha256(json.dumps(obj, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def snapshot_1(root: Path) -> dict:
    nodes = sorted(r["id"] for r in schema.read_rows(root / "nodes.csv") if r.get("id"))
    terms = sorted((r.get("term", ""), r.get("neighbor_field", ""), r.get("neighbor_term", ""))
                   for r in schema.read_rows(root / "neighbor_terms.csv"))
    return {"nodes": nodes, "terms_hash": _hash(terms), "n_terms": len(terms)}


def snapshot_candidates(root: Path) -> dict:
    gaps = sorted((r.get("id", ""), r.get("statement", ""), r.get("merged_from", ""))
                  for r in schema.read_rows(root / "gaps.csv"))
    filt = sorted((r.get("id", ""), r.get("statement", "")) for r in schema.read_rows(root / "filtered.csv"))
    return {"gaps_hash": _hash(gaps), "filtered_hash": _hash(filt), "n_gaps": len(gaps)}


def load(root: Path) -> dict:
    p = Path(root) / FILE
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def _save(root: Path, data: dict) -> None:
    (Path(root) / FILE).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def is_interactive() -> bool:
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def _ask(prompt: str, interactive: Callable[[], bool], ask: Callable[[str], str]) -> None:
    if not interactive():
        raise ConfirmError("确认点只能由你本人在自己的终端（PowerShell）里运行；"
                           "检测到当前不是交互式终端，可能是模型在代跑，已拒绝。")
    ans = ask(prompt + "\n输入 确认 继续，其他任意输入取消：").strip()
    if ans not in ("确认", "yes", "YES"):
        raise ConfirmError("已取消，没有写入任何确认记录。")


def confirm_1(root: Path, *, note: str = "", interactive=is_interactive, ask=input,
              show: Callable[[str], None] = print) -> dict:
    root = Path(root)
    nodes = schema.read_rows(root / "nodes.csv")
    terms = schema.read_rows(root / "neighbor_terms.csv")
    lines = ["确认点 1：节点清单与邻近领域术语对照表", "", f"节点（{len(nodes)} 个）："]
    lines += [f"  {n['id']} | {n.get('name', '')} | {n.get('first_public', '')} | {n.get('family', '')}" for n in nodes]
    lines += ["", f"术语对照（{len(terms)} 条）："]
    lines += [f"  {t.get('term', '')} ↔ {t.get('neighbor_field', '')}：{t.get('neighbor_term', '')}" for t in terms]
    show("\n".join(lines))
    _ask("以上清单确认无误？", interactive, ask)
    data = load(root)
    data["1"] = {"at": datetime.now().isoformat(timespec="seconds"), "note": note, **snapshot_1(root)}
    _save(root, data)
    return data["1"]


def confirm_2(root: Path, *, note: str = "", change_reason: str = "", interactive=is_interactive,
              ask=input, show: Callable[[str], None] = print) -> dict:
    """确认属性并冻结规则。change_reason 非空表示冻结后要改规则（强制冻结，报告里会列出）。"""
    root = Path(root)
    attrs = schema.read_rows(root / "rubric" / "attributes.csv")
    inc = schema.read_rows(root / "rubric" / "incompatible.csv")
    lines = ["确认点 2：属性定义与相容性表（确认后冻结，之后不再增删属性）", "", f"属性（{len(attrs)} 个）："]
    for a in attrs:
        lines.append(f"  {a['id']} {a.get('name', '')}：{a.get('question', '')}")
        lines.append(f"      ✓ 标准：{a.get('check_criterion', '')}")
        if a.get("partial_criterion"):
            lines.append(f"      △ 标准：{a['partial_criterion']}")
    lines += ["", f"不相容的属性对（{len(inc)} 对）："]
    lines += [f"  {r['attr_a']} × {r['attr_b']}：{r.get('reason', '')}" for r in inc]
    if change_reason:
        lines += ["", f"⚠ 这是冻结后的修改，理由：{change_reason}。这条修改会写进报告。"]
    show("\n".join(lines))
    _ask("以上属性和相容性表确认无误？", interactive, ask)
    lock = rubric.freeze(root, force=bool(change_reason), reason=change_reason)
    data = load(root)
    data["2"] = {"at": datetime.now().isoformat(timespec="seconds"), "note": note,
                 "rubric_files": lock["files"], "forced": bool(change_reason)}
    _save(root, data)
    return data["2"]


def lock_candidates(root: Path) -> dict:
    data = load(root)
    if "candidates" not in data:
        data["candidates"] = {"at": datetime.now().isoformat(timespec="seconds"), **snapshot_candidates(root)}
        _save(root, data)
    return data["candidates"]
