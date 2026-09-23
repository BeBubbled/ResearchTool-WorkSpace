"""Tests for the local research-gap evidence workflow."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from flask import Flask

from research_gaps import (
    ResearchGapService,
    create_research_gap_blueprint,
    download_pdf,
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
        self.temp_dir.cleanup()

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
        self.assertEqual(len(client.get(f"/api/research-gaps/projects/{project_id}/papers").get_json()["papers"]), 1)
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
