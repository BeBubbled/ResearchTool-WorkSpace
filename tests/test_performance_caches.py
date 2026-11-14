"""File caches behind the library / search endpoints, and the per-lane toolbox workers."""

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from researchtool import file_cache
from researchtool import web_panel
from researchtool.global_search import FileTextCache, GlobalSearch


class StatCacheTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.cache = file_cache.StatCache()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_value_is_computed_once_per_file_version(self) -> None:
        path = self.root / "a.txt"
        path.write_text("one", encoding="utf-8")
        calls = []

        def compute(target: Path) -> str:
            calls.append(target)
            return target.read_text(encoding="utf-8")

        self.assertEqual(self.cache.get(path, compute), "one")
        self.assertEqual(self.cache.get(path, compute), "one")
        self.assertEqual(len(calls), 1)
        path.write_text("three", encoding="utf-8")  # different size: new signature
        self.assertEqual(self.cache.get(path, compute), "three")
        self.assertEqual(len(calls), 2)

    def test_invalidate_covers_same_size_rewrites(self) -> None:
        path = self.root / "a.json"
        path.write_text('{"v": 1}', encoding="utf-8")
        self.assertEqual(self.cache.read_json(path), {"v": 1})
        path.write_text('{"v": 2}', encoding="utf-8")
        self.cache.invalidate(path)
        self.assertEqual(self.cache.read_json(path), {"v": 2})

    def test_read_json_returns_private_copies_and_missing_marker(self) -> None:
        path = self.root / "m.json"
        path.write_text('{"items": [1]}', encoding="utf-8")
        first = self.cache.read_json(path)
        first["items"].append(2)
        self.assertEqual(self.cache.read_json(path), {"items": [1]})
        self.assertIs(self.cache.read_json(self.root / "absent.json"), file_cache.MISSING)
        (self.root / "bad.json").write_text("{", encoding="utf-8")
        self.assertIs(self.cache.read_json(self.root / "bad.json"), file_cache.MISSING)

    def test_missing_files_are_not_cached(self) -> None:
        path = self.root / "later.txt"
        self.assertIsNone(self.cache.get(path, lambda target: target.read_text() if target.exists() else None))
        path.write_text("now", encoding="utf-8")
        self.assertEqual(self.cache.get(path, lambda target: target.read_text()), "now")


