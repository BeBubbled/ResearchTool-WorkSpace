"""Tests for the local research-gap evidence workflow."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from flask import Flask
from pypdf import PdfWriter

from research_gaps import (
    ArxivClient,
    PaperSource,
    ResearchGapService,
    ResearchGapStore,
    create_research_gap_blueprint,
    download_pdf,
    extract_pdf_document,
    parse_external_identifier,
    split_text,
)


class FakeDownloadResponse:
    status_code = 200
    headers = {"Content-Length": "18", "Content-Type": "application/pdf"}

    def raise_for_status(self):
        return None

    def iter_content(self, _size):
        yield b"%PDF-fake content"


class FakeDownloadSession:
    def get(self, *_args, **_kwargs):
        return FakeDownloadResponse()


class ResearchGapTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.reader_file = self.root / "reader.md"
        self.reader_text = (
            "# Overlapping object generation\n\n"
            "Our method improves quality.\n\n"
            "However, performance degrades when objects overlap.\n\n"
            + "Supporting experiment context. " * 120
        )
        self.reader_file.write_text(self.reader_text, encoding="utf-8")
        self.service = ResearchGapService(
            self.root / "research",
            llm_request=self.fake_llm,
            llm_presets=lambda: [{"id": "test", "name": "Test", "model": "fake"}],
            reader_source=lambda key: {
                "entry": {
                    "id": key, "title": "Overlap Paper", "sourceName": "reader.md",
                    "renderKind": "markdown", "useOcr": False,
                },
                "path": str(self.reader_file),
            } if key == "a" * 64 else None,
        )

    def tearDown(self) -> None:
        self.service.executor.shutdown(wait=True)
        self.service.import_executor.shutdown(wait=True)
        self.temp_dir.cleanup()

    def wait_for_import(self, job_id: str) -> dict:
        job = self.service.get_paper_import(job_id)
        for _ in range(300):
            if job["status"] not in {"queued", "running"}:
                return job
            time.sleep(0.01)
            job = self.service.get_paper_import(job_id)
        self.fail(f"Import job did not finish: {job}")

    @staticmethod
    def fake_llm(_preset_id, messages):
        system = messages[0]["content"]
        if "科研证据抽取器" in system:
            return json.dumps({"items": [
                {
                    "kind": "contribution", "title": "提升生成质量", "detail": "方法改善了质量。",
                    "confidence": 0.9, "evidence": "Our method improves quality.",
                    "locator": "Section 1", "relationType": "proposes",
                },
                {
                    "kind": "gap", "title": "重叠物体下的稳健生成", "detail": "物体重叠时性能下降。",
                    "confidence": 0.8, "evidence": "However, performance degrades when objects overlap.",
                    "locator": "Section 1", "relationType": "fails_on",
                },
                {
                    "kind": "limitation", "title": "不存在的证据", "detail": "应被丢弃。",
                    "confidence": 1, "evidence": "This sentence was never in the paper.",
                    "locator": "Unknown", "relationType": "fails_on",
                },
            ]})
        if "合并同一篇论文" in system:
            raw = messages[1]["content"].split("候选事实：\n", 1)[1]
            return raw
        raise AssertionError("Unexpected normalization call for the first gap")

    def test_identifier_parsing(self) -> None:
        self.assertEqual(parse_external_identifier("https://doi.org/10.1000/test"), ("DOI", "10.1000/test"))
        self.assertEqual(parse_external_identifier("arXiv:2401.12345v2"), ("ARXIV", "2401.12345v2"))
        self.assertEqual(parse_external_identifier("https://arxiv.org/pdf/2401.12345.pdf"), ("ARXIV", "2401.12345"))
        self.assertEqual(parse_external_identifier("a" * 40), ("S2", "a" * 40))
        self.assertIsNone(parse_external_identifier("diffusion models for 3D"))

    def test_text_chunking_is_bounded_and_complete_enough(self) -> None:
        chunks = split_text(("paragraph " * 1200) + "\n\n" + ("tail " * 1200), 1000)
        self.assertGreater(len(chunks), 2)
        self.assertTrue(all(len(chunk) <= 1000 for chunk in chunks))
        self.assertIn("tail", chunks[-1])

    def test_pdf_document_metadata_and_filename_fallback(self) -> None:
        with_metadata = self.root / "metadata.pdf"
        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        writer.add_metadata({"/Title": "Embedded Title", "/Author": "Alice; Bob"})
        with with_metadata.open("wb") as handle:
            writer.write(handle)
        _text, pages, title, authors = extract_pdf_document(with_metadata)
        self.assertEqual(pages, 1)
        self.assertEqual(title, "Embedded Title")
        self.assertEqual(authors, ["Alice", "Bob"])

        fallback = self.root / "filename fallback.pdf"
        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        writer.add_metadata({"/Title": "Untitled"})
        with fallback.open("wb") as handle:
            writer.write(handle)
        self.assertEqual(extract_pdf_document(fallback)[2], "filename fallback")

    def test_arxiv_atom_feed_is_normalized(self) -> None:
        feed = """<?xml version="1.0" encoding="UTF-8"?>
        <feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
          <entry>
            <id>https://arxiv.org/abs/1706.03762v7</id>
            <title>Attention Is All You Need</title>
            <summary>Transformer abstract.</summary>
            <published>2017-06-12T17:57:34Z</published>
            <author><name>Ashish Vaswani</name></author>
            <arxiv:doi>10.5555/3295222.3295349</arxiv:doi>
            <link title="pdf" href="https://arxiv.org/pdf/1706.03762v7" type="application/pdf" />
          </entry>
        </feed>"""
        papers = ArxivClient._parse_feed(feed)
        self.assertEqual(len(papers), 1)
        self.assertEqual(papers[0]["arxivId"], "1706.03762v7")
        self.assertEqual(papers[0]["title"], "Attention Is All You Need")
        self.assertEqual(papers[0]["year"], 2017)
        self.assertEqual(papers[0]["authors"], ["Ashish Vaswani"])
        legacy = ArxivClient._parse_feed(feed.replace("1706.03762v7", "cs/9901001v2"))
        self.assertEqual(legacy[0]["arxivId"], "cs/9901001v2")

    def test_batch_arxiv_titles_keep_per_title_results(self) -> None:
        project = self.service.create_project("Batch", "Import by exact arXiv title")
        metadata = {
            "paperId": "arxiv:1706.03762v7", "title": "Attention Is All You Need",
            "abstract": "Transformer abstract.", "year": 2017, "venue": "arXiv",
            "authors": ["Ashish Vaswani"], "url": "https://arxiv.org/abs/1706.03762v7",
            "doi": "", "arxivId": "1706.03762v7", "pdfUrl": "https://arxiv.org/pdf/1706.03762v7",
            "publicationDate": "2017-06-12", "citationCount": 0, "referenceCount": 0,
        }
        with (
            patch.object(self.service.arxiv, "resolve_title", side_effect=[(metadata, [metadata]), (None, [])]),
            patch.object(self.service, "_acquire_semantic_source", return_value=PaperSource(
                metadata["abstract"], "abstract", "abstract", None
            )),
        ):
            result = self.service.import_arxiv_titles(project["id"], [
                "Attention Is All You Need", "A title that is not on arXiv",
            ])
        self.assertEqual(result["imported"], 1)
        self.assertEqual(result["failed"], 1)
        self.assertEqual([item["status"] for item in result["results"]], ["imported", "not_found"])
        self.assertEqual(self.service.list_papers(project["id"])[0]["sourceKind"], "arxiv")

    def test_recursive_folder_import_deduplicates_content_and_preserves_source(self) -> None:
        project = self.service.create_project("Folder", "Import a local PDF tree")
        folder = self.root / "papers"
        nested = folder / "nested"
        nested.mkdir(parents=True)
        first = folder / "first.pdf"
        duplicate = nested / "renamed.pdf"
        first.write_bytes(b"same-pdf-content")
        duplicate.write_bytes(b"same-pdf-content")
        (nested / "ignore.txt").write_text("not a paper", encoding="utf-8")
        symlink = nested / "linked.pdf"
        try:
            symlink.symlink_to(first)
        except OSError:
            symlink = None

        def fake_extract(path: Path):
            return ("Extracted full text. " * 140, 7, f"Title for {path.stem}", ["Local Author"])

        with patch("research_gaps.extract_pdf_document", side_effect=fake_extract):
            job = self.service.create_paper_import(project["id"], str(folder), recursive=True)
            job = self.wait_for_import(job["id"])

        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["total"], 2)
        self.assertEqual(job["imported"], 1)
        self.assertEqual(job["duplicates"], 1)
        papers = self.service.list_papers(project["id"])
        self.assertEqual(len(papers), 1)
        self.assertEqual(papers[0]["sourceKind"], "local_pdf")
        self.assertEqual(papers[0]["authors"], ["Local Author"])
        self.assertTrue(papers[0]["sourceAvailable"])
        self.assertIn(papers[0]["sourceRelativePath"], {"first.pdf", "nested/renamed.pdf"})
        self.assertEqual(first.read_bytes(), b"same-pdf-content")
        items = self.service.list_paper_import_items(job["id"], limit=10)["items"]
        self.assertEqual({item["status"] for item in items}, {"imported", "duplicate"})
        if symlink is not None:
            self.assertNotIn("nested/linked.pdf", {item["relativePath"] for item in items})

    def test_folder_import_skips_low_text_and_keeps_file_failures_isolated(self) -> None:
        project = self.service.create_project("Failures", "Report unusable PDFs")
        folder = self.root / "failure-papers"
        folder.mkdir()
        for name in ("good.pdf", "scan.pdf", "broken.pdf"):
            (folder / name).write_bytes(name.encode())

        def fake_extract(path: Path):
            if path.name == "broken.pdf":
                raise ValueError("damaged PDF")
            if path.name == "scan.pdf":
                return ("tiny", 2, "Scanned", [])
            return ("Usable paper text. " * 150, 8, "Embedded paper title", ["A; B"])

        with patch("research_gaps.extract_pdf_document", side_effect=fake_extract):
            job = self.service.create_paper_import(project["id"], str(folder))
            job = self.wait_for_import(job["id"])

        self.assertEqual(job["status"], "completed")
        self.assertEqual((job["imported"], job["skipped"], job["failed"]), (1, 1, 1))
        self.assertEqual(len(self.service.list_papers(project["id"])), 1)
        skipped = self.service.list_paper_import_items(job["id"], status="skipped")["items"]
        failed = self.service.list_paper_import_items(job["id"], status="failed")["items"]
        self.assertIn("少于 2000 字符", skipped[0]["error"])
        self.assertIn("damaged PDF", failed[0]["error"])

    def test_import_manifest_paginates_more_than_one_thousand_files(self) -> None:
        project = self.service.create_project("Large", "Large persistent import manifest")
        folder = self.root / "large-folder"
        folder.mkdir()
        for index in range(1005):
            (folder / f"paper-{index:04d}.pdf").touch()
        job_id, timestamp = "import_large", time.time()
        with self.service.store.connect() as db:
            db.execute(
                """INSERT INTO paper_import_jobs(id,project_id,folder_path,recursive,status,stage,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?)""",
                (job_id, project["id"], str(folder), 1, "paused", "paused", timestamp, timestamp),
            )
        self.service._scan_paper_import(job_id, folder, True)
        first_page = self.service.list_paper_import_items(job_id, offset=0, limit=200)
        last_page = self.service.list_paper_import_items(job_id, offset=1000, limit=200)
        self.assertEqual(first_page["total"], 1005)
        self.assertEqual(len(first_page["items"]), 200)
        self.assertEqual(len(last_page["items"]), 5)

    def test_folder_import_pause_and_resume_keeps_completed_work(self) -> None:
        project = self.service.create_project("Pause", "Pause a long folder import")
        folder = self.root / "pause-folder"
        folder.mkdir()
        for index in range(3):
            (folder / f"paper-{index}.pdf").write_bytes(f"paper-{index}".encode())
        entered = threading.Event()
        release = threading.Event()
        calls = 0

        def slow_extract(path: Path):
            nonlocal calls
            calls += 1
            if calls == 1:
                entered.set()
                release.wait(2)
            return ("Long enough paper text. " * 120, 4, path.stem, [])

        with patch("research_gaps.extract_pdf_document", side_effect=slow_extract):
            job = self.service.create_paper_import(project["id"], str(folder))
            self.assertTrue(entered.wait(2))
            paused_request = self.service.pause_paper_import(job["id"])
            self.assertTrue(paused_request["pauseRequested"])
            release.set()
            paused = self.wait_for_import(job["id"])
            self.assertEqual(paused["status"], "paused")
            self.assertEqual(paused["imported"], 1)
            resumed = self.service.resume_paper_import(job["id"])
            self.assertEqual(resumed["status"], "queued")
            completed = self.wait_for_import(job["id"])
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["imported"], 3)

    def test_restart_marks_active_import_interrupted_and_requeues_current_item(self) -> None:
        project = self.service.create_project("Restart", "Recover an interrupted import")
        timestamp = time.time()
        with self.service.store.connect() as db:
            db.execute(
                """INSERT INTO paper_import_jobs(id,project_id,folder_path,recursive,status,stage,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?)""",
                ("import_restart", project["id"], str(self.root), 1, "running", "importing", timestamp, timestamp),
            )
            db.execute(
                """INSERT INTO paper_import_items(
                id,job_id,relative_path,source_path,status,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?)""",
                ("item_restart", "import_restart", "paper.pdf", str(self.root / "paper.pdf"),
                 "processing", timestamp, timestamp),
            )
        ResearchGapStore(self.service.store.database)
        with self.service.store.connect() as db:
            job_status = db.execute(
                "SELECT status FROM paper_import_jobs WHERE id='import_restart'"
            ).fetchone()["status"]
            item_status = db.execute(
                "SELECT status FROM paper_import_items WHERE id='item_restart'"
            ).fetchone()["status"]
        self.assertEqual(job_status, "interrupted")
        self.assertEqual(item_status, "pending")

    def test_project_import_analysis_review_and_export(self) -> None:
        project = self.service.create_project("3D generation", "Study overlap failure conditions")
        paper, created = self.service.import_reader_paper(project["id"], "a" * 64)
        self.assertTrue(created)
        self.assertEqual(paper["evidenceLevel"], "fulltext")
        duplicate, created = self.service.import_reader_paper(project["id"], "a" * 64)
        self.assertFalse(created)
        self.assertEqual(duplicate["id"], paper["id"])

        job = self.service.submit_analysis(project["id"], [paper["id"]], "test")
        for _ in range(100):
            job = self.service.get_job(job["id"])
            if job["status"] not in {"queued", "running"}:
                break
            time.sleep(0.02)
        self.assertEqual(job["status"], "completed", job)
        gaps = self.service.list_gaps(project["id"])
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0]["status"], "OPEN")
        self.assertTrue(gaps[0]["provisional"])
        self.assertEqual(gaps[0]["evidenceCount"], 1)

        queue = self.service.review_queue(project["id"])
        self.assertEqual(len(queue), 3)
        self.assertNotIn("不存在的证据", json.dumps(queue, ensure_ascii=False))
        relation = next(item for item in queue if item["itemType"] == "relation")
        updated = self.service.update_relation(relation["id"], {"reviewStatus": "accepted"})
        self.assertFalse(updated["provisional"])

        exported = self.service.export_project(project["id"])
        self.assertEqual(exported["schemaVersion"], 1)
        self.assertEqual(exported["project"]["id"], project["id"])
        self.assertEqual(len(exported["papers"]), 1)

    def test_codex_backend_records_model_and_uses_codex_request(self) -> None:
        project = self.service.create_project("Codex", "Use ChatGPT/Codex allowance")
        paper, _created = self.service.import_reader_paper(project["id"], "a" * 64)
        calls = []
        self.service.codex_status = lambda: {
            "chatgptAuthenticated": True,
            "defaultModel": "codex-test",
            "defaultReasoningEffort": "none",
            "models": [{"id": "codex-test", "supportedEfforts": ["none", "high"], "defaultEffort": "high"}],
        }
        self.service.codex_request = lambda messages, model, effort: (
            calls.append((model, effort)) or self.fake_llm("", messages)
        )
        job = self.service.submit_analysis(
            project["id"], [paper["id"]], "", backend="codex", model="ignored-model", effort="high"
        )
        for _ in range(100):
            job = self.service.get_job(job["id"])
            if job["status"] not in {"queued", "running"}:
                break
            time.sleep(0.02)
        self.assertEqual(job["status"], "completed", job)
        self.assertEqual(job["backend"], "codex")
        self.assertEqual(job["model"], "codex-test")
        self.assertEqual(job["effort"], "none")
        self.assertTrue(calls)
        self.assertTrue(all(call == ("codex-test", "none") for call in calls))

    def test_gap_status_requires_two_solving_papers_and_respects_new_failure(self) -> None:
        project = self.service.create_project("Status", "Status rules")
        timestamp = time.time()
        with self.service.store.connect() as db:
            gap_id = "gap_status"
            db.execute(
                "INSERT INTO gaps(id,project_id,canonical_title,status,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (gap_id, project["id"], "Robust overlap", "OPEN", timestamp, timestamp),
            )
            for index, year in enumerate((2023, 2024, 2025), start=1):
                db.execute(
                    """INSERT INTO papers(id,project_id,source_kind,source_key,title,authors_json,year,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?)""",
                    (f"paper_{index}", project["id"], "test", f"test:{index}", f"Paper {index}", "[]", year, timestamp, timestamp),
                )
            db.execute(
                """INSERT INTO relations(id,project_id,paper_id,gap_id,relation_type,review_status,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?)""",
                ("rel_1", project["id"], "paper_1", gap_id, "solves", "accepted", timestamp, timestamp),
            )
        self.assertEqual(self.service.recompute_gap(gap_id)["status"], "PARTIALLY_ADDRESSED")
        with self.service.store.connect() as db:
            db.execute(
                """INSERT INTO relations(id,project_id,paper_id,gap_id,relation_type,review_status,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?)""",
                ("rel_2", project["id"], "paper_2", gap_id, "solves", "accepted", timestamp, timestamp),
            )
        self.assertEqual(self.service.recompute_gap(gap_id)["status"], "MATURE")
        with self.service.store.connect() as db:
            db.execute(
                """INSERT INTO relations(id,project_id,paper_id,gap_id,relation_type,review_status,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?)""",
                ("rel_3", project["id"], "paper_3", gap_id, "fails_on", "accepted", timestamp, timestamp),
            )
        self.assertEqual(self.service.recompute_gap(gap_id)["status"], "PARTIALLY_ADDRESSED")

    def test_blueprint_crud_and_confirmed_delete(self) -> None:
        app = Flask(__name__)
        app.register_blueprint(create_research_gap_blueprint(self.service))
        client = app.test_client()
        created = client.post("/api/research-gaps/projects", json={"name": "Project", "topic": "Topic"})
        self.assertEqual(created.status_code, 201, created.get_json())
        project_id = created.get_json()["project"]["id"]
        imported = client.post(
            f"/api/research-gaps/projects/{project_id}/papers", json={"readerCacheKey": "a" * 64}
        )
        self.assertEqual(imported.status_code, 201, imported.get_json())
        folder = self.root / "api-folder"
        folder.mkdir()
        (folder / "api.pdf").write_bytes(b"api-pdf")
        with patch("research_gaps.extract_pdf_document", return_value=("API text. " * 250, 3, "API Paper", [])):
            response = client.post(
                f"/api/research-gaps/projects/{project_id}/paper-imports",
                json={"folderPath": str(folder), "recursive": True},
            )
            self.assertEqual(response.status_code, 202, response.get_json())
            import_id = response.get_json()["job"]["id"]
            for _ in range(200):
                progress = client.get(f"/api/research-gaps/paper-imports/{import_id}").get_json()["job"]
                if progress["status"] not in {"queued", "running"}:
                    break
                time.sleep(0.01)
        self.assertEqual(progress["status"], "completed")
        items = client.get(f"/api/research-gaps/paper-imports/{import_id}/items?limit=10").get_json()
        self.assertEqual(items["items"][0]["status"], "imported")
        self.assertEqual(len(client.get(f"/api/research-gaps/projects/{project_id}/papers").get_json()["papers"]), 2)
        rejected = client.delete(f"/api/research-gaps/projects/{project_id}", json={"confirmName": "wrong"})
        self.assertEqual(rejected.status_code, 400)
        deleted = client.delete(f"/api/research-gaps/projects/{project_id}", json={"confirmName": "Project"})
        self.assertEqual(deleted.status_code, 200)

    def test_pdf_download_checks_magic_and_writes_atomically(self) -> None:
        destination = self.root / "download" / "paper.pdf"
        with patch("research_gaps.safe_download_url", side_effect=lambda value: value):
            result = download_pdf("https://example.org/paper.pdf", destination, FakeDownloadSession())
        self.assertEqual(result, destination)
        self.assertTrue(destination.read_bytes().startswith(b"%PDF-"))
        self.assertFalse(destination.with_suffix(".download").exists())


if __name__ == "__main__":
    unittest.main()
