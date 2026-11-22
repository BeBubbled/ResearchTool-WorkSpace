"""生成一个合格的迷你回测演示项目，供人工核验（VERIFICATION.md 第 3 节）。

    python -m tests.gapr_make_demo <新目录> [--upto 4|5]

数据是 docs/backtest-ltsf-2022.md 的 LTSF 回测（11 个方法、7 个属性），来源标注都是「已有知识」。
--upto 4：做到第 4 步（属性已确认并冻结、矩阵和依据已填）；--upto 5：再把第 5 步的候选归类写好。

注意：为了能自动生成，两个确认点由本脚本代填（记录里会写明 note=演示数据）。
这只用于核验，不能用在真实调研上；脚本只会新建目录，目录已存在时拒绝运行。
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from researchtool.gapr import confirm, schema
from researchtool.gapr import project as prj
from researchtool.gapr.checks import Ctx, compute_mechanical
from tests.test_gapr_workflow import FIX, build_step1, build_step2_3, evidence_rows, write

NOTE = "演示数据：由 tests/make_demo.py 代填，仅用于核验"


def build(root: Path, upto: int) -> None:
    prj.init(root, domain="深度模型的长期时间序列预测（演示）", mode="backtest", start="2017-01-01",
             cutoff="2022-06-30", scale="mini", audience="核验者")
    build_step1(root)
    confirm.confirm_1(root, note=NOTE, interactive=lambda: True, ask=lambda _: "确认", show=lambda s: None)
    build_step2_3(root)
    shutil.copy(FIX / "attributes.csv", root / "rubric" / "attributes.csv")
    confirm.confirm_2(root, note=NOTE, interactive=lambda: True, ask=lambda _: "确认", show=lambda s: None)
    shutil.copy(FIX / "matrix.csv", root / "matrix.csv")
    write(root, "matrix_evidence.csv", evidence_rows())
    if upto < 5:
        return
    rows, _ = compute_mechanical(Ctx(prj.load(root)))
    schema.write_rows(root / "gaps_mechanical.csv", schema.MECHANICAL_COLUMNS, rows)
    by = {r["cand_id"]: r for r in rows}
    gaps = [
        {"id": "G01", "statement": "让 token 带上局部语义", "merged_from": "C01", "rules": "R1",
         "partial_coverage": by["C01"]["partial_coverage"], "demand": "单个数值没有含义，注意力无从对齐",
         "splice_test": "拼接无效：需要改变 token 的构成方式", "category": "疑似真空白"},
        {"id": "G02", "statement": "显式建模变量间依赖", "merged_from": "C02", "rules": "R1",
         "partial_coverage": by["C02"]["partial_coverage"], "demand": "多变量预测的本意就是利用变量间关系",
         "splice_test": "拼接无效：通道独立与通道混合互相冲突", "category": "疑似真空白"},
    ]
    filtered = [{"id": f"F-{cid}", "statement": r["statement"], "merged_from": cid, "stage": "需求过滤",
                 "reason": "演示：未展开"} for cid, r in by.items() if cid not in ("C01", "C02")]
    write(root, "gaps.csv", gaps)
    write(root, "filtered.csv", filtered)


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("dir")
    ap.add_argument("--upto", type=int, choices=[4, 5], default=4)
    a = ap.parse_args()
    root = Path(a.dir)
    if root.exists():
        print(f"{root} 已存在，演示脚本只新建目录", file=sys.stderr)
        return 1
    build(root, a.upto)
    print(f"已生成演示项目 {root}（做到第 {a.upto} 步）。下一步：python -m researchtool.gapr check {root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
