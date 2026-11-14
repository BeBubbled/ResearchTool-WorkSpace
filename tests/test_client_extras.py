"""Tests for the cross-module client features: activity feed, global search, citations,
usage ledger, environment check, toolbox re-run and the revision compile / write-back tools."""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from researchtool import activity
from researchtool import citations
from researchtool import global_search
from researchtool import revision_tools
from researchtool import usage_ledger
from researchtool import web_panel
LOCAL = {"Origin": "http://localhost"}


class ActivityTest(unittest.TestCase):
    def test_states(self) -> None:
        self.assertEqual(activity.activity_state("running"), "active")
        self.assertEqual(activity.activity_state("ready", busy=True), "active")
        self.assertEqual(activity.activity_state("completed_with_warnings"), "warning")
        self.assertEqual(activity.activity_state("interrupted"), "failed")
        self.assertEqual(activity.activity_state("cancelled"), "cancelled")
        self.assertEqual(activity.activity_state("ready"), "done")

    def test_collect_orders_active_first_and_survives_failing_sources(self) -> None:
        def broken():
            raise RuntimeError("module down")

        old = activity.activity_item(id="a", module="toolbox", title="old", status="completed", created_at=1, finished_at=5)
        new = activity.activity_item(id="b", module="reader", title="new", status="failed", created_at=2, finished_at=9)
        running = activity.activity_item(id="c", module="gaps", title="run", status="running", created_at=3)
        data = activity.collect([lambda: [old, new], broken, lambda: [running]])
        self.assertEqual([item["id"] for item in data["items"]], ["c", "b", "a"])
        self.assertEqual(data["active"]["gaps"], 1)
        self.assertEqual(data["activeTotal"], 1)
        self.assertEqual(data["sourceErrors"], 1)
        self.assertIsNone(data["items"][0]["finishedAt"])


