"""Smoke tests for the local toolbox API."""

from __future__ import annotations

import io
import json
import tempfile
import threading
import time
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
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


def html_bundle(filename: str = "paper.html", text: str = "<html><body><h1>Paper</h1><p>Readable text.</p></body></html>") -> bytes:
    content = io.BytesIO()
    with zipfile.ZipFile(content, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(filename, text)
        archive.writestr("images/figure.png", b"image bytes")
    return content.getvalue()


def translated_request_content(value: str, translate: object) -> str:
    """Make test LLMs understand both plain and structured translation requests."""
    try:
        payload = json.loads(value) if value.startswith("{") else None
    except json.JSONDecodeError:
        payload = None
    if not isinstance(payload, dict) or payload.get("task") != "translate_markdown_segments_v1":
        return translate(value)
    for part in payload.get("parts", []):
        if isinstance(part, dict) and isinstance(part.get("text"), str):
            part["text"] = translate(part["text"])
    return json.dumps(payload, ensure_ascii=False)


class ToolboxApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.previous_jobs_dir = web_panel.JOBS_DIR
        self.previous_reader_cache_dir = web_panel.READER_CACHE_DIR
        web_panel.JOBS_DIR = Path(self.temp_dir.name) / "jobs"
        web_panel.READER_CACHE_DIR = Path(self.temp_dir.name) / "reader-cache"
        web_panel.JOBS_DIR.mkdir()
        with web_panel.reader_manager.lock:
            web_panel.reader_manager.documents.clear()
        web_panel.app.config.update(TESTING=True)
        self.client = web_panel.app.test_client()

    def tearDown(self) -> None:
        web_panel.JOBS_DIR = self.previous_jobs_dir
        web_panel.READER_CACHE_DIR = self.previous_reader_cache_dir
        with web_panel.reader_manager.lock:
            web_panel.reader_manager.documents.clear()
        self.temp_dir.cleanup()

    def test_tools_are_listed(self) -> None:
        response = self.client.get("/api/tools")
        self.assertEqual(response.status_code, 200)
        ids = {tool["id"] for tool in response.get_json()["tools"]}
        self.assertTrue({"anki", "image_crop", "stack_videos", "bibtex"}.issubset(ids))

    def test_main_falls_back_from_an_occupied_port_with_cp1252_console(self) -> None:
        class OccupiedSocket:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def bind(self, _address):
                raise OSError(10048, "端口已被占用")

        class TestServer:
            server_port = 43210

            def serve_forever(self):
                raise KeyboardInterrupt

        stdout_bytes = io.BytesIO()
        stderr_bytes = io.BytesIO()
        stdout = io.TextIOWrapper(stdout_bytes, encoding="cp1252")
        stderr = io.TextIOWrapper(stderr_bytes, encoding="cp1252")
        try:
            with (
                patch.dict(web_panel.os.environ, {"WEB_PANEL_PORT": "8765"}, clear=False),
                patch.object(web_panel.sys, "stdout", stdout),
                patch.object(web_panel.sys, "stderr", stderr),
                patch.object(web_panel.socket, "socket", return_value=OccupiedSocket()),
                patch.object(web_panel, "make_server", return_value=TestServer()) as make_server,
                patch.object(web_panel, "open_browser_when_ready") as open_browser,
            ):
                web_panel.main()
                stdout.flush()
                output = stdout_bytes.getvalue().decode("cp1252")
            self.assertIn("Using another port", output)
            self.assertIn("Ready: http://127.0.0.1:43210/", output)
            self.assertEqual(make_server.call_args.args[1], 0)
            open_browser.assert_called_once_with("http://127.0.0.1:43210/")
        finally:
            stdout.detach()
            stderr.detach()

    def test_reader_page_exposes_word_style_selection_toolbar(self) -> None:
        response = self.client.get("/reader")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        for control_id in (
            "selectionHighlightColor",
            "selectionTextColor",
            "clearSelectionFormat",
        ):
            self.assertIn(f'id="{control_id}"', html)
        for format_name in ("bold", "italic", "underline", "strike"):
            self.assertIn(f'data-format-toggle="{format_name}"', html)
        self.assertIn('id="assistantResizeHandle"', html)
        self.assertIn('id="paperResizeHandle"', html)
        self.assertIn('id="paperWidthSlider"', html)
        self.assertIn('id="readerAxisSlider"', html)
        self.assertIn('id="readerAxisValue"', html)
        self.assertIn('id="assistantWidthSlider"', html)
        self.assertIn('id="assistantWidthValue"', html)
        self.assertIn('id="outlinePanel"', html)
        self.assertIn('id="showTableOfContents"', html)
        self.assertIn('id="paperDocumentTitle"', html)
        self.assertIn('id="startSpeech"', html)
        self.assertIn('id="stopSpeech"', html)
        self.assertIn('id="speechPlaybackRate"', html)
        for control_id in (
            "readerFontSize",
            "readerLetterSpacing",
            "readerLineHeight",
            "readerParagraphSpacing",
            "resetReadingLayout",
            "headerMarginTop",
            "headerMarginRight",
            "headerMarginBottom",
            "headerMarginLeft",
            "bodyMarginTop",
            "bodyMarginRight",
            "bodyMarginBottom",
            "bodyMarginLeft",
            "footerMarginTop",
            "footerMarginRight",
            "footerMarginBottom",
            "footerMarginLeft",
        ):
            self.assertIn(f'id="{control_id}"', html)
        self.assertIn('data-assistant-width-delta="-40"', html)
        self.assertIn('data-assistant-width-delta="40"', html)
        self.assertIn('data-paper-width-delta="100"', html)
        self.assertIn('data-reader-axis-delta="-40"', html)
        self.assertIn('id="markdownAssets"', html)
        self.assertIn('id="translationFile"', html)
        self.assertIn('id="interleavedViewButton"', html)
        self.assertIn('id="sideBySideViewButton"', html)
        self.assertIn('id="liveTranslationViewButton"', html)
        self.assertIn("每次打开都会重新应用当前版本的阅读器功能", html)
        self.assertIn(".html,.htm", html)
        self.assertLess(html.index("selection-format-toolbar"), html.index("actionButtons"))

    def test_azure_speech_uses_configured_region_voice_and_escaped_ssml(self) -> None:
        azure_response = SimpleNamespace(ok=True, status_code=200, content=b"mp3")
        environment = {
            "AZURE_SPEECH_KEY": "test-key",
            "AZURE_SPEECH_REGION": "centralus",
            "AZURE_SPEECH_VOICE": "zh-CN-YunxiNeural",
        }
        with (
            patch.dict(web_panel.os.environ, environment, clear=False),
            patch.object(web_panel.requests, "post", return_value=azure_response) as post,
        ):
            audio, voice = web_panel.synthesize_azure_speech("A < B & C")
        self.assertEqual(audio, b"mp3")
        self.assertEqual(voice, "zh-CN-YunxiNeural")
        url = post.call_args.args[0]
        options = post.call_args.kwargs
        self.assertEqual(
            url,
            "https://centralus.tts.speech.microsoft.com/cognitiveservices/v1",
        )
        self.assertEqual(options["headers"]["X-Microsoft-OutputFormat"], web_panel.AZURE_SPEECH_OUTPUT_FORMAT)
        self.assertIn(b"A &lt; B &amp; C", options["data"])
        self.assertIn(b"<prosody rate='+0%'>", options["data"])

    def test_azure_speech_applies_and_validates_reading_rate(self) -> None:
        azure_response = SimpleNamespace(ok=True, status_code=200, content=b"mp3")
        environment = {
            "AZURE_SPEECH_KEY": "test-key",
            "AZURE_SPEECH_REGION": "centralus",
            "AZURE_SPEECH_VOICE": "zh-CN-YunxiNeural",
        }
        with (
            patch.dict(web_panel.os.environ, environment, clear=False),
            patch.object(web_panel.requests, "post", return_value=azure_response) as post,
        ):
            web_panel.synthesize_azure_speech("稍快朗读。", 1.25)
        self.assertIn(b"<prosody rate='+25%'>", post.call_args.kwargs["data"])
        with self.assertRaisesRegex(ValueError, "0.5"):
            web_panel.synthesize_azure_speech("过快。", 2.5)

    def test_reader_speech_returns_mpeg_for_pdf_markdown_and_html_without_exposing_credentials(self) -> None:
        for source_type in ("pdf", "markdown", "html"):
            document = web_panel.ReaderDocument(
                id=f"speech-{source_type}",
                root=Path(self.temp_dir.name),
                title=f"Speech {source_type}",
                source_type=source_type,
                mode="direct",
                render_kind=source_type,
                status="ready",
            )
            web_panel.reader_manager.add(document)
        with patch.object(
            web_panel,
            "synthesize_azure_speech",
            return_value=(b"ID3 audio", "zh-CN-YunxiNeural"),
        ) as synthesize:
            for source_type in ("pdf", "markdown", "html"):
                with self.subTest(source_type=source_type):
                    response = self.client.post(
                        f"/api/reader/documents/speech-{source_type}/speech",
                        json={"text": "需要朗读的段落。"},
                    )
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.mimetype, "audio/mpeg")
                    self.assertEqual(response.get_data(), b"ID3 audio")
                    self.assertEqual(response.headers["Cache-Control"], "no-store")
                    self.assertEqual(response.headers["X-Speech-Voice"], "zh-CN-YunxiNeural")
                    self.assertEqual(response.headers["X-Speech-Rate"], "1.0")
        self.assertEqual(synthesize.call_count, 3)
        synthesize.assert_any_call("需要朗读的段落。", 1.0)

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
        library = self.client.get("/api/reader/library").get_json()["documents"]
        self.assertEqual(len(library), 1)
        self.assertEqual(library[0]["renderKind"], "markdown")
        self.assertTrue(library[0]["canTranslateDocument"])
        self.assertEqual(library[0]["readerUpdatePolicy"], "latest")
        self.assertEqual(library[0]["readerFeatureVersion"], web_panel.READER_FEATURE_VERSION)
        self.assertTrue(library[0]["capabilities"]["latestReader"])
        self.assertTrue(content["capabilities"]["liveParagraphTranslation"])
        saved_state = {
            "notes": [{"id": "note-1", "kind": "answer", "selection": "score", "text": "缓存回答"}],
            "textFormats": [{"id": "format-1", "bold": True, "anchor": {"view": "original"}}],
            "liveTranslations": [
                {
                    "blockId": "b2",
                    "sourceHash": "49:1234abcd",
                    "translation": "这一段的持久译文。",
                    "cached": False,
                    "updatedAt": 123456,
                },
                {
                    "blockId": "../invalid",
                    "sourceHash": "bad",
                    "translation": "不应保留",
                },
            ],
        }
        state_response = self.client.put(
            f"/api/reader/documents/{document_id}/state",
            json=saved_state,
        )
        self.assertEqual(state_response.status_code, 200, state_response.get_json())
        reopened = self.client.post(f"/api/reader/library/{library[0]['id']}/open")
        self.assertEqual(reopened.status_code, 201, reopened.get_json())
        self.assertEqual(reopened.get_json()["cacheKey"], status["cacheKey"])
        self.assertEqual(reopened.get_json()["readerUpdatePolicy"], "latest")
        self.assertTrue(reopened.get_json()["capabilities"]["readingSettings"])
        reopened_content = self.client.get(
            f"/api/reader/documents/{reopened.get_json()['id']}/content"
        ).get_json()
        self.assertIn("score", reopened_content["blocks"][1]["content"])
        self.assertEqual(reopened_content["readerFeatureVersion"], web_panel.READER_FEATURE_VERSION)
        self.assertTrue(reopened_content["capabilities"]["selectionActions"])
        restored_state = self.client.get(
            f"/api/reader/documents/{reopened.get_json()['id']}/state"
        ).get_json()
        self.assertEqual(restored_state["notes"], saved_state["notes"])
        self.assertEqual(restored_state["textFormats"], saved_state["textFormats"])
        self.assertEqual(restored_state["liveTranslations"], saved_state["liveTranslations"][:1])

    def test_reader_library_can_persistently_rename_an_existing_project(self) -> None:
        response = self.client.post(
            "/api/reader/documents",
            data={
                "file": (
                    io.BytesIO(b"# Original heading\n\nReadable content."),
                    "original-paper.md",
                ),
            },
            content_type="multipart/form-data",
        )
        status = self.wait_for_reader(response.get_json()["id"])
        cache_key = status["cacheKey"]

        renamed = self.client.patch(
            f"/api/reader/library/{cache_key}",
            json={"title": "扩散模型论文 · 第一章"},
        )

        self.assertEqual(renamed.status_code, 200, renamed.get_json())
        self.assertEqual(renamed.get_json()["document"]["title"], "扩散模型论文 · 第一章")
        self.assertTrue(renamed.get_json()["document"]["titleCustomized"])
        manifest = web_panel.read_reader_cache_manifest(cache_key)
        self.assertEqual(manifest["document"]["title"], "扩散模型论文 · 第一章")
        self.assertTrue(manifest["document"]["titleCustomized"])

        reopened = self.client.post(f"/api/reader/library/{cache_key}/open")
        self.assertEqual(reopened.status_code, 201, reopened.get_json())
        self.assertEqual(reopened.get_json()["title"], "扩散模型论文 · 第一章")
        self.assertTrue(reopened.get_json()["titleCustomized"])

        reimported = self.client.post(
            "/api/reader/documents",
            data={
                "file": (
                    io.BytesIO(b"# Original heading\n\nReadable content."),
                    "original-paper.md",
                ),
            },
            content_type="multipart/form-data",
        )
        self.wait_for_reader(reimported.get_json()["id"])
        library = self.client.get("/api/reader/library").get_json()["documents"]
        self.assertEqual(library[0]["title"], "扩散模型论文 · 第一章")
        self.assertTrue(library[0]["titleCustomized"])

    def test_reader_generated_markdown_translation_is_postprocessed_before_cache(self) -> None:
        environment = {
            "LLM_NAME": "Managed Markdown",
            "LLM_BASE_URL": "https://llm.example/v1",
            "LLM_API_KEY": "secret",
            "LLM_MODEL": "model",
        }

        def fake_backend(_job, _source_root, target_root, _sources):
            translated = target_root / "paper_zh-CN.md"
            translated.write_text("$$E=mc^2$$\n", encoding="utf-8")
            return [translated]

        with patch.dict("os.environ", environment, clear=True), patch.object(
            web_panel, "run_translation_backend", side_effect=fake_backend
        ):
            response = self.client.post(
                "/api/reader/documents",
                data={
                    "useOcr": "false",
                    "generateTranslation": "true",
                    "useLocalCache": "true",
                    "llm": json.dumps({"mode": "preset", "presetId": "default"}),
                    "file": (io.BytesIO(b"Source prose."), "paper.md"),
                },
                content_type="multipart/form-data",
            )
            self.assertEqual(response.status_code, 202, response.get_json())
            status = self.wait_for_reader(response.get_json()["id"])

        self.assertEqual(status["status"], "ready", status)
        content = self.client.get(f"/api/reader/documents/{status['id']}/content").get_json()
        self.assertEqual(content["translatedBlocks"][0]["type"], "math")
        self.assertEqual(content["translatedBlocks"][0]["content"], "$$\nE=mc^2\n$$")
        cache_manifest = web_panel.read_reader_cache_manifest(status["cacheKey"])
        entries = list(cache_manifest["translations"].values())
        self.assertEqual(entries[0]["markdownPostprocessVersion"], web_panel.TRANSLATION_MARKDOWN_POSTPROCESS_VERSION)

    def test_reader_library_rejects_uuid_titles_and_recovers_asset_parent_name(self) -> None:
        self.assertIsNone(
            web_panel.clean_reader_title("132a8368-53b6-4006-bfe1-b23ff44ae3ac")
        )
        self.assertEqual(
            web_panel.reader_source_display_title(
                "Preface_Chapter_1.html-assets/bbc32808-5748-468f-90eb-336d7fcd169a.html"
            ),
            "Preface Chapter 1",
        )

    def test_reader_translates_one_clicked_paragraph_and_reuses_its_cache(self) -> None:
        response = self.client.post(
            "/api/reader/documents",
            data={
                "file": (
                    io.BytesIO(b"# Paper\n\nTranslate only this paragraph.\n\nLeave this paragraph untouched."),
                    "paper.md",
                ),
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
        paragraph = content["blocks"][1]
        payload = {
            "blockId": paragraph["id"],
            "sourceText": "The client must not override the server paragraph.",
            "llm": {
                "mode": "custom",
                "name": "Test model",
                "baseUrl": "https://llm.example/v1",
                "apiKey": "secret",
                "model": "test-model",
            },
        }

        with patch.object(web_panel, "translate_text_block", return_value="只翻译这一段。") as translate:
            first = self.client.post(
                f"/api/reader/documents/{document_id}/paragraph-translations",
                json=payload,
            )
            second = self.client.post(
                f"/api/reader/documents/{document_id}/paragraph-translations",
                json=payload,
            )

        self.assertEqual(first.status_code, 200, first.get_json())
        self.assertFalse(first.get_json()["cached"])
        self.assertEqual(first.get_json()["translation"], "只翻译这一段。")
        self.assertEqual(second.status_code, 200, second.get_json())
        self.assertTrue(second.get_json()["cached"])
        self.assertEqual(translate.call_count, 1)
        self.assertEqual(translate.call_args.args[1], "Translate only this paragraph.")

    def test_reader_imports_an_existing_source_translation_pair_without_llm(self) -> None:
        response = self.client.post(
            "/api/reader/documents",
            data={
                "useOcr": "true",
                "generateTranslation": "true",
                "useLocalCache": "true",
                "file": (io.BytesIO("# Original\n\nSource text.".encode()), "paper.md"),
                "translationFile": (io.BytesIO("# 译文\n\n译文内容。".encode()), "paper_zh-CN.mmd"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 201, response.get_json())
        document = response.get_json()
        self.assertEqual(document["status"], "ready")
        self.assertEqual(document["mode"], "markdown_pair")
        self.assertTrue(document["hasTranslation"])
        self.assertFalse(document["useOcr"])
        content = self.client.get(f"/api/reader/documents/{document['id']}/content").get_json()
        self.assertIn("Source text.", str(content["blocks"]))
        self.assertIn("译文内容。", str(content["translatedBlocks"]))
        library = self.client.get("/api/reader/library").get_json()["documents"]
        self.assertTrue(library[0]["hasTranslation"])

    def test_reader_llm_alignment_merges_erroneously_split_translation_paragraphs(self) -> None:
        source_blocks = [
            {"id": "b1", "type": "paragraph", "section": "", "content": "First source paragraph."},
            {"id": "b2", "type": "paragraph", "section": "", "content": "Second source paragraph."},
        ]
        translated_blocks = [
            {"id": "b1", "type": "paragraph", "section": "", "content": "第一段译文的前半部分，"},
            {"id": "b2", "type": "paragraph", "section": "", "content": "以及错误换行后的后半部分。"},
            {"id": "b3", "type": "paragraph", "section": "", "content": "第二段译文。"},
        ]
        response_content = json.dumps({
            "groups": [
                {
                    "sourceIds": ["S:b1"],
                    "translationIds": ["T:b1", "T:b2"],
                    "confidence": 0.98,
                },
                {
                    "sourceIds": ["S:b2"],
                    "translationIds": ["T:b3"],
                    "confidence": 0.96,
                },
            ]
        })

        class FakeOpenAI:
            def __init__(self, **_kwargs):
                self.chat = SimpleNamespace(
                    completions=SimpleNamespace(
                        create=lambda **_request: SimpleNamespace(
                            choices=[SimpleNamespace(message=SimpleNamespace(content=response_content))]
                        )
                    )
                )

        job = web_panel.Job(
            "alignment-test",
            web_panel.READER_TOOL,
            Path(self.temp_dir.name),
            {},
            translation_config={
                "name": "Test",
                "baseUrl": "https://example.test/v1",
                "apiKey": "test-key",
                "model": "test-model",
            },
        )
        with patch.object(web_panel, "OpenAI", FakeOpenAI):
            aligned = web_panel.align_reader_markdown_blocks(job, source_blocks, translated_blocks)

        self.assertEqual(len(aligned), 2)
        self.assertEqual(aligned[0]["sourceIds"], ["b1"])
        self.assertEqual(aligned[0]["translationIds"], ["b1", "b2"])
        self.assertEqual(aligned[0]["content"], "第一段译文的前半部分，\n\n以及错误换行后的后半部分。")
        self.assertEqual(aligned[1]["sourceIds"], ["b2"])

    def test_chat_completion_retries_without_unsupported_temperature_and_remembers_model(self) -> None:
        calls: list[dict[str, object]] = []

        class FakeCompletions:
            def create(self, **request):
                calls.append(request)
                if "temperature" in request:
                    raise RuntimeError(
                        "Unsupported value: 'temperature' does not support 0 with this model. "
                        "Only the default (1) value is supported."
                    )
                return "accepted"

        client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))
        config = {
            "baseUrl": "https://example.test/v1",
            "model": "temperature-limited-model",
        }

        first = web_panel.create_compatible_chat_completion(
            client,
            config,
            model=config["model"],
            temperature=0,
            messages=[],
        )
        second = web_panel.create_compatible_chat_completion(
            client,
            config,
            model=config["model"],
            temperature=0,
            messages=[],
        )

        self.assertEqual(first, "accepted")
        self.assertEqual(second, "accepted")
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[0]["temperature"], 0)
        self.assertNotIn("temperature", calls[1])
        self.assertNotIn("temperature", calls[2])
        self.assertTrue(config["_omitTemperature"])

    def test_chat_completion_retries_only_transient_failures(self) -> None:
        class ProviderError(RuntimeError):
            def __init__(self, status_code: int):
                super().__init__(f"provider status {status_code}")
                self.status_code = status_code

        attempts = 0

        class TransientCompletions:
            def create(self, **_request):
                nonlocal attempts
                attempts += 1
                if attempts < 4:
                    raise ProviderError(503)
                return "accepted"

        transient_client = SimpleNamespace(chat=SimpleNamespace(completions=TransientCompletions()))
        config = {
            "baseUrl": "https://retry.example/v1",
            "apiKey": "retry-key",
            "model": "retry-model",
            "concurrency": 1,
        }
        with patch.object(web_panel.time, "sleep") as sleep, patch.object(web_panel.random, "uniform", return_value=0):
            result = web_panel.create_compatible_chat_completion(
                transient_client,
                config,
                model=config["model"],
                temperature=0,
                messages=[],
            )
        self.assertEqual(result, "accepted")
        self.assertEqual(attempts, 4)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1.0, 2.0, 4.0])

        ordinary_calls = 0

        class OrdinaryCompletions:
            def create(self, **_request):
                nonlocal ordinary_calls
                ordinary_calls += 1
                raise ProviderError(400)

        ordinary_client = SimpleNamespace(chat=SimpleNamespace(completions=OrdinaryCompletions()))
        ordinary_config = {**config, "apiKey": "ordinary-key"}
        with patch.object(web_panel.time, "sleep") as sleep:
            with self.assertRaisesRegex(ProviderError, "400"):
                web_panel.create_compatible_chat_completion(
                    ordinary_client,
                    ordinary_config,
                    model=ordinary_config["model"],
                    temperature=0,
                    messages=[],
                )
        self.assertEqual(ordinary_calls, 1)
        sleep.assert_not_called()

    def test_global_llm_limiter_is_shared_by_simultaneous_calls(self) -> None:
        lock = threading.Lock()
        active = 0
        maximum_active = 0

        class DelayedCompletions:
            def create(self, **_request):
                nonlocal active, maximum_active
                with lock:
                    active += 1
                    maximum_active = max(maximum_active, active)
                try:
                    time.sleep(0.02)
                    return "accepted"
                finally:
                    with lock:
                        active -= 1

        client = SimpleNamespace(chat=SimpleNamespace(completions=DelayedCompletions()))
        config = web_panel.prepare_llm_runtime_config({
            "baseUrl": "https://shared-limit.example/v1",
            "apiKey": "shared-key",
            "model": "shared-model",
            "concurrency": 2,
        }, "preset:shared-limit-test")
        second_config = web_panel.prepare_llm_runtime_config({
            "baseUrl": "https://shared-limit.example/v1",
            "apiKey": "shared-key",
            "model": "shared-model",
            "concurrency": 2,
        }, "preset:shared-limit-test")

        def request_once(index: int) -> object:
            selected = config if index % 2 == 0 else second_config
            return web_panel.create_compatible_chat_completion(
                client,
                selected,
                model=selected["model"],
                temperature=0,
                messages=[],
            )

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(request_once, range(8)))
        self.assertEqual(results, ["accepted"] * 8)
        self.assertEqual(maximum_active, 2)

    def test_lowering_global_llm_limit_waits_for_existing_requests_to_drain(self) -> None:
        registry = web_panel.LlmConcurrencyRegistry()
        key = "preset:dynamic-limit-test"
        registry.configure(key, 2)
        acquired = [threading.Event() for _ in range(3)]
        release = [threading.Event() for _ in range(3)]

        def occupy(index: int) -> None:
            with registry.slot(key, 2):
                acquired[index].set()
                release[index].wait(1)

        first = threading.Thread(target=occupy, args=(0,))
        second = threading.Thread(target=occupy, args=(1,))
        first.start()
        second.start()
        self.assertTrue(acquired[0].wait(1))
        self.assertTrue(acquired[1].wait(1))

        registry.configure(key, 1)
        third = threading.Thread(target=occupy, args=(2,))
        third.start()
        self.assertFalse(acquired[2].wait(0.03))
        release[0].set()
        first.join(1)
        self.assertFalse(acquired[2].wait(0.03))
        release[1].set()
        second.join(1)
        self.assertTrue(acquired[2].wait(1))
        release[2].set()
        third.join(1)

    def test_reader_alignment_rejects_reordered_or_missing_ids(self) -> None:
        source_blocks = [{"id": "b1"}, {"id": "b2"}]
        translated_blocks = [{"id": "b1"}, {"id": "b2"}]
        with self.assertRaisesRegex(RuntimeError, "changed, skipped, duplicated, or reordered"):
            web_panel.validate_reader_alignment_groups(
                {
                    "groups": [{
                        "sourceIds": ["S:b2", "S:b1"],
                        "translationIds": ["T:b1"],
                    }]
                },
                source_blocks,
                translated_blocks,
            )

    def test_reader_alignment_coalesces_one_sided_llm_groups_without_losing_ids(self) -> None:
        source_blocks = [{"id": f"b{index}"} for index in range(1, 5)]
        translated_blocks = [{"id": f"b{index}"} for index in range(1, 5)]

        groups = web_panel.validate_reader_alignment_groups(
            {
                "groups": [
                    {"sourceIds": [], "translationIds": ["T:b1"], "confidence": 0.7},
                    {"sourceIds": ["S:b1"], "translationIds": [], "confidence": 0.8},
                    {"sourceIds": ["S:b2"], "translationIds": ["T:b2"], "confidence": 0.95},
                    {"sourceIds": [], "translationIds": ["T:b3"], "confidence": 0.6},
                    {"sourceIds": ["S:b3"], "translationIds": [], "confidence": 0.65},
                    {"sourceIds": ["S:b4"], "translationIds": ["T:b4"], "confidence": 0.9},
                ]
            },
            source_blocks,
            translated_blocks,
        )

        self.assertEqual(
            [(group["sourceIds"], group["translationIds"]) for group in groups],
            [
                (["b1"], ["b1"]),
                (["b2"], ["b2"]),
                (["b3"], ["b3"]),
                (["b4"], ["b4"]),
            ],
        )
        self.assertEqual(groups[0]["confidence"], 0.7)
        self.assertEqual(groups[2]["confidence"], 0.6)

    def test_reader_alignment_attaches_trailing_one_sided_group_to_previous_group(self) -> None:
        groups = web_panel.validate_reader_alignment_groups(
            {
                "groups": [
                    {"sourceIds": ["S:b1"], "translationIds": ["T:b1"]},
                    {"sourceIds": ["S:b2"], "translationIds": []},
                ]
            },
            [{"id": "b1"}, {"id": "b2"}],
            [{"id": "b1"}],
        )

        self.assertEqual(groups[0]["sourceIds"], ["b1", "b2"])
        self.assertEqual(groups[0]["translationIds"], ["b1"])

    def test_reader_alignment_extracts_monotonic_heading_image_and_formula_anchors(self) -> None:
        source_blocks = [
            {"id": "b1", "type": "heading", "level": 2, "content": "2 Method"},
            {"id": "b2", "type": "paragraph", "content": "![Architecture](images/architecture.png)"},
            {"id": "b3", "type": "math", "content": r"\[z_t = \alpha_t x + \sigma_t \epsilon\]"},
        ]
        translated_blocks = [
            {"id": "b1", "type": "heading", "level": 2, "content": "2 方法"},
            {"id": "b2", "type": "paragraph", "content": "额外的译文段落。"},
            {"id": "b3", "type": "paragraph", "content": "![模型结构](images/architecture.png)"},
            {"id": "b4", "type": "paragraph", "content": "另一个额外段落。"},
            {"id": "b5", "type": "math", "content": r"\[z_t = \alpha_t x + \sigma_t \epsilon\]"},
        ]

        anchors = web_panel.reader_hard_alignment_anchors(source_blocks, translated_blocks)

        self.assertEqual(
            [(anchor["sourceIndex"], anchor["translationIndex"]) for anchor in anchors],
            [(0, 0), (1, 2), (2, 4)],
        )
        self.assertIn("heading-number", anchors[0]["kinds"])
        self.assertIn("image", anchors[1]["kinds"])
        self.assertIn("formula", anchors[2]["kinds"])

    def test_reader_alignment_uses_anchors_as_batch_boundaries_after_paragraph_drift(self) -> None:
        source_blocks = [
            {"id": "s1", "type": "heading", "level": 1, "content": "1 Introduction"},
            {"id": "s2", "type": "paragraph", "content": "Source paragraph one."},
            {"id": "s3", "type": "paragraph", "content": "Source paragraph two."},
            {"id": "s4", "type": "heading", "level": 1, "content": "2 Method"},
            {"id": "s5", "type": "paragraph", "content": "Source method."},
        ]
        translated_blocks = [
            {"id": "t1", "type": "heading", "level": 1, "content": "1 引言"},
            {"id": "t2", "type": "paragraph", "content": "第一段前半。"},
            {"id": "t3", "type": "paragraph", "content": "第一段后半。"},
            {"id": "t4", "type": "paragraph", "content": "第二段。"},
            {"id": "t5", "type": "heading", "level": 1, "content": "2 方法"},
            {"id": "t6", "type": "paragraph", "content": "方法译文。"},
        ]
        job = web_panel.Job("anchor-batches", web_panel.READER_TOOL, Path(self.temp_dir.name), {})

        plan = web_panel.reader_alignment_plan(job, source_blocks, translated_blocks)
        batches = [
            (item["sourceBlocks"], item["translationBlocks"])
            for item in plan
            if item["type"] == "batch"
        ]
        locked = [item["group"] for item in plan if item["type"] == "locked"]

        self.assertEqual(
            [([block["id"] for block in source], [block["id"] for block in target]) for source, target in batches],
            [(["s2", "s3"], ["t2", "t3", "t4"]), (["s5"], ["t6"])],
        )
        self.assertEqual(
            [(group["sourceIds"], group["translationIds"]) for group in locked],
            [(["s1"], ["t1"]), (["s4"], ["t5"])],
        )

    def test_reader_alignment_promotes_a_unique_translated_special_sentence(self) -> None:
        source_blocks = [
            {"id": f"s{index}", "type": "paragraph", "content": f"Ordinary source paragraph {index}."}
            for index in range(10)
        ]
        source_blocks[5]["content"] = (
            "We train DiT-XL/2 for 7M optimization steps on ImageNet at 256 x 256 resolution "
            "and report an FID score of 2.27."
        )
        translated_blocks = [
            {"id": f"t{index}", "type": "paragraph", "content": f"普通译文段落 {index}。"}
            for index in range(10)
        ]
        translated_blocks[6]["content"] = (
            "我们在 256 x 256 分辨率的 ImageNet 上训练 DiT-XL/2 共 7M 个优化步骤，并报告 2.27 的 FID 分数。"
        )
        candidates = web_panel.reader_soft_anchor_candidates(source_blocks, translated_blocks, [])
        self.assertEqual(len(candidates), 1)

        anchors = web_panel.match_reader_soft_alignment_anchors(
            candidates,
            {
                candidates[0]["id"]: (
                    "我们在 256 x 256 分辨率的 ImageNet 上训练 DiT-XL/2 共 7M 个优化步骤，"
                    "并报告 2.27 的 FID 分数。"
                )
            },
            translated_blocks,
        )

        self.assertEqual(len(anchors), 1)
        self.assertEqual(anchors[0]["sourceIndex"], 5)
        self.assertEqual(anchors[0]["translationIndex"], 6)
        self.assertEqual(anchors[0]["strength"], "soft")

    def test_reader_import_can_queue_and_complete_llm_pair_alignment(self) -> None:
        submitted: list[web_panel.Job] = []
        llm = {
            "mode": "custom",
            "name": "Alignment test",
            "baseUrl": "https://example.test/v1",
            "apiKey": "test-key",
            "model": "test-model",
        }
        with patch.object(web_panel.job_manager, "submit", side_effect=submitted.append):
            response = self.client.post(
                "/api/reader/documents",
                data={
                    "alignTranslation": "true",
                    "llm": json.dumps(llm),
                    "file": (
                        io.BytesIO("First source paragraph.\n\nSecond source paragraph.".encode()),
                        "paper.md",
                    ),
                    "translationFile": (
                        io.BytesIO("第一段前半。\n\n第一段后半。\n\n第二段。".encode()),
                        "paper_zh-CN.mmd",
                    ),
                },
                content_type="multipart/form-data",
            )
        self.assertEqual(response.status_code, 202, response.get_json())
        self.assertTrue(response.get_json()["alignTranslation"])
        self.assertEqual(
            response.get_json()["alignmentProgress"],
            {
                "stage": "queued",
                "label": "原文与译文已导入，正在等待 LLM 段落对齐。",
                "completed": 0,
                "total": 0,
                "percent": 0,
            },
        )
        self.assertEqual(len(submitted), 1)

        response_content = json.dumps({
            "groups": [
                {
                    "sourceIds": ["S:b1"],
                    "translationIds": ["T:b1", "T:b2"],
                    "confidence": 0.99,
                },
                {
                    "sourceIds": ["S:b2"],
                    "translationIds": ["T:b3"],
                    "confidence": 0.97,
                },
            ]
        })

        class FakeOpenAI:
            def __init__(self, **_kwargs):
                self.chat = SimpleNamespace(
                    completions=SimpleNamespace(
                        create=lambda **_request: SimpleNamespace(
                            choices=[SimpleNamespace(message=SimpleNamespace(content=response_content))]
                        )
                    )
                )

        with patch.object(web_panel, "OpenAI", FakeOpenAI):
            web_panel.run_reader_document(submitted[0])

        document_id = response.get_json()["id"]
        status = self.client.get(f"/api/reader/documents/{document_id}").get_json()
        content = self.client.get(f"/api/reader/documents/{document_id}/content").get_json()
        self.assertEqual(status["status"], "ready")
        self.assertEqual(status["message"], "原文与译文已完成 LLM 段落对齐。")
        self.assertEqual(status["alignmentProgress"]["stage"], "complete")
        self.assertEqual(status["alignmentProgress"]["percent"], 100)
        self.assertEqual(status["alignmentProgress"]["completed"], 1)
        self.assertEqual(status["alignmentProgress"]["total"], 1)
        self.assertEqual(content["translatedBlocks"][0]["sourceIds"], ["b1"])
        self.assertEqual(content["translatedBlocks"][0]["translationIds"], ["b1", "b2"])

    def test_reader_rejects_an_existing_translation_with_a_different_format(self) -> None:
        response = self.client.post(
            "/api/reader/documents",
            data={
                "file": (io.BytesIO(b"# Original"), "paper.md"),
                "translationFile": (io.BytesIO(b"<p>Translation</p>"), "paper.html"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 400, response.get_json())
        self.assertIn("same document format", response.get_json()["error"])

    def test_reader_keeps_same_named_html_source_and_translation_separate(self) -> None:
        response = self.client.post(
            "/api/reader/documents",
            data={
                "file": (io.BytesIO(b"<html><body>Original HTML</body></html>"), "paper.html"),
                "translationFile": (io.BytesIO("<html><body>译文 HTML</body></html>".encode()), "paper.html"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 201, response.get_json())
        content = self.client.get(
            f"/api/reader/documents/{response.get_json()['id']}/content"
        ).get_json()
        self.assertNotEqual(content["documentUrl"], content["translatedDocumentUrl"])
        self.assertTrue(content["capabilities"]["interleavedView"])
        self.assertTrue(content["capabilities"]["sideBySideView"])
        original = self.client.get(content["documentUrl"])
        translated = self.client.get(content["translatedDocumentUrl"])
        self.assertIn(b"Original HTML", original.data)
        self.assertIn("译文 HTML".encode(), translated.data)
        original.close()
        translated.close()

    def test_reader_can_queue_an_existing_html_pair_for_llm_alignment(self) -> None:
        submitted: list[web_panel.Job] = []
        with patch.object(web_panel.job_manager, "submit", side_effect=submitted.append):
            response = self.client.post(
                "/api/reader/documents",
                data={
                    "alignTranslation": "true",
                    "llm": json.dumps({
                        "mode": "custom",
                        "name": "HTML alignment",
                        "baseUrl": "https://example.test/v1",
                        "apiKey": "test-key",
                        "model": "test-model",
                    }),
                    "file": (
                        io.BytesIO(b"<html><body><h1>1 Paper</h1><p>Source.</p></body></html>"),
                        "paper.html",
                    ),
                    "translationFile": (
                        io.BytesIO("<html><body><h1>1 论文</h1><p>译文。</p></body></html>".encode()),
                        "paper_zh-CN.html",
                    ),
                },
                content_type="multipart/form-data",
            )

        self.assertEqual(response.status_code, 202, response.get_json())
        self.assertEqual(response.get_json()["mode"], "html_pair")
        self.assertTrue(response.get_json()["alignTranslation"])
        self.assertEqual(len(submitted), 1)
        self.assertEqual(submitted[0].translation_config["model"], "test-model")

    def test_reader_imports_and_reopens_an_existing_pdf_pair(self) -> None:
        response = self.client.post(
            "/api/reader/documents",
            data={
                "file": (io.BytesIO(b"%PDF-1.4\noriginal"), "paper.pdf"),
                "translationFile": (io.BytesIO(b"%PDF-1.4\ntranslated"), "paper_zh-CN.pdf"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 201, response.get_json())
        content = self.client.get(
            f"/api/reader/documents/{response.get_json()['id']}/content"
        ).get_json()
        self.assertEqual(content["renderKind"], "pdf")
        self.assertIsNotNone(content["translatedDocumentUrl"])
        library_entry = self.client.get("/api/reader/library").get_json()["documents"][0]
        reopened = self.client.post(
            f"/api/reader/library/{library_entry['id']}/open",
            json={"includeTranslation": True},
        )
        self.assertEqual(reopened.status_code, 201, reopened.get_json())
        reopened_content = self.client.get(
            f"/api/reader/documents/{reopened.get_json()['id']}/content"
        ).get_json()
        self.assertIsNotNone(reopened_content["translatedDocumentUrl"])

    def test_ocr_translation_pair_can_be_imported_directly_into_reader(self) -> None:
        job = web_panel.Job(
            "ocr-reader-pair",
            web_panel.TOOL_BY_ID["pdf_ocr_translate"],
            web_panel.JOBS_DIR / "ocr-reader-pair",
            {},
            status="completed",
            phase="translation_complete",
        )
        output = job.root / "output"
        output.mkdir(parents=True)
        source = output / "论文.md"
        translated = output / "论文_zh-CN.md"
        source.write_text("# Original\n\nSource paragraph.", encoding="utf-8")
        translated.write_text("# 译文\n\n翻译段落。", encoding="utf-8")
        web_panel.add_artifact(job, source, "ocr", "Markdown")
        web_panel.add_artifact(job, translated, "translation", "Simplified Chinese translation")
        with web_panel.job_manager.lock:
            web_panel.job_manager.jobs[job.id] = job

        status = self.client.get(f"/api/jobs/{job.id}").get_json()
        self.assertEqual(len(status["readerPairs"]), 1)
        imported = self.client.post(
            f"/api/jobs/{job.id}/reader-imports",
            json={"pairId": status["readerPairs"][0]["id"]},
        )
        self.assertEqual(imported.status_code, 201, imported.get_json())
        payload = imported.get_json()
        self.assertEqual(payload["status"], "ready")
        self.assertTrue(payload["hasTranslation"])
        self.assertEqual(payload["title"], "论文")
        self.assertEqual(payload["readerUrl"], f"/reader?document={payload['id']}")
        content = self.client.get(f"/api/reader/documents/{payload['id']}/content").get_json()
        self.assertIn("Source paragraph.", str(content["blocks"]))
        self.assertIn("翻译段落。", str(content["translatedBlocks"]))
        self.assertTrue(self.client.get("/api/reader/library").get_json()["documents"][0]["hasTranslation"])

    def test_direct_translation_exposes_each_completed_pair_for_reader_import(self) -> None:
        job = web_panel.Job(
            "direct-reader-pairs",
            web_panel.TOOL_BY_ID["document_translate"],
            web_panel.JOBS_DIR / "direct-reader-pairs",
            {},
            status="completed",
            phase="translation_complete",
        )
        source_root = job.root / "input" / "research"
        output_root = job.root / "output" / "research"
        source_root.mkdir(parents=True)
        output_root.mkdir(parents=True)
        (source_root / "0001_paper.md").write_text("Original paper", encoding="utf-8")
        (output_root / "0001_paper_zh-CN.md").write_text("论文译文", encoding="utf-8")
        notes = source_root / "notes"
        translated_notes = output_root / "notes"
        notes.mkdir()
        translated_notes.mkdir()
        (notes / "0002_page.html").write_text("<html><title>Page</title><body>Original page</body></html>", encoding="utf-8")
        (translated_notes / "0002_page_zh-CN.html").write_text("<html><body>页面译文</body></html>", encoding="utf-8")
        with web_panel.job_manager.lock:
            web_panel.job_manager.jobs[job.id] = job

        status = self.client.get(f"/api/jobs/{job.id}").get_json()
        self.assertEqual([pair["title"] for pair in status["readerPairs"]], ["paper", "page"])
        markdown_pair = status["readerPairs"][0]
        imported = self.client.post(
            f"/api/jobs/{job.id}/reader-imports",
            json={"pairId": markdown_pair["id"]},
        )
        self.assertEqual(imported.status_code, 201, imported.get_json())
        document_id = imported.get_json()["id"]
        content = self.client.get(f"/api/reader/documents/{document_id}/content").get_json()
        self.assertEqual(content["renderKind"], "markdown")
        self.assertIn("Original paper", str(content["blocks"]))
        self.assertIn("论文译文", str(content["translatedBlocks"]))
        html_pair = status["readerPairs"][1]
        imported_html = self.client.post(
            f"/api/jobs/{job.id}/reader-imports",
            json={"pairId": html_pair["id"]},
        )
        self.assertEqual(imported_html.status_code, 201, imported_html.get_json())
        html_content = self.client.get(
            f"/api/reader/documents/{imported_html.get_json()['id']}/content"
        ).get_json()
        self.assertEqual(html_content["renderKind"], "html")
        self.assertTrue(html_content["documentUrl"].endswith("/0002_page.html"))
        self.assertTrue(html_content["translatedDocumentUrl"].endswith("/0002_page_zh-CN.html"))

    def test_reader_markdown_images_are_served_cached_and_restored(self) -> None:
        markdown = b"# Illustrated Paper\n\n![Figure](Paper.assets/figure.png)\n"
        initial = self.client.post(
            "/api/reader/documents",
            data={
                "useOcr": "false",
                "generateTranslation": "false",
                "file": (io.BytesIO(markdown), "paper.md"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(initial.status_code, 202, initial.get_json())
        initial_id = initial.get_json()["id"]
        for _ in range(100):
            initial_status = self.client.get(f"/api/reader/documents/{initial_id}").get_json()
            if initial_status["status"] in {"ready", "failed"}:
                break
            time.sleep(0.02)
        self.assertEqual(initial_status["status"], "ready", initial_status)
        saved_state = {"notes": [{"id": "note-without-images", "text": "keep me"}]}
        self.assertEqual(
            self.client.put(f"/api/reader/documents/{initial_id}/state", json=saved_state).status_code,
            200,
        )

        response = self.client.post(
            "/api/reader/documents",
            data={
                "useOcr": "false",
                "generateTranslation": "false",
                "file": (io.BytesIO(markdown), "paper.md"),
                "assetManifest": json.dumps([{"relativePath": "Paper.assets/figure.png"}]),
                "assets": (io.BytesIO(b"image bytes"), "figure.png"),
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
        self.assertEqual(status["cacheKey"], initial_status["cacheKey"])
        state_after_assets = self.client.get(f"/api/reader/documents/{document_id}/state").get_json()
        self.assertEqual(state_after_assets["notes"], saved_state["notes"])

        content = self.client.get(f"/api/reader/documents/{document_id}/content").get_json()
        image = self.client.get(f"{content['assetBase']}Paper.assets/figure.png")
        self.assertEqual(image.status_code, 200)
        self.assertEqual(image.data, b"image bytes")
        image.close()

        library = self.client.get("/api/reader/library").get_json()["documents"]
        self.assertEqual(len(library), 1)
        cache_root = web_panel.reader_cache_root(library[0]["id"]) / "source"
        self.assertEqual((cache_root / "Paper.assets" / "figure.png").read_bytes(), b"image bytes")

        reopened = self.client.post(f"/api/reader/library/{library[0]['id']}/open")
        self.assertEqual(reopened.status_code, 201, reopened.get_json())
        reopened_id = reopened.get_json()["id"]
        reopened_content = self.client.get(f"/api/reader/documents/{reopened_id}/content").get_json()
        restored = self.client.get(f"{reopened_content['assetBase']}Paper.assets/figure.png")
        self.assertEqual(restored.status_code, 200)
        self.assertEqual(restored.data, b"image bytes")
        restored.close()

    def test_reader_html_image_directory_preserves_paths_and_refreshes_cache(self) -> None:
        html = '<html><head><title>HTML Images</title></head><body><img src="images/图片.png"></body></html>'.encode()
        initial = self.client.post(
            "/api/reader/documents",
            data={
                "useOcr": "false",
                "generateTranslation": "false",
                "file": (io.BytesIO(html), "paper.html"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(initial.status_code, 202, initial.get_json())
        initial_status = self.wait_for_reader(initial.get_json()["id"])
        self.assertEqual(initial_status["status"], "ready", initial_status)

        with_assets = self.client.post(
            "/api/reader/documents",
            data={
                "useOcr": "false",
                "generateTranslation": "false",
                "file": (io.BytesIO(html), "paper.html"),
                "assetManifest": json.dumps([{"relativePath": "images/图片.png"}]),
                "assets": (io.BytesIO(b"html image bytes"), "图片.png"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(with_assets.status_code, 202, with_assets.get_json())
        status = self.wait_for_reader(with_assets.get_json()["id"])
        self.assertEqual(status["status"], "ready", status)
        self.assertEqual(status["cacheKey"], initial_status["cacheKey"])
        content = self.client.get(f"/api/reader/documents/{status['id']}/content").get_json()
        image = self.client.get(f"{content['assetBase']}images/%E5%9B%BE%E7%89%87.png")
        self.assertEqual(image.status_code, 200)
        self.assertEqual(image.data, b"html image bytes")
        image.close()

        library = self.client.get("/api/reader/library").get_json()["documents"]
        reopened = self.client.post(f"/api/reader/library/{library[0]['id']}/open")
        self.assertEqual(reopened.status_code, 201, reopened.get_json())
        reopened_content = self.client.get(
            f"/api/reader/documents/{reopened.get_json()['id']}/content"
        ).get_json()
        restored = self.client.get(
            f"{reopened_content['assetBase']}images/%E5%9B%BE%E7%89%87.png"
        )
        self.assertEqual(restored.status_code, 200)
        self.assertEqual(restored.data, b"html image bytes")
        restored.close()

    def test_reader_translates_raw_html_with_managed_backend_and_reuses_cache(self) -> None:
        source_html = (
            b"<!doctype html><html><head><title>Direct HTML Paper</title></head>"
            b"<body><h1>Introduction</h1><p>Readable source text.</p><script>alert(1)</script></body></html>"
        )
        config = {
            "mode": "custom",
            "name": "HTML translator",
            "baseUrl": "https://llm.example/v1",
            "apiKey": "secret",
            "model": "model",
        }

        rejected = self.client.post(
            "/api/reader/documents",
            data={
                "useOcr": "false",
                "generateTranslation": "true",
                "useLocalCache": "true",
                "llm": json.dumps(config),
                "file": (io.BytesIO(source_html), "paper.html"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(rejected.status_code, 400, rejected.get_json())
        self.assertIn("global LLM manager", rejected.get_json()["error"])

        def fake_backend(_job, source_root, target_root, sources):
            self.assertEqual(source_root, target_root)
            self.assertEqual(sources[0].suffix, ".html")
            translated = target_root / "paper_zh-CN.html"
            translated.write_text(
                sources[0].read_text(encoding="utf-8").replace("Introduction", "介绍").replace("Readable source text.", "可阅读的原文。"),
                encoding="utf-8",
            )
            return [translated]

        environment = {
            "LLM_NAME": "Managed HTML",
            "LLM_BASE_URL": "https://llm.example/v1",
            "LLM_API_KEY": "secret",
            "LLM_MODEL": "model",
        }
        with patch.dict("os.environ", environment, clear=True), patch.object(
            web_panel, "run_translation_backend", side_effect=fake_backend
        ) as backend:
            response = self.client.post(
                "/api/reader/documents",
                data={
                    "useOcr": "false",
                    "generateTranslation": "true",
                    "useLocalCache": "true",
                    "llm": json.dumps({"mode": "preset", "presetId": "default"}),
                    "file": (io.BytesIO(source_html), "paper.html"),
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
        backend.assert_called_once()

        self.assertEqual(status["status"], "ready", status)
        self.assertEqual(status["sourceType"], "html")
        self.assertEqual(status["renderKind"], "html")
        self.assertFalse(status["useOcr"])
        self.assertEqual(status["title"], "Direct HTML Paper")
        content = self.client.get(f"/api/reader/documents/{document_id}/content").get_json()
        self.assertEqual(content["renderKind"], "html")
        self.assertTrue(content["documentUrl"].endswith("/paper.html"))
        self.assertTrue(content["translatedDocumentUrl"].endswith("/paper_zh-CN.html"))
        original = self.client.get(content["documentUrl"])
        self.assertIn(b"Readable source text", original.data)
        self.assertIn("default-src 'none'", original.headers["Content-Security-Policy"])
        original.close()
        translated = self.client.get(content["translatedDocumentUrl"])
        self.assertIn("可阅读的原文", translated.data.decode("utf-8"))
        self.assertIn("alert(1)", translated.data.decode("utf-8"))
        translated.close()
        library = self.client.get("/api/reader/library").get_json()["documents"]
        self.assertEqual(len(library), 1)
        self.assertEqual(library[0]["sourceType"], "html")
        self.assertFalse(library[0]["useOcr"])
        self.assertTrue(library[0]["hasTranslation"])
        self.assertTrue(library[0]["canTranslateDocument"])
        reopened = self.client.post(
            f"/api/reader/library/{library[0]['id']}/open",
            json={"includeTranslation": True},
        )
        self.assertEqual(reopened.status_code, 201, reopened.get_json())
        self.assertEqual(reopened.get_json()["sourceType"], "html")
        self.assertFalse(reopened.get_json()["useOcr"])
        self.assertIn("translation", reopened.get_json()["cacheHits"])

    def test_reader_reuses_cached_ocr_for_same_pdf(self) -> None:
        calls = {"ocr": 0}

        def fake_ocr(job, _source):
            calls["ocr"] += 1
            output = job.root / "output"
            output.mkdir(parents=True)
            html = output / "paper.html"
            html.write_text("<html><head><title>Recovered PDF Title</title></head><body><p>Original text</p></body></html>", encoding="utf-8")
            return html

        def run_once(identifier):
            root = web_panel.JOBS_DIR / f"reader-{identifier}"
            input_dir = root / "input"
            input_dir.mkdir(parents=True)
            source = input_dir / "paper.pdf"
            source.write_bytes(b"%PDF-1.7 same paper")
            document = web_panel.ReaderDocument(
                identifier,
                root,
                "paper",
                "pdf",
                "ocr",
                render_kind="html",
                use_ocr=True,
                generate_translation=False,
                cache_key=web_panel.reader_source_cache_key(source),
            )
            job = web_panel.Job(
                identifier,
                web_panel.READER_TOOL,
                root,
                {},
                operation="reader",
                reader_document_id=identifier,
            )
            web_panel.reader_manager.add(document)
            web_panel.run_reader_document(job)
            return document

        with patch.object(web_panel, "run_reader_html_ocr", side_effect=fake_ocr):
            first = run_once("cache-first")
            second = run_once("cache-second")

        self.assertEqual(first.cache_hits, [])
        self.assertEqual(second.cache_hits, ["ocr"])
        self.assertEqual(calls, {"ocr": 1})
        self.assertIn("本地缓存", second.message)
        self.assertTrue((second.root / "output" / "paper.html").is_file())

        # A cached OCR result can be selected even when Mathpix credentials are
        # unavailable, because no external OCR request will be made.
        with patch.dict("os.environ", {}, clear=True), patch.object(web_panel.job_manager, "submit"):
            response = self.client.post(
                "/api/reader/documents",
                data={
                    "useOcr": "true",
                    "generateTranslation": "false",
                    "useLocalCache": "true",
                    "file": (io.BytesIO(b"%PDF-1.7 same paper"), "renamed.pdf"),
                },
                content_type="multipart/form-data",
            )
        self.assertEqual(response.status_code, 202, response.get_json())
        self.assertEqual(response.get_json()["cacheAvailable"], ["ocr"])

        # Simulate a cache created before source filename/title metadata was
        # persisted. The library should recover a useful title from OCR HTML.
        manifest = web_panel.read_reader_cache_manifest(first.cache_key)
        manifest["ocr"].pop("title", None)
        manifest["ocr"].pop("sourceName", None)
        manifest["document"].pop("title", None)
        manifest["document"].pop("sourceName", None)
        web_panel.write_reader_cache_manifest(first.cache_key, manifest)
        library = self.client.get("/api/reader/library")
        self.assertEqual(library.status_code, 200)
        cached_documents = library.get_json()["documents"]
        self.assertEqual(len(cached_documents), 1)
        self.assertEqual(cached_documents[0]["title"], "Recovered PDF Title")
        self.assertFalse(cached_documents[0]["hasTranslation"])

        opened = self.client.post(f"/api/reader/library/{cached_documents[0]['id']}/open")
        self.assertEqual(opened.status_code, 201, opened.get_json())
        self.assertEqual(opened.get_json()["status"], "ready")
        self.assertEqual(opened.get_json()["cacheHits"], ["ocr"])
        resumed_id = opened.get_json()["id"]
        content = self.client.get(f"/api/reader/documents/{resumed_id}/content")
        self.assertEqual(content.status_code, 200, content.get_json())
        self.assertTrue(content.get_json()["documentUrl"].endswith("/paper.html"))
        self.assertIsNone(content.get_json()["translatedDocumentUrl"])
        original = self.client.get(content.get_json()["documentUrl"])
        self.assertIn(b"Original text", original.data)
        original.close()

        def fake_backend(_job, _source_root, target_root, sources):
            translated = target_root / "paper_zh-CN.html"
            translated.write_text(
                sources[0].read_text(encoding="utf-8").replace("Original text", "缓存译文"),
                encoding="utf-8",
            )
            return [translated]

        environment = {
            "LLM_NAME": "Managed OCR HTML",
            "LLM_BASE_URL": "https://llm.example/v1",
            "LLM_API_KEY": "secret",
            "LLM_MODEL": "model",
        }
        with patch.dict("os.environ", environment, clear=True), patch.object(
            web_panel, "run_translation_backend", side_effect=fake_backend
        ) as backend:
            opened_with_translation = self.client.post(
                f"/api/reader/library/{cached_documents[0]['id']}/open",
                json={
                    "includeTranslation": True,
                    "llm": {"mode": "preset", "presetId": "default"},
                },
            )
            self.assertEqual(opened_with_translation.status_code, 202, opened_with_translation.get_json())
            translated_id = opened_with_translation.get_json()["id"]
            for _ in range(100):
                translated_status = self.client.get(f"/api/reader/documents/{translated_id}").get_json()
                if translated_status["status"] in {"ready", "failed"}:
                    break
                time.sleep(0.02)
        backend.assert_called_once()
        self.assertEqual(translated_status["status"], "ready", translated_status)
        translated_content = self.client.get(f"/api/reader/documents/{translated_id}/content").get_json()
        translated_response = self.client.get(translated_content["translatedDocumentUrl"])
        self.assertIn("缓存译文", translated_response.data.decode("utf-8"))
        translated_response.close()

    def test_reader_prefers_pdf_metadata_title_and_keeps_original_filename(self) -> None:
        with patch.dict("os.environ", {"MATHPIX_APP_ID": "id", "MATHPIX_APP_KEY": "key"}), \
             patch.object(web_panel, "pdf_document_title", return_value="Metadata Paper Title"), \
             patch.object(web_panel.job_manager, "submit"):
            response = self.client.post(
                "/api/reader/documents",
                data={
                    "useOcr": "true",
                    "generateTranslation": "false",
                    "file": (io.BytesIO(b"%PDF-1.7 metadata"), "原始论文文件名.pdf"),
                },
                content_type="multipart/form-data",
            )
        self.assertEqual(response.status_code, 202, response.get_json())
        self.assertEqual(response.get_json()["title"], "Metadata Paper Title")
        document = web_panel.reader_manager.get(response.get_json()["id"])
        self.assertEqual(document.source_name, "原始论文文件名.pdf")

    def test_reader_opens_native_pdf_without_mathpix_and_supports_ranges(self) -> None:
        with patch.object(web_panel.requests, "post") as mathpix_post:
            response = self.client.post(
                "/api/reader/documents",
                data={
                    "useOcr": "false",
                    "generateTranslation": "false",
                    "file": (io.BytesIO(b"%PDF-1.4\nnative text layer"), "paper.pdf"),
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
        self.assertEqual(status["renderKind"], "pdf")
        self.assertFalse(status["useOcr"])
        mathpix_post.assert_not_called()
        content = self.client.get(f"/api/reader/documents/{document_id}/content").get_json()
        self.assertEqual(content["renderKind"], "pdf")
        self.assertEqual(content["documentUrl"], f"/api/reader/documents/{document_id}/source")
        partial = self.client.get(content["documentUrl"], headers={"Range": "bytes=0-3"})
        self.assertEqual(partial.status_code, 206)
        self.assertEqual(partial.data, b"%PDF")
        partial.close()
        library = self.client.get("/api/reader/library").get_json()["documents"]
        self.assertEqual(len(library), 1)
        self.assertEqual(library[0]["renderKind"], "pdf")
        self.assertFalse(library[0]["canTranslateDocument"])
        reopened = self.client.post(f"/api/reader/library/{library[0]['id']}/open")
        self.assertEqual(reopened.status_code, 201, reopened.get_json())
        self.assertEqual(reopened.get_json()["cacheKey"], status["cacheKey"])
        reopened_content = self.client.get(
            f"/api/reader/documents/{reopened.get_json()['id']}/content"
        ).get_json()
        reopened_pdf = self.client.get(reopened_content["documentUrl"], headers={"Range": "bytes=0-3"})
        self.assertEqual(reopened_pdf.status_code, 206)
        self.assertEqual(reopened_pdf.data, b"%PDF")
        reopened_pdf.close()

    def test_reader_rejects_full_pdf_translation_without_ocr(self) -> None:
        response = self.client.post(
            "/api/reader/documents",
            data={
                "useOcr": "false",
                "generateTranslation": "true",
                "file": (io.BytesIO(b"%PDF-1.4"), "paper.pdf"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("requires OCR", response.get_json()["error"])

    def test_reader_translates_pdf_ocr_html_with_go_backend(self) -> None:
        def fake_ocr(job, _source):
            output = job.root / "output"
            output.mkdir(parents=True, exist_ok=True)
            html = output / "paper.html"
            html.write_text("<html><body><p>OCR source.</p></body></html>", encoding="utf-8")
            return html

        def fake_backend(_job, _source_root, target_root, sources):
            translated = target_root / "paper_zh-CN.html"
            translated.write_text(
                sources[0].read_text(encoding="utf-8").replace("OCR source.", "OCR 译文。"),
                encoding="utf-8",
            )
            return [translated]

        environment = {
            "MATHPIX_APP_ID": "id",
            "MATHPIX_APP_KEY": "key",
            "LLM_NAME": "Managed OCR HTML",
            "LLM_BASE_URL": "https://llm.example/v1",
            "LLM_API_KEY": "secret",
            "LLM_MODEL": "model",
        }
        with patch.dict("os.environ", environment, clear=True), \
             patch.object(web_panel, "run_reader_html_ocr", side_effect=fake_ocr), \
             patch.object(web_panel, "run_translation_backend", side_effect=fake_backend) as backend:
            response = self.client.post(
                "/api/reader/documents",
                data={
                    "useOcr": "true",
                    "generateTranslation": "true",
                    "llm": json.dumps({"mode": "preset", "presetId": "default"}),
                    "file": (io.BytesIO(b"%PDF-1.4"), "paper.pdf"),
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
        backend.assert_called_once()
        self.assertEqual(status["status"], "ready", status)
        content = self.client.get(f"/api/reader/documents/{document_id}/content").get_json()
        self.assertTrue(content["translatedDocumentUrl"].endswith("/paper_zh-CN.html"))
        translated = self.client.get(content["translatedDocumentUrl"])
        self.assertIn("OCR 译文", translated.data.decode("utf-8"))
        translated.close()

    def test_reader_ocr_uses_safe_html_bundle_and_serves_it_with_csp(self) -> None:
        bundle = html_bundle(text='<html><body><h1>Method</h1><p>OCR text.</p><img src="images/figure.png"></body></html>')

        def fake_get(url, **_kwargs):
            if url.endswith("/pdf/mock-pdf"):
                return FakeResponse({"status": "completed"})
            if url.endswith("/converter/mock-pdf"):
                return FakeResponse({"conversion_status": {"html.zip": {"status": "completed"}}})
            if url.endswith(".html.zip"):
                return FakeResponse(content=bundle)
            return FakeResponse(ok=False, text="unexpected URL")

        with patch.dict("os.environ", {"MATHPIX_APP_ID": "id", "MATHPIX_APP_KEY": "key"}), \
             patch.object(web_panel.requests, "post", return_value=FakeResponse({"pdf_id": "mock-pdf"})) as mathpix_post, \
             patch.object(web_panel.requests, "get", side_effect=fake_get), \
             patch.object(web_panel, "POLL_INTERVAL_SECONDS", 0):
            response = self.client.post(
                "/api/reader/documents",
                data={
                    "useOcr": "true",
                    "generateTranslation": "false",
                    "file": (io.BytesIO(b"%PDF-1.4"), "paper.pdf"),
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
        self.assertEqual(status["renderKind"], "html")
        options = json.loads(mathpix_post.call_args.kwargs["data"]["options_json"])
        self.assertEqual(options["conversion_formats"], {"html.zip": True})
        content = self.client.get(f"/api/reader/documents/{document_id}/content").get_json()
        html = self.client.get(content["documentUrl"])
        self.assertEqual(html.status_code, 200)
        self.assertIn(b"OCR text.", html.data)
        self.assertIn("default-src 'none'", html.headers["Content-Security-Policy"])
        html.close()

    def test_html_bundle_rejects_parent_path_escape(self) -> None:
        bundle = io.BytesIO()
        with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("paper.html", "<p>safe</p>")
            archive.writestr("../escape.png", b"unsafe")
        output = Path(self.temp_dir.name) / "html-output"
        output.mkdir()
        with self.assertRaisesRegex(RuntimeError, "unsafe path"):
            web_panel.extract_html_bundle(bundle.getvalue(), output)
        self.assertFalse((Path(self.temp_dir.name) / "escape.png").exists())

    def test_reader_legacy_pdf_mode_still_enables_ocr(self) -> None:
        with patch.dict("os.environ", {"MATHPIX_APP_ID": "id", "MATHPIX_APP_KEY": "key"}), \
             patch.object(web_panel.job_manager, "submit"):
            response = self.client.post(
                "/api/reader/documents",
                data={"mode": "ocr", "file": (io.BytesIO(b"%PDF-1.4"), "legacy.pdf")},
                content_type="multipart/form-data",
            )
        self.assertEqual(response.status_code, 202, response.get_json())
        self.assertTrue(response.get_json()["useOcr"])
        self.assertEqual(response.get_json()["renderKind"], "html")

    def test_reader_question_accepts_page_context_without_markdown_block(self) -> None:
        document = web_panel.ReaderDocument(
            "pdf-context",
            Path(self.temp_dir.name) / "pdf-context",
            "Paper",
            "pdf",
            "pdf",
            render_kind="pdf",
            status="ready",
        )
        web_panel.reader_manager.add(document)
        with patch.object(web_panel, "OpenAI") as client_class:
            client = client_class.return_value
            client.chat.completions.create.return_value = SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="页面解释"))]
            )
            response = self.client.post(
                "/api/reader/documents/pdf-context/questions",
                json={
                    "selection": "selected equation",
                    "context": "nearby page context",
                    "section": "Method",
                    "pageNumber": 3,
                    "action": "explain",
                    "llm": {
                        "mode": "custom",
                        "name": "test",
                        "baseUrl": "https://llm.example/v1",
                        "apiKey": "key",
                        "model": "model",
                    },
                },
            )
        self.assertEqual(response.status_code, 200, response.get_json())
        prompt = client.chat.completions.create.call_args.kwargs["messages"][1]["content"]
        self.assertIn("nearby page context", prompt)
        self.assertIn("Method", prompt)

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
            preset = web_panel.save_llm_preset("Lab Gateway", "https://llm.example/v1", "secret-key", "research-model", 8)
            self.assertEqual(preset["id"], "lab_gateway")
            self.assertEqual(preset["concurrency"], 8)
            self.assertIn("LLM_PRESETS='LAB_GATEWAY'", env_file.read_text(encoding="utf-8"))
            self.assertIn("LLM_PRESET_LAB_GATEWAY_API_KEY='secret-key'", env_file.read_text(encoding="utf-8"))
            self.assertIn("LLM_PRESET_LAB_GATEWAY_CONCURRENCY=8", env_file.read_text(encoding="utf-8"))
            with self.assertRaises(ValueError):
                web_panel.save_llm_preset("Lab Gateway", "https://llm.example/v1", "other-key", "research-model")

    def test_global_llm_presets_can_be_listed_edited_and_deleted(self) -> None:
        env_file = Path(self.temp_dir.name) / ".env"
        with patch.object(web_panel, "ENV_FILE", env_file), patch.dict("os.environ", {}, clear=True):
            created = self.client.post("/api/llm-presets", json={
                "name": "Lab Gateway",
                "baseUrl": "https://llm.example/v1",
                "apiKey": "secret-key",
                "model": "research-model",
                "concurrency": 4,
            })
            self.assertEqual(created.status_code, 201, created.get_json())
            preset_id = created.get_json()["preset"]["id"]

            listed = self.client.get("/api/llm-presets")
            self.assertEqual(listed.status_code, 200)
            self.assertEqual(listed.get_json()["presets"][0]["name"], "Lab Gateway")
            self.assertEqual(listed.get_json()["presets"][0]["concurrency"], 4)
            self.assertNotIn("apiKey", listed.get_json()["presets"][0])

            updated = self.client.put(f"/api/llm-presets/{preset_id}", json={
                "name": "Lab Gateway 2",
                "baseUrl": "https://gateway.example/v2",
                "apiKey": "",
                "model": "research-model-v2",
                "concurrency": 12,
            })
            self.assertEqual(updated.status_code, 200, updated.get_json())
            self.assertEqual(updated.get_json()["preset"]["id"], preset_id)
            selected = web_panel.translation_config_from_request({
                "llm": {"mode": "preset", "presetId": preset_id},
            })
            self.assertEqual(selected["apiKey"], "secret-key")
            self.assertEqual(selected["model"], "research-model-v2")
            self.assertEqual(selected["concurrency"], 12)

            deleted = self.client.delete(f"/api/llm-presets/{preset_id}")
            self.assertEqual(deleted.status_code, 200, deleted.get_json())
            self.assertEqual(self.client.get("/api/llm-presets").get_json()["presets"], [])
            env_text = env_file.read_text(encoding="utf-8")
            self.assertNotIn("LLM_PRESET_LAB_GATEWAY_API_KEY", env_text)

            invalid = self.client.post("/api/llm-presets", json={
                "name": "Invalid concurrency",
                "baseUrl": "https://llm.example/v1",
                "apiKey": "secret-key",
                "model": "research-model",
                "concurrency": 65,
            })
            self.assertEqual(invalid.status_code, 400)
            self.assertIn("between 1 and 64", invalid.get_json()["error"])
            invalid_fraction = self.client.post("/api/llm-presets", json={
                "name": "Fractional concurrency",
                "baseUrl": "https://llm.example/v1",
                "apiKey": "secret-key",
                "model": "research-model",
                "concurrency": 1.5,
            })
            self.assertEqual(invalid_fraction.status_code, 400)
            self.assertIn("integer", invalid_fraction.get_json()["error"])

    def test_global_speech_config_can_be_saved_tested_and_removed_without_exposing_key(self) -> None:
        env_file = Path(self.temp_dir.name) / ".env"
        with patch.object(web_panel, "ENV_FILE", env_file), patch.dict("os.environ", {}, clear=True):
            initial = self.client.get("/api/speech-config")
            self.assertEqual(initial.status_code, 200)
            self.assertFalse(initial.get_json()["speech"]["configured"])
            self.assertEqual(initial.get_json()["speech"]["region"], "centralus")
            self.assertNotIn("apiKey", initial.get_json()["speech"])

            saved = self.client.put("/api/speech-config", json={
                "apiKey": "speech-secret",
                "region": "centralus",
                "voice": "zh-CN-YunxiNeural",
            })
            self.assertEqual(saved.status_code, 200, saved.get_json())
            self.assertTrue(saved.get_json()["speech"]["configured"])
            self.assertNotIn("apiKey", saved.get_json()["speech"])
            env_text = env_file.read_text(encoding="utf-8")
            self.assertIn("AZURE_SPEECH_KEY='speech-secret'", env_text)
            self.assertIn("AZURE_SPEECH_REGION=centralus", env_text)
            self.assertIn("AZURE_SPEECH_VOICE='zh-CN-YunxiNeural'", env_text)

            updated = self.client.put("/api/speech-config", json={
                "apiKey": "",
                "region": "eastus",
                "voice": "zh-CN-XiaoxiaoNeural",
            })
            self.assertEqual(updated.status_code, 200, updated.get_json())
            self.assertEqual(web_panel.os.environ["AZURE_SPEECH_KEY"], "speech-secret")
            self.assertEqual(updated.get_json()["speech"]["region"], "eastus")

            with patch.object(
                web_panel,
                "synthesize_azure_speech",
                return_value=(b"test audio", "zh-CN-XiaoxiaoNeural"),
            ) as synthesize:
                tested = self.client.post("/api/speech-config/test")
            self.assertEqual(tested.status_code, 200, tested.get_json())
            result = tested.get_json()["result"]
            self.assertEqual(result["region"], "eastus")
            self.assertEqual(result["audioBytes"], len(b"test audio"))
            self.assertNotIn("apiKey", result)
            synthesize.assert_called_once_with("这是一次语音连接测试。")

            removed = self.client.delete("/api/speech-config")
            self.assertEqual(removed.status_code, 200, removed.get_json())
            self.assertFalse(self.client.get("/api/speech-config").get_json()["speech"]["configured"])
            removed_env = env_file.read_text(encoding="utf-8")
            self.assertNotIn("AZURE_SPEECH_KEY", removed_env)
            self.assertNotIn("AZURE_SPEECH_REGION", removed_env)
            self.assertNotIn("AZURE_SPEECH_VOICE", removed_env)

    def test_saved_llm_preset_can_be_tested_without_exposing_its_key(self) -> None:
        environment = {
            "LLM_NAME": "Test Provider",
            "LLM_BASE_URL": "https://llm.example/v1",
            "LLM_API_KEY": "secret-key",
            "LLM_MODEL": "test-model",
        }
        with patch.dict("os.environ", environment, clear=True), patch.object(web_panel, "OpenAI") as client_class:
            client = client_class.return_value
            client.chat.completions.create.return_value = SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="OK"))],
            )
            test_message = "这是一次 LLM 连接测试。请只回复：OK"
            response = self.client.post("/api/llm-presets/default/test", json={"message": test_message})
            self.assertEqual(response.status_code, 200, response.get_json())
            result = response.get_json()["result"]
            self.assertEqual(result["name"], "Test Provider")
            self.assertEqual(result["model"], "test-model")
            self.assertEqual(result["response"], "OK")
            self.assertFalse(result["contentEmpty"])
            self.assertNotIn("apiKey", result)
            self.assertGreaterEqual(result["latencyMs"], 0)
            client_class.assert_called_once_with(
                api_key="secret-key",
                base_url="https://llm.example/v1",
                timeout=180.0,
                max_retries=0,
            )
            call = client.chat.completions.create.call_args.kwargs
            self.assertEqual(call["model"], "test-model")
            self.assertNotIn("max_tokens", call)
            self.assertNotIn("max_completion_tokens", call)
            self.assertEqual(call["messages"], [{"role": "user", "content": test_message}])

            client.chat.completions.create.return_value = SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=""), finish_reason="stop")],
            )
            empty = self.client.post("/api/llm-presets/default/test", json={"message": test_message})
            self.assertEqual(empty.status_code, 200, empty.get_json())
            self.assertTrue(empty.get_json()["result"]["contentEmpty"])
            self.assertEqual(empty.get_json()["result"]["finishReason"], "stop")

            client.chat.completions.create.side_effect = RuntimeError("provider rejected secret-key")
            failed = self.client.post("/api/llm-presets/default/test", json={"message": test_message})
            self.assertEqual(failed.status_code, 502)
            self.assertNotIn("secret-key", failed.get_json()["error"])
            self.assertIn("[redacted]", failed.get_json()["error"])

            missing_message = self.client.post("/api/llm-presets/default/test", json={})
            self.assertEqual(missing_message.status_code, 400)
            self.assertIn("cannot be empty", missing_message.get_json()["error"])

    def test_saved_llm_preset_concurrency_probe_reaches_configured_limit(self) -> None:
        environment = {
            "LLM_NAME": "Parallel Provider",
            "LLM_BASE_URL": "https://llm.example/v1",
            "LLM_API_KEY": "parallel-secret",
            "LLM_MODEL": "parallel-model",
            "LLM_CONCURRENCY": "4",
        }
        lock = threading.Lock()
        active = 0
        maximum_active = 0

        class FakeOpenAI:
            def __init__(self, **_kwargs):
                self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

            def create(self, **_request):
                nonlocal active, maximum_active
                with lock:
                    active += 1
                    maximum_active = max(maximum_active, active)
                try:
                    time.sleep(0.01)
                    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="OK"))])
                finally:
                    with lock:
                        active -= 1

        with patch.dict("os.environ", environment, clear=True), patch.object(web_panel, "OpenAI", FakeOpenAI):
            response = self.client.post("/api/llm-presets/default/concurrency-test")

        self.assertEqual(response.status_code, 200, response.get_json())
        result = response.get_json()["result"]
        self.assertTrue(result["reachedConfiguredLimit"])
        self.assertEqual(result["stableConcurrency"], 4)
        self.assertEqual(result["totalRequests"], 4)
        self.assertEqual([stage["concurrency"] for stage in result["stages"]], [4])
        self.assertEqual(maximum_active, 4)
        self.assertNotIn("parallel-secret", json.dumps(result))

    def test_saved_llm_preset_concurrency_probe_reports_rate_limited_stage(self) -> None:
        environment = {
            "LLM_NAME": "Limited Provider",
            "LLM_BASE_URL": "https://llm.example/v1",
            "LLM_API_KEY": "limited-secret",
            "LLM_MODEL": "limited-model",
            "LLM_CONCURRENCY": "4",
        }
        lock = threading.Lock()
        active = 0

        class RateLimitError(RuntimeError):
            status_code = 429

        class FakeOpenAI:
            def __init__(self, **_kwargs):
                self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

            def create(self, **_request):
                nonlocal active
                with lock:
                    active += 1
                    request_active = active
                try:
                    time.sleep(0.02)
                    if request_active > 2:
                        raise RateLimitError("limited-secret was rate limited")
                    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="OK"))])
                finally:
                    with lock:
                        active -= 1

        with patch.dict("os.environ", environment, clear=True), patch.object(web_panel, "OpenAI", FakeOpenAI):
            response = self.client.post("/api/llm-presets/default/concurrency-test")

        self.assertEqual(response.status_code, 200, response.get_json())
        result = response.get_json()["result"]
        self.assertFalse(result["reachedConfiguredLimit"])
        self.assertEqual(result["stableConcurrency"], 0)
        self.assertIsNone(result["recommendedConcurrency"])
        failed_stage = result["stages"][-1]
        self.assertEqual(failed_stage["concurrency"], 4)
        self.assertEqual(failed_stage["errors"], {"rate_limited": 2})
        self.assertNotIn("limited-secret", json.dumps(result))

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

    def wait_for_reader(self, document_id: str):
        for _ in range(100):
            status = self.client.get(f"/api/reader/documents/{document_id}").get_json()
            if status["status"] in {"ready", "failed"}:
                return status
            time.sleep(0.02)
        self.fail("Reader document did not finish in time.")

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
                    "tool": "pdf_ocr_translate", "options": json.dumps({
                        "ocrFormats": ["docx", "md", "html", "tex.zip"],
                        "localSourcePath": str(original_pdf),
                        "repairMarkdown": True,
                        "repairLlm": {"mode": "preset", "presetId": "default"},
                    }),
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
        self.assertEqual(names, {
            "paper.mmd", "paper_legacy.mmd", "paper.lines.json", "paper.docx",
            "paper.md", "paper_legacy.md", "paper.html",
        })
        translation_support = {artifact["name"]: artifact["translationSupported"] for artifact in status["artifacts"]}
        self.assertTrue(translation_support["paper.mmd"])
        self.assertTrue(translation_support["paper_legacy.mmd"])
        self.assertTrue(translation_support["paper.md"])
        self.assertTrue(translation_support["paper_legacy.md"])
        self.assertTrue(translation_support["paper.html"])
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
        self.assertEqual(
            (local_output / "paper_legacy.mmd").read_text(encoding="utf-8"),
            f"\\includegraphics{{paper.assets/{crop_name}}}\n",
        )
        self.assertEqual((local_output / "paper.md").read_text(encoding="utf-8"), f"![Figure](paper.assets/{crop_name})\n![Fallback](paper.assets/{fallback_name})\n")
        self.assertEqual((local_output / "paper_legacy.md").read_text(encoding="utf-8"), f"![Figure](paper.assets/{crop_name})\n![Fallback](paper.assets/{fallback_name})\n")
        html = (local_output / "paper.html").read_text(encoding="utf-8")
        self.assertIn(f'src="paper.assets/{crop_name}"', html)
        self.assertIn(f"paper.assets/{fallback_name} 2x", html)
        self.assertIn("https://cdn.mathpix.com/fonts/cmu.css", html)
        lines = json.loads((local_output / "paper.lines.json").read_text(encoding="utf-8"))
        self.assertEqual(lines["text_display"], f"\\includegraphics{{paper.assets/{crop_name}}}")
        self.assertEqual(fallback_requests["count"], 1)
        completed_job = web_panel.job_manager.get(job_id)
        self.assertNotIn("repairLlm", completed_job.options)
        self.assertIsNone(completed_job.markdown_repair_config)

    def test_pdf_ocr_requests_only_the_mmd_bundle_when_markdown_is_not_selected(self) -> None:
        job = web_panel.Job("mmd-only", web_panel.TOOL_BY_ID["pdf_ocr_translate"], Path(self.temp_dir.name) / "mmd-only", {"ocrFormats": ["docx"], "repairMarkdown": False})
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
        job = web_panel.Job("md-bundle-failure", web_panel.TOOL_BY_ID["pdf_ocr_translate"], Path(self.temp_dir.name) / "md-bundle-failure", {"ocrFormats": ["md"], "repairMarkdown": False})
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

    def test_markdown_repair_splits_glued_fence_deterministically(self) -> None:
        source = "Text.[^1]```\n"
        repaired = web_panel.deterministic_markdown_repairs(source)
        self.assertIn("[^1]\n\n```", repaired)

    def test_markdown_repair_splits_text_before_backtick_opening_fence(self) -> None:
        source = "Paragraph```python\nprint('ok')\n```\n"

        repaired = web_panel.deterministic_markdown_repairs(source)

        self.assertEqual(repaired, "Paragraph\n\n```python\nprint('ok')\n```\n")
        self.assertEqual(web_panel.markdown_repair_errors(source, repaired), [])

    def test_markdown_repair_splits_text_before_backtick_closing_fence(self) -> None:
        source = "```python\nprint('ok')```\n"

        repaired = web_panel.deterministic_markdown_repairs(source)

        self.assertEqual(repaired, "```python\nprint('ok')\n```\n")
        self.assertEqual(web_panel.markdown_repair_errors(source, repaired), [])

    def test_markdown_repair_preserves_inline_code_and_tilde_fences(self) -> None:
        source = (
            "Use ```inline``` code.\n\n"
            "~~~python\n"
            "text```python\n"
            "~~~\n\n"
            "```python\n"
            "value = '```'\n"
            "```\n"
        )

        self.assertEqual(web_panel.deterministic_markdown_repairs(source), source)

    def test_markdown_repair_validator_reports_glued_backtick_fence(self) -> None:
        errors = web_panel.markdown_repair_errors("Paragraph```python\n", "Paragraph```python\n")

        self.assertIn("text precedes a backtick fence at line 1", errors)

    def test_markdown_repair_retries_with_validator_diagnostics(self) -> None:
        job = web_panel.Job(
            "semantic-repair",
            web_panel.TOOL_BY_ID["pdf_ocr_translate"],
            Path(self.temp_dir.name) / "semantic-repair",
            {"repairMarkdown": True},
            markdown_repair_config={"name": "test", "baseUrl": "https://example.test/v1", "apiKey": "key", "model": "model"},
        )
        source = "Sentence.\n\n[^1]\n\n[^1]: Note.\n"
        fixed = "Sentence.[^1]\n\n[^1]: Note.\n"

        with patch.object(web_panel, "llm_repair_markdown_region", side_effect=[source, fixed]) as repair:
            result = web_panel.repair_markdown_text(job, source, "md")

        self.assertEqual(result, fixed)
        self.assertEqual(repair.call_count, 2)
        self.assertIn("standalone footnote references remain", repair.call_args.args[4])

    def test_markdown_repair_converts_math_pseudocode_region(self) -> None:
        job = web_panel.Job(
            "algorithm-repair",
            web_panel.TOOL_BY_ID["pdf_ocr_translate"],
            Path(self.temp_dir.name) / "algorithm-repair",
            {"repairMarkdown": True},
            markdown_repair_config={"name": "test", "baseUrl": "https://example.test/v1", "apiKey": "key", "model": "model"},
        )
        source = "```text\nAlgorithm 1 Policy Update\npi := \\arg\\max_a Q(a)\n```\n"
        fixed = "$$\n\\begin{aligned}\n&\\textbf{Algorithm 1: Policy Update}\\\\\n&\\pi \\gets \\arg\\max_a Q(a)\n\\end{aligned}\n$$\n"

        with patch.object(web_panel, "llm_repair_markdown_region", return_value=fixed):
            result = web_panel.repair_markdown_text(job, source, "mmd")

        self.assertEqual(result, fixed)
        self.assertEqual(web_panel.markdown_repair_errors(source, result), [])

    def test_markdown_repair_regions_do_not_expand_algorithm_fence_errors_to_every_formula(self) -> None:
        source = (
            "```text\nAlgorithm 1: update\nx := $y$\n```\n\n"
            + ("Ordinary prose.\n" * 16)
            + "\n"
            "$$\na = b\n$$\n\n"
            "$$\nc = d\n$$\n"
        )

        regions = web_panel.markdown_repair_regions(source)

        self.assertEqual(len(regions), 1)
        self.assertIn("Algorithm 1", source[regions[0].start:regions[0].end])
        self.assertNotIn("c = d", source[regions[0].start:regions[0].end])

    def test_markdown_repair_uses_saved_llm_concurrency(self) -> None:
        job = web_panel.Job(
            "repair-concurrency",
            web_panel.TOOL_BY_ID["markdown_repair"],
            Path(self.temp_dir.name) / "repair-concurrency",
            {},
            markdown_repair_config={"concurrency": 3},
        )

        self.assertEqual(web_panel.translation_concurrency(job), 3)

    def test_markdown_repair_reconciles_merged_visual_footnotes(self) -> None:
        job = web_panel.Job(
            "footnote-repair",
            web_panel.TOOL_BY_ID["pdf_ocr_translate"],
            Path(self.temp_dir.name) / "footnote-repair",
            {"repairMarkdown": True},
            markdown_repair_config={"name": "test", "baseUrl": "https://example.test/v1", "apiKey": "key", "model": "model"},
        )
        source = "First claim.${ }^{3}$\n\nSecond claim.${ }^{4}$\n\n[^38]: ${ }^{3}$ First note.\n    ${ }^{4}$ Second note.\n"
        fixed = "First claim.[^38]\n\nSecond claim.[^38a]\n\n[^38]: First note.\n\n[^38a]: Second note.\n"

        with patch.object(web_panel, "llm_repair_markdown_region", return_value=fixed):
            result = web_panel.repair_markdown_text(job, source, "md")

        self.assertEqual(result, fixed)
        self.assertEqual(web_panel.markdown_repair_errors(source, result), [])

    def test_merged_footnote_repair_is_idempotent_and_preserves_code_and_images(self) -> None:
        source = (
            "Claim${ }^{3}$ and another${ }^{4}$.\n\n"
            "[^38]\n\n"
            "[^38]: ${ }^{3}$ First note.\n    ${ }^{4}$ Second note.\n\n"
            "![Figure](paper.assets/figure.png)\n\n"
            "```text\n${ }^{3}$ must stay code\n```\n"
        )

        repaired, changes = web_panel.repair_merged_markdown_footnotes(source)
        repeated, repeated_changes = web_panel.repair_merged_markdown_footnotes(repaired)

        self.assertIn("Claim[^38] and another[^38a].", repaired)
        self.assertIn("[^38]: First note.\n\n[^38a]: Second note.", repaired)
        self.assertIn("![Figure](paper.assets/figure.png)", repaired)
        self.assertIn("${ }^{3}$ must stay code", repaired)
        self.assertTrue(changes)
        self.assertEqual(repeated, repaired)
        self.assertEqual(repeated_changes, [])

    def test_chinese_punctuation_normalization_preserves_protected_markdown_literals(self) -> None:
        source = (
            "中文，标题（甲）：“测试”！ [链接](https://example.test/a，b)\n\n"
            "`x，y`\n\n"
            "$$x，y$$\n\n"
            "~~~text\n中文，代码\n~~~\n"
        )

        normalized, changes = web_panel.normalize_chinese_punctuation(source)

        self.assertIn('中文,标题(甲):"测试"!', normalized)
        self.assertIn("https://example.test/a，b", normalized)
        self.assertIn("`x，y`", normalized)
        self.assertIn("$$x，y$$", normalized)
        self.assertIn("中文，代码", normalized)
        self.assertTrue(changes)

    def test_markdown_repair_image_placeholders_round_trip_exactly(self) -> None:
        source = "![Figure](<paper assets/figure one.png>)\n\\includegraphics{paper.assets/figure-two.png}\n"
        protected, images = web_panel.protect_markdown_repair_images(source)

        self.assertNotIn("figure one.png", protected)
        self.assertEqual(web_panel.restore_markdown_repair_images(protected, images), source)
        with self.assertRaisesRegex(RuntimeError, "image placeholders"):
            web_panel.restore_markdown_repair_images(protected.replace("[[[OCR_IMAGE_0000]]]", ""), images)

    def test_markdown_repair_validator_rejects_changed_images_and_structures(self) -> None:
        original = "![Figure](paper.assets/figure.png)\n\nText.[^1]\n\n[^1]: Note.\n"
        changed = "![Figure](other/figure.png)\n\n[^1]\n\n```text\nAlgorithm 1: x = $y$\n"

        errors = web_panel.markdown_repair_errors(original, changed)

        self.assertIn("localized image references changed", errors)
        self.assertTrue(any("unclosed fenced block" in error for error in errors))
        self.assertIn("standalone footnote references remain", errors)

    def test_translation_math_postprocessor_repairs_high_confidence_boundaries(self) -> None:
        glued, glued_changes, glued_errors = web_panel.postprocess_translated_markdown_text("$$E=mc^2$$\n")
        stray, stray_changes, stray_errors = web_panel.postprocess_translated_markdown_text("$$正文，继续说明。\n")
        missing, missing_changes, missing_errors = web_panel.postprocess_translated_markdown_text(
            "结论：\\operatorname{Law}_{X}(x)\n$$\n"
        )

        self.assertEqual(glued, "$$\nE=mc^2\n$$\n")
        self.assertEqual(stray, "正文，继续说明。\n")
        self.assertEqual(missing, "结论：\n\n$$\n\\operatorname{Law}_{X}(x)\n$$\n")
        self.assertTrue(glued_changes)
        self.assertTrue(stray_changes)
        self.assertTrue(missing_changes)
        self.assertEqual(glued_errors, [])
        self.assertEqual(stray_errors, [])
        self.assertEqual(missing_errors, [])

    def test_translation_math_postprocessor_skips_fences_and_inline_math(self) -> None:
        source = "```latex\n$$正文，必须保持不变。\n```\n\n$x$${ }^1$\n"

        repaired, changes, errors = web_panel.postprocess_translated_markdown_text(source)

        self.assertEqual(repaired, source)
        self.assertEqual(changes, [])
        self.assertEqual(errors, [])

    def test_translation_math_postprocessor_reports_ambiguous_math_damage(self) -> None:
        errors = web_panel.postprocess_translated_markdown_text("$$\nE=mc^2\n# 被吞掉的标题\n")[2]
        escaped_errors = web_panel.postprocess_translated_markdown_text("$$\nx \\# y\n$$\n")[2]

        self.assertTrue(any("Markdown block structure" in error for error in errors))
        self.assertTrue(any("missing a closing" in error for error in errors))
        self.assertEqual(escaped_errors, [])

    def test_translation_math_finalization_is_atomic_and_keeps_unfixed_batch(self) -> None:
        root = Path(self.temp_dir.name) / "translation-postprocess"
        output_dir = root / "output"
        output_dir.mkdir(parents=True)
        job = web_panel.Job("translation-postprocess", web_panel.TOOL_BY_ID["document_translate"], root, {})
        invalid = output_dir / "invalid_zh-CN.md"
        valid = output_dir / "valid_zh-CN.mmd"
        invalid.write_text("$$\nE=mc^2\n# 被吞掉的标题\n", encoding="utf-8")
        valid.write_text("$$E=mc^2$$\n", encoding="utf-8")

        with self.assertRaises(web_panel.TranslationMarkdownPostprocessError) as raised:
            web_panel.finalize_translated_markdown_outputs(job, [invalid, valid])

        self.assertFalse(invalid.exists())
        self.assertFalse(valid.exists())
        self.assertEqual(web_panel.translation_unfixed_path(invalid).read_text(encoding="utf-8"), "$$\nE=mc^2\n# 被吞掉的标题\n")
        self.assertEqual(web_panel.translation_unfixed_path(valid).read_text(encoding="utf-8"), "$$E=mc^2$$\n")
        self.assertEqual(set(raised.exception.preserved_outputs), {
            web_panel.translation_unfixed_path(invalid), web_panel.translation_unfixed_path(valid),
        })

    def test_translated_markdown_finalization_normalizes_chinese_punctuation(self) -> None:
        root = Path(self.temp_dir.name) / "translation-punctuation"
        output_dir = root / "output"
        output_dir.mkdir(parents=True)
        job = web_panel.Job("translation-punctuation", web_panel.TOOL_BY_ID["document_translate"], root, {})
        output = output_dir / "paper_zh-CN.md"
        output.write_text("中文，测试！ `x，y`\n", encoding="utf-8")

        web_panel.finalize_translated_markdown_outputs(job, [output])

        self.assertEqual(output.read_text(encoding="utf-8"), "中文,测试! `x，y`\n")

    def test_standalone_markdown_repair_tool_writes_fixed_copy_without_translation(self) -> None:
        root = Path(self.temp_dir.name) / "standalone-markdown-repair"
        input_dir = root / "input"
        input_dir.mkdir(parents=True)
        source = input_dir / "paper.mmd"
        source.write_text("$$E=mc^2$$\n", encoding="utf-8")
        job = web_panel.Job(
            "standalone-markdown-repair",
            web_panel.TOOL_BY_ID["markdown_repair"],
            root,
            {"deepRepair": False, "mathRepair": True},
        )

        web_panel.run_markdown_repair_tool(job)

        output = root / "output" / "paper_fixed.mmd"
        self.assertEqual(output.read_text(encoding="utf-8"), "$$\nE=mc^2\n$$\n")
        self.assertEqual(job.phase, "markdown_repair_complete")
        self.assertEqual(job.download_path, output)

    def test_standalone_markdown_repair_splits_footnotes_and_optionally_normalizes_punctuation(self) -> None:
        root = Path(self.temp_dir.name) / "standalone-footnote-repair"
        input_dir = root / "input"
        input_dir.mkdir(parents=True)
        source = input_dir / "paper.md"
        source.write_text(
            "正文，标记${ }^{3}$ 和 ${ }^{4}$。\n\n[^38]\n\n[^38]: ${ }^{3}$ 第一条。\n    ${ }^{4}$ 第二条。\n",
            encoding="utf-8",
        )
        job = web_panel.Job(
            "standalone-footnote-repair",
            web_panel.TOOL_BY_ID["markdown_repair"],
            root,
            {"deepRepair": False, "mathRepair": False, "footnoteRepair": True, "normalizeChinesePunctuation": True},
        )

        web_panel.run_markdown_repair_tool(job)

        output = root / "output" / "paper_fixed.md"
        self.assertEqual(
            output.read_text(encoding="utf-8"),
            "正文,标记[^38] 和 [^38a].\n\n\n\n[^38]: 第一条.\n\n[^38a]: 第二条.\n",
        )

    def test_ocr_markdown_repair_splits_merged_footnotes_before_deep_repair(self) -> None:
        root = Path(self.temp_dir.name) / "ocr-footnote-repair"
        output_dir = root / "output"
        output_dir.mkdir(parents=True)
        job = web_panel.Job(
            "ocr-footnote-repair",
            web_panel.TOOL_BY_ID["pdf_ocr_translate"],
            root,
            {"repairMarkdown": True},
            markdown_repair_config={"name": "test", "baseUrl": "https://example.test/v1", "apiKey": "key", "model": "model"},
        )
        source = "Claim${ }^{3}$ and another${ }^{4}$.\n\n[^38]\n\n[^38]: ${ }^{3}$ First note.\n    ${ }^{4}$ Second note.\n"
        assets = web_panel.LocalAssetStore(output_dir / "paper.assets")

        web_panel.write_ocr_text_artifact(job, output_dir / "paper.md", "md", source, assets)

        self.assertEqual(
            (output_dir / "paper.md").read_text(encoding="utf-8"),
            "Claim[^38] and another[^38a].\n\n\n\n[^38]: First note.\n\n[^38a]: Second note.\n",
        )
        self.assertTrue((output_dir / "paper_legacy.md").is_file())

    def test_standalone_markdown_repair_tool_keeps_original_when_validation_fails(self) -> None:
        root = Path(self.temp_dir.name) / "standalone-markdown-repair-failure"
        input_dir = root / "input"
        input_dir.mkdir(parents=True)
        source = input_dir / "paper.md"
        original = "$$\nE=mc^2\n# 被吞掉的标题\n"
        source.write_text(original, encoding="utf-8")
        job = web_panel.Job(
            "standalone-markdown-repair-failure",
            web_panel.TOOL_BY_ID["markdown_repair"],
            root,
            {"deepRepair": False, "mathRepair": True},
        )

        with self.assertRaises(web_panel.MarkdownRepairOutputError):
            web_panel.run_markdown_repair_tool(job)

        raw = root / "output" / "paper_fixed_unfixed.md"
        self.assertEqual(raw.read_text(encoding="utf-8"), original)
        self.assertEqual(job.phase, "markdown_repair_failed")
        self.assertEqual(job.download_path, raw)

    def test_failed_markdown_repair_keeps_only_localized_legacy_artifact(self) -> None:
        root = Path(self.temp_dir.name) / "repair-fallback"
        output_dir = root / "output"
        assets_dir = output_dir / "paper.assets"
        assets_dir.mkdir(parents=True)
        job = web_panel.Job(
            "repair-fallback",
            web_panel.TOOL_BY_ID["pdf_ocr_translate"],
            root,
            {"repairMarkdown": True},
            markdown_repair_config={"name": "test", "baseUrl": "https://example.test/v1", "apiKey": "key", "model": "model"},
        )
        assets = web_panel.LocalAssetStore(assets_dir, asset_paths={"figure.png"})
        output = output_dir / "paper.md"

        with patch.object(web_panel, "repair_markdown_text", side_effect=RuntimeError("invalid repair")):
            web_panel.write_ocr_text_artifact(job, output, "md", "![Figure](./images/figure.png)\n", assets)

        legacy = output_dir / "paper_legacy.md"
        self.assertFalse(output.exists())
        self.assertEqual(legacy.read_text(encoding="utf-8"), "![Figure](paper.assets/figure.png)\n")
        self.assertEqual([artifact["name"] for artifact in job.artifacts], ["paper_legacy.md"])
        self.assertTrue(job.artifacts[0]["translationSupported"])
        self.assertTrue(any("invalid repair" in warning for warning in job.warnings))

    def test_pdf_ocr_repair_failure_completes_with_warnings_and_other_artifacts(self) -> None:
        root = Path(self.temp_dir.name) / "repair-job-fallback"
        input_dir = root / "input"
        input_dir.mkdir(parents=True)
        source = input_dir / "paper.pdf"
        source.write_bytes(b"%PDF mock")
        job = web_panel.Job(
            "repair-job-fallback",
            web_panel.TOOL_BY_ID["pdf_ocr_translate"],
            root,
            {"ocrFormats": ["md"], "repairMarkdown": True},
            markdown_repair_config={"name": "test", "baseUrl": "https://example.test/v1", "apiKey": "key", "model": "model"},
        )
        job.source_stem = "paper"
        bundle = markdown_bundle("paper.mmd", "![Figure](./images/figure.png)\n")

        def fake_get(url, **_kwargs):
            if url.endswith("/pdf/mock-pdf"):
                return FakeResponse({"status": "completed"})
            if url.endswith("/converter/mock-pdf"):
                return FakeResponse({"conversion_status": {"mmd.zip": {"status": "completed"}, "md": {"status": "completed"}}})
            if url.endswith(".mmd.zip"):
                return FakeResponse(content=bundle)
            if url.endswith(".md"):
                return FakeResponse(content=b"![Figure](./images/figure.png)\n")
            if url.endswith(".lines.json"):
                return FakeResponse(content=b"{}")
            return FakeResponse(content=b"mock")

        with patch.dict("os.environ", {"MATHPIX_APP_ID": "id", "MATHPIX_APP_KEY": "key"}), \
             patch.object(web_panel.requests, "post", return_value=FakeResponse({"pdf_id": "mock-pdf"})), \
             patch.object(web_panel.requests, "get", side_effect=fake_get), \
             patch.object(web_panel, "repair_markdown_text", side_effect=RuntimeError("repair rejected")), \
             patch.object(web_panel, "POLL_INTERVAL_SECONDS", 0):
            web_panel.run_job(job)

        names = {artifact["name"] for artifact in job.artifacts}
        self.assertEqual(job.status, "completed_with_warnings")
        self.assertEqual(names, {"paper_legacy.mmd", "paper_legacy.md", "paper.lines.json"})
        self.assertFalse((root / "output" / "paper.mmd").exists())
        self.assertFalse((root / "output" / "paper.md").exists())
        self.assertTrue(job.download_path.is_file())
        self.assertIsNone(job.markdown_repair_config)

    def test_ocr_job_requires_repair_llm_only_when_repair_is_enabled(self) -> None:
        original_pdf = Path(self.temp_dir.name) / "repair-config.pdf"
        original_pdf.write_bytes(b"%PDF source")
        payload = {
            "tool": "pdf_ocr_translate",
            "manifest": json.dumps([{"relativePath": "repair-config.pdf", "workName": "repair-config"}]),
            "files": (io.BytesIO(b"%PDF upload"), "repair-config.pdf"),
        }
        with patch.dict("os.environ", {"MATHPIX_APP_ID": "id", "MATHPIX_APP_KEY": "key"}, clear=True):
            enabled = self.client.post(
                "/api/jobs",
                data={**payload, "options": json.dumps({"localSourcePath": str(original_pdf), "repairMarkdown": True})},
                content_type="multipart/form-data",
            )
        self.assertEqual(enabled.status_code, 400)
        self.assertIn("LLM", enabled.get_json()["error"])

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

        block_math = "Before.\n\n$$\n\\begin{aligned}\nx &= y + 1\\\\\n\\end{aligned}\n$$\n\nAfter."
        protected_math, math_literals = web_panel.protect_literals(block_math)
        self.assertEqual(len(math_literals), 1)
        nested_math = (
            r"Suppose $A=\left[\begin{array}{ll}A_{11} & A_{12} \\ A_{21} & A_{22}"
            r"\end{array}\right]$ and $\mathcal{B}[g,z](v)$ are fixed."
        )
        protected_nested, nested_literals = web_panel.protect_literals(nested_math)
        self.assertFalse(any(web_panel.PLACEHOLDER_PATTERN.search(value) for value in nested_literals.values()))
        self.assertEqual(web_panel.restore_literals(protected_nested, nested_literals), nested_math)
        with patch.object(web_panel, "llm_translate", side_effect=lambda _job, value: value):
            restored_nested = web_panel.translate_text_block(None, nested_math)
        self.assertEqual(restored_nested, nested_math)
        self.assertNotIn("KEEP_", restored_nested)
        self.assertNotIn(r"\\begin{aligned}", protected_math)

        def emits_reserved_marker(_job: object, value: str) -> str:
            return "marker changed" if "[[[KEEP_" in value else "译文[[[KEEP_0000]]]"

        with patch.object(web_panel, "llm_translate", side_effect=emits_reserved_marker):
            recovered = web_panel.translate_text_block(None, source)
        self.assertNotIn("[[[KEEP_", recovered)
        for literal in literals.values():
            self.assertIn(literal, recovered)

    def test_marker_mismatch_creates_shareable_debug_jsonl_without_configured_key(self) -> None:
        root = Path(self.temp_dir.name) / "translation-debug"
        (root / "output").mkdir(parents=True)
        job = web_panel.Job(
            "translation-debug",
            web_panel.TOOL_BY_ID["document_translate"],
            root,
            {},
            translation_config={
                "name": "Debug provider",
                "baseUrl": "https://llm.example/v1?token=base-url-secret",
                "apiKey": "configured-api-secret",
                "model": "debug-model",
                "concurrency": 4,
            },
        )

        def fake_translate(_job: object, value: str) -> str:
            return "marker was removed" if "[[[KEEP_" in value else "译文"

        with patch.object(web_panel, "llm_translate", side_effect=fake_translate):
            translated = web_panel.translate_text_block(job, "Text with $x^2$.", debug_context="paper.md")

        self.assertIn("$x^2$", translated)
        debug_path = root / "output" / "translation_debug.jsonl"
        payload = json.loads(debug_path.read_text(encoding="utf-8").strip())
        self.assertEqual(payload["event"], "protected_literal_mismatch")
        self.assertEqual(payload["context"], "paper.md:chunk:1/1")
        self.assertEqual(payload["model"], "debug-model")
        self.assertEqual(payload["provider"], "https://llm.example")
        self.assertTrue(payload["missingMarkers"])
        self.assertIn("protectedInput", payload)
        self.assertIn("modelOutput", payload)
        debug_text = debug_path.read_text(encoding="utf-8")
        self.assertNotIn("configured-api-secret", debug_text)
        self.assertNotIn("base-url-secret", debug_text)
        snapshot = job.snapshot()
        self.assertEqual(snapshot["translationDebugCount"], 1)
        self.assertTrue(snapshot["translationDebugUrl"].endswith("/translation_debug.jsonl"))
        self.assertTrue(any(item["kind"] == "translation_debug" for item in snapshot["artifacts"]))

    def test_translation_repairs_missing_boundary_formula_without_retry(self) -> None:
        root = Path(self.temp_dir.name) / "boundary-formula"
        (root / "output").mkdir(parents=True)
        job = web_panel.Job(
            "boundary-formula",
            web_panel.TOOL_BY_ID["document_translate"],
            root,
            {},
            translation_config={"concurrency": 1},
        )
        source = "$$\nJ(\\theta)=0\n$$\n\nFollowing explanation."
        with patch.object(web_panel, "llm_translate", return_value="后续解释。") as translate:
            result = web_panel.translate_text_block(job, source)
        self.assertEqual(result, "$$\nJ(\\theta)=0\n$$\n\n后续解释。")
        translate.assert_called_once()
        self.assertEqual(job.translation_debug_count, 0)

    def test_translation_repairs_neighbor_marker_substitution_without_retranslating_block(self) -> None:
        source = "### Heading\n\nLet $a$ be $b$ in $c$.\n\nTail paragraph."

        def duplicate_neighbor(_job: object, value: str) -> str:
            markers = web_panel.PLACEHOLDER_PATTERN.findall(value)
            return value.replace(markers[1], markers[0])

        with patch.object(web_panel, "llm_translate", side_effect=duplicate_neighbor) as translate:
            result = web_panel.translate_text_block(None, source)
        self.assertEqual(result, source)
        self.assertNotIn("KEEP_", result)
        translate.assert_called_once()

    def test_translation_retranslates_only_block_with_missing_marker(self) -> None:
        source = (
            "### Notes\n\n"
            "Opening $a$ remains unchanged.\n\n"
            "[^39]: The notion $q$ only makes sense if $r$ whenever $s$.\n\n"
            "[^40]: Neighboring note $z$ must remain separate."
        )
        calls: list[str] = []

        def omit_one_marker(_job: object, value: str) -> str:
            calls.append(value)
            markers = web_panel.PLACEHOLDER_PATTERN.findall(value)
            if len(markers) == 5:
                return value.replace(markers[3], "")
            return value

        with patch.object(web_panel, "llm_translate", side_effect=omit_one_marker):
            result = web_panel.translate_text_block(None, source)
        self.assertEqual(result, source)
        self.assertIn("\n\n[^39]:", result)
        self.assertIn("\n\n[^40]:", result)
        self.assertNotIn("KEEP_", result)
        self.assertGreater(len(calls), 1)
        self.assertTrue(all("[^40]:" not in call for call in calls[1:]))

    def test_translation_preserves_chunk_boundary_blank_lines_and_repairs_markdown_structure(self) -> None:
        source = "### Heading\n\nBody paragraph.\n\n[^1]: Footnote text.\n\n"
        calls = 0

        def collapse_first_response(_job: object, value: str) -> str:
            nonlocal calls
            calls += 1
            if calls == 1:
                return "structured JSON unsupported"
            if calls == 2:
                return value.replace("\n\n", " ").rstrip()
            return value.rstrip()

        with patch.object(web_panel, "llm_translate", side_effect=collapse_first_response):
            result = web_panel.translate_text_block(None, source, preserve_markdown_structure=True)
        self.assertEqual(result, source)
        self.assertGreater(calls, 1)
        self.assertEqual(
            web_panel.markdown_structure_signature(result),
            web_panel.markdown_structure_signature(source),
        )

    def test_structured_markdown_batch_keeps_literals_and_boundaries_outside_model_output(self) -> None:
        source = "### Heading\n\nLet $x$ remain.\n\n[^1]: Footnote."
        requests: list[str] = []

        def translate_json_segments(_job: object, value: str) -> str:
            requests.append(value)
            payload = json.loads(value)
            replacements = {
                "Heading": "标题",
                "Let ": "令 ",
                " remain.": " 保持不变。",
                "Footnote.": "脚注。",
            }
            for part in payload["parts"]:
                if "text" in part:
                    part["text"] = replacements[part["text"]]
            return json.dumps(payload, ensure_ascii=False)

        with patch.object(web_panel, "llm_translate", side_effect=translate_json_segments):
            result = web_panel.translate_text_block(None, source, preserve_markdown_structure=True)
        self.assertEqual(result, "### 标题\n\n令 $x$ 保持不变。\n\n[^1]: 脚注。")
        self.assertEqual(len(requests), 1)
        self.assertNotIn("KEEP_", requests[0])
        request_payload = json.loads(requests[0])
        self.assertTrue(any(part.get("literal") == "$x$" for part in request_payload["parts"]))
        self.assertTrue(any("boundary" in part for part in request_payload["parts"]))

    def test_translation_allows_unchanged_duplicate_inline_math_but_not_duplicate_links(self) -> None:
        inline_source = "We minimize $J$ directly."

        def duplicate_inline(_job: object, value: str) -> str:
            marker = web_panel.PLACEHOLDER_PATTERN.search(value).group(0)
            return f"我们先最小化 {marker}，再检查 {marker}。"

        with patch.object(web_panel, "llm_translate", side_effect=duplicate_inline) as translate:
            inline_result = web_panel.translate_text_block(None, inline_source)
        self.assertEqual(inline_result, "我们先最小化 $J$，再检查 $J$。")
        translate.assert_called_once()

        link_source = "Read [paper](https://example.com)."
        calls = 0

        def duplicate_link(_job: object, value: str) -> str:
            nonlocal calls
            calls += 1
            marker = web_panel.PLACEHOLDER_PATTERN.search(value)
            return value.replace(marker.group(0), marker.group(0) * 2) if marker else value

        with patch.object(web_panel, "llm_translate", side_effect=duplicate_link):
            link_result = web_panel.translate_text_block(None, link_source)
        self.assertEqual(link_result, link_source)
        self.assertGreater(calls, 1)

    def test_markdown_translation_resumes_saved_chunks_and_uses_stable_partial_name(self) -> None:
        root = Path(self.temp_dir.name)
        source = root / "paper.md"
        output = root / "paper_zh-CN.partial.md"
        source.write_text("A" * 6000 + "\n\n" + "B" * 6000, encoding="utf-8")
        job = web_panel.Job(
            "markdown-resume",
            web_panel.TOOL_BY_ID["document_translate"],
            root,
            {},
            translation_config={"name": "Test", "baseUrl": "https://llm.example", "apiKey": "key", "model": "model"},
        )

        def first_attempt(_job: object, value: str) -> str:
            def translate(text: str) -> str:
                if text.startswith("A"):
                    return "甲" * len(text)
                raise RuntimeError("temporary failure")

            return translated_request_content(value, translate)

        with patch.object(web_panel, "llm_translate", side_effect=first_attempt):
            with self.assertRaisesRegex(RuntimeError, "temporary failure"):
                web_panel.translate_markdown(job, source, output)

        self.assertEqual(job.translation_progress, {"completed": 2, "total": 3, "active": 0, "concurrency": 1})
        self.assertTrue(web_panel.translation_resume_manifest_path(output).is_file())
        with patch.object(
            web_panel,
            "llm_translate",
            side_effect=lambda _job, value: translated_request_content(value, lambda text: "乙" * len(text)),
        ) as resumed:
            web_panel.translate_markdown(job, source, output)
        resumed.assert_called_once()
        self.assertEqual(output.read_text(encoding="utf-8"), "甲" * 6000 + "\n\n" + "乙" * 6000)
        self.assertFalse(web_panel.translation_resume_manifest_path(output).exists())
        self.assertEqual(web_panel.partial_translation_path(source).name, "paper_zh-CN.partial.md")
        self.assertEqual(web_panel.partial_translation_path(root / "paper_zh-CN.md").name, "paper_zh-CN.partial.md")

    def test_markdown_translation_runs_chunks_concurrently_but_commits_in_order(self) -> None:
        root = Path(self.temp_dir.name)
        source = root / "parallel.md"
        output = root / "parallel_zh-CN.partial.md"
        source.write_text("".join(letter * 6000 for letter in "ABCD"), encoding="utf-8")
        job = web_panel.Job(
            "markdown-parallel",
            web_panel.TOOL_BY_ID["document_translate"],
            root,
            {},
            translation_config={
                "name": "Test",
                "baseUrl": "https://llm.example",
                "apiKey": "parallel-key",
                "model": "model",
                "concurrency": 4,
            },
        )
        lock = threading.Lock()
        active = 0
        maximum_active = 0
        translations = {"A": "甲", "B": "乙", "C": "丙", "D": "丁"}
        delays = {"A": 0.06, "B": 0.04, "C": 0.02, "D": 0.0}

        def fake_translate(_job: object, value: str) -> str:
            nonlocal active, maximum_active
            payload = json.loads(value)
            source_text = next(part["text"] for part in payload["parts"] if "text" in part)
            source_key = source_text[0]
            with lock:
                active += 1
                maximum_active = max(maximum_active, active)
            try:
                time.sleep(delays[source_key])
                return translated_request_content(value, lambda text: translations[source_key] * len(text))
            finally:
                with lock:
                    active -= 1

        with patch.object(web_panel, "llm_translate", side_effect=fake_translate):
            web_panel.translate_markdown(job, source, output)

        self.assertEqual(output.read_text(encoding="utf-8"), "".join(translations[key] * 6000 for key in "ABCD"))
        self.assertEqual(maximum_active, 4)
        self.assertEqual(job.translation_progress, {"completed": 4, "total": 4, "active": 0, "concurrency": 4})

    def test_concurrent_translation_failure_keeps_only_continuous_partial_prefix(self) -> None:
        root = Path(self.temp_dir.name)
        source = root / "parallel-failure.md"
        output = root / "parallel-failure_zh-CN.partial.md"
        source.write_text("".join(letter * 6000 for letter in "ABCD"), encoding="utf-8")
        job = web_panel.Job(
            "markdown-parallel-failure",
            web_panel.TOOL_BY_ID["document_translate"],
            root,
            {},
            translation_config={
                "name": "Test",
                "baseUrl": "https://llm.example",
                "apiKey": "parallel-failure-key",
                "model": "model",
                "concurrency": 3,
            },
        )

        def fake_translate(_job: object, value: str) -> str:
            payload = json.loads(value)
            source_text = next(part["text"] for part in payload["parts"] if "text" in part)
            if source_text.startswith("A"):
                time.sleep(0.03)
                return translated_request_content(value, lambda text: "甲" * len(text))
            if source_text.startswith("B"):
                time.sleep(0.04)
                raise RuntimeError("permanent failure")
            return translated_request_content(value, lambda text: "不应提交" * len(text))

        with patch.object(web_panel, "llm_translate", side_effect=fake_translate):
            with self.assertRaisesRegex(RuntimeError, "permanent failure"):
                web_panel.translate_markdown(job, source, output)

        self.assertEqual(output.read_text(encoding="utf-8"), "甲" * 6000)
        self.assertEqual(job.translation_progress, {"completed": 1, "total": 4, "active": 0, "concurrency": 3})

    def test_concurrent_scheduler_stops_submitting_after_any_inflight_failure(self) -> None:
        job = web_panel.Job(
            "scheduler-failure",
            web_panel.TOOL_BY_ID["document_translate"],
            Path(self.temp_dir.name),
            {},
            translation_config={"concurrency": 3},
        )
        started: list[int] = []
        lock = threading.Lock()

        def task(index: int) -> str:
            with lock:
                started.append(index)
            if index == 2:
                raise RuntimeError("fast failure")
            time.sleep(0.03)
            return str(index)

        tasks = [lambda index=index: task(index) for index in range(6)]
        committed: list[str] = []
        with self.assertRaisesRegex(RuntimeError, "fast failure"):
            web_panel.run_ordered_translation_tasks(job, tasks, lambda _index, value: committed.append(value))
        self.assertEqual(sorted(started), [0, 1, 2])
        self.assertEqual(committed, [])

    def test_reader_translates_markdown_one_block_at_a_time_with_stable_pairs(self) -> None:
        root = Path(self.temp_dir.name)
        output = root / "reader_zh-CN.partial.md"
        source_blocks = web_panel.markdown_to_reader_blocks(
            "# Introduction\n\nFirst paragraph.\n\nSecond paragraph."
        )
        translations = iter(["介绍", "第一段。", "第二段。"])
        with patch.object(web_panel, "llm_translate", side_effect=lambda _job, _text: next(translations)):
            translated_blocks = web_panel.translate_reader_markdown_blocks(None, source_blocks, output)

        self.assertEqual(
            [block["pairId"] for block in translated_blocks],
            [block["id"] for block in source_blocks],
        )
        self.assertEqual([block["content"] for block in translated_blocks], ["介绍", "第一段。", "第二段。"])
        round_trip = web_panel.markdown_to_reader_blocks(output.read_text(encoding="utf-8"))
        self.assertEqual(len(round_trip), len(source_blocks))
        self.assertEqual([block["type"] for block in round_trip], [block["type"] for block in source_blocks])

    def test_reader_translation_cache_restores_aligned_markdown_blocks(self) -> None:
        cache_key = "a" * 64
        translated = Path(self.temp_dir.name) / "paper_zh-CN.md"
        translated.write_text("# 标题\n\n译文。", encoding="utf-8")
        aligned_blocks = [
            {"id": "b1", "pairId": "b1", "type": "heading", "level": 1, "section": "标题", "content": "标题"},
            {"id": "b2", "pairId": "b2", "type": "paragraph", "section": "标题", "content": "译文。"},
        ]
        config = {"baseUrl": "https://llm.example/v1", "model": "model"}
        document = web_panel.ReaderDocument(
            "aligned-source",
            Path(self.temp_dir.name) / "source-job",
            "paper",
            "markdown",
            "markdown_translate",
            render_kind="markdown",
            cache_key=cache_key,
            translated_blocks=aligned_blocks,
        )
        web_panel.store_reader_translation_cache(document, config, translated)

        restored_document = web_panel.ReaderDocument(
            "aligned-restored",
            Path(self.temp_dir.name) / "restored-job",
            "paper",
            "markdown",
            "markdown_translate",
            render_kind="markdown",
            cache_key=cache_key,
        )
        destination = Path(self.temp_dir.name) / "restored.md"
        self.assertTrue(
            web_panel.restore_reader_translation_cache(restored_document, config, destination)
        )
        self.assertEqual(restored_document.translated_blocks, aligned_blocks)
        manifest = web_panel.read_reader_cache_manifest(cache_key)
        entry = manifest["translations"][web_panel.reader_translation_cache_key(config)]
        self.assertEqual(entry["markdownPostprocessVersion"], web_panel.TRANSLATION_MARKDOWN_POSTPROCESS_VERSION)
        self.assertFalse(web_panel.reader_translation_cache_entry_is_current({"pairingVersion": web_panel.READER_PAIRING_VERSION}))

    def test_reader_translation_cache_restores_aligned_html_source_and_translation(self) -> None:
        cache_key = "b" * 64
        config = {"baseUrl": "https://llm.example/v1", "model": "html-model"}
        source_root = Path(self.temp_dir.name) / "html-source-job"
        source_output = source_root / "output"
        source_output.mkdir(parents=True)
        source = source_output / "paper.html"
        translated = source_output / "paper_zh-CN.html"
        source.write_text(
            '<html><body><p data-reader-pair-id="reader-aligned-1">Source.</p></body></html>',
            encoding="utf-8",
        )
        translated.write_text(
            '<html><body><p data-reader-pair-id="reader-aligned-1">译文。</p></body></html>',
            encoding="utf-8",
        )
        document = web_panel.ReaderDocument(
            "html-source",
            source_root,
            "paper",
            "html",
            "html_pair",
            render_kind="html",
            cache_key=cache_key,
            document_filename="paper.html",
        )
        web_panel.store_reader_translation_cache(document, config, translated)

        restored_root = Path(self.temp_dir.name) / "html-restored-job"
        restored_output = restored_root / "output"
        restored_output.mkdir(parents=True)
        restored_source = restored_output / "paper.html"
        restored_source.write_text(
            '<html><body><p data-reader-pair-id="stale-pair">Source.</p></body></html>',
            encoding="utf-8",
        )
        restored_document = web_panel.ReaderDocument(
            "html-restored",
            restored_root,
            "paper",
            "html",
            "html_pair",
            render_kind="html",
            cache_key=cache_key,
            document_filename="paper.html",
        )
        restored_translation = restored_output / "paper_zh-CN.html"

        self.assertTrue(
            web_panel.restore_reader_translation_cache(
                restored_document,
                config,
                restored_translation,
            )
        )
        self.assertIn("reader-aligned-1", restored_source.read_text(encoding="utf-8"))
        self.assertIn("reader-aligned-1", restored_translation.read_text(encoding="utf-8"))

    def test_reader_html_translation_preserves_stable_pair_ids(self) -> None:
        root = Path(self.temp_dir.name)
        source = root / "reader.html"
        output = root / "reader_zh-CN.html"
        source.write_text(
            "<html><body><h2>Heading</h2><div>First paragraph.</div><div>Second paragraph.</div></body></html>",
            encoding="utf-8",
        )
        web_panel.annotate_reader_html_pair_ids(source)
        with patch.object(web_panel, "llm_translate", side_effect=["标题", "第一段。", "第二段。"]):
            web_panel.translate_html(None, source, output)

        original = web_panel.BeautifulSoup(source.read_text(encoding="utf-8"), "html.parser")
        translated = web_panel.BeautifulSoup(output.read_text(encoding="utf-8"), "html.parser")
        original_ids = [node.get("data-reader-pair-id") for node in original.body.find_all(True)]
        translated_ids = [node.get("data-reader-pair-id") for node in translated.body.find_all(True)]
        self.assertEqual(translated_ids, original_ids)
        self.assertTrue(all(original_ids))

    def test_reader_html_alignment_extracts_semantic_blocks_and_ignores_page_chrome(self) -> None:
        soup = web_panel.BeautifulSoup(
            """
            <html><body>
              <nav>Navigation links</nav>
              <header><h1 id="paper-title">1 Introduction</h1></header>
              <article>
                <p>Readable paragraph.</p>
                <p hidden>Hidden paragraph.</p>
                <figure><img src="images/architecture.png" alt="Architecture"><figcaption>Figure 1.</figcaption></figure>
              </article>
              <footer>Copyright notice</footer>
            </body></html>
            """,
            "html.parser",
        )

        blocks, tags = web_panel.html_to_reader_alignment_blocks(soup)

        self.assertEqual([block["type"] for block in blocks], ["heading", "paragraph", "figure"])
        self.assertEqual(blocks[0]["htmlId"], "paper-title")
        self.assertIn("![Architecture](images/architecture.png)", blocks[2]["content"])
        self.assertEqual([tag.name for tag in tags.values()], ["h1", "p", "figure"])
        self.assertNotIn("Navigation links", str(blocks))
        self.assertNotIn("Copyright notice", str(blocks))
        self.assertNotIn("Hidden paragraph", str(blocks))

    def test_reader_html_alignment_annotates_one_to_many_dom_groups(self) -> None:
        root = Path(self.temp_dir.name)
        source = root / "source.html"
        translated = root / "translated.html"
        source.write_text(
            """
            <html><body>
              <h2>1 Introduction</h2>
              <p>One source paragraph.</p>
              <figure><img src="images/figure.png"><figcaption>Figure 1.</figcaption></figure>
            </body></html>
            """,
            encoding="utf-8",
        )
        translated.write_text(
            """
            <html><body>
              <h2>1 引言</h2>
              <p>译文段落的前半部分。</p>
              <p>译文段落的后半部分。</p>
              <figure><img src="images/figure.png"><figcaption>图 1。</figcaption></figure>
            </body></html>
            """,
            encoding="utf-8",
        )
        response_content = json.dumps({
            "groups": [{
                "sourceIds": ["S:b2"],
                "translationIds": ["T:b2", "T:b3"],
                "confidence": 0.95,
            }]
        })

        class FakeOpenAI:
            def __init__(self, **_kwargs):
                self.chat = SimpleNamespace(
                    completions=SimpleNamespace(
                        create=lambda **_request: SimpleNamespace(
                            choices=[SimpleNamespace(message=SimpleNamespace(content=response_content))]
                        )
                    )
                )

        job = web_panel.Job(
            "html-alignment",
            web_panel.READER_TOOL,
            root,
            {},
            translation_config={
                "name": "Test",
                "baseUrl": "https://example.test/v1",
                "apiKey": "test-key",
                "model": "test-model",
            },
        )
        with patch.object(web_panel, "OpenAI", FakeOpenAI):
            groups = web_panel.align_reader_html_documents(job, source, translated)

        source_soup = web_panel.BeautifulSoup(source.read_text(encoding="utf-8"), "html.parser")
        translated_soup = web_panel.BeautifulSoup(translated.read_text(encoding="utf-8"), "html.parser")
        source_paragraph_id = source_soup.p["data-reader-pair-id"]
        translated_paragraph_ids = [
            paragraph["data-reader-pair-id"]
            for paragraph in translated_soup.find_all("p")
        ]
        self.assertEqual(len(groups), 3)
        self.assertEqual(translated_paragraph_ids, [source_paragraph_id, source_paragraph_id])
        self.assertEqual(
            source_soup.h2["data-reader-pair-id"],
            translated_soup.h2["data-reader-pair-id"],
        )
        self.assertEqual(
            source_soup.figure["data-reader-pair-id"],
            translated_soup.figure["data-reader-pair-id"],
        )
        self.assertEqual(
            set(source_soup.figure["data-reader-alignment-anchor"].split(",")),
            {"figure-number", "image"},
        )

    def test_html_translation_saves_parts_and_merges_them_at_completion(self) -> None:
        root = Path(self.temp_dir.name)
        source = root / "source.html"
        output = root / "translated.partial.html"
        source.write_text(
            f'<!doctype html><p>{"A" * 6000}{"B" * 20}</p><p>Second section</p>'
            "<script>keep()</script><pre><span>nested keep</span></pre>",
            encoding="utf-8",
        )
        translations = iter(["甲" * 6000, "乙" * 20, "第二部分"])

        with patch.object(web_panel, "llm_translate", side_effect=lambda _job, _text: next(translations)):
            web_panel.translate_html(None, source, output)

        translated = output.read_text(encoding="utf-8")
        self.assertIn("甲" * 6000 + "乙" * 20, translated)
        self.assertIn("第二部分", translated)
        self.assertIn("keep()", translated)
        self.assertIn("nested keep", translated)
        self.assertFalse(web_panel.html_translation_parts_path(output).exists())

    def test_html_translation_flattens_nodes_concurrently_and_preserves_dom_order(self) -> None:
        root = Path(self.temp_dir.name)
        source = root / "parallel.html"
        output = root / "parallel_zh-CN.partial.html"
        source.write_text("<html><body><p>Alpha</p><p>Beta</p><p>Gamma</p></body></html>", encoding="utf-8")
        job = web_panel.Job(
            "html-parallel",
            web_panel.TOOL_BY_ID["document_translate"],
            root,
            {},
            translation_config={
                "name": "Test",
                "baseUrl": "https://llm.example",
                "apiKey": "html-parallel-key",
                "model": "model",
                "concurrency": 3,
            },
        )
        translations = {"Alpha": "甲", "Beta": "乙", "Gamma": "丙"}
        delays = {"Alpha": 0.05, "Beta": 0.02, "Gamma": 0.0}

        def fake_translate(_job: object, value: str) -> str:
            time.sleep(delays[value])
            return translations[value]

        with patch.object(web_panel, "llm_translate", side_effect=fake_translate):
            web_panel.translate_html(job, source, output)

        translated = web_panel.BeautifulSoup(output.read_text(encoding="utf-8"), "html.parser")
        self.assertEqual([node.get_text() for node in translated.find_all("p")], ["甲", "乙", "丙"])
        self.assertEqual(job.translation_progress, {"completed": 3, "total": 3, "active": 0, "concurrency": 3})

    def test_failed_html_translation_keeps_staged_parts_and_previewable_partial_html(self) -> None:
        root = Path(self.temp_dir.name)
        source = root / "source.html"
        output = root / "translated.partial.html"
        source.write_text(f"<p>{'A' * 6000}{'B' * 20}</p><p>Still original</p>", encoding="utf-8")

        with patch.object(
            web_panel,
            "llm_translate",
            side_effect=["甲" * 6000, RuntimeError("provider unavailable")],
        ):
            with self.assertRaisesRegex(RuntimeError, "provider unavailable"):
                web_panel.translate_html(None, source, output)

        partial = output.read_text(encoding="utf-8")
        self.assertIn("甲" * 6000, partial)
        self.assertIn("B" * 20, partial)
        self.assertIn("Still original", partial)
        parts_dir = web_panel.html_translation_parts_path(output)
        self.assertTrue((parts_dir / "000000-000000.txt").exists())
        self.assertEqual(
            (parts_dir / "000000-000000.txt").read_text(encoding="utf-8"),
            "甲" * 6000,
        )

        with patch.object(web_panel, "llm_translate", side_effect=["乙" * 20, "第二部分"]):
            web_panel.translate_html(None, source, output)
        completed = output.read_text(encoding="utf-8")
        self.assertIn("甲" * 6000 + "乙" * 20, completed)
        self.assertIn("第二部分", completed)
        self.assertFalse(parts_dir.exists())

    def test_document_translation_handles_mixed_markdown_and_html_folder_uploads(self) -> None:
        manifest = [
            {"relativePath": "research/paper.mmd", "workName": "paper"},
            {"relativePath": "research/paper.md", "workName": "paper"},
            {"relativePath": "research/site/page.html", "workName": "page"},
        ]
        backend_calls = []

        def fake_backend(job, source_root, output_root, sources):
            backend_calls.append([source.relative_to(source_root).as_posix() for source in sources])
            outputs = []
            for source in sources:
                relative = source.relative_to(source_root)
                output = web_panel.translated_backend_output(output_root, relative)
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(f"ZH:{source.read_text(encoding='utf-8')}", encoding="utf-8")
                outputs.append(output)
            job.translation_progress = {"completed": len(sources), "total": len(sources), "active": 0, "concurrency": 2}
            return outputs

        environment = {
            "LLM_NAME": "Managed",
            "LLM_BASE_URL": "https://managed.example/v1",
            "LLM_API_KEY": "managed-secret",
            "LLM_MODEL": "managed-model",
            "LLM_CONCURRENCY": "2",
        }
        with patch.dict("os.environ", environment, clear=True), patch.object(web_panel, "run_translation_backend", side_effect=fake_backend):
            response = self.client.post(
                "/api/jobs",
                data={
                    "tool": "document_translate",
                    "options": json.dumps({"llm": {"mode": "preset", "presetId": "default"}}),
                    "manifest": json.dumps(manifest),
                    "files": [
                        (io.BytesIO(b"MMD source"), "paper.mmd"),
                        (io.BytesIO(b"Markdown source"), "paper.md"),
                        (io.BytesIO(b"<html><body>HTML source</body></html>"), "page.html"),
                    ],
                },
                content_type="multipart/form-data",
            )
            self.assertEqual(response.status_code, 202, response.get_json())
            job_id = response.get_json()["id"]
            status = self.wait_for_job(job_id)

        self.assertEqual(status["status"], "completed", status["logs"])
        self.assertEqual(
            backend_calls,
            [["research/0002_paper.md", "research/0001_paper.mmd", "research/site/0003_page.html"]],
        )
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
                    "research/site/0003_page_zh-CN.html",
                },
            )
            self.assertIn(b"ZH:Markdown source", archive.read("research/0002_paper_zh-CN.md"))
            self.assertIn(b"ZH:<html>", archive.read("research/site/0003_page_zh-CN.html"))
        download.close()

    def test_translation_backend_receives_credentials_only_over_stdin_and_reports_progress(self) -> None:
        root = Path(self.temp_dir.name) / "backend-adapter"
        source_root = root / "input"
        output_root = root / "output"
        source_root.mkdir(parents=True)
        output_root.mkdir(parents=True)
        source = source_root / "paper.html"
        source.write_text("<p>Source</p>", encoding="utf-8")
        expected = output_root / "paper_zh-CN.html"
        expected.write_text("<p>译文</p>", encoding="utf-8")
        executable = root / "ai-markdown-translator.exe"
        executable.write_bytes(b"test executable")
        job = web_panel.Job(
            "backend-adapter",
            web_panel.TOOL_BY_ID["document_translate"],
            root,
            {},
            translation_config={
                "name": "Managed",
                "baseUrl": "https://llm.example/v1",
                "apiKey": "stdin-only-secret",
                "model": "managed-model",
                "concurrency": 3,
            },
        )

        class RecordingInput:
            def __init__(self):
                self.value = ""

            def write(self, value):
                self.value += value

            def close(self):
                return None

        class OutputLines:
            def __iter__(self):
                return iter([
                    '{"type":"progress","total":4,"completed":2,"active":1,"concurrency":3}\n',
                    '{"type":"log","message":"backend ready"}\n',
                    '{"type":"complete"}\n',
                ])

            def close(self):
                return None

        fake_process = SimpleNamespace(stdin=RecordingInput(), stdout=OutputLines(), wait=lambda: 0)
        with patch.object(web_panel, "translation_backend_executable", return_value=executable), \
             patch.object(web_panel.subprocess, "Popen", return_value=fake_process) as popen:
            outputs = web_panel.run_translation_backend(job, source_root, output_root, [source])

        self.assertEqual(outputs, [expected])
        self.assertEqual(job.translation_progress, {"total": 4, "completed": 2, "active": 1, "concurrency": 3})
        payload = json.loads(fake_process.stdin.value)
        self.assertEqual(payload["llm"]["apiKey"], "stdin-only-secret")
        self.assertEqual(payload["files"], ["paper.html"])
        self.assertIn("HTML tags", payload["llm"]["systemPrompt"])
        self.assertIn("scripts", payload["llm"]["systemPrompt"])
        self.assertEqual(payload["llm"]["maxInputTokens"], 8000)
        command = popen.call_args.args[0]
        self.assertEqual(command, [str(executable)])
        self.assertNotIn("stdin-only-secret", " ".join(command))
        self.assertNotIn("stdin-only-secret", payload["database"])
        self.assertIn("backend ready", "\n".join(job.logs))

    def test_failed_document_translation_can_be_requeued_with_its_saved_progress(self) -> None:
        job = web_panel.Job(
            "document-retry",
            web_panel.TOOL_BY_ID["document_translate"],
            Path(self.temp_dir.name) / "document-retry",
            {},
            status="failed",
            phase="translation_partial",
            translation_progress={"completed": 4, "total": 94},
        )
        with web_panel.job_manager.lock:
            web_panel.job_manager.jobs[job.id] = job
        llm = {"mode": "preset", "presetId": "default"}
        environment = {
            "LLM_NAME": "Retry provider",
            "LLM_BASE_URL": "https://llm.example/v1",
            "LLM_API_KEY": "secret",
            "LLM_MODEL": "retry-model",
        }

        with patch.dict("os.environ", environment, clear=True), patch.object(web_panel.job_manager, "submit") as submit:
            response = self.client.post(f"/api/jobs/{job.id}/translation-retry", json={"llm": llm})

        self.assertEqual(response.status_code, 202, response.get_json())
        self.assertEqual(job.status, "queued")
        self.assertEqual(job.phase, "translation_queued")
        self.assertEqual(job.translation_progress, {"completed": 4, "total": 94})
        self.assertEqual(job.translation_config["model"], "retry-model")
        submit.assert_called_once_with(job)

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
        mmd = output_dir / "paper.mmd"
        mmd.write_text("MMD", encoding="utf-8")
        web_panel.add_artifact(job, mmd, "ocr", "Mathpix Markdown")
        job.status = "completed"
        web_panel.job_manager.jobs[job.id] = job
        with patch.dict("os.environ", {"LLM_BASE_URL": "https://llm.example", "LLM_API_KEY": "key", "LLM_MODEL": "model"}):
            response = self.client.post(f"/api/jobs/{job.id}/translations", json={"artifactId": "paper.docx", "llm": {"mode": "preset", "presetId": "default"}})
        self.assertEqual(response.status_code, 400)
        self.assertTrue(next(item for item in job.artifacts if item["name"] == "paper.mmd")["translationSupported"])

    def test_pdf_translation_translates_html_with_go_backend_and_rejects_duplicate_output(self) -> None:
        job = web_panel.Job("translation-html", web_panel.TOOL_BY_ID["pdf_ocr_translate"], Path(self.temp_dir.name) / "translation-html", {})
        output_dir = job.root / "output"
        output_dir.mkdir(parents=True)
        source = output_dir / "paper.html"
        source.write_text("<html><body><p>Original prose.</p><script>keep()</script></body></html>", encoding="utf-8")
        job.local_save_dir = Path(self.temp_dir.name) / "saved-paper"
        job.local_save_dir.mkdir()
        web_panel.add_artifact(job, source, "ocr", "HTML")
        job.status = "completed"
        web_panel.job_manager.jobs[job.id] = job

        with patch.dict("os.environ", {"LLM_BASE_URL": "https://llm.example", "LLM_API_KEY": "key", "LLM_MODEL": "model"}), \
             patch.object(web_panel, "run_translation_backend") as backend:
            def fake_backend(_job, source_root, target_root, sources):
                self.assertEqual(sources, [source])
                output = target_root / "paper_zh-CN.html"
                output.write_text(sources[0].read_text(encoding="utf-8").replace("Original prose.", "原始正文。"), encoding="utf-8")
                return [output]
            backend.side_effect = fake_backend
            custom_llm = {"mode": "custom", "name": "Temporary", "baseUrl": "https://custom.example/v1", "apiKey": "custom-secret", "model": "custom-model"}
            rejected = self.client.post(f"/api/jobs/{job.id}/translations", json={"artifactId": "paper.html", "llm": custom_llm})
            self.assertEqual(rejected.status_code, 400, rejected.get_json())
            self.assertIn("global LLM manager", rejected.get_json()["error"])
            managed_llm = {"mode": "preset", "presetId": "default"}
            response = self.client.post(f"/api/jobs/{job.id}/translations", json={"artifactId": "paper.html", "llm": managed_llm})
            self.assertEqual(response.status_code, 202, response.get_json())
            status = self.wait_for_job(job.id)
            duplicate = self.client.post(f"/api/jobs/{job.id}/translations", json={"artifactId": "paper.html", "llm": managed_llm})
        self.assertEqual(status["status"], "completed", status["logs"])
        translated = output_dir / "paper_zh-CN.html"
        self.assertIn("原始正文", translated.read_text(encoding="utf-8"))
        self.assertIn("keep()", (job.local_save_dir / "paper_zh-CN.html").read_text(encoding="utf-8"))
        self.assertIn("paper_zh-CN.html", {artifact["name"] for artifact in status["artifacts"]})
        self.assertIsNone(job.translation_config)
        self.assertEqual(duplicate.status_code, 409)

    def test_document_markdown_translation_postprocesses_before_packaging(self) -> None:
        root = Path(self.temp_dir.name) / "document-postprocess"
        input_dir = root / "input"
        input_dir.mkdir(parents=True)
        source = input_dir / "paper.md"
        source.write_text("Original prose.", encoding="utf-8")
        job = web_panel.Job(
            "document-postprocess",
            web_panel.TOOL_BY_ID["document_translate"],
            root,
            {},
            translation_config={"name": "Test", "baseUrl": "https://llm.example", "apiKey": "key", "model": "model"},
        )

        def fake_backend(_job, _source_root, target_root, _sources):
            output = target_root / "paper_zh-CN.md"
            output.write_text("$$E=mc^2$$\n", encoding="utf-8")
            return [output]

        with patch.object(web_panel, "run_translation_backend", side_effect=fake_backend):
            web_panel.run_document_translation(job)

        translated = root / "output" / "paper_zh-CN.md"
        self.assertEqual(translated.read_text(encoding="utf-8"), "$$\nE=mc^2\n$$\n")
        self.assertEqual(job.phase, "translation_complete")
        self.assertEqual(job.download_path, translated)

    def test_pdf_markdown_translation_saves_validated_result_beside_ocr(self) -> None:
        job = web_panel.Job("translation-validated", web_panel.TOOL_BY_ID["pdf_ocr_translate"], Path(self.temp_dir.name) / "translation-validated", {})
        output_dir = job.root / "output"
        output_dir.mkdir(parents=True)
        source = output_dir / "paper.md"
        source.write_text("Original prose.", encoding="utf-8")
        job.local_save_dir = Path(self.temp_dir.name) / "saved-validated-paper"
        job.local_save_dir.mkdir()
        job.pending_artifact_id = source.name
        job.translation_config = {"name": "Test", "baseUrl": "https://llm.example", "apiKey": "key", "model": "model"}
        web_panel.add_artifact(job, source, "ocr", "Markdown")

        def fake_backend(_job, _source_root, target_root, _sources):
            output = target_root / "paper_zh-CN.md"
            output.write_text("$$E=mc^2$$\n", encoding="utf-8")
            return [output]

        with patch.object(web_panel, "run_translation_backend", side_effect=fake_backend):
            web_panel.run_pdf_translation(job)

        translated = output_dir / "paper_zh-CN.md"
        self.assertEqual(translated.read_text(encoding="utf-8"), "$$\nE=mc^2\n$$\n")
        self.assertEqual((job.local_save_dir / translated.name).read_text(encoding="utf-8"), translated.read_text(encoding="utf-8"))
        self.assertIn(translated.name, {item["name"] for item in job.artifacts})

    def test_pdf_markdown_translation_keeps_unfixed_copy_and_allows_retry(self) -> None:
        job = web_panel.Job("translation-unfixed", web_panel.TOOL_BY_ID["pdf_ocr_translate"], Path(self.temp_dir.name) / "translation-unfixed", {})
        output_dir = job.root / "output"
        output_dir.mkdir(parents=True)
        source = output_dir / "paper.md"
        source.write_text("Original prose.", encoding="utf-8")
        job.local_save_dir = Path(self.temp_dir.name) / "saved-unfixed-paper"
        job.local_save_dir.mkdir()
        job.operation = "translate"
        job.pending_artifact_id = source.name
        job.translation_config = {"name": "Test", "baseUrl": "https://llm.example", "apiKey": "key", "model": "model"}
        web_panel.add_artifact(job, source, "ocr", "Markdown")

        def fake_backend(_job, _source_root, target_root, _sources):
            output = target_root / "paper_zh-CN.md"
            output.write_text("$$\nE=mc^2\n# 被吞掉的标题\n", encoding="utf-8")
            return [output]

        with patch.object(web_panel, "run_translation_backend", side_effect=fake_backend):
            with self.assertRaises(web_panel.TranslationMarkdownPostprocessError):
                web_panel.run_pdf_translation(job)

        raw = output_dir / "paper_zh-CN_unfixed.md"
        self.assertTrue(raw.is_file())
        self.assertFalse((output_dir / "paper_zh-CN.md").exists())
        self.assertEqual((job.local_save_dir / raw.name).read_text(encoding="utf-8"), raw.read_text(encoding="utf-8"))
        self.assertIn(raw.name, {item["name"] for item in job.artifacts})
        self.assertEqual(job.phase, "translation_postprocess_failed")
        self.assertIsNotNone(job.download_path)

        job.status = "failed"
        web_panel.job_manager.jobs[job.id] = job
        with patch.dict("os.environ", {"LLM_BASE_URL": "https://llm.example", "LLM_API_KEY": "key", "LLM_MODEL": "model"}):
            retry = self.client.post(f"/api/jobs/{job.id}/translations", json={"artifactId": source.name, "llm": {"mode": "preset", "presetId": "default"}})
        self.assertEqual(retry.status_code, 202, retry.get_json())

    def test_failed_translation_keeps_backend_resume_state_and_progress(self) -> None:
        job = web_panel.Job("translation-partial", web_panel.TOOL_BY_ID["pdf_ocr_translate"], Path(self.temp_dir.name) / "translation-partial", {})
        output_dir = job.root / "output"
        output_dir.mkdir(parents=True)
        source = output_dir / "paper.md"
        source.write_text("A" * 6000 + "\n\n" + "B" * 6000, encoding="utf-8")
        job.local_save_dir = Path(self.temp_dir.name) / "saved-paper"
        job.local_save_dir.mkdir()
        job.operation = "translate"
        job.pending_artifact_id = source.name
        job.translation_config = {"name": "Test", "baseUrl": "https://llm.example", "apiKey": "key", "model": "model"}
        web_panel.add_artifact(job, source, "ocr", "Markdown")

        def fail_backend(backend_job, _source_root, _output_root, _sources):
            backend_job.translation_progress = {"completed": 2, "total": 3, "active": 0, "concurrency": 1}
            (backend_job.root / "translation_backend.sqlite3").write_bytes(b"persisted chunk state")
            raise RuntimeError("provider unavailable")

        with patch.object(web_panel, "run_translation_backend", side_effect=fail_backend):
            web_panel.job_manager.submit(job)
            status = self.wait_for_job(job.id)

        self.assertEqual(status["status"], "failed", status["logs"])
        self.assertEqual(status["phase"], "translation_partial")
        self.assertEqual(status["translationProgress"], {"completed": 2, "total": 3, "active": 0, "concurrency": 1})
        self.assertTrue((job.root / "translation_backend.sqlite3").is_file())
        self.assertFalse((output_dir / "paper_zh-CN.partial.md").exists())
        self.assertIn("persisting 2/3", "\n".join(status["logs"]))

    def test_named_llm_presets_hide_keys_and_custom_config_is_validated(self) -> None:
        environment = {
            "LLM_NAME": "Legacy", "LLM_BASE_URL": "https://legacy.example/v1", "LLM_API_KEY": "legacy-secret", "LLM_MODEL": "legacy-model", "LLM_CONCURRENCY": "6",
            "LLM_PRESETS": "deepseek,company_proxy",
            "LLM_PRESET_DEEPSEEK_NAME": "DeepSeek", "LLM_PRESET_DEEPSEEK_BASE_URL": "https://api.deepseek.com", "LLM_PRESET_DEEPSEEK_API_KEY": "deepseek-secret", "LLM_PRESET_DEEPSEEK_MODEL": "deepseek-chat", "LLM_PRESET_DEEPSEEK_CONCURRENCY": "64",
            "LLM_PRESET_COMPANY_PROXY_NAME": "Company Proxy", "LLM_PRESET_COMPANY_PROXY_BASE_URL": "https://llm.company.example/v1", "LLM_PRESET_COMPANY_PROXY_API_KEY": "company-secret", "LLM_PRESET_COMPANY_PROXY_MODEL": "proxy-model",
        }
        with patch.dict("os.environ", environment, clear=True):
            presets = web_panel.public_llm_presets()
            self.assertEqual([item["id"] for item in presets], ["default", "deepseek", "company_proxy"])
            self.assertNotIn("apiKey", presets[0])
            selected = web_panel.translation_config_from_request({"llm": {"mode": "preset", "presetId": "company_proxy"}})
            custom = web_panel.translation_config_from_request({"llm": {"mode": "custom", "name": "Temporary", "baseUrl": "https://custom.example/v1", "apiKey": "custom-secret", "model": "custom-model", "concurrency": 7}})
        self.assertEqual([item["concurrency"] for item in presets], [6, 64, 1])
        self.assertEqual(selected["apiKey"], "company-secret")
        self.assertEqual(custom["name"], "Temporary")
        self.assertEqual(custom["model"], "custom-model")
        self.assertEqual(custom["concurrency"], 7)

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
            "concurrency": 1,
        }])
        self.assertNotIn("apiKey", pdf_tool["llmPresets"][0])

    def test_github_config_api_persists_without_exposing_token_and_tests_public_branch(self) -> None:
        env_file = Path(self.temp_dir.name) / ".env"
        environment = {
            "GITHUB_TOKEN": "",
            "GITHUB_REPOSITORY": "",
            "GITHUB_BRANCH": "",
            "GITHUB_IMAGE_ROOT": "",
        }
        with patch.object(web_panel, "ENV_FILE", env_file), patch.dict(web_panel.os.environ, environment, clear=False):
            saved = self.client.put("/api/github-config", json={
                "token": "github-secret",
                "repository": "owner/images",
                "branch": "main",
                "imageRoot": "assets/papers",
            })
            self.assertEqual(saved.status_code, 200, saved.get_json())
            self.assertNotIn("token", saved.get_json()["github"])
            edited = self.client.put("/api/github-config", json={
                "token": "",
                "repository": "owner/images",
                "branch": "publish/images",
                "imageRoot": "assets/papers",
            })
            self.assertEqual(edited.status_code, 200, edited.get_json())
            self.assertEqual(web_panel.os.environ["GITHUB_TOKEN"], "github-secret")
            public = self.client.get("/api/github-config").get_json()["github"]
            self.assertEqual(public["repository"], "owner/images")
            self.assertNotIn("github-secret", json.dumps(public))
            self.assertIn("GITHUB_TOKEN='github-secret'", env_file.read_text(encoding="utf-8"))
            responses = [
                {"id": 123, "full_name": "owner/images", "private": False, "visibility": "public"},
                {"name": "publish/images"},
            ]
            with patch.object(web_panel, "github_api_request", side_effect=responses) as github_request:
                tested = self.client.post("/api/github-config/test")
            self.assertEqual(tested.status_code, 200, tested.get_json())
            self.assertEqual(tested.get_json()["result"]["repositoryId"], 123)
            self.assertEqual(github_request.call_count, 2)
            removed = self.client.delete("/api/github-config")
            self.assertEqual(removed.status_code, 200)
            self.assertFalse(self.client.get("/api/github-config").get_json()["github"]["configured"])

    def test_github_connection_rejects_private_repository(self) -> None:
        config = {"token": "secret", "repository": "owner/private", "branch": "main", "imageRoot": "images"}
        with (
            patch.object(web_panel, "github_config", return_value=config),
            patch.object(web_panel, "github_api_request", return_value={"private": True}) as github_request,
        ):
            with self.assertRaisesRegex(RuntimeError, "公开仓库"):
                web_panel.test_github_connection()
        github_request.assert_called_once()

    def test_github_markdown_parser_covers_supported_syntax_and_ignores_code(self) -> None:
        markdown = """![inline](images/a.png)
![nested](images/chart(1).png)
![angle](<images/a b.png> \"title\")
![figure][fig]
[fig]: images/ref.png \"title\"
![[images/wiki.png|caption]]
\\includegraphics[width=1]{images/latex.png}
<img src=\"images/html.png\" srcset=\"images/one.png 1x, images/two.png 2x\">
`![code](images/code.png)`
```md
![fenced](images/fenced.png)
```
![remote](https://example.com/a.png)
![data](data:image/png;base64,abc)
![root](/images/root.png)
"""
        references = web_panel.github_markdown_references(markdown)
        local = [
            path for path in (web_panel.local_markdown_asset_path(item.target) for item in references)
            if path is not None
        ]
        self.assertEqual(local, [
            "images/a.png", "images/chart(1).png", "images/a b.png", "images/ref.png", "images/wiki.png",
            "images/latex.png", "images/html.png", "images/one.png", "images/two.png",
        ])
        self.assertNotIn("images/code.png", [item.target for item in references])
        self.assertNotIn("images/fenced.png", [item.target for item in references])

    def test_github_markdown_publish_uses_dated_sha_paths_and_writes_only_after_success(self) -> None:
        job = web_panel.Job(
            "github-markdown",
            web_panel.TOOL_BY_ID["markdown_github"],
            Path(self.temp_dir.name) / "github-markdown",
            {},
        )
        source = job.root / "input" / "paper" / "论文.md"
        image = job.root / "input" / "paper" / "images" / "图.png"
        source.parent.mkdir(parents=True)
        image.parent.mkdir(parents=True)
        image.write_bytes(b"same image")
        source.write_text(
            "![A](images/%E5%9B%BE.png)\n![Again](images/%E5%9B%BE.png)\n`![No](images/%E5%9B%BE.png)`\n",
            encoding="utf-8",
        )
        publication = {
            "commitSha": "a" * 40,
            "commitUrl": "https://github.com/owner/images/commit/" + "a" * 40,
            "uploadedImageCount": 1,
            "reusedImageCount": 0,
            "commitCreated": True,
        }
        config = {"token": "secret", "repository": "owner/images", "branch": "main", "imageRoot": "assets"}
        with (
            patch.object(web_panel, "github_config", return_value=config),
            patch.object(web_panel, "publish_github_image_blobs", return_value=publication) as publish,
        ):
            web_panel.run_github_markdown_publish(job)
        images = publish.call_args.args[1]
        self.assertEqual(len(images), 1)
        remote_path = next(iter(images))
        self.assertRegex(remote_path, r"^assets/\d{4}/\d{2}/\d{2}/[0-9a-f]{64}\.png$")
        output = job.download_path.read_text(encoding="utf-8")
        raw = f"https://raw.githubusercontent.com/owner/images/{'a' * 40}/{remote_path}"
        self.assertEqual(output.count(raw), 2)
        self.assertIn("`![No](images/%E5%9B%BE.png)`", output)
        self.assertEqual(job.download_name, "论文_github.md")
        self.assertEqual(job.publication["commitSha"], "a" * 40)

    def test_github_publish_validates_missing_images_before_network(self) -> None:
        job = web_panel.Job(
            "github-missing",
            web_panel.TOOL_BY_ID["markdown_github"],
            Path(self.temp_dir.name) / "github-missing",
            {},
        )
        source = job.root / "input" / "paper.md"
        source.parent.mkdir(parents=True)
        source.write_text("![Missing](images/missing.png)\n", encoding="utf-8")
        with patch.object(web_panel, "publish_github_image_blobs") as publish:
            with self.assertRaisesRegex(ValueError, "未随任务提供"):
                web_panel.run_github_markdown_publish(job)
        publish.assert_not_called()
        self.assertIsNone(job.download_path)

    def test_github_git_data_publish_updates_branch_once_without_force(self) -> None:
        config = {"token": "secret", "repository": "owner/images", "branch": "feature/images", "imageRoot": "assets"}
        responses = [
            {"private": False},
            {"object": {"sha": "head-sha"}},
            {"tree": {"sha": "base-tree"}},
            {"tree": [], "truncated": False},
            {"sha": "blob-sha"},
            {"sha": "new-tree"},
            {"sha": "new-commit"},
            {"ref": "refs/heads/feature/images"},
        ]
        with patch.object(web_panel, "github_api_request", side_effect=responses) as github_request:
            result = web_panel.publish_github_image_blobs(config, {"assets/2026/08/02/hash.png": b"image"}, "paper.md")
        self.assertEqual(result["commitSha"], "new-commit")
        self.assertEqual(result["uploadedImageCount"], 1)
        self.assertEqual(result["reusedImageCount"], 0)
        self.assertEqual(github_request.call_count, 8)
        patch_call = github_request.call_args_list[-1]
        self.assertEqual(patch_call.args[0], "PATCH")
        self.assertIn("feature%2Fimages", patch_call.args[1])
        self.assertEqual(patch_call.kwargs["json_body"], {"sha": "new-commit", "force": False})

    def test_github_git_data_publish_reuses_existing_blob_without_empty_commit(self) -> None:
        config = {"token": "secret", "repository": "owner/images", "branch": "main", "imageRoot": "assets"}
        path = "assets/2026/08/02/hash.png"
        responses = [
            {"private": False},
            {"object": {"sha": "head-sha"}},
            {"tree": {"sha": "base-tree"}},
            {"tree": [{"path": path, "type": "blob", "sha": "blob-sha"}], "truncated": False},
            {"sha": "blob-sha"},
        ]
        with patch.object(web_panel, "github_api_request", side_effect=responses) as github_request:
            result = web_panel.publish_github_image_blobs(config, {path: b"same"}, "paper.md")
        self.assertEqual(github_request.call_count, 5)
        self.assertFalse(result["commitCreated"])
        self.assertEqual(result["uploadedImageCount"], 0)
        self.assertEqual(result["reusedImageCount"], 1)

    def test_github_job_requires_exactly_one_markdown_document(self) -> None:
        environment = {
            "GITHUB_TOKEN": "secret",
            "GITHUB_REPOSITORY": "owner/images",
            "GITHUB_BRANCH": "main",
            "GITHUB_IMAGE_ROOT": "assets",
        }
        manifest = [
            {"relativePath": "one.md", "workName": "one"},
            {"relativePath": "two.mmd", "workName": "two"},
        ]
        with patch.dict(web_panel.os.environ, environment, clear=False):
            response = self.client.post(
                "/api/jobs",
                data={
                    "tool": "markdown_github",
                    "options": "{}",
                    "manifest": json.dumps(manifest),
                    "files": [(io.BytesIO(b"![A](a.png)"), "one.md"), (io.BytesIO(b"text"), "two.mmd")],
                },
                content_type="multipart/form-data",
            )
        self.assertEqual(response.status_code, 400)
        self.assertIn("恰好", response.get_json()["error"])


if __name__ == "__main__":
    unittest.main()