class ReaderCacheReadTest(unittest.TestCase):
    """Manifest and reader-state reads go through the cache but still see every write."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.previous = web_panel.READER_CACHE_DIR
        web_panel.READER_CACHE_DIR = Path(self.temp_dir.name) / "reader-cache"
        self.key = "ab" * 32
        root = web_panel.reader_cache_root(self.key)
        (root / "source").mkdir(parents=True)
        (root / "source" / "paper.md").write_text("# Paper\n\nText.", encoding="utf-8")
        web_panel.write_reader_cache_manifest(self.key, {"document": {
            "storageRoot": "source", "sourceFilename": "paper.md", "renderKind": "markdown",
            "sourceName": "paper.md", "sourceType": "markdown", "title": "First title",
        }})

    def tearDown(self) -> None:
        web_panel.READER_CACHE_DIR = self.previous
        self.temp_dir.cleanup()

    def test_manifest_writes_are_visible_immediately(self) -> None:
        self.assertEqual(web_panel.reader_cache_library_entry(self.key)["title"], "First title")
        manifest = web_panel.read_reader_cache_manifest(self.key)
        manifest["document"]["title"] = "Other title"  # same length: same size on disk
        web_panel.write_reader_cache_manifest(self.key, manifest)
        self.assertEqual(web_panel.reader_cache_library_entry(self.key)["title"], "Other title")

    def test_callers_cannot_mutate_the_cached_manifest(self) -> None:
        web_panel.read_reader_cache_manifest(self.key)["document"]["title"] = "Changed in memory"
        self.assertEqual(web_panel.read_reader_cache_manifest(self.key)["document"]["title"], "First title")

    def test_reader_state_round_trip(self) -> None:
        self.assertEqual(web_panel.read_reader_persistent_state(self.key)["notes"], [])
        web_panel.write_reader_persistent_state(self.key, {"notes": [{"id": "n1", "text": "hello"}]})
        self.assertEqual(web_panel.read_reader_persistent_state(self.key)["notes"], [{"id": "n1", "text": "hello"}])
        web_panel.write_reader_persistent_state(self.key, {"notes": [{"id": "n2", "text": "hello"}]})
        self.assertEqual(web_panel.read_reader_persistent_state(self.key)["notes"][0]["id"], "n2")

    def test_html_title_is_parsed_only_when_no_title_is_stored(self) -> None:
        root = web_panel.reader_cache_root(self.key)
        (root / "ocr").mkdir()
        (root / "ocr" / "paper.html").write_text("<html><head><title>From HTML</title></head></html>", encoding="utf-8")
        manifest = web_panel.read_reader_cache_manifest(self.key)
        manifest["document"] = {"storageRoot": "ocr", "documentFilename": "paper.html", "renderKind": "html",
                                "sourceName": "", "title": "Stored"}
        web_panel.write_reader_cache_manifest(self.key, manifest)
        with patch.object(web_panel, "_parse_ocr_html_title", wraps=web_panel._parse_ocr_html_title) as parse:
            self.assertEqual(web_panel.reader_cache_library_entry(self.key)["title"], "Stored")
            parse.assert_not_called()
            manifest["document"]["title"] = ""
            web_panel.write_reader_cache_manifest(self.key, manifest)
            self.assertEqual(web_panel.reader_cache_library_entry(self.key)["title"], "From HTML")
            self.assertEqual(web_panel.reader_cache_library_entry(self.key)["title"], "From HTML")
            self.assertEqual(parse.call_count, 1)


class SearchFoldedTextTest(unittest.TestCase):
    def test_reports_match_case_and_whitespace_insensitively(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            report = Path(folder) / "report.md"
            report.write_text("The   Sparse\nAttention result", encoding="utf-8")
            search = GlobalSearch(
                papers=lambda: [{"id": "k" * 64, "title": "Paper"}],
                notes=lambda key: [],
                analysis_files=lambda key: [("a1", report)],
            )
            hits = search.search("sparse attention")
            self.assertEqual([hit["kind"] for hit in hits], ["analysis"])
            self.assertEqual(search.search("absent words"), [])
            self.assertEqual(FileTextCache().read_entry(report)[1], "the sparse attention result")


class JobLaneTest(unittest.TestCase):
    def test_lanes(self) -> None:
        tool = lambda tool_id: next(spec for spec in web_panel.TOOLS + (web_panel.READER_TOOL,) if spec.id == tool_id)
        job = lambda tool_id: web_panel.Job(tool_id, tool(tool_id), Path(tempfile.gettempdir()), {})
        self.assertEqual(web_panel.job_lane(job("paper_reader")), "reader")
        self.assertEqual(web_panel.job_lane(job("pdf_ocr_translate")), "remote")
        self.assertEqual(web_panel.job_lane(job("video_crop")), "local")

    def test_a_long_local_job_does_not_block_the_reader_lane(self) -> None:
        release = threading.Event()
        finished: list[str] = []

        def fake_run(job) -> None:
            if job.tool.id == "video_crop":
                release.wait(5)
            finished.append(job.tool.id)

        tools = {spec.id: spec for spec in web_panel.TOOLS + (web_panel.READER_TOOL,)}
        with patch.object(web_panel, "run_job", fake_run):
            manager = web_panel.JobManager()
            manager.submit(web_panel.Job("slow", tools["video_crop"], Path(tempfile.gettempdir()), {}))
            manager.submit(web_panel.Job("fast", tools["paper_reader"], Path(tempfile.gettempdir()), {}))
            deadline = time.time() + 5
            while "paper_reader" not in finished and time.time() < deadline:
                time.sleep(0.01)
            self.assertEqual(finished, ["paper_reader"])
            release.set()
            manager.queues["local"].join()
        self.assertEqual(finished, ["paper_reader", "video_crop"])


if __name__ == "__main__":
    unittest.main()
