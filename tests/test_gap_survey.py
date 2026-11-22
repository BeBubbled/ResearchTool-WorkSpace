"""演化树调研：项目、检查、文件查看、命令白名单和子进程运行（不联网）。"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from flask import Flask

from researchtool.gap_survey import GapSurveyService, create_gap_survey_blueprint, gapr_available

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(gapr_available()[0], "gapr 需要 Python 3.11+")
class GapSurveyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.service = GapSurveyService(Path(self.temp.name) / "projects", workdir=ROOT)
        app = Flask(__name__)
        app.register_blueprint(create_gap_survey_blueprint(self.service))
        self.client = app.test_client()
        self.headers = {"X-Gap-Survey": "1"}

    def tearDown(self) -> None:
        for job in self.service.jobs.values():
            if job.running and job.process:
                job.process.kill()
                job.process.wait()
        self.temp.cleanup()

    def create(self, **extra):
        data = {"id": "ltsf-测试", "domain": "长期时序预测", "mode": "backtest", "start": "2017-01-01", "cutoff": "2022-06-30", "scale": "mini", **extra}
        return self.client.post("/api/gap-survey/projects", json=data, headers=self.headers)

    def wait(self, job_id: str) -> dict:
        for _ in range(300):
            job = self.client.get(f"/api/gap-survey/jobs/{job_id}").get_json()["job"]
            if not job["running"]:
                return job
            time.sleep(0.1)
        self.fail("命令超时")

    def test_create_list_and_overview(self) -> None:
        self.assertEqual(self.client.post("/api/gap-survey/projects", json={"id": "x"}).status_code, 403)
        created = self.create()
        self.assertEqual(created.status_code, 200, created.get_json())
        project = created.get_json()["project"]
        self.assertEqual((project["mode"], project["cutoff"], project["scale"]), ("backtest", "2022-06-30", "mini"))
        self.assertEqual([s["step"] for s in project["steps"]], list(range(8)))
        self.assertTrue(project["steps"][0]["passed"])
        self.assertEqual([c["point"] for c in project["confirmations"]], ["1", "2"])
        self.assertIn("-m researchtool.gapr confirm", project["confirmations"][0]["command"])
        self.assertFalse(project["confirmations"][0]["confirmedAt"])
        self.assertIn("nodes.csv", [f["name"] for f in project["files"]])
        listed = self.client.get("/api/gap-survey/projects").get_json()["projects"]
        self.assertEqual([p["id"] for p in listed], ["ltsf-测试"])
        self.assertEqual(self.create().status_code, 400)  # 重名
        self.assertEqual(self.create(id="../evil").status_code, 400)
        self.assertEqual(self.create(id="bt2", cutoff="").status_code, 400)  # 回测必须有截止日
        self.assertEqual(self.create(id="bt3", domain='a"b').status_code, 400)  # 会破坏 project.toml

    def test_edit_project_inputs(self) -> None:
        self.create(start="")
        url = "/api/gap-survey/projects/ltsf-测试"
        before = self.client.get(url).get_json()["project"]
        self.assertFalse(before["steps"][0]["passed"])  # 没填起始时间
        self.assertEqual(self.client.put(url, json={"start": "2017-01-01"}).status_code, 403)  # 缺请求标识
        saved = self.client.put(url, headers=self.headers, json={
            "start": "2017-01-01", "audience": "组会", "include": "只看深度模型", "scale": "quick"})
        self.assertEqual(saved.status_code, 200, saved.get_json())
        project = saved.get_json()["project"]
        self.assertTrue(project["steps"][0]["passed"])
        self.assertEqual((project["start"], project["audience"], project["include"], project["scale"]),
                         ("2017-01-01", "组会", "只看深度模型", "quick"))
        toml = (self.service.project_dir("ltsf-测试") / "project.toml").read_text(encoding="utf-8")
        self.assertIn('start = "2017-01-01"          # 时间范围起点', toml)  # 注释保留
        self.assertIn("[citation_filter]", toml)
        bad = [{"start": "2017/01/01"}, {"domain": 'a"b'}, {"scale": "huge"}, {"mode": "survey"}, {"cutoff": ""}]
        for body in bad:
            self.assertEqual(self.client.put(url, headers=self.headers, json=body).status_code, 400, body)
        # 回测：检索过之后截止日锁定
        self.assertEqual(self.client.put(url, headers=self.headers, json={"cutoff": "2022-01-31"}).status_code, 200)
        (self.service.project_dir("ltsf-测试") / "search_log.csv").write_text("ts,query\n1,x\n", encoding="utf-8")
        project = self.client.get(url).get_json()["project"]
        self.assertIn("不能再改", project["cutoffLocked"])
        locked = self.client.put(url, headers=self.headers, json={"cutoff": "2022-06-30"})
        self.assertEqual(locked.status_code, 400)
        self.assertEqual(self.client.put(url, headers=self.headers, json={"cutoff": "2022-01-31", "audience": "导师"}).status_code, 200)
        self.assertEqual(self.client.get(url).get_json()["project"]["cutoff"], "2022-01-31")

    def test_file_view_is_whitelisted(self) -> None:
        self.create()
        url = "/api/gap-survey/projects/ltsf-测试/file?name="
        table = self.client.get(url + "nodes.csv").get_json()
        self.assertEqual((table["kind"], table["total"]), ("table", 0))
        self.assertEqual(table["columns"][:3], ["id", "name", "first_public"])
        toml = self.client.get(url + "project.toml").get_json()
        self.assertIn('mode = "backtest"', toml["text"])
        self.assertEqual(self.client.get(url + "../../x.csv").status_code, 400)
        self.assertEqual(self.client.get(url + "confirmations.json").status_code, 400)
        self.assertEqual(self.client.get("/api/gap-survey/projects/nope/file?name=nodes.csv").status_code, 404)

    def test_argv_whitelist(self) -> None:
        self.create()
        path = self.service.project_dir("ltsf-测试")
        argv = self.service.build_argv(path, "search", {"query": "long horizon forecasting", "tool": "openalex",
                                                        "limit": 5, "to": "2022-01-01", "postCutoff": True})
        self.assertEqual(argv[:3], ["search", str(path.resolve()), "long horizon forecasting"])
        self.assertIn("--tool=openalex", argv)
        self.assertIn("--limit=5", argv)
        self.assertIn("--post-cutoff", argv)
        alias = self.service.build_argv(path, "contexts", {"id": "arXiv:2205.13504", "alias": "DLinear, LTSF-Linear"})
        self.assertEqual([a for a in alias if a.startswith("--alias")], ["--alias=DLinear", "--alias=LTSF-Linear"])
        with self.assertRaises(ValueError):
            self.service.build_argv(path, "confirm", {})
        with self.assertRaises(ValueError):
            self.service.build_argv(path, "search", {"query": "--help"})
        with self.assertRaises(ValueError):
            self.service.build_argv(path, "search", {"query": "x", "tool": "google"})
        with self.assertRaises(ValueError):
            self.service.build_argv(path, "search", {"query": "x", "from": "yesterday"})
        with self.assertRaises(ValueError):
            self.service.build_argv(path, "quote", {"key": "arXiv:1", "text": "line\nbreak"})
        refused = self.client.post("/api/gap-survey/projects/ltsf-测试/run", json={"command": "confirm", "args": {}}, headers=self.headers)
        self.assertEqual(refused.status_code, 400)

    def test_gate_runs_in_subprocess(self) -> None:
        self.create()
        started = self.client.post("/api/gap-survey/projects/ltsf-测试/run", json={"command": "gaps"}, headers=self.headers)
        self.assertEqual(started.status_code, 200, started.get_json())
        job = self.wait(started.get_json()["job"]["id"])
        self.assertEqual(job["returncode"], 1)
        self.assertIn("第 1 步", job["output"])  # 关卡：前面的步骤还没做
        jobs = self.client.get("/api/gap-survey/projects/ltsf-测试/jobs").get_json()["jobs"]
        self.assertEqual(jobs[0]["command"], "gaps")

    def test_quote_without_evidence(self) -> None:
        self.create()
        started = self.client.post("/api/gap-survey/projects/ltsf-测试/run", headers=self.headers, json={
            "command": "quote", "args": {"key": "arXiv:2205.13504", "text": "an embarrassingly simple one-layer linear model"}})
        job = self.wait(started.get_json()["job"]["id"])
        self.assertEqual(job["returncode"], 1)
        self.assertIn("没有登记的全文", job["output"])


if __name__ == "__main__":
    unittest.main()
