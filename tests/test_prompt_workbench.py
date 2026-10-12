"""Prompt persistence, local access boundaries, and binary handoff lifecycle."""
import base64
import io
import json
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from flask import Flask

from prompt_workbench import CHUNK_BYTES, TRANSFER_TTL, PromptWorkbench, create_prompt_blueprint

ROOT = Path(__file__).resolve().parents[1]
HEADERS = {"X-Prompt-Workbench": "1"}


class PromptWorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = PromptWorkbench(Path(self.temp.name), ROOT / "browser_extension/prompt_bridge")
        app = Flask(__name__, template_folder=str(ROOT / "templates"))
        app.config["TESTING"] = True
        app.register_blueprint(create_prompt_blueprint(self.service))
        self.client = app.test_client()

    def tearDown(self):
        self.temp.cleanup()

    def transfer(self, content=b"attachment", name="论文.txt"):
        result = self.client.post("/api/prompts/transfers", data={"prompt": "请分析\n\n保留公式 α", "files": (io.BytesIO(content), name)}, headers=HEADERS)
        self.assertEqual(result.status_code, 201, result.get_data(as_text=True))
        data = result.get_json()
        return data["id"], {**HEADERS, "Authorization": f"Bearer {data['token']}"}

    def test_library_crud_persists_and_empty_library_is_not_reseeded(self):
        defaults = self.client.get("/api/prompts/templates").get_json()["templates"]
        self.assertGreaterEqual(len(defaults), 3)
        item = self.client.post("/api/prompts/templates", json={"title": "自定义", "category": "科研", "content": "α <script>不是脚本</script>"}, headers=HEADERS).get_json()["template"]
        self.client.put(f"/api/prompts/templates/{item['id']}", json={"title": "新名称", "content": "新内容"}, headers=HEADERS)
        restarted = PromptWorkbench(self.service.root, self.service.extension_dir)
        self.assertEqual(restarted.templates()[-1]["content"], "新内容")
        for row in restarted.templates():
            self.assertEqual(self.client.delete(f"/api/prompts/templates/{row['id']}", headers=HEADERS).status_code, 200)
        self.assertEqual(restarted.templates(), [])

    def test_invalid_template_inputs_do_not_change_library(self):
        for payload in ([], {"title": " ", "content": "x"}, {"title": "x", "content": 5}, {"title": "x" * 81, "content": "y"}):
            self.assertEqual(self.client.post("/api/prompts/templates", json=payload, headers=HEADERS).status_code, 400)
        self.assertFalse((self.service.root / "templates.json").exists())

    def test_cross_site_and_rebound_hosts_are_rejected(self):
        self.assertEqual(self.client.post("/api/prompts/templates", json={}).status_code, 403)
        for headers in ({"Origin":"https://evil.example"}, {"Origin":"null"}, {"Sec-Fetch-Site":"cross-site"}):
            self.assertEqual(self.client.get("/api/prompts/templates", headers=headers).status_code, 403)
        self.assertEqual(self.client.get("/prompts", base_url="http://attacker.example").status_code, 403)

    def test_binary_chunks_preserve_bytes_and_require_capability(self):
        content = bytes(range(256)) * (CHUNK_BYTES // 256 + 7)
        transfer_id, headers = self.transfer(content, "../路径/实验.bin")
        path = f"/api/prompts/transfers/{transfer_id}"
        self.assertEqual(self.client.get(path).status_code, 404)
        self.assertEqual(self.client.get(path, headers={"Authorization":"Bearer wrong"}).status_code, 404)
        metadata = self.client.get(path, headers=headers).get_json()
        self.assertNotIn("token", metadata)
        self.assertEqual(metadata["files"][0]["name"], "实验.bin")
        extension_headers = {**headers, "Origin":"chrome-extension://" + "a" * 32}
        first = self.client.get(path + "/files/0?offset=0", headers=extension_headers).get_json()
        second = self.client.get(path + f"/files/0?offset={first['nextOffset']}", headers=extension_headers).get_json()
        self.assertFalse(first["done"])
        self.assertTrue(second["done"])
        self.assertEqual(base64.b64decode(first["data"]) + base64.b64decode(second["data"]), content)
        self.assertEqual(self.client.get(path + "/files/0?offset=-1", headers=headers).status_code, 400)
        self.assertEqual(self.client.get(path + "/files/0?offset=999999999", headers=headers).status_code, 400)
        self.assertEqual(self.client.get(path + "/files/8", headers=headers).status_code, 404)

    def test_terminal_status_cleans_files_and_cannot_be_reopened(self):
        for status in ("completed", "error", "cancelled"):
            transfer_id, headers = self.transfer()
            path = f"/api/prompts/transfers/{transfer_id}"
            result = self.client.post(path + "/status", headers=headers, json={"status":status,"message":"done"})
            self.assertEqual(result.get_json()["status"], status)
            self.assertFalse((self.service.root / "transfers" / transfer_id).exists())
            result = self.client.post(path + "/status", headers=headers, json={"status":"opening"})
            self.assertEqual(result.get_json()["status"], status)
            self.assertEqual(self.client.get(path + "/files/0", headers=headers).status_code, 404)

    def test_expiration_removes_files_and_capability(self):
        transfer_id, headers = self.transfer()
        with patch("prompt_workbench.time.time", return_value=time.time() + TRANSFER_TTL + 1):
            self.assertEqual(self.client.get(f"/api/prompts/transfers/{transfer_id}", headers=headers).status_code, 404)
        self.assertFalse((self.service.root / "transfers" / transfer_id).exists())

    def test_size_and_count_limits_leave_no_partial_upload(self):
        with patch("prompt_workbench.MAX_BYTES", 10):
            response = self.client.post("/api/prompts/transfers", data={"prompt":"test","files":(io.BytesIO(b"12345678901"),"x.txt")}, headers=HEADERS)
        self.assertEqual(response.status_code, 413)
        self.assertEqual(list((self.service.root / "transfers").iterdir()), [])
        response = self.client.post("/api/prompts/transfers", data={"prompt":"test","files":[(io.BytesIO(b"x"),f"{i}.txt") for i in range(21)]}, headers=HEADERS)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client.post("/api/prompts/transfers", data={"prompt":" "}, headers=HEADERS).status_code, 400)

    def test_page_and_extension_archive_are_available(self):
        self.assertIn("提交到 ChatGPT", self.client.get("/prompts").get_data(as_text=True))
        result = self.client.get("/api/prompts/extension.zip")
        with zipfile.ZipFile(io.BytesIO(result.data)) as archive:
            manifest = json.loads(archive.read("research-prompt-bridge/manifest.json"))
            self.assertEqual(manifest["manifest_version"], 3)
            self.assertEqual(len(archive.namelist()), 5)


if __name__ == "__main__":
    unittest.main()
