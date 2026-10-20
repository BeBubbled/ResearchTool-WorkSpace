"""Integration contracts without real model turns or public network calls."""
import io
import json
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

from flask import Flask

from research_studio import MODULES, ResearchStudio, StudioRunner, create_research_studio_blueprint

ROOT = Path(__file__).resolve().parents[1]
HEADERS = {"X-ResearchStudio": "1"}
ACCOUNT = {"chatgptAuthenticated": True, "defaultModel": "configured-model", "defaultReasoningEffort": "high"}


class FakeRunner:
    def __init__(self):
        self.calls = []
        self.release = threading.Event()
        self.release.set()
        self.write_output = True

    def run(self, root, skills, prompt, model, effort, cancel, progress, **kwargs):
        self.calls.append((root, skills, prompt, model, effort))
        while not self.release.wait(0.02):
            if cancel.is_set():
                raise InterruptedError("cancelled")
        if self.write_output:
            output = root / "output"
            output.mkdir()
            (output / "report.md").write_text("# Evidence\nA retrieved paper and its limitations.", encoding="utf-8")
            (output / "preview.html").write_text("<script>alert('unsafe')</script>", encoding="utf-8")
        progress("完成文献整理")
        return "检索完成，尚需人工审核。"


class StudioTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.runner = FakeRunner()
        self.account = Mock(return_value=dict(ACCOUNT))
        self.service = ResearchStudio(self.root / "studio", self.runner, self.account,
            project_source=lambda key: {"facts": [{"review": "pending"}]} if key == "project" else None)
        self.service.doctor = Mock(return_value={"modules": [
            {"id": name, "ready": True, "skills": [name], "missing": []} for name in MODULES]})
        app = Flask(__name__, template_folder=str(ROOT / "templates"))
        app.config["TESTING"] = True
        app.register_blueprint(create_research_studio_blueprint(self.service))
        self.client = app.test_client()

    def tearDown(self):
        self.runner.release.set()
        self.service.executor.shutdown(wait=True)
        self.temp.cleanup()

    def submit(self, **extra):
        data = {"module": "paper-search", "query": "扩散模型 2025–2026", "confirmUsage": "true", **extra}
        return self.client.post("/api/researchstudio/jobs", data=data, headers=HEADERS)

    def wait(self, job_id):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            job = self.service.read(job_id)
            if job["status"] not in {"queued", "running", "cancelling"}:
                return job
            time.sleep(0.01)
        self.fail("worker did not finish")

    def test_submit_persists_snapshot_and_uses_global_model(self):
        response = self.submit(projectId="project")
        self.assertEqual(response.status_code, 202, response.json)
        job = self.wait(response.json["id"])
        self.assertEqual(job["status"], "completed")
        work, skills, prompt, model, effort = self.runner.calls[0]
        self.assertEqual((model, effort), ("configured-model", "high"))
        self.assertEqual(work.name, "work")
        self.assertFalse((work / "job.json").exists())
        self.assertIn("扩散模型 2025–2026", prompt)
        self.assertIn("pending", (work / "project-context.json").read_text())
        self.assertIn("paper-search", skills)
        restarted = ResearchStudio(self.service.root, self.runner, self.account)
        self.assertEqual(restarted.list_jobs()[0]["id"], job["id"])
        restarted.executor.shutdown(wait=True)

    def test_missing_dependencies_account_and_consent_do_not_start_model(self):
        self.assertEqual(self.submit(confirmUsage="false").status_code, 400)
        self.account.return_value = {"chatgptAuthenticated": False}
        self.assertEqual(self.submit().status_code, 400)
        self.account.return_value = ACCOUNT
        self.service.doctor.return_value["modules"][1].update(ready=False, missing=["feedparser"])
        response = self.submit()
        self.assertEqual(response.status_code, 400)
        self.assertIn("feedparser", response.json["error"])
        self.assertEqual(self.runner.calls, [])

    def test_local_origin_and_header_protection(self):
        self.assertEqual(self.client.post("/api/researchstudio/jobs", data={}).status_code, 403)
        for headers in ({"Origin": "https://attacker.test"}, {"Origin": "null"}, {"Sec-Fetch-Site": "cross-site"}):
            self.assertEqual(self.client.get("/api/researchstudio/jobs", headers=headers).status_code, 403)
        self.assertEqual(self.client.get("/researchstudio", base_url="http://attacker.test").status_code, 403)
        self.assertEqual(self.client.get("/researchstudio").status_code, 200)

    def test_invalid_inputs_and_bounded_upload_leave_no_task(self):
        for data in ({"module":"bad"}, {"query":""}, {"query":"x" * 20001}, {"module":"paper2assets"}, {"projectId":"missing"}, {"paperId":"missing"}):
            self.assertEqual(self.submit(**data).status_code, 400)
        self.assertEqual(self.submit(paper=(io.BytesIO(b"not a pdf"), "paper.pdf")).status_code, 400)
        with patch("research_studio.MAX_PDF_BYTES", 6):
            self.assertEqual(self.submit(paper=(io.BytesIO(b"%PDF-12345"), "p.pdf")).status_code, 400)
        self.assertEqual(self.service.list_jobs(), [])
        self.assertEqual(list((self.service.root / "jobs").iterdir()), [])

    def test_library_pdf_copied_not_modified(self):
        original = self.root / "original.pdf"
        content = b"%PDF-1.7\noriginal"
        original.write_bytes(content)
        self.service.paper_source = lambda key: original
        response = self.submit(paperId="cached")
        job = self.wait(response.json["id"])
        self.assertEqual(original.read_bytes(), content)
        self.assertEqual((self.service.job_root(job["id"]) / "work/paper.pdf").read_bytes(), content)
        response = self.submit(paperId="cached", paper=(io.BytesIO(content), "p.pdf"))
        self.assertEqual(response.status_code, 400)

    def test_partial_output_is_not_reported_as_success(self):
        self.runner.write_output = False
        response = self.submit()
        job = self.wait(response.json["id"])
        self.assertEqual(job["status"], "incomplete")
        self.assertIn("report.md", job["error"])
        self.assertEqual(self.service.artifacts(job["id"])[0]["path"], "summary.md")

    def test_blog_files_cannot_bypass_failed_or_non_strict_upstream_qa(self):
        job_id = self.submit().json["id"]
        self.wait(job_id)
        job = self.service.update(job_id, module="paper2blog")
        output = self.service.job_root(job_id) / "work/output"
        for name in ("blog_zh.docx", "blog_en.docx"):
            (output / name).write_bytes(b"document")
        report = output / "assets/meta/reports/blog_qa_report.json"
        report.parent.mkdir(parents=True)
        for payload in ({"passed":False,"options":{"strict":True}}, {"passed":True,"options":{"strict":False}}):
            report.write_text(json.dumps(payload))
            self.assertTrue(self.service.validate_outputs(job))
        report.write_text(json.dumps({"passed":True,"options":{"strict":True}}))
        self.assertEqual(self.service.validate_outputs(job), [])

    def test_idea_card_requires_upstream_done_and_preserves_failed_validation(self):
        job_id = self.submit().json["id"]
        self.wait(job_id)
        job = self.service.update(job_id, module="idea-spark")
        phase = self.service.job_root(job_id) / "work/output/phase4"
        phase.mkdir()
        for name in ("idea.std.zh.md", "idea.std.en.md", "idea.detail.en.md"):
            (phase / name).write_text("A research proposal")
        with patch("research_studio.subprocess.run") as run:
            run.return_value = Mock(returncode=0, stdout="STATE  : READY_TO_RENDER\n")
            self.assertTrue(self.service.validate_outputs(job))
            run.return_value.stdout = "STATE  : DONE\nTYPE   : terminal\n"
            self.assertEqual(self.service.validate_outputs(job), [])
            (phase / "work").mkdir()
            (phase / "work/failed_card.json").write_text("{}")
            self.assertTrue(self.service.validate_outputs(job))

    def test_cancel_running_and_queued_jobs(self):
        self.runner.release.clear()
        first = self.submit().json["id"]
        deadline = time.monotonic() + 2
        while not self.runner.calls and time.monotonic() < deadline:
            time.sleep(0.01)
        second = self.submit().json["id"]
        for job_id in (second, first):
            self.assertEqual(self.client.post(f"/api/researchstudio/jobs/{job_id}/cancel", headers=HEADERS).status_code, 200)
        self.assertEqual(self.wait(first)["status"], "cancelled")
        self.assertEqual(self.wait(second)["status"], "cancelled")
        self.assertEqual(len(self.runner.calls), 1)

    def test_restart_does_not_replay_unfinished_job(self):
        response = self.submit()
        job_id = response.json["id"]
        self.wait(job_id)
        # A real restart has no old worker left to write its trailing log entry.
        self.service.executor.shutdown(wait=True)
        self.service.update(job_id, status="running")
        restarted = ResearchStudio(self.service.root, self.runner, self.account)
        self.assertEqual(restarted.read(job_id)["status"], "interrupted")
        self.assertEqual(len(self.runner.calls), 1)
        restarted.executor.shutdown(wait=True)

    def test_downloads_force_attachment_and_zip_excludes_inputs_and_metadata(self):
        job_id = self.submit(projectId="project").json["id"]
        self.wait(job_id)
        response = self.client.get(f"/api/researchstudio/jobs/{job_id}/files/output/preview.html")
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment", response.headers["Content-Disposition"])
        response.close()
        response = self.client.get(f"/api/researchstudio/jobs/{job_id}/bundle")
        with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
            self.assertEqual(set(archive.namelist()), {"summary.md", "report.md", "preview.html"})
        response.close()
        for path in ("job.json", "paper.pdf", "output/../../job.json", "output/x:y", "output/.env"):
            response = self.client.get(f"/api/researchstudio/jobs/{job_id}/files/{path}")
            self.assertIn(response.status_code, (400,404), path)


