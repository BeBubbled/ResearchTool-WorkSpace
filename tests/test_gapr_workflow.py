"""完整流程与逐步关卡：先搭一个合格的迷你回测项目，再逐项破坏，确认每道检查都能拦住。"""

import contextlib
import io
import shutil
import tempfile
import unittest
from pathlib import Path

from researchtool.gapr import checks, cli, confirm, rubric, schema
from researchtool.gapr import project as prj
from researchtool.gapr.fulltext import FulltextStore

FIX = Path(__file__).parent / "fixtures" / "gapr_ltsf_2022"

FAMILIES = {"概率与协变量": ["deepar", "tft"], "MLP": ["nbeats", "nhits", "dlinear"],
            "Transformer": ["informer", "pyraformer", "autoformer", "fedformer"], "分布漂移": ["revin", "nst"]}
NODES = [  # id, 名称, 首次公开, 依据, arXiv id
    ("deepar", "DeepAR", "2017-04-13", "arXiv v1", "1704.04110"),
    ("tft", "TFT", "2019-12-19", "arXiv v1", "1912.09363"),
    ("nbeats", "N-BEATS", "2019-05-24", "arXiv v1", "1905.10437"),
    ("nhits", "N-HiTS", "2022-01-30", "arXiv v1", "2201.12886"),
    ("dlinear", "DLinear", "2022-05-26", "arXiv v1", "2205.13504"),
    ("informer", "Informer", "2020-12-14", "arXiv v1", "2012.07436"),
    ("pyraformer", "Pyraformer", "2022-01-28", "publicationDate", ""),
    ("autoformer", "Autoformer", "2021-06-24", "arXiv v1", "2106.13008"),
    ("fedformer", "FEDformer", "2022-01-30", "arXiv v1", "2201.12740"),
    ("revin", "RevIN", "2022-01-28", "publicationDate", ""),
    ("nst", "Non-stationary Transformer", "2022-05-28", "arXiv v1", "2205.14415"),
]
CHECKMARKS = [("DeepAR", "P"), ("TFT", "P"), ("N-BEATS / N-HiTS", "H"), ("Informer / Pyraformer", "H"),
              ("Autoformer / FEDformer", "H"), ("RevIN", "D"), ("RevIN", "H"),
              ("Non-stationary Transformer", "D"), ("Non-stationary Transformer", "H"),
              ("DLinear", "L"), ("DLinear", "H")]


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


def yes(*_):
    return "确认"


def write(root, name, rows):
    cols = schema.TABLES.get(name) or schema.RUBRIC_TABLES[Path(name).name]
    schema.write_rows(root / name, cols, rows)


def build_step1(root: Path):
    fam_of = {n: f for f, ns in FAMILIES.items() for n in ns}
    write(root, "families.csv", [{"family": f, "representative_papers": "、".join(ns),
                                  "from_survey": "survey A", "source_tag": "已有知识"} for f, ns in FAMILIES.items()])
    write(root, "nodes.csv", [{"id": i, "name": n, "first_public": d, "date_basis": b, "arxiv_id": a,
                               "url": f"https://example.org/{i}", "family": fam_of[i], "source_tag": "已有知识"}
                              for i, n, d, b, a in NODES])
    write(root, "neighbor_terms.csv", [{"term": "预测区间", "neighbor_field": "统计学",
                                        "neighbor_term": "conformal prediction", "added_by": "Agent",
                                        "source_tag": "已有知识"}])
    schema.append_row(root / "search_log.csv", schema.TABLES["search_log.csv"],
                      {"ts": "2026-11-21T00:00:00", "query": "long-term forecasting survey", "tool": "s2"})


def build_step2_3(root: Path):
    rows = schema.read_rows(root / "nodes.csv")
    for r in rows:
        r["problem"], r["core_idea"] = "某个问题", "某个思路"
    write(root, "nodes.csv", rows)
    write(root, "defects.csv", [{"id": f"d_{i}", "node_id": i, "defect": f"{n} 的缺陷", "kind": "自述",
                                 "source_tag": "已有知识"} for i, n, *_ in NODES])
    write(root, "edges.csv", [{"parent_id": "revin", "defect_id": "d_revin", "child_id": "nst",
                               "source_tag": "已有知识"}])


