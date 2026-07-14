"""Smoke tests for the local toolbox API."""

from __future__ import annotations

import io
import json
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

import web_panel
from PIL import Image


class ToolboxApiTest(unittest.TestCase):
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

    def test_tools_are_listed(self) -> None:
        response = self.client.get("/api/tools")
        self.assertEqual(response.status_code, 200)
        ids = {tool["id"] for tool in response.get_json()["tools"]}
        self.assertTrue({"anki", "image_crop", "stack_videos", "bibtex"}.issubset(ids))

    def test_unknown_tool_is_rejected(self) -> None:
        response = self.client.post("/api/jobs", data={"tool": "nope"})
        self.assertEqual(response.status_code, 400)

    def test_anki_job_completes_and_downloads(self) -> None:
        manifest = [{"relativePath": "cards.csv", "workName": "ordered cards"}]
        response = self.client.post(
            "/api/jobs",
            data={
                "tool": "anki",
                "options": json.dumps({"front": "Front", "back": "Back"}),
                "manifest": json.dumps(manifest),
                "files": (io.BytesIO(b"Front,Back\nQ1,A1\nQ2,A2\n"), "cards.csv"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 202)
        job_id = response.get_json()["id"]
        for _ in range(50):
            status = self.client.get(f"/api/jobs/{job_id}").get_json()
            if status["status"] in {"completed", "failed"}:
                break
            time.sleep(0.02)
        self.assertEqual(status["status"], "completed", status["logs"])
        download = self.client.get(f"/api/jobs/{job_id}/download")
        self.assertEqual(download.status_code, 200)
        self.assertIn(b"Q1\tA1", download.data)
        download.close()

    def test_batch_image_job_returns_zip(self) -> None:
        image_bytes = io.BytesIO()
        Image.new("RGB", (4, 4), "white").save(image_bytes, format="PNG")
        image_bytes.seek(0)
        response = self.client.post(
            "/api/jobs",
            data={
                "tool": "image_crop",
                "options": json.dumps({"crop": 2, "out": 4}),
                "manifest": json.dumps([{"relativePath": "folder/source.png", "workName": "first"}]),
                "files": (image_bytes, "source.png"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 202)
        job_id = response.get_json()["id"]
        for _ in range(100):
            status = self.client.get(f"/api/jobs/{job_id}").get_json()
            if status["status"] in {"completed", "failed"}:
                break
            time.sleep(0.03)
        self.assertEqual(status["status"], "completed", status["logs"])
        download = self.client.get(f"/api/jobs/{job_id}/download")
        self.assertEqual(download.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(download.data)) as archive:
            self.assertTrue(any(name.endswith("0001_first.png") for name in archive.namelist()))
        download.close()


if __name__ == "__main__":
    unittest.main()