class FakeRuntime:
    def __init__(self, mode="complete"):
        self.listeners, self.calls = [], []
        self.mode = mode
        self.started = threading.Event()

    def add_listener(self, listener):
        self.listeners.append(listener)

    def request(self, method, params, timeout=30):
        self.calls.append((method,params))
        if method == "thread/start":
            return {"thread":{"id":"thread"}}
        if method == "turn/start":
            self.started.set()
            if self.mode == "complete":
                # Notifications may arrive before the turn/start response.
                for listener in self.listeners:
                    listener({"method":"item/completed", "params":{"threadId":"unrelated", "turnId":"turn", "item":{"type":"agentMessage", "text":"WRONG"}}})
                    listener({"method":"item/completed", "params":{"threadId":"thread", "turnId":"turn", "item":{"type":"agentMessage", "text":"result"}}})
                    listener({"method":"turn/completed", "params":{"threadId":"thread", "turn":{"id":"turn", "status":"completed"}}})
            return {"turn":{"id":"turn"}}
        return {}


class RunnerTests(unittest.TestCase):
    def test_skill_injection_workspace_boundary_and_early_notifications(self):
        runtime = FakeRuntime()
        runner = StudioRunner(runtime)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            text = runner.run(root, {"paper-search":root / "SKILL.md"}, "$paper-search query", "model", "high", threading.Event(), lambda x: None)
        self.assertEqual(text, "result")
        params = next(p for m,p in runtime.calls if m == "turn/start")
        self.assertEqual(params["sandboxPolicy"]["writableRoots"], [str(root)])
        self.assertEqual(params["sandboxPolicy"]["type"], "workspaceWrite")
        self.assertEqual(params["input"][1]["type"], "skill")
        self.assertEqual(runtime.calls[-1][0], "thread/archive")

    def test_timeout_interrupts_and_archives(self):
        runtime = FakeRuntime("wait")
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(TimeoutError):
                StudioRunner(runtime,timeout=0).run(Path(directory),{},"query","model","high",threading.Event(),lambda x: None)
        self.assertEqual([m for m,p in runtime.calls][-2:], ["turn/interrupt","thread/archive"])

    def test_pre_cancelled_job_never_starts_runtime(self):
        runtime = FakeRuntime()
        event = threading.Event(); event.set()
        with self.assertRaises(InterruptedError):
            StudioRunner(runtime).run(Path("unused"),{},"query","model","high",event,lambda x: None)
        self.assertEqual(runtime.calls, [])


if __name__ == "__main__":
    unittest.main()
