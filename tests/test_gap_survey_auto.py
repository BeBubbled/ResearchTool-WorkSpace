"""演化树自动模式：用假的 Claude 按阶段写入合格数据，检查编排、关卡反馈、代确认记录和停止（不联网、不耗额度）。"""

from __future__ import annotations

import json
import shutil
import tempfile
import threading
import unittest
from pathlib import Path

from flask import Flask

from researchtool.gap_survey import GapSurveyService, create_gap_survey_blueprint, gapr_available

ROOT = Path(__file__).resolve().parents[1]

if gapr_available()[0]:
    from researchtool.gapr import checks, confirm, schema
    from researchtool.gapr import project as prj
    from tests.test_gapr_workflow import FIX, build_step1, build_step2_3, evidence_rows, write


def build_step5(root: Path) -> None:
    rows, _ = checks.compute_mechanical(checks.Ctx(prj.load(root)))
    schema.write_rows(root / "gaps_mechanical.csv", schema.MECHANICAL_COLUMNS, rows)
    by = {r["cand_id"]: r for r in rows}
    write(root, "gaps.csv", [{"id": "G01", "statement": "让 token 带上局部语义", "merged_from": "C01", "rules": "R1",
                              "partial_coverage": by["C01"]["partial_coverage"], "demand": "注意力无从对齐",
                              "splice_test": "拼接无效", "category": "疑似真空白"}])
    write(root, "filtered.csv", [{"id": f"F-{cid}", "statement": r["statement"], "merged_from": cid, "stage": "需求过滤",
                                  "reason": "测试"} for cid, r in by.items() if cid != "C01"])


def build_step6_backtest(root: Path) -> None:
    confirm.lock_candidates(root)
    gaps = schema.read_rows(root / "gaps.csv")
    for g in gaps:
        g.update(verdict="未命中", closest_work="PatchTST（测试）", independent_review="未复核")
    write(root, "gaps.csv", gaps)
    schema.append_row(root / "search_log.csv", schema.TABLES["search_log.csv"],
                      {"ts": "2026-11-21T00:00:00", "query": "patch tokens", "tool": "arxiv", "for_gap": "G01",
                       "kind": "phrasing"})


class FakeClaude:
    """按会话名里的阶段标题写入对应步骤的数据；确认点 1 第一次审核不通过，用来走一遍“意见交回修改”。"""

    def __init__(self, *, block: threading.Event | None = None):
        self.calls: list[dict] = []
        self.reviews: list[str] = []
        self.block = block

    def status(self, **_):
        return {"plan": True}

    def run_agent(self, system, user, *, cwd, model, effort, session_id, resume, allowed, disallowed,
                  extra_env=None, name="", timeout=None, on_event=None, permission_mode=None):
        root = Path(cwd)
        self.calls.append({"name": name, "resume": resume, "session": session_id, "user": user,
                           "allowed": allowed, "disallowed": disallowed, "env": extra_env, "system": system,
                           "mode": permission_mode})
        if on_event:
            on_event({"kind": "tool", "name": "Bash", "input": {"command": "gapr check . --step 1"}})
        if self.block is not None:
            while not self.block.wait(0.05):
                on_event and on_event({"kind": "text", "text": "工作中"})
        if "综述与节点清单" in name and not (root / "families.csv").read_text(encoding="utf-8-sig").count("\n") > 1:
            build_step1(root)
        elif "方法节点表" in name:
            build_step2_3(root)
        elif "归纳属性" in name:
            shutil.copy(FIX / "attributes.csv", root / "rubric" / "attributes.csv")
        elif "填属性矩阵" in name:
            shutil.copy(FIX / "matrix.csv", root / "matrix.csv")
            write(root, "matrix_evidence.csv", evidence_rows())
        elif "列出候选空白" in name:
            build_step5(root)
        elif "逐条验证" in name:
            build_step6_backtest(root)
        elif "生成报告" in name:
            (root / "report.md").write_text("# 报告\n\n" + "测试内容。" * 200, encoding="utf-8")
        return "完成"

    def run(self, messages, model, effort="high", on_text=None):
        text = messages[-1]["content"]
        self.reviews.append(text)
        if "节点清单" in text and sum("节点清单" in r for r in self.reviews) == 1:
            return '{"approve": false, "summary": "少了一个方法族", "problems": ["补充 PatchTST"]}'
        return '好的：{"approve": true, "summary": "可以", "problems": []}'