def evidence_rows():
    return [{"method": m, "attr": a, "value": "✓", "evidence": "实验表", "source_tag": "已有知识"}
            for m, a in CHECKMARKS]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "ltsf"
        prj.init(self.root, domain="LTSF", mode="backtest", start="2017-01-01", cutoff="2022-06-30",
                 scale="mini", audience="组会")
        self.p = prj.load(self.root)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def errors(self, step):
        return [str(i) for i in checks.run(prj.load(self.root), step)[0].errors]

    def to_step3(self):
        build_step1(self.root)
        confirm.confirm_1(self.root, interactive=lambda: True, ask=yes, show=lambda s: None)
        build_step2_3(self.root)

    def to_step4(self):
        self.to_step3()
        shutil.copy(FIX / "attributes.csv", self.root / "rubric" / "attributes.csv")
        confirm.confirm_2(self.root, interactive=lambda: True, ask=yes, show=lambda s: None)
        shutil.copy(FIX / "matrix.csv", self.root / "matrix.csv")
        write(self.root, "matrix_evidence.csv", evidence_rows())


class TestConfirm(Base):
    def test_model_cannot_confirm(self):
        build_step1(self.root)
        code, _, err = run("confirm", str(self.root), "1")  # 测试里 stdin 不是终端
        self.assertEqual(code, 1)
        self.assertIn("终端", err)
        self.assertEqual(confirm.load(self.root), {})

    def test_cancel_writes_nothing(self):
        build_step1(self.root)
        with self.assertRaises(confirm.ConfirmError):
            confirm.confirm_1(self.root, interactive=lambda: True, ask=lambda _: "不", show=lambda s: None)
        self.assertEqual(confirm.load(self.root), {})

    def test_confirm1_requires_valid_step1(self):
        code, _, err = run("confirm", str(self.root), "1")
        self.assertEqual(code, 1)
        self.assertIn("第 1 步", err)


class TestStep1(Base):
    def test_unconfirmed_is_error(self):
        build_step1(self.root)
        errs = self.errors(1)
        self.assertEqual(len(errs), 1, errs)
        self.assertIn("确认点 1", errs[0])

    def test_passes_after_confirm(self):
        build_step1(self.root)
        confirm.confirm_1(self.root, interactive=lambda: True, ask=yes, show=lambda s: None)
        self.assertEqual(self.errors(1), [])

    def test_node_added_after_confirm(self):
        self.to_step3()
        rows = schema.read_rows(self.root / "nodes.csv")
        rows.append({**rows[0], "id": "patchtst", "name": "PatchTST"})
        write(self.root, "nodes.csv", rows)
        self.assertTrue(any("新增 ['patchtst']" in e for e in self.errors(1)))

    def test_backtest_date_after_cutoff(self):
        build_step1(self.root)
        rows = schema.read_rows(self.root / "nodes.csv")
        rows[0]["first_public"] = "2022-11-27"
        write(self.root, "nodes.csv", rows)
        self.assertTrue(any("晚于回测截止日" in e for e in self.errors(1)))

    def test_family_needs_two_and_survey_search(self):
        build_step1(self.root)
        rows = [r for r in schema.read_rows(self.root / "nodes.csv") if r["id"] != "tft"]
        write(self.root, "nodes.csv", rows)
        (self.root / "search_log.csv").unlink()
        errs = self.errors(1)
        self.assertTrue(any("只有 1 个节点" in e for e in errs))
        self.assertTrue(any("综述" in e for e in errs))


class TestStep2Quotes(Base):
    def test_claimed_quote_must_be_found(self):
        self.to_step3()
        rows = schema.read_rows(self.root / "defects.csv")
        rows[0].update(source_tag="论文原文", quote="we only consider short horizons in this work")
        write(self.root, "defects.csv", rows)
        self.assertTrue(any("没有登记的全文" in e for e in self.errors(2)))
        FulltextStore(self.root).add("arXiv:1704.04110", "Limitations. We only consider short horizons "
                                     "in this work and leave long ones.", "https://arxiv.org/abs/1704.04110", "test")
        self.assertEqual(self.errors(2), [])
        rows[0]["quote"] = "we fully solve long horizons in this work"
        write(self.root, "defects.csv", rows)
        self.assertTrue(any("原文中找不到" in e for e in self.errors(2)))

    def test_tampered_evidence(self):
        self.to_step3()
        path = FulltextStore(self.root).add("arXiv:1704.04110", "some text here", "u", "test")
        path.write_text("changed", encoding="utf-8")
        self.assertTrue(any("被改动" in e for e in self.errors(2)))


