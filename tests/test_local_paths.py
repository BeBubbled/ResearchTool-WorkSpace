from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

from flask import Flask

from local_paths import LocalPathService, create_local_paths_blueprint


class LocalPathServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.search = self.root / "search"
        (self.search / "a" / "b").mkdir(parents=True)
        self.picked: list[str] = []
        self.service = LocalPathService(
            self.root / "state.json",
            picker=lambda kind, title, suffixes, initial: self.picked.pop(0) if self.picked else "",
            search_roots=lambda: [self.search],
        )
        app = Flask(__name__)
        app.register_blueprint(create_local_paths_blueprint(self.service))
        self.client = app.test_client()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write(self, relative: str, content: bytes, mtime: float | None = None) -> Path:
        path = self.search / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def locate(self, name: str, size: int, mtime: float | None) -> dict:
        response = self.client.post("/api/local-paths/locate", json={"name": name, "size": size, "lastModified": mtime * 1000 if mtime else None})
        self.assertEqual(response.status_code, 200, response.get_json())
        return response.get_json()

    def test_locates_unique_nested_match(self) -> None:
        stamp = time.time() - 1000
        target = self.write("a/b/paper.pdf", b"%PDF-1", stamp)
        self.write("a/paper.pdf", b"%PDF-other-size", stamp)
        result = self.locate("paper.pdf", 6, stamp)
        self.assertEqual(Path(result["path"]), target.resolve())

    def test_ambiguous_match_returns_candidates_only(self) -> None:
        stamp = time.time() - 1000
        self.write("a/paper.pdf", b"%PDF-1", stamp)
        self.write("a/b/paper.pdf", b"%PDF-1", stamp)
        result = self.locate("paper.pdf", 6, stamp)
        self.assertIsNone(result["path"])
        self.assertEqual(len(result["candidates"]), 2)

    def test_mtime_mismatch_is_not_auto_selected(self) -> None:
        self.write("a/paper.pdf", b"%PDF-1", time.time() - 5000)
        result = self.locate("paper.pdf", 6, time.time())
        self.assertIsNone(result["path"])
        self.assertEqual(len(result["candidates"]), 1)

    def test_name_cannot_traverse(self) -> None:
        result = self.locate("../../secret.pdf", 3, None)
        self.assertIsNone(result["path"])

    def test_pick_file_issues_download_token_and_remembers_dir(self) -> None:
        target = self.write("a/picked.pdf", b"%PDF-picked")
        self.picked.append(str(target))
        payload = self.client.post("/api/local-paths/pick", json={"kind": "file", "suffixes": [".pdf"]}).get_json()
        self.assertEqual(payload["path"], str(target))
        download = self.client.get(f"/api/local-paths/picked/{payload['token']}")
        self.assertEqual(download.data, b"%PDF-picked")
        download.close()
        self.assertIn(target.parent, self.service.recent_dirs())
        reloaded = LocalPathService(self.root / "state.json", picker=lambda *a: "", search_roots=lambda: [])
        self.assertIn(target.parent, reloaded.recent_dirs())

    def test_pick_rejects_wrong_suffix_and_handles_cancel(self) -> None:
        self.picked.append(str(self.write("a/notes.txt", b"x")))
        self.assertEqual(self.client.post("/api/local-paths/pick", json={"kind": "file", "suffixes": ["pdf"]}).status_code, 400)
        self.assertEqual(self.client.post("/api/local-paths/pick", json={"kind": "folder"}).get_json(), {"cancelled": True})

    def test_unknown_token_is_rejected(self) -> None:
        self.assertEqual(self.client.get("/api/local-paths/picked/nope").status_code, 404)


if __name__ == "__main__":
    unittest.main()