class GlobalSearchTest(unittest.TestCase):
    def test_searches_every_source_with_snippets_and_links(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            report = Path(folder) / "report.md"
            report.write_text("# Report\nThe diffusion sampler uses a learned score function.", encoding="utf-8")
            search = global_search.GlobalSearch(
                papers=lambda: [{"id": "k" * 64, "title": "Score Matching Paper", "sourceName": "score.pdf"}],
                notes=lambda key: [{"id": "n1", "action": "解释公式", "selection": "the score function", "text": "梯度"}],
                analysis_files=lambda key: [("an1", report)],
                revision_projects=lambda: [{"id": "p1", "title": "Draft", "comments": [{"id": "c1", "text": "clarify score", "anchors": [{"quote": "x"}]}]}],
                gaps=lambda needle: [{"id": "g1", "projectId": "pr", "canonicalTitle": "Score drift", "description": "d"}],
                prompts=lambda: [{"id": "t1", "title": "Explain", "content": "explain the score"}],
            )
            results = search.search("score")
        kinds = [item["kind"] for item in results]
        self.assertEqual(kinds, ["paper", "note", "analysis", "comment", "gap", "prompt"])
        self.assertIn("note=n1", results[1]["url"])
        self.assertIn("analysis=an1", results[2]["url"])
        self.assertIn("learned score", results[2]["snippet"])
        self.assertEqual(results[3]["url"], "/paper-revision?project=p1&comment=c1")
        self.assertEqual(search.search("s"), [])

    def test_snippet_window(self) -> None:
        text = "a " * 100 + "needle" + " b" * 100
        self.assertIn("needle", global_search.snippet(text, "NEEDLE"))
        self.assertTrue(global_search.snippet(text, "needle").startswith("…"))


class FakeHttp:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append(url)
        for prefix, response in self.responses:
            if url.startswith(prefix):
                return response
        return SimpleNamespace(status_code=404, text="", json=lambda: {})


class CitationTest(unittest.TestCase):
    def test_crossref_match_uses_doi_bibtex(self) -> None:
        crossref = SimpleNamespace(status_code=200, json=lambda: {"message": {"items": [
            {"DOI": "10.1/wrong", "title": ["A Totally Different Paper"]},
            {"DOI": "10.1/right", "title": ["Attention Is All You Need"]},
        ]}})
        bib = SimpleNamespace(status_code=200, text="@inproceedings{vaswani2017, title={Attention Is All You Need}}")
        http = FakeHttp([("https://api.crossref.org", crossref), ("https://doi.org/10.1/right", bib)])
        found = citations.CitationResolver(get=http).resolve("Attention is all you need")
        self.assertTrue(found["verified"])
        self.assertEqual(found["source"], "crossref")
        self.assertNotIn("https://doi.org/10.1/wrong", http.calls)

    def test_falls_back_to_semantic_scholar_then_local(self) -> None:
        crossref = SimpleNamespace(status_code=200, json=lambda: {"message": {"items": []}})
        s2 = SimpleNamespace(status_code=200, json=lambda: {"data": [{"title": "Deep Residual Learning for Image Recognition",
                                                                     "citationStyles": {"bibtex": "@article{he2016, title={Deep Residual}}"}}]})
        http = FakeHttp([("https://api.crossref.org", crossref), ("https://api.semanticscholar.org", s2)])
        found = citations.CitationResolver(get=http).resolve("Deep residual learning for image recognition")
        self.assertEqual(found["source"], "semantic_scholar")
        miss = citations.CitationResolver(get=FakeHttp([])).resolve("Unknown {Weird} Title", authors=["Ada Lovelace"], year=1843)
        self.assertFalse(miss["verified"])
        self.assertIn("@misc{lovelace1843unknown", miss["bibtex"])
        self.assertIn("\\{Weird\\}", miss["bibtex"])

    def test_offline_mode_skips_network(self) -> None:
        http = FakeHttp([])
        found = citations.CitationResolver(get=http).resolve("Some Title", online=False)
        self.assertEqual(found["source"], "local")
        self.assertEqual(http.calls, [])


class UsageLedgerTest(unittest.TestCase):
    def test_records_and_summarises_by_engine_and_feature(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            ledger = usage_ledger.UsageLedger(Path(folder) / "usage.jsonl")
            ledger.record("api", "three_pass", label="preset", inputTokens=100, outputTokens=40)
            ledger.record("api", "reader_question", inputTokens=10, outputTokens=5)
            ledger.record("mathpix", "reader_ocr", pages=12)
            ledger.record("mathpix", "reader_ocr", calls=0, reused=1)
            ledger.record("unknown", "x")
            summary = ledger.summary(30)
            text = (Path(folder) / "usage.jsonl").read_text(encoding="utf-8")
        api = next(item for item in summary["engines"] if item["engine"] == "api")
        mathpix = next(item for item in summary["engines"] if item["engine"] == "mathpix")
        self.assertEqual((api["calls"], api["inputTokens"], api["outputTokens"]), (2, 110, 45))
        self.assertEqual((mathpix["calls"], mathpix["pages"], mathpix["reused"]), (1, 12, 1))
        self.assertEqual(api["features"][0]["label"], "精读 Three-Pass")
        self.assertNotIn("unknown", text)

    def test_tokens_from_chat_and_responses_shapes(self) -> None:
        chat = SimpleNamespace(usage=SimpleNamespace(prompt_tokens=7, completion_tokens=3))
        responses = {"usage": {"input_tokens": 11, "output_tokens": 2}}
        self.assertEqual(usage_ledger.tokens_from_response(chat), (7, 3))
        self.assertEqual(usage_ledger.tokens_from_response(responses), (11, 2))
        self.assertEqual(usage_ledger.tokens_from_response(object()), (0, 0))


class RevisionToolsTest(unittest.TestCase):
    def test_parse_log_collects_errors_and_warnings(self) -> None:
        log = "\n".join([
            "./main.tex:12: Undefined control sequence.",
            "l.12 \\foo",
            "LaTeX Warning: Reference `fig:a' on page 1 undefined on input line 3.",
            "! Emergency stop.",
        ])
        errors, warnings = revision_tools.parse_log(log)
        self.assertEqual(len(errors), 2)
        self.assertIn("l.12", errors[0])
        self.assertEqual(len(warnings), 1)

    def test_safe_relative_rejects_escapes(self) -> None:
        for name in ("../x.tex", "/abs.tex", "C:/x.tex", "a/../../b.tex"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                revision_tools.safe_relative(name)

    @unittest.skipUnless(shutil.which("git"), "git is not installed")
    def test_write_back_only_into_clean_git_files(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "paper"
            root.mkdir()
            (root / "main.tex").write_text("old body\n", encoding="utf-8")
            (root / "notes.tex").write_text("notes\n", encoding="utf-8")
            run = lambda *args: subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
            run("init", "-q")
            run("-c", "user.email=t@example.com", "-c", "user.name=t", "add", ".")
            run("-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-qm", "init")
            (root / "notes.tex").write_text("uncommitted edit\n", encoding="utf-8")
            files = [{"name": "main.tex", "text": "new body\n"}, {"name": "notes.tex", "text": "tool notes\n"}]
            project = {"revision": 2, "origin": {"root": str(root), "main": "main.tex"}}
            tools = revision_tools.RevisionTools(Path(folder) / "work", read=lambda pid: project, files=lambda pid, rev: files)
            plan = tools.write_back_plan("p")
            self.assertEqual([item["name"] for item in plan["changes"]], ["main.tex"])
            self.assertEqual([item["name"] for item in plan["blocked"]], ["notes.tex"])
            with self.assertRaises(ValueError):
                tools.write_back("p")
            files[1]["text"] = "uncommitted edit\n"  # identical to disk → nothing to write, not blocked
            result = tools.write_back("p")
            self.assertEqual(result["written"], ["main.tex"])
            self.assertEqual((root / "main.tex").read_text(encoding="utf-8"), "new body\n")
            self.assertIn("checkout", result["undo"])

    def test_write_back_requires_local_folder(self) -> None:
        tools = revision_tools.RevisionTools(Path(tempfile.gettempdir()), read=lambda pid: {"revision": 1, "origin": {"kind": "upload"}},
                                             files=lambda pid, rev: [])
        with self.assertRaises(ValueError):
            tools.write_back_plan("p")


class ClientEndpointsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.previous_jobs_dir = web_panel.JOBS_DIR
        web_panel.JOBS_DIR = Path(self.temp_dir.name) / "jobs"
        web_panel.JOBS_DIR.mkdir()
        web_panel.app.config.update(TESTING=True)
        self.client = web_panel.app.test_client()

    def tearDown(self) -> None:
        web_panel.JOBS_DIR = self.previous_jobs_dir
        self.temp_dir.cleanup()

    def test_activity_feed_includes_toolbox_jobs(self) -> None:
        job = web_panel.Job("a" * 32, web_panel.TOOL_BY_ID["bibtex"], web_panel.JOBS_DIR / ("a" * 32), {})
        job.status, job.finished_at = "completed", time.time()
        with web_panel.job_manager.lock:
            web_panel.job_manager.jobs[job.id] = job
        try:
            data = self.client.get("/api/activity").get_json()
        finally:
            with web_panel.job_manager.lock:
                web_panel.job_manager.jobs.pop(job.id, None)
        item = next(item for item in data["items"] if item["id"] == f"tool:{job.id}")
        self.assertEqual((item["module"], item["state"], item["url"]), ("toolbox", "done", f"/?job={job.id}"))
        self.assertIn("active", data)

    def test_search_endpoint_requires_two_characters(self) -> None:
        self.assertEqual(self.client.get("/api/search?q=a").get_json()["results"], [])
        with patch.object(web_panel.global_search, "search", return_value=[{"kind": "paper"}]) as search:
            self.assertEqual(self.client.get("/api/search?q=diffusion").get_json()["results"], [{"kind": "paper"}])
            search.assert_called_once_with("diffusion")

    def test_doctor_lists_checks(self) -> None:
        engines = {"codex": {"ready": False, "error": "未登录"}, "claude": {"ready": True}, "google": {"ready": False, "error": ""},
                   "api": [], "ready": ["claude"]}
        with patch.object(web_panel.engine_catalog, "snapshot", return_value=engines), \
                patch.object(web_panel.research_studio_service, "doctor", return_value={"modules": [{"ready": True}]}):
            data = self.client.get("/api/doctor").get_json()
        ids = {check["id"]: check for check in data["checks"]}
        self.assertTrue(ids["ai-any"]["ok"])
        self.assertFalse(ids["api"]["ok"])
        self.assertTrue(ids["api"]["fix"])
        self.assertIn("tool-bibtex", ids)
        self.assertIn("latex", ids)

    def test_usage_endpoint_reads_ledger(self) -> None:
        previous = usage_ledger.LEDGER.path
        usage_ledger.LEDGER.configure(Path(self.temp_dir.name) / "usage.jsonl")
        try:
            usage_ledger.record("google", "revision", characters=500)
            data = self.client.get("/api/usage?days=7").get_json()
        finally:
            usage_ledger.LEDGER.path = previous
        self.assertEqual(data["days"], 7)
        self.assertEqual(data["engines"][0]["characters"], 500)

    def test_plan_submissions_are_counted(self) -> None:
        with patch.object(web_panel.usage_ledger, "record") as record:
            with web_panel.app.test_request_context("/api/researchstudio/jobs", method="POST"):
                web_panel.record_plan_usage(web_panel.Response(status=202))
            with web_panel.app.test_request_context("/api/research-gaps/projects/p1/analysis", method="POST", json={"backend": "api"}):
                web_panel.record_plan_usage(web_panel.Response(status=202))
        record.assert_called_once_with("codex", "studio")

    def test_rerun_copies_uploads_and_options(self) -> None:
        response = self.client.post("/api/jobs", headers=LOCAL, data={
            "tool": "bibtex", "options": json.dumps({}), "manifest": json.dumps([{"relativePath": "titles.txt", "workName": "titles"}]),
            "files": (io.BytesIO(b"Attention Is All You Need\n"), "titles.txt"),
        })
        self.assertEqual(response.status_code, 202, response.get_json())
        job_id = response.get_json()["id"]
        source = web_panel.job_manager.get(job_id)
        for _ in range(100):  # the worker may pick it up; wait until it is no longer running
            if source.status not in {"queued", "running"}:
                break
            time.sleep(0.05)
        with patch.object(web_panel.job_manager, "submit") as submit:
            if source.status in {"queued", "running"}:
                source.status = "failed"
            listed = next(job for job in self.client.get("/api/jobs").get_json()["jobs"] if job["id"] == job_id)
            self.assertTrue(listed["rerunnable"])
            self.assertEqual(listed["fileCount"], 1)
            rerun = self.client.post(f"/api/jobs/{job_id}/rerun", headers=LOCAL, json={})
            self.assertEqual(rerun.status_code, 202, rerun.get_json())
            new_job = submit.call_args[0][0]
        self.assertEqual(new_job.uploads, source.uploads)
        self.assertTrue(all((new_job.root / path).is_file() for path in new_job.uploads))
        with web_panel.job_manager.lock:
            web_panel.job_manager.jobs.pop(job_id, None)

    def test_rerun_refuses_translation_jobs(self) -> None:
        job = web_panel.Job("b" * 32, web_panel.TOOL_BY_ID["document_translate"], web_panel.JOBS_DIR / ("b" * 32), {})
        job.status, job.uploads = "completed", ["input/a.md"]
        with web_panel.job_manager.lock:
            web_panel.job_manager.jobs[job.id] = job
        try:
            response = self.client.post(f"/api/jobs/{job.id}/rerun", headers=LOCAL, json={})
        finally:
            with web_panel.job_manager.lock:
                web_panel.job_manager.jobs.pop(job.id, None)
        self.assertEqual(response.status_code, 409)

    def test_library_bibtex_uses_resolver(self) -> None:
        key = "c" * 64
        with patch.object(web_panel.paper_library, "document", return_value={"id": key, "title": "Paper Title.pdf"}), \
                patch.object(web_panel.citation_resolver, "resolve", return_value={"bibtex": "@misc{x}", "source": "local", "verified": False}) as resolve:
            data = self.client.post("/api/library/bibtex", headers=LOCAL, json={"keys": [key, "bad"]}).get_json()
        resolve.assert_called_once_with("Paper Title", online=True)
        self.assertEqual(data["unverified"], 1)
        self.assertIn("@misc{x}", data["bibtex"])

    def test_pages_load_client_extras(self) -> None:
        for path in ("/", "/library", "/reader", "/settings"):
            with self.subTest(path=path):
                html = self.client.get(path).get_data(as_text=True)
                self.assertIn("/static/client_extras.js", html)
        self.assertIn("/static/vendor/mathjax/es5/tex-mml-chtml.js", self.client.get("/reader").get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