class TestStep3(Base):
    def test_defect_must_belong_to_parent(self):
        self.to_step3()
        write(self.root, "edges.csv", [{"parent_id": "revin", "defect_id": "d_deepar", "child_id": "nst",
                                        "source_tag": "已有知识"}])
        self.assertTrue(any("不是父节点" in e for e in self.errors(3)))


class TestGatesAndGaps(Base):
    def test_matrix_and_gaps_refused_before_confirm2(self):
        self.to_step3()
        shutil.copy(FIX / "attributes.csv", self.root / "rubric" / "attributes.csv")
        self.assertEqual(run("matrix-template", str(self.root))[0], 1)
        self.assertEqual(run("gaps", str(self.root))[0], 1)

    def test_full_flow_reproduces_backtest(self):
        self.to_step4()
        for s in range(5):
            self.assertEqual(self.errors(s), [], f"第 {s} 步")
        code, out, err = run("gaps", str(self.root))
        self.assertEqual(code, 0, err)
        rows = schema.read_rows(self.root / "gaps_mechanical.csv")
        r12 = [(r["rule"], r["attrs"]) for r in rows if r["rule"] in ("R1", "R2", "R2′")]
        self.assertEqual(r12, [("R1", "S"), ("R1", "V"), ("R1", "Z"),
                               ("R2", "L×D"), ("R2", "L×P"), ("R2", "D×P"), ("R2", "P×H")])
        r3 = [r["ref"] for r in rows if r["rule"] == "R3"]
        self.assertEqual(len(r3), 10)  # 11 个缺陷里只有 d_revin 有子方法
        self.assertNotIn("d_revin", r3)

    def test_missing_evidence_blocks_gaps(self):
        self.to_step4()
        write(self.root, "matrix_evidence.csv", evidence_rows()[:-1])
        code, _, err = run("gaps", str(self.root))
        self.assertEqual(code, 1)
        self.assertIn("没有依据", err)

    def test_rubric_change_after_confirm2(self):
        self.to_step4()
        p = self.root / "rubric" / "attributes.csv"
        p.write_text(p.read_text(encoding="utf-8-sig") + "X,新属性,?,?,?,\n", encoding="utf-8")
        self.assertTrue(any("被修改" in e for e in self.errors(4)))
        self.assertEqual(run("gaps", str(self.root))[0], 1)

    def test_reconfirm_after_matrix_needs_reason(self):
        self.to_step4()
        with self.assertRaises(rubric.RubricError):
            confirm.confirm_2(self.root, interactive=lambda: True, ask=yes, show=lambda s: None)
        confirm.confirm_2(self.root, change_reason="属性 S 定义有歧义", interactive=lambda: True,
                          ask=yes, show=lambda s: None)
        self.assertEqual(len(rubric.forced_changes(self.root)), 1)

    def test_crlf_does_not_break_lock(self):
        self.to_step4()
        p = self.root / "rubric" / "scoring.md"
        p.write_bytes(p.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
        rubric.verify(self.root)


class TestStep5(Base):
    def setUp(self):
        super().setUp()
        self.to_step4()
        run("gaps", str(self.root))
        self.mech = schema.read_rows(self.root / "gaps_mechanical.csv")
        self.r3 = [m["cand_id"] for m in self.mech if m["rule"] == "R3"]

    def _gaps(self, merged_c01="C01", rules="R1"):
        gaps = [{"id": "G01", "statement": "让 token 带上局部语义", "merged_from": merged_c01, "rules": rules,
                 "partial_coverage": "N-BEATS / N-HiTS；Autoformer / FEDformer", "demand": "注意力无从对齐",
                 "splice_test": "拼接无效：需要改 token 化方式", "category": "疑似真空白"}]
        filtered = [{"id": f"F{c}", "statement": "x", "merged_from": c, "stage": "需求过滤", "reason": "说不出需求"}
                    for c in ["C02", "C03", "C04", "C05", "C06", "C07", *self.r3]]
        write(self.root, "gaps.csv", gaps)
        write(self.root, "filtered.csv", filtered)

    def test_complete_accounting_passes(self):
        self._gaps()
        self.assertEqual(self.errors(5), [])

    def test_candidate_cannot_vanish(self):
        self._gaps()
        rows = schema.read_rows(self.root / "filtered.csv")[1:]
        write(self.root, "filtered.csv", rows)
        self.assertTrue(any("既没并入" in e and "C02" in e for e in self.errors(5)))

    def test_rules_derived_from_merged(self):
        self._gaps(rules="R1;R3")
        self.assertTrue(any("rules 应为 R1" in e for e in self.errors(5)))

    def test_stale_mechanical(self):
        self._gaps()
        rows = schema.read_rows(self.root / "edges.csv")
        rows.append({"parent_id": "deepar", "defect_id": "d_deepar", "child_id": "tft", "source_tag": "已有知识"})
        write(self.root, "edges.csv", rows)
        self.assertTrue(any("重新运行 gapr gaps" in e for e in self.errors(5)))

    def test_backtest_lock(self):
        self._gaps()
        confirm.lock_candidates(self.root)
        g = schema.read_rows(self.root / "gaps.csv")
        g[0].update(verdict="命中", filled_by="PatchTST", months_after_cutoff="5", closest_work="PatchTST")
        write(self.root, "gaps.csv", g)
        schema.append_row(self.root / "search_log.csv", schema.TABLES["search_log.csv"],
                          {"query": "patch token transformer", "tool": "arxiv", "for_gap": "G01", "kind": "phrasing"})
        self.assertEqual(self.errors(6), [])
        g[0]["statement"] = "改过的陈述"
        write(self.root, "gaps.csv", g)
        self.assertTrue(any("锁定之后" in e for e in self.errors(6)))


class TestSurveyStep6And7(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "s"
        prj.init(self.root, domain="可解释性", mode="survey", start="2020-01-01", cutoff="2026-11-21")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def step(self, n, gap):
        write(self.root, "gaps.csv", [gap])
        return [str(i) for i in checks.run(prj.load(self.root), n)[0].errors]

    def log(self, query, kind, tool="s2", tf="~"):
        schema.append_row(self.root / "search_log.csv", schema.TABLES["search_log.csv"],
                          {"query": query, "tool": tool, "time_filter": tf, "for_gap": "G01", "kind": kind})

    GAP = {"id": "G01", "statement": "s", "verdict": "确认空白", "confidence": "高",
           "closest_work": "X 差在 Y", "independent_review": "已复核，未找到反例"}

    def test_confirmed_gap_needs_searches(self):
        errs = self.step(6, self.GAP)
        self.assertTrue(any("只有 0 种说法" in e for e in errs))
        self.assertTrue(any("只支持低" in e for e in errs))

    def test_confidence_levels(self):
        for q in ("a b", "c d", "e f"):
            self.log(q, "phrasing")
        self.log("g h", "phrasing", tool="arxiv", tf="2026-01-01~")
        self.assertTrue(any("只支持低" in e for e in self.step(6, self.GAP)))
        self.log("cites:x", "forward")
        self.assertTrue(any("只支持中" in e for e in self.step(6, self.GAP)))
        self.log("conformal", "cross")
        self.assertEqual(self.step(6, self.GAP), [])

    def test_counterexample_overrides(self):
        errs = self.step(6, {**self.GAP, "confidence": "低", "independent_review": "找到反例：Z 2025"})
        self.assertTrue(any("反例" in e for e in errs))

    def test_scores(self):
        g = {**self.GAP, "score_sources": "2", "score_demand": "3", "score_splice": "1",
             "score_crowding": "2", "score_feasibility": "4", "score_total": "12"}
        errs = self.step(7, g)
        self.assertTrue(any("score_feasibility" in e for e in errs))
        g["score_feasibility"] = "3"
        self.assertTrue(any("score_total 应为 11" in e for e in self.step(7, g)))


if __name__ == "__main__":
    unittest.main()