@unittest.skipUnless(gapr_available()[0], "gapr 需要 Python 3.11+")
class AutoModeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.fake = FakeClaude()
        self.service = GapSurveyService(Path(self.temp.name) / "projects", workdir=ROOT, claude=self.fake)
        app = Flask(__name__)
        app.register_blueprint(create_gap_survey_blueprint(self.service))
        self.client = app.test_client()
        self.h = {"X-Gap-Survey": "1"}

    def tearDown(self) -> None:
        for flag in self.service.auto.stop_flags.values():
            flag.set()
        if self.fake.block:
            self.fake.block.set()
        for thread in self.service.auto.threads.values():
            thread.join(10)
        self.temp.cleanup()

    def create(self, **extra):
        data = {"id": "auto-test", "domain": "长期时序预测", "mode": "backtest", "start": "2017-01-01",
                "cutoff": "2022-06-30", "scale": "mini", "auto": "1", **extra}
        res = self.client.post("/api/gap-survey/projects", json=data, headers=self.h)
        self.assertEqual(res.status_code, 200, res.get_json())
        return res.get_json()["project"]

    def run_to_end(self, pid="auto-test"):
        res = self.client.post(f"/api/gap-survey/projects/{pid}/auto/start", headers=self.h,
                               json={"model": "sonnet", "effort": "low", "acknowledged": True})
        self.assertEqual(res.status_code, 200, res.get_json())
        self.service.auto.threads[pid].join(60)
        return self.client.get(f"/api/gap-survey/projects/{pid}/auto").get_json()["auto"]

    def test_full_run_records_claude_confirmations(self) -> None:
        project = self.create()
        self.assertTrue(project["auto"]["enabled"])
        auto = self.run_to_end()
        self.assertEqual(auto["status"], "done", auto["message"])
        root = self.service.project_dir("auto-test")
        conf = confirm.load(root)
        self.assertEqual((conf["1"]["by"], conf["2"]["by"]), ("claude-auto", "claude-auto"))
        self.assertIn("审核意见", conf["1"]["review"])
        self.assertTrue((root / "rubric.lock.json").exists())  # 确认点 2 冻结了规则
        self.assertTrue((root / "report.md").exists())
        # 确认点 1 第一次不通过：意见交回第 1 步的会话（resume）修改
        fix = [c for c in self.fake.calls if c["resume"] and "审核者" in c["user"]]
        self.assertEqual(len(fix), 1)
        self.assertIn("补充 PatchTST", fix[0]["user"])
        step1_session = next(c["session"] for c in self.fake.calls if "综述与节点清单" in c["name"] and not c["resume"])
        self.assertEqual(fix[0]["session"], step1_session)
        # 工具边界：Bash 只能 gapr，禁止 gapr confirm；gapr 指向本仓库
        for call in self.fake.calls:
            self.assertEqual(call["allowed"], ["Bash(gapr:*)", "Write(./**)", "Edit(./**)"])
            self.assertEqual(call["mode"], "dontAsk")
            self.assertIn("Bash(gapr confirm:*)", call["disallowed"])
            self.assertIn(str(ROOT), call["env"]["PYTHONPATH"])
        self.assertIn("不要运行 `gapr confirm`", self.fake.calls[0]["system"])
        # 回测：第 7 步和独立复核跳过
        skipped = {p["key"] for p in auto["phases"] if p["skipped"]}
        self.assertEqual(skipped, {"review6", "step7"})
        self.assertTrue(all(p["done"] for p in auto["phases"]))
        self.assertTrue(any(e["kind"] == "tool" and "gapr check" in e["text"] for e in auto["log"]))
        # 检查结果里一直标着“由 Claude 代为确认”
        res = checks.run(prj.load(root))
        warns = [str(i) for r in res for i in r.issues]
        self.assertTrue(any("由 Claude 代为确认" in w and "确认点 1" in w for w in warns))
        self.assertTrue(any("由 Claude 代为确认" in w and "确认点 2" in w for w in warns))
        overview = self.client.get("/api/gap-survey/projects/auto-test").get_json()["project"]
        self.assertTrue(all(c["byClaude"] for c in overview["confirmations"]))
        self.assertIn("decisions.md", [n for n, _ in __import__("researchtool.gap_survey", fromlist=["x"]).VIEWABLE_FILES])

    def test_start_requires_enable_and_acknowledgement(self) -> None:
        self.create(auto="")
        url = "/api/gap-survey/projects/auto-test/auto"
        self.assertFalse(self.client.get(url).get_json()["auto"]["enabled"])
        no_ack = self.client.post(url + "/start", headers=self.h, json={"model": "opus", "effort": "high"})
        self.assertEqual(no_ack.status_code, 400)
        not_enabled = self.client.post(url + "/start", headers=self.h, json={"acknowledged": True})
        self.assertIn("没有开启自动模式", not_enabled.get_json()["error"])
        self.assertEqual(self.client.post(url + "/enable", json={"enabled": True}).status_code, 403)  # 缺请求标识
        self.assertTrue(self.client.post(url + "/enable", headers=self.h, json={"enabled": True}).get_json()["auto"]["enabled"])
        # 状态存在项目目录之外：Claude 会话改不到
        state_file = self.service.auto._path("auto-test")
        self.assertTrue(state_file.exists())
        self.assertNotIn(self.service.project_dir("auto-test").resolve(), state_file.resolve().parents)

    def test_missing_domain_pauses(self) -> None:
        self.create(domain="")
        auto = self.run_to_end()
        self.assertEqual(auto["status"], "paused")
        self.assertIn("领域", auto["message"])
        self.assertEqual(self.fake.calls, [])

    def test_stop_and_manual_commands_blocked_while_running(self) -> None:
        self.fake.block = threading.Event()
        self.create()
        url = "/api/gap-survey/projects/auto-test"
        started = self.client.post(url + "/auto/start", headers=self.h, json={"acknowledged": True})
        self.assertTrue(started.get_json()["auto"]["running"])
        run = self.client.post(url + "/run", headers=self.h, json={"command": "gaps"})
        self.assertIn("自动运行进行中", run.get_json()["error"])
        self.client.post(url + "/auto/stop", headers=self.h)
        self.service.auto.threads["auto-test"].join(10)
        auto = self.client.get(url + "/auto").get_json()["auto"]
        self.assertEqual(auto["status"], "stopped")

    def test_check_errors_are_sent_back_to_the_same_session(self) -> None:
        fake = self.fake
        original = fake.run_agent
        state = {"step2": 0}

        def sloppy(system, user, **kw):
            if "方法节点表" in kw["name"]:
                state["step2"] += 1
                root = Path(kw["cwd"])
                fake.calls.append({"name": kw["name"], "resume": kw["resume"], "session": kw["session_id"], "user": user,
                                   "allowed": kw["allowed"], "disallowed": kw["disallowed"], "env": kw["extra_env"],
                                   "system": system})
                build_step2_3(root)
                if state["step2"] == 1:  # 第一轮漏写一个节点的缺陷
                    rows = [d for d in schema.read_rows(root / "defects.csv") if d["node_id"] != "tft"]
                    write(root, "defects.csv", rows)
                return "完成"
            return original(system, user, **kw)

        fake.run_agent = sloppy
        self.create()
        auto = self.run_to_end()
        self.assertEqual(auto["status"], "done", auto["message"])
        step2 = [c for c in fake.calls if "方法节点表" in c["name"]]
        self.assertEqual([c["resume"] for c in step2], [False, True])
        self.assertEqual(step2[0]["session"], step2[1]["session"])
        self.assertIn("节点 tft", step2[1]["user"])
        self.assertTrue(any(e["kind"] == "check" for e in auto["log"]))

    def test_phase_that_never_passes_pauses(self) -> None:
        original = self.fake.run_agent

        def lazy(system, user, **kw):
            if "方法节点表" in kw["name"]:
                return "我觉得做完了"
            return original(system, user, **kw)

        self.fake.run_agent = lazy
        self.create()
        auto = self.run_to_end()
        self.assertEqual(auto["status"], "paused")
        self.assertEqual(auto["phase"], "step2")
        self.assertIn("仍有", auto["message"])

    def test_review_reply_parsing(self) -> None:
        from researchtool.gap_survey_auto import parse_review
        self.assertTrue(parse_review('```json\n{"approve": true, "summary": "ok"}\n```')["approve"])
        bad = parse_review("看起来不错")
        self.assertFalse(bad["approve"])
        self.assertTrue(bad["unparsed"])

    def test_interrupted_state_after_restart(self) -> None:
        self.create()
        state = self.service.auto.load("auto-test")
        state["status"] = "running"
        self.service.auto.save("auto-test", state)
        self.assertEqual(self.service.auto.load("auto-test")["status"], "interrupted")


if __name__ == "__main__":
    unittest.main()
