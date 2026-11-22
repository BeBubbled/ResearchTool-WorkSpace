"""项目目录里每个 CSV 的列定义，以及读写 CSV 的统一入口。

列名用英文，便于代码稳定引用；每列的中文含义写在 COLUMN_DOCS 里，
报告渲染和 README 都从这里取。CSV 一律用带 BOM 的 UTF-8 写出，
这样 Windows 上的 Excel 直接打开不会乱码。
"""

from __future__ import annotations

import csv
from pathlib import Path

ENCODING_WRITE = "utf-8-sig"
ENCODING_READ = "utf-8-sig"  # 同时兼容有 BOM 和无 BOM

# 文件名 -> 列。矩阵 matrix.csv 的列由属性决定，不在这里。
TABLES: dict[str, list[str]] = {
    # 第 1 步
    "families.csv": ["family", "representative_papers", "from_survey", "source_tag"],
    "nodes.csv": [
        "id", "name", "first_public", "date_basis", "url", "arxiv_id", "s2_id",
        "family", "problem", "core_idea", "key_assumptions", "improved_by",
        "credibility", "source_tag",
    ],
    "neighbor_terms.csv": ["term", "neighbor_field", "neighbor_term", "added_by", "source_tag"],
    # 第 2–3 步
    "defects.csv": ["id", "node_id", "defect", "kind", "quote", "source_ref", "source_tag"],
    "edges.csv": ["parent_id", "defect_id", "child_id", "evidence_quote", "evidence_loc", "source_tag"],
    "forks.csv": ["defect_id", "child_ids", "difference"],
    # R4 / R5：争议、停滞、综述开放问题，由 Agent 判断后填入
    "open_problems.csv": ["id", "rule", "statement", "source_ref", "source_tag"],
    # 第 4 步（matrix.csv 另行生成）
    "matrix_evidence.csv": ["method", "attr", "value", "evidence", "source_tag"],
    # 第 5–7 步
    "gaps.csv": [
        "id", "statement", "merged_from", "rules", "partial_coverage", "demand",
        "splice_test", "category", "closest_work", "verdict", "confidence",
        "independent_review", "filled_by", "months_after_cutoff",
        "score_sources", "score_demand", "score_splice", "score_crowding",
        "score_feasibility", "score_total", "next_step",
    ],
    "filtered.csv": ["id", "statement", "merged_from", "stage", "reason"],
    # 全程
    "search_log.csv": [
        "ts", "query", "tool", "time_filter", "n_results", "n_dropped_cutoff",
        "result_ids", "relevant", "from_cache", "error", "for_gap", "kind",
    ],
}

# 规则文件：第 4 步确认点 2 之后冻结，之后不再修改。
RUBRIC_TABLES: dict[str, list[str]] = {
    "attributes.csv": ["id", "name", "question", "check_criterion", "partial_criterion", "derived_from"],
    "incompatible.csv": ["attr_a", "attr_b", "reason"],
}

# 机械生成的候选空白（gapr gaps 的输出，不要手改）
MECHANICAL_COLUMNS = ["cand_id", "rule", "attrs", "statement", "partial_coverage", "ref"]

COLUMN_DOCS: dict[str, str] = {
    "source_tag": "来源类型：检索核对 / 论文原文 / 已有知识",
    "first_public": "首次公开日期（arXiv v1 优先）",
    "date_basis": "日期依据：arXiv v1 / publicationDate / year",
    "kind": "缺陷来源：自述 / 他人指出 / 审稿",
    "rule": "命中规则：R1 / R2 / R2′ / R3 / R4 / R5",
    "for_gap": "这次检索是为哪条候选空白做的（第 6 步）",
    "kind": "第 6 步检索类型：phrasing 不同说法 / forward 正向追踪 / venue 最新会议 / cross 跨领域",
}

SOURCE_TAGS = ("检索核对", "论文原文", "已有知识")


def read_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding=ENCODING_READ, newline="") as f:
        return [{k: (v or "").strip() for k, v in row.items() if k is not None} for row in csv.DictReader(f)]


def write_rows(path: Path, columns: list[str], rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding=ENCODING_WRITE, newline="") as f:
        w = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in columns})


def append_row(path: Path, columns: list[str], row: dict[str, object]) -> None:
    if path.exists():
        with path.open(encoding=ENCODING_READ, newline="") as f:
            header = next(csv.reader(f), [])
        if header and header != columns:  # 列定义升级过：按新列重写旧文件
            write_rows(path, columns, read_rows(path))
    new = not path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding=ENCODING_WRITE if new else "utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerow({c: row.get(c, "") for c in columns})
