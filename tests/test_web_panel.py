"""Smoke tests for the local toolbox API."""

from __future__ import annotations

import io
import json
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import web_panel
from PIL import Image


class FakeResponse:
    def __init__(self, payload=None, content=b"", ok=True, text=""):
        self.payload = payload if payload is not None else {}
        self.content = content
        self.ok = ok
        self.text = text
        self.reason = "mock response"

    def json(self):
        return self.payload


def markdown_bundle(filename: str, text: str, image_name: str = "figure.png") -> bytes:
    content = io.BytesIO()
    with zipfile.ZipFile(content, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(filename, text)
        archive.writestr(f"images/{image_name}", b"image bytes")
    return content.getvalue()


class ToolboxApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.previous_jobs_dir = web_panel.JOBS_DIR
        web_panel.JOBS_DIR = Path(self.temp_dir.name) / "jobs"
        web_panel.JOBS_DIR.mkdir()
        with web_panel.reader_manager.lock:
            web_panel.reader_manager.documents.clear()
        web_panel.app.config.update(TESTING=True)
        self.client = web_panel.app.test_client()

    def tearDown(self) -> None:
        web_panel.JOBS_DIR = self.previous_jobs_dir
        with web_panel.reader_manager.lock:
            web_panel.reader_manager.documents.clear()
        self.temp_dir.cleanup()

    def test_tools_are_listed(self) -> None:
        response = self.client.get("/api/tools")
        self.assertEqual(response.status_code, 200)
        ids = {tool["id"] for tool in response.get_json()["tools"]}
        self.assertTrue({"anki", "image_crop", "stack_videos", "bibtex"}.issubset(ids))

    def test_reader_opens_markdown_and_builds_context_blocks(self) -> None:
        response = self.client.post(
            "/api/reader/documents",
            data={
                "mode": "ocr",
                "file": (io.BytesIO(b"# Diffusion Models\n\nThe score $s_\\theta(x,t)$ estimates noise.\n\n$$\n x_t = \\alpha_t x_0 + \\sigma_t \\epsilon\n$$\n"), "paper.md"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 202, response.get_json())
        document_id = response.get_json()["id"]
        for _ in range(100):
            status = self.client.get(f"/api/reader/documents/{document_id}").get_json()
            if status["status"] in {"ready", "failed"}:
                break
            time.sleep(0.02)
        self.assertEqual(status["status"], "ready", status)
        content = self.client.get(f"/api/reader/documents/{document_id}/content").get_json()
        self.assertEqual(content["blocks"][0]["type"], "heading")
        self.assertIn("score", content["blocks"][1]["content"])
        self.assertEqual(content["blocks"][2]["type"], "math")

    def test_research_reader_prompt_specializes_formula_geometry(self) -> None:
        config = {"name": "test", "baseUrl": "https://llm.example", "apiKey": "key", "model": "model"}
        block = {"id": "b2", "section": "Method", "content": "$x_t = \\alpha_t x_0 + \\sigma_t \\epsilon$"}
        with patch.object(web_panel, "OpenAI") as client_class:
            client = client_class.return_value
            client.chat.completions.create.return_value = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="几何解释"))])
            answer = web_panel.ask_reader_llm(config, "geometry", "x_t", block, [block], "")
        self.assertEqual(answer, "几何解释")
        call = client.chat.completions.create.call_args.kwargs
        self.assertIn("扩散模型", call["messages"][0]["content"])
        self.assertIn("几何意义", call["messages"][1]["content"])
        self.assertIn("$x_t", call["messages"][1]["content"])

    def test_reader_markdown_parser_keeps_mathpix_latex_tables_and_lists_together(self) -> None:
        blocks = web_panel.markdown_to_reader_blocks(
            "\\section{Method}\n\n"
            "\\begin{align}\n"
            "x_t &= \\alpha_t x_0 + \\sigma_t \\epsilon \\\\n"
            "\\epsilon &\\sim \\mathcal{N}(0, I)\n"
            "\\end{align}\n\n"
            "| Model | FID |\n| --- | ---: |\n| DDPM | 3.1 |\n\n"
            "- first\n  - nested\n"
        )
        self.assertEqual([block["type"] for block in blocks], ["heading", "math", "table", "list"])
        self.assertIn("\\begin{align}", blocks[1]["content"])
        self.assertIn("\\end{align}", blocks[1]["content"])
        self.assertIn("DDPM", blocks[2]["content"])
        self.assertIn("nested", blocks[3]["content"])

    def test_reader_can_persist_a_named_llm_preset_locally(self) -> None:
        env_file = Path(self.temp_dir.name) / ".env"
        with patch.object(web_panel, "ENV_FILE", env_file), patch.dict("os.environ", {}, clear=True):
            preset = web_panel.save_llm_preset("Lab Gateway", "https://llm.example/v1", "secret-key", "research-model")
            self.assertEqual(preset["id"], "lab_gateway")
            self.assertIn("LLM_PRESETS='LAB_GATEWAY'", env_file.read_text(encoding="utf-8"))
            self.assertIn("LLM_PRESET_LAB_GATEWAY_API_KEY='secret-key'", env_file.read_text(encoding="utf-8"))
            with self.assertRaises(ValueError):
                web_panel.save_llm_preset("Lab Gateway", "https://llm.example/v1", "other-key", "research-model")

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

    def wait_for_job(self, job_id: str):
        for _ in range(100):
            status = self.client.get(f"/api/jobs/{job_id}").get_json()
            if status["status"] in {"completed", "completed_with_warnings", "failed"}:
                return status
            time.sleep(0.02)
        self.fail("Job did not finish in time.")

    def test_pdf_ocr_downloads_named_artifacts_and_warns_for_failed_conversion(self) -> None:
        crop_url = "https://cdn.mathpix.com/cropped/paper-02.jpg?height=1105&width=1369&top_left_y=275&top_left_x=380"
        crop_name = "paper-02_1105_1369_275_380.jpg"
        fallback_url = "https://cdn.mathpix.com/cropped/paper-03.jpg?height=400&width=600&top_left_y=20&top_left_x=30"
        fallback_name = "paper-03_400_600_20_30.jpg"
        fallback_requests = {"count": 0}
        mmd_bundle = markdown_bundle(
            "paper.mmd",
            f"\\includegraphics{{./images/{crop_name}}}\n",
            crop_name,
        )

        def fake_get(url, **_kwargs):
            if url.endswith("/pdf/mock-pdf"):
                return FakeResponse({"status": "completed", "percent_done": 100})
            if url.endswith("/converter/mock-pdf"):
                return FakeResponse({"conversion_status": {
                    "docx": {"status": "completed"}, "mmd.zip": {"status": "completed"}, "md": {"status": "completed"},
                    "html": {"status": "completed"}, "tex.zip": {"status": "error"},
                }})
            if url.endswith(".mmd.zip"):
                return FakeResponse(content=mmd_bundle)
            if url.endswith(".md"):
                return FakeResponse(content=f"![Figure]({crop_url})\n![Fallback]({fallback_url})\n".encode())
            if url.endswith(".html"):
                return FakeResponse(content=f'<link rel="stylesheet" href="https://cdn.mathpix.com/fonts/cmu.css"><img src="{crop_url}"><source srcset="{fallback_url} 2x">'.encode())
            if url.endswith(".lines.json"):
                return FakeResponse(content=json.dumps({"text_display": f"\\includegraphics{{{crop_url}}}"}).encode())
            if url == fallback_url:
                fallback_requests["count"] += 1
                return FakeResponse(content=b"fallback image")
            extension = url.rsplit(".", 1)[-1]
            return FakeResponse(content=f"mock {extension}".encode())

        original_pdf = Path(self.temp_dir.name) / "paper.pdf"
        original_pdf.write_bytes(b"original PDF location")
        with patch.dict("os.environ", {"MATHPIX_APP_ID": "id", "MATHPIX_APP_KEY": "key", "LLM_BASE_URL": "https://llm.example", "LLM_API_KEY": "llm-key", "LLM_MODEL": "model"}), \
             patch.object(web_panel.requests, "post", return_value=FakeResponse({"pdf_id": "mock-pdf"})) as post, \
             patch.object(web_panel.requests, "get", side_effect=fake_get), \
             patch.object(web_panel, "POLL_INTERVAL_SECONDS", 0):
            response = self.client.post(
                "/api/jobs",
                data={
                    "tool": "pdf_ocr_translate", "options": json.dumps({"ocrFormats": ["docx", "md", "html", "tex.zip"], "localSourcePath": str(original_pdf)}),
                    "manifest": json.dumps([{ "relativePath": "paper.pdf", "workName": "ignored" }]),
                    "files": (io.BytesIO(b"%PDF mock"), "paper.pdf"),
                },
                content_type="multipart/form-data",
            )
            self.assertEqual(response.status_code, 202, response.get_json())
            job_id = response.get_json()["id"]
            status = self.wait_for_job(job_id)
        self.assertEqual(status["status"], "completed_with_warnings", status["logs"])
        names = {artifact["name"] for artifact in status["artifacts"]}
        self.assertEqual(names, {"paper.mmd", "paper.lines.json", "paper.docx", "paper.md", "paper.html"})
        self.assertTrue(any("tex.zip" in warning for warning in status["warnings"]))
        options_json = json.loads(post.call_args.kwargs["data"]["options_json"])
        self.assertEqual(options_json["conversion_formats"], {"docx": True, "md": True, "html": True, "tex.zip": True, "mmd.zip": True})
        artifact = self.client.get(f"/api/jobs/{job_id}/artifacts/paper.mmd")
        self.assertEqual(artifact.status_code, 200)
        artifact.close()
        archive = self.client.get(f"/api/jobs/{job_id}/download")
        with zipfile.ZipFile(io.BytesIO(archive.data)) as bundle:
            self.assertEqual(set(bundle.namelist()), names | {f"paper.assets/{crop_name}", f"paper.assets/{fallback_name}"})
        archive.close()
        local_output = original_pdf.with_suffix("")
        self.assertEqual({path.name for path in local_output.iterdir()}, names | {"paper.pdf", "paper.assets"})
        self.assertEqual((local_output / "paper.pdf").read_bytes(), b"%PDF mock")
        self.assertEqual((local_output / "paper.assets" / crop_name).read_bytes(), b"image bytes")
        self.assertEqual((local_output / "paper.assets" / fallback_name).read_bytes(), b"fallback image")
        self.assertEqual(
            (local_output / "paper.mmd").read_text(encoding="utf-8"),
            f"\\includegraphics{{paper.assets/{crop_name}}}\n",
        )
        self.assertEqual((local_output / "paper.md").read_text(encoding="utf-8"), f"![Figure](paper.assets/{crop_name})\n![Fallback](paper.assets/{fallback_name})\n")
        html = (local_output / "paper.html").read_text(encoding="utf-8")
        self.assertIn(f'src="paper.assets/{crop_name}"', html)
        self.assertIn(f"paper.assets/{fallback_name} 2x", html)
        self.assertIn("https://cdn.mathpix.com/fonts/cmu.css", html)
        lines = json.loads((local_output / "paper.lines.json").read_text(encoding="utf-8"))
        self.assertEqual(lines["text_display"], f"\\includegraphics{{paper.assets/{crop_name}}}")
        self.assertEqual(fallback_requests["count"], 1)

    def test_pdf_ocr_requests_only_the_mmd_bundle_when_markdown_is_not_selected(self) -> None:
        job = web_panel.Job("mmd-only", web_panel.TOOL_BY_ID["pdf_ocr_translate"], Path(self.temp_dir.name) / "mmd-only", {"ocrFormats": ["docx"]})
        input_dir = job.root / "input"
        input_dir.mkdir(parents=True)
        source = input_dir / "paper.pdf"
        source.write_bytes(b"%PDF mock")
        job.source_stem = "paper"
        bundle = markdown_bundle("paper.mmd", "![Figure](./images/figure.png)\n")

        def fake_get(url, **_kwargs):
            if url.endswith("/pdf/mock-pdf"):
                return FakeResponse({"status": "completed"})
            if url.endswith("/converter/mock-pdf"):
                return FakeResponse({"conversion_status": {"docx": {"status": "completed"}, "mmd.zip": {"status": "completed"}}})
            if url.endswith(".mmd.zip"):
                return FakeResponse(content=bundle)
            return FakeResponse(content=b"mock")

        with patch.dict("os.environ", {"MATHPIX_APP_ID": "id", "MATHPIX_APP_KEY": "key"}), \
             patch.object(web_panel.requests, "post", return_value=FakeResponse({"pdf_id": "mock-pdf"})) as post, \
             patch.object(web_panel.requests, "get", side_effect=fake_get), \
             patch.object(web_panel, "POLL_INTERVAL_SECONDS", 0):
            web_panel.run_pdf_ocr(job, [source])

        requested = json.loads(post.call_args.kwargs["data"]["options_json"])["conversion_formats"]
        self.assertEqual(requested, {"docx": True, "mmd.zip": True})
        self.assertFalse((job.root / "output" / "paper.md").exists())
        self.assertTrue((job.root / "output" / "paper.assets" / "figure.png").exists())

    def test_pdf_ocr_warns_and_omits_markdown_when_its_conversion_fails(self) -> None:
        job = web_panel.Job("md-bundle-failure", web_panel.TOOL_BY_ID["pdf_ocr_translate"], Path(self.temp_dir.name) / "md-bundle-failure", {"ocrFormats": ["md"]})
        input_dir = job.root / "input"
        input_dir.mkdir(parents=True)
        source = input_dir / "paper.pdf"
        source.write_bytes(b"%PDF mock")
        job.source_stem = "paper"
        bundle = markdown_bundle("paper.mmd", "![Figure](./images/figure.png)\n")

        def fake_get(url, **_kwargs):
            if url.endswith("/pdf/mock-pdf"):
                return FakeResponse({"status": "completed"})
            if url.endswith("/converter/mock-pdf"):
                return FakeResponse({"conversion_status": {"mmd.zip": {"status": "completed"}, "md": {"status": "error"}}})
            if url.endswith(".mmd.zip"):
                return FakeResponse(content=bundle)
            return FakeResponse(content=b"mock")

        with patch.dict("os.environ", {"MATHPIX_APP_ID": "id", "MATHPIX_APP_KEY": "key"}), \
             patch.object(web_panel.requests, "post", return_value=FakeResponse({"pdf_id": "mock-pdf"})), \
             patch.object(web_panel.requests, "get", side_effect=fake_get), \
             patch.object(web_panel, "POLL_INTERVAL_SECONDS", 0):
            web_panel.run_pdf_ocr(job, [source])

        self.assertTrue((job.root / "output" / "paper.mmd").exists())
        self.assertFalse((job.root / "output" / "paper.md").exists())
        self.assertIn("Mathpix could not convert md.", job.warnings)

    def test_pdf_ocr_fails_when_the_required_mmd_bundle_cannot_be_downloaded(self) -> None:
        job = web_panel.Job("mmd-bundle-failure", web_panel.TOOL_BY_ID["pdf_ocr_translate"], Path(self.temp_dir.name) / "mmd-bundle-failure", {})
        input_dir = job.root / "input"
        input_dir.mkdir(parents=True)
        source = input_dir / "paper.pdf"
        source.write_bytes(b"%PDF mock")
        job.source_stem = "paper"

        def fake_get(url, **_kwargs):
            if url.endswith("/pdf/mock-pdf"):
                return FakeResponse({"status": "completed"})
            if url.endswith("/converter/mock-pdf"):
                return FakeResponse({"conversion_status": {"docx": {"status": "completed"}, "html": {"status": "completed"}, "tex.zip": {"status": "completed"}, "mmd.zip": {"status": "completed"}, "md": {"status": "completed"}}})
            if url.endswith(".mmd.zip"):
                return FakeResponse(ok=False, text="bundle missing")
            return FakeResponse(content=b"mock")

        with patch.dict("os.environ", {"MATHPIX_APP_ID": "id", "MATHPIX_APP_KEY": "key"}), \
             patch.object(web_panel.requests, "post", return_value=FakeResponse({"pdf_id": "mock-pdf"})), \
             patch.object(web_panel.requests, "get", side_effect=fake_get), \
             patch.object(web_panel, "POLL_INTERVAL_SECONDS", 0):
            with self.assertRaisesRegex(RuntimeError, "mmd.zip"):
                web_panel.run_pdf_ocr(job, [source])

        self.assertFalse((job.root / "output" / "paper.mmd").exists())

    def test_unavailable_fallback_image_keeps_its_url_and_records_a_warning(self) -> None:
        job = web_panel.Job("asset-warning", web_panel.TOOL_BY_ID["pdf_ocr_translate"], Path(self.temp_dir.name) / "asset-warning", {})
        assets_dir = job.root / "output" / "paper.assets"
        assets_dir.mkdir(parents=True)
        assets = web_panel.LocalAssetStore(assets_dir)
        image_url = "https://cdn.mathpix.com/cropped/paper-04.jpg?height=10&width=20&top_left_y=30&top_left_x=40"

        with patch.object(web_panel.requests, "get", return_value=FakeResponse(ok=False, text="expired")):
            rewritten = web_panel.rewrite_text_asset_references(job, f"![Missing]({image_url})", assets)

        self.assertEqual(rewritten, f"![Missing]({image_url})")
        self.assertTrue(any(image_url in warning for warning in job.warnings))

    def test_translation_protects_markdown_literals_and_html_structure(self) -> None:
        source = "Text with \\(x^2\\), [link](https://example.com), and `code`."
        protected, literals = web_panel.protect_literals(source)
        self.assertIn("[link]", protected)
        self.assertNotIn("https://example.com", protected)
        self.assertNotIn(r"\(x^2\)", protected)
        self.assertIn("https://example.com", literals.values())
        with patch.object(web_panel, "llm_translate", side_effect=lambda _job, text: text):
            self.assertEqual(web_panel.translate_text_block(None, source), source)
            root = Path(self.temp_dir.name)
            html_source = root / "source.html"
            html_output = root / "translated.html"
            html_source.write_text('<p>Hello <a href="https://example.com">world</a></p><script>keep()</script>', encoding="utf-8")
            web_panel.translate_html(None, html_source, html_output)
        html = html_output.read_text(encoding="utf-8")
        self.assertIn('href="https://example.com"', html)
        self.assertIn("keep()", html)
        calls: list[str] = []

        def fake_translate(_job: object, value: str) -> str:
            calls.append(value)
            return "changed"

        with patch.object(web_panel, "llm_translate", side_effect=fake_translate):
            translated = web_panel.translate_text_block(None, source)
        self.assertTrue(any("[[[KEEP_" in call for call in calls))
        self.assertTrue(any("[[[KEEP_" not in call for call in calls))
        for literal in literals.values():
            self.assertIn(literal, translated)

        marker = next(iter(literals))
        with self.assertRaisesRegex(RuntimeError, "protected"):
            web_panel.restore_literals(protected.replace(marker, "changed"), literals)

    def test_document_translation_handles_folder_uploads_and_prioritizes_markdown(self) -> None:
        class FakeOpenAI:
            def __init__(self, **_kwargs):
                self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

            @staticmethod
            def create(**kwargs):
                content = kwargs["messages"][-1]["content"]
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=f"ZH:{content}"))])

        llm = {"mode": "custom", "name": "Temporary", "baseUrl": "https://custom.example/v1", "apiKey": "custom-secret", "model": "custom-model"}
        manifest = [
            {"relativePath": "research/paper.mmd", "workName": "paper"},
            {"relativePath": "research/paper.md", "workName": "paper"},
            {"relativePath": "research/notes/page.html", "workName": "page"},
        ]
        with patch.object(web_panel, "OpenAI", FakeOpenAI):
            response = self.client.post(
                "/api/jobs",
                data={
                    "tool": "document_translate",
                    "options": json.dumps({"llm": llm}),
                    "manifest": json.dumps(manifest),
                    "files": [
                        (io.BytesIO(b"MMD source"), "paper.mmd"),
                        (io.BytesIO(b"Markdown source"), "paper.md"),
                        (io.BytesIO(b"<p>HTML source</p>"), "page.html"),
                    ],
                },
                content_type="multipart/form-data",
            )
            self.assertEqual(response.status_code, 202, response.get_json())
            job_id = response.get_json()["id"]
            status = self.wait_for_job(job_id)

        self.assertEqual(status["status"], "completed", status["logs"])
        translation_logs = [line for line in status["logs"] if line.startswith("[TRANSLATION] (")]
        self.assertIn("research/0002_paper.md", translation_logs[0])
        self.assertTrue(any("research/0001_paper.mmd" in line for line in translation_logs[1:]))
        self.assertTrue(any("research/notes/0003_page.html" in line for line in translation_logs[1:]))
        job = web_panel.job_manager.get(job_id)
        self.assertIsNotNone(job)
        assert job is not None
        self.assertIsNone(job.translation_config)
        self.assertNotIn("llm", job.options)
        download = self.client.get(f"/api/jobs/{job_id}/download")
        self.assertEqual(download.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(download.data)) as archive:
            names = set(archive.namelist())
            self.assertEqual(
                names,
                {
                    "research/0001_paper_zh-CN.mmd",
                    "research/0002_paper_zh-CN.md",
                    "research/notes/0003_page_zh-CN.html",
                },
            )
            self.assertIn(b"ZH:Markdown source", archive.read("research/0002_paper_zh-CN.md"))
        download.close()

    def test_pdf_translation_rejects_unavailable_config_and_unsupported_output(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            response = self.client.post(
                "/api/jobs",
                data={"tool": "pdf_ocr_translate", "options": "{}", "manifest": "[]"},
                content_type="multipart/form-data",
            )
        self.assertEqual(response.status_code, 503)
        self.assertIn("MATHPIX_APP_ID", response.get_json()["error"])

        job = web_panel.Job("translation-test", web_panel.TOOL_BY_ID["pdf_ocr_translate"], Path(self.temp_dir.name) / "translation-test", {})
        output_dir = job.root / "output"
        output_dir.mkdir(parents=True)
        docx = output_dir / "paper.docx"
        docx.write_bytes(b"docx")
        web_panel.add_artifact(job, docx, "ocr", "DOCX")
        job.status = "completed"
        web_panel.job_manager.jobs[job.id] = job
        with patch.dict("os.environ", {"LLM_BASE_URL": "https://llm.example", "LLM_API_KEY": "key", "LLM_MODEL": "model"}):
            response = self.client.post(f"/api/jobs/{job.id}/translations", json={"artifactId": "paper.docx", "llm": {"mode": "preset", "presetId": "default"}})
        self.assertEqual(response.status_code, 400)

    def test_pdf_translation_reuses_ocr_job_and_rejects_duplicate_output(self) -> None:
        job = web_panel.Job("translation-mmd", web_panel.TOOL_BY_ID["pdf_ocr_translate"], Path(self.temp_dir.name) / "translation-mmd", {})
        output_dir = job.root / "output"
        output_dir.mkdir(parents=True)
        source = output_dir / "paper.mmd"
        source.write_text("A formula: \\(x^2\\).", encoding="utf-8")
        job.local_save_dir = Path(self.temp_dir.name) / "saved-paper"
        job.local_save_dir.mkdir()
        web_panel.add_artifact(job, source, "ocr", "Mathpix Markdown")
        job.status = "completed"
        web_panel.job_manager.jobs[job.id] = job

        class FakeOpenAI:
            def __init__(self, **_kwargs):
                self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

            @staticmethod
            def create(**kwargs):
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=kwargs["messages"][-1]["content"]))])

        with patch.dict("os.environ", {"LLM_BASE_URL": "https://llm.example", "LLM_API_KEY": "key", "LLM_MODEL": "model"}), \
             patch.object(web_panel, "OpenAI", FakeOpenAI):
            custom_llm = {"mode": "custom", "name": "Temporary", "baseUrl": "https://custom.example/v1", "apiKey": "custom-secret", "model": "custom-model"}
            response = self.client.post(f"/api/jobs/{job.id}/translations", json={"artifactId": "paper.mmd", "llm": custom_llm})
            self.assertEqual(response.status_code, 202, response.get_json())
            status = self.wait_for_job(job.id)
            duplicate = self.client.post(f"/api/jobs/{job.id}/translations", json={"artifactId": "paper.mmd", "llm": custom_llm})
        self.assertEqual(status["status"], "completed", status["logs"])
        translated = output_dir / "paper_zh-CN.mmd"
        self.assertEqual(translated.read_text(encoding="utf-8"), "A formula: \\(x^2\\).")
        self.assertEqual((job.local_save_dir / "paper_zh-CN.mmd").read_text(encoding="utf-8"), "A formula: \\(x^2\\).")
        self.assertIn("paper_zh-CN.mmd", {artifact["name"] for artifact in status["artifacts"]})
        self.assertIsNone(job.translation_config)
        self.assertEqual(duplicate.status_code, 409)

    def test_failed_translation_keeps_a_downloadable_partial_result_and_progress(self) -> None:
        job = web_panel.Job("translation-partial", web_panel.TOOL_BY_ID["pdf_ocr_translate"], Path(self.temp_dir.name) / "translation-partial", {})
        output_dir = job.root / "output"
        output_dir.mkdir(parents=True)
        source = output_dir / "paper.mmd"
        source.write_text("A" * 6000 + "\n\n" + "B" * 6000, encoding="utf-8")
        job.local_save_dir = Path(self.temp_dir.name) / "saved-paper"
        job.local_save_dir.mkdir()
        job.operation = "translate"
        job.pending_artifact_id = source.name
        job.translation_config = {"name": "Test", "baseUrl": "https://llm.example", "apiKey": "key", "model": "model"}
        web_panel.add_artifact(job, source, "ocr", "Mathpix Markdown")

        with patch.object(web_panel, "llm_translate", side_effect=["甲" * 6000, RuntimeError("provider unavailable")]):
            web_panel.job_manager.submit(job)
            status = self.wait_for_job(job.id)

        partial = output_dir / "paper_zh-CN.partial.mmd"
        self.assertEqual(status["status"], "failed", status["logs"])
        self.assertEqual(status["phase"], "translation_partial")
        self.assertEqual(status["translationProgress"], {"completed": 2, "total": 3})
        self.assertTrue(partial.exists())
        self.assertEqual(partial.read_text(encoding="utf-8"), "甲" * 6000 + "\n\n")
        self.assertTrue((job.local_save_dir / partial.name).exists())
        artifact_names = {artifact["name"] for artifact in status["artifacts"]}
        self.assertIn(partial.name, artifact_names)
        download = self.client.get(f"/api/jobs/{job.id}/artifacts/{partial.name}")
        self.assertEqual(download.status_code, 200)
        download.close()

    def test_named_llm_presets_hide_keys_and_custom_config_is_validated(self) -> None:
        environment = {
            "LLM_NAME": "Legacy", "LLM_BASE_URL": "https://legacy.example/v1", "LLM_API_KEY": "legacy-secret", "LLM_MODEL": "legacy-model",
            "LLM_PRESETS": "deepseek,company_proxy",
            "LLM_PRESET_DEEPSEEK_NAME": "DeepSeek", "LLM_PRESET_DEEPSEEK_BASE_URL": "https://api.deepseek.com", "LLM_PRESET_DEEPSEEK_API_KEY": "deepseek-secret", "LLM_PRESET_DEEPSEEK_MODEL": "deepseek-chat",
            "LLM_PRESET_COMPANY_PROXY_NAME": "Company Proxy", "LLM_PRESET_COMPANY_PROXY_BASE_URL": "https://llm.company.example/v1", "LLM_PRESET_COMPANY_PROXY_API_KEY": "company-secret", "LLM_PRESET_COMPANY_PROXY_MODEL": "proxy-model",
        }
        with patch.dict("os.environ", environment, clear=True):
            presets = web_panel.public_llm_presets()
            self.assertEqual([item["id"] for item in presets], ["default", "deepseek", "company_proxy"])
            self.assertNotIn("apiKey", presets[0])
            selected = web_panel.translation_config_from_request({"llm": {"mode": "preset", "presetId": "company_proxy"}})
            custom = web_panel.translation_config_from_request({"llm": {"mode": "custom", "name": "Temporary", "baseUrl": "https://custom.example/v1", "apiKey": "custom-secret", "model": "custom-model"}})
        self.assertEqual(selected["apiKey"], "company-secret")
        self.assertEqual(custom["name"], "Temporary")
        self.assertEqual(custom["model"], "custom-model")

    def test_pdf_ocr_exposes_safe_local_llm_presets(self) -> None:
        environment = {
            "LLM_NAME": "Local provider",
            "LLM_BASE_URL": "https://llm.example/v1",
            "LLM_API_KEY": "local-secret",
            "LLM_MODEL": "local-model",
        }
        with patch.dict("os.environ", environment, clear=True):
            response = self.client.get("/api/tools")
        self.assertEqual(response.status_code, 200)
        pdf_tool = next(tool for tool in response.get_json()["tools"] if tool["id"] == "pdf_ocr_translate")
        self.assertEqual(pdf_tool["llmPresets"], [{
            "id": "default",
            "name": "Local provider",
            "baseUrl": "https://llm.example/v1",
            "model": "local-model",
        }])
        self.assertNotIn("apiKey", pdf_tool["llmPresets"][0])


if __name__ == "__main__":
    unittest.main()
