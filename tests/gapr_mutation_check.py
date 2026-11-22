"""变异核验：故意把某条规则的实现改坏，对应的测试必须失败。

如果把规则改坏了测试还是通过，说明这个测试并没有真正检查这条规则。
改坏只在内存里进行（unittest.mock），不会修改任何源文件。

用法（在仓库根目录）：
    python -m tests.gapr_mutation_check
全部显示“抓到”才算通过；退出码 0 = 全部抓到。
"""

from __future__ import annotations

import sys
import unittest
from unittest import mock

from researchtool.gapr import checks
from researchtool.gapr.checks import StepResult

# (编号, 改坏了什么, 要打的补丁 {目标: 替换值}, 应该失败的测试)
MUTATIONS = [
    ("M1", "把 △ 当作覆盖（违反第 5 步 R1/R2 规则）",
     {"researchtool.gapr.matrix._covers": lambda values, pc: all(v in ("Y", "P") for v in values)},
     "tests.test_gapr_matrix.TestLtsfBacktestMatrix.test_r1_columns"),
    ("M2", "冻结后不再核对规则文件（违反全程规则 4）",
     {"researchtool.gapr.rubric.verify": lambda root: {}},
     "tests.test_gapr_workflow.TestGatesAndGaps.test_rubric_change_after_confirm2"),
    ("M3", "原句核对一律放行（违反全程规则 1）",
     {"researchtool.gapr.fulltext.quote_in": lambda norm, quote: (True, "")},
     "tests.test_gapr_workflow.TestStep2Quotes.test_claimed_quote_must_be_found"),
    ("M4", "确认点不再要求用户在终端确认",
     {"researchtool.gapr.confirm._ask": lambda *a, **k: None, "researchtool.gapr.confirm._require_terminal": lambda *a, **k: None},
     "tests.test_gapr_workflow.TestConfirm.test_model_cannot_confirm"),
    ("M5", "去掉第 5 步检查（候选可以悄悄消失，违反全程规则 6）",
     # 检查函数在导入时存进了 STEPS 列表，要替换列表里的那一项才生效
     {"researchtool.gapr.checks.STEPS": [*checks.STEPS[:5], lambda c: StepResult(5, True), *checks.STEPS[6:]]},
     "tests.test_gapr_workflow.TestStep5.test_candidate_cannot_vanish"),
    ("M6", "置信度不看检索记录（违反第 6 步置信度规则）",
     {"researchtool.gapr.checks._achieved": lambda cov: 2},
     "tests.test_gapr_workflow.TestSurveyStep6And7.test_confidence_levels"),
    ("M7", "回测日期防火墙失效",
     {"researchtool.gapr.paper.after_cutoff": lambda p, cutoff: False},
     "tests.test_gapr_sources.TestFirewall.test_apply"),
    ("M8", "判为确认空白不再要求检索记录（违反全程规则 3）",
     {"researchtool.gapr.checks._search_coverage": lambda c, gid: {"phrasing": 9, "recent_arxiv": True, "forward": True,
                                                      "cross": True, "venue": True, "n": 9}},
     "tests.test_gapr_workflow.TestSurveyStep6And7.test_confirmed_gap_needs_searches"),
    ("M9", "不再要求每个 ✓ 都有依据（违反第 4 步）",
     {"researchtool.gapr.checks.STEPS": [*checks.STEPS[:4], lambda c, before_freeze=False: StepResult(4, True),
                            *checks.STEPS[5:]]},
     "tests.test_gapr_workflow.TestGatesAndGaps.test_missing_evidence_blocks_gaps"),
    ("M10", "第 6 步回测不再核对候选锁定",
     {"researchtool.gapr.confirm.snapshot_candidates": lambda root: {"gaps_hash": "", "filtered_hash": "", "n_gaps": 0}},
     "tests.test_gapr_workflow.TestStep5.test_backtest_lock"),
]


def run_one(test_id: str) -> bool:
    import io
    suite = unittest.defaultTestLoader.loadTestsFromName(test_id)
    res = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(suite)
    return res.wasSuccessful()


def main() -> int:
    for stream in (sys.stdout,):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    missed = 0
    for mid, what, patches, test_id in MUTATIONS:
        if not run_one(test_id):
            print(f"{mid} 前提不成立：{test_id} 在未改坏时就失败了")
            missed += 1
            continue
        ctxs = [mock.patch(t, v) for t, v in patches.items()]
        for c in ctxs:
            c.start()
        try:
            passed = run_one(test_id)
        finally:
            for c in ctxs:
                c.stop()
        status = "抓到" if not passed else "没抓到"
        missed += passed
        print(f"{mid} {status}：{what} → {test_id.split('.', 1)[1]}")
    print(f"\n{len(MUTATIONS) - missed}/{len(MUTATIONS)} 个变异被测试抓到")
    return 1 if missed else 0


if __name__ == "__main__":
    raise SystemExit(main())
