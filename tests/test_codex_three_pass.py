"""Tests for the local Codex Three-Pass runtime without using real quota."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from codex_three_pass import (
    AnalysisSession,
    ApiThreePassAnalysisManager,
    CodexAppServerClient,
    CodexProtocolError,
    CodexTextRunner,
    DirectApiExecutor,
    ThreePassAnalysisManager,
    build_chunk_plan,
    default_phase_lengths,
    infer_api_protocol,
    normalize_api_base_url,
    phase_length_rule,
    sse_events,
    validate_phase_lengths,
)
import web_panel


class FakeRuntime:
    def __init__(self, *, authenticated: bool = True) -> None:
        self.authenticated = authenticated
        self.listeners = []
        self.turn_count = 0
        self.interrupted = []

    def add_listener(self, listener):
        self.listeners.append(listener)

    def status(self):
        return {
            "available": True,
            "authMode": "chatgpt" if self.authenticated else "apiKey",
            "chatgptAuthenticated": self.authenticated,
            "planType": "plus" if self.authenticated else None,
            "models": [{
                "id": "test-model",
                "displayName": "Test Model",
                "isDefault": True,
                "defaultEffort": "high",
                "supportedEfforts": ["medium", "high"],
            }],
            "defaultModel": "test-model",
            "rateLimits": None,
        }

    def request(self, method, params=None, timeout=30):
        params = params or {}
        if method == "thread/start":
            return {"thread": {"id": "thread-test"}}
        if method == "thread/resume":
            return {"thread": {"id": params["threadId"]}}
        if method == "turn/interrupt":
            self.interrupted.append(params["turnId"])
            return {}
        if method != "turn/start":
            raise AssertionError(f"Unexpected fake method: {method}")
        self.turn_count += 1
        turn_id = f"turn-{self.turn_count}"
        if params.get("outputSchema"):
            text = json.dumps({"markdown": f"# Phase {self.turn_count}\n\nEvidence-grounded result."})
        else:
            text = "这是基于论文与报告的后续回答。"
        for listener in self.listeners:
            listener({
                "method": "item/agentMessage/delta",
                "params": {
                    "threadId": params["threadId"],
                    "turnId": turn_id,
                    "itemId": f"item-{self.turn_count}",
                    "delta": text[:12],
                },
            })
            listener({
                "method": "item/completed",
                "params": {
                    "threadId": params["threadId"],
                    "turnId": turn_id,
                    "completedAtMs": int(time.time() * 1000),
                    "item": {"type": "agentMessage", "id": f"item-{self.turn_count}", "text": text},
                },
            })
            listener({
                "method": "turn/completed",
                "params": {
                    "threadId": params["threadId"],
                    "turn": {"id": turn_id, "status": "completed", "items": [], "error": None},
                },
            })
        return {"turn": {"id": turn_id, "status": "inProgress", "items": [], "error": None}}

    def start_login(self, flow):
        return {"loginId": "login-test", "flow": flow, "status": "pending", "authUrl": "https://example.test/login"}

    def login_status(self, login_id):
        return {"loginId": login_id, "status": "completed", "success": True}

    def logout(self):
        self.authenticated = False


class CrashingRuntime(FakeRuntime):
    def request(self, method, params=None, timeout=30):
        if method != "turn/start":
            return super().request(method, params, timeout)
        self.turn_count += 1
        turn_id = f"turn-{self.turn_count}"

        def crash():
            for listener in self.listeners:
                listener({"method": "paper_lens/runtime_stopped", "params": {"error": "synthetic crash"}})

        threading.Timer(0.02, crash).start()
        return {"turn": {"id": turn_id, "status": "inProgress", "items": [], "error": None}}


class InterruptibleRuntime(FakeRuntime):
    def request(self, method, params=None, timeout=30):
        params = params or {}
        if method == "turn/start":
            self.turn_count += 1
            turn_id = f"turn-{self.turn_count}"
            return {"turn": {"id": turn_id, "status": "inProgress", "items": [], "error": None}}
        if method == "turn/interrupt":
            self.interrupted.append(params["turnId"])
            for listener in self.listeners:
                listener({
                    "method": "turn/completed",
                    "params": {
                        "threadId": params["threadId"],
                        "turn": {"id": params["turnId"], "status": "interrupted", "items": [], "error": None},
                    },
                })
            return {}
        return super().request(method, params, timeout)


class FollowupInterruptibleRuntime(FakeRuntime):
    def request(self, method, params=None, timeout=30):
        params = params or {}
        if method == "turn/start" and self.turn_count >= 4:
            self.turn_count += 1
            return {"turn": {"id": f"turn-{self.turn_count}", "status": "inProgress", "items": [], "error": None}}
        if method == "turn/interrupt":
            self.interrupted.append(params["turnId"])
            for listener in self.listeners:
                listener({
                    "method": "turn/completed",
                    "params": {
                        "threadId": params["threadId"],
                        "turn": {"id": params["turnId"], "status": "interrupted", "items": [], "error": None},
                    },
                })
            return {}
        return super().request(method, params, timeout)


class FakeDirectClient:
    def __init__(self, fail_optional=False):
        self.calls = []
        self.fail_optional = fail_optional
        self.responses = SimpleNamespace(create=self.create_response)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create_chat))

    def create_response(self, **request):
        self.calls.append(("responses", request))
        if self.fail_optional and len(self.calls) == 1:
            raise RuntimeError("Unsupported parameter: text.format")
        return SimpleNamespace(id=f"resp-{len(self.calls)}", output_text=json.dumps({"markdown": f"# API result {len(self.calls)}"}))

    def create_chat(self, **request):
        self.calls.append(("chat_completions", request))
        content = json.dumps({"markdown": f"# Chat result {len(self.calls)}"})
        return SimpleNamespace(id=f"chat-{len(self.calls)}", choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


def wait_for(predicate, timeout=4):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("Timed out waiting for background Three-Pass work")


class ThreePassManagerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.skill = root / "SKILL.md"
        self.skill.write_text("---\nname: three-pass-paper-reader\ndescription: test\n---\n", encoding="utf-8")
        self.runtime = FakeRuntime()
        self.manager = ThreePassAnalysisManager(root, self.skill, self.runtime)
        self.analyses = root / "analyses"

    def tearDown(self):
        self.temp.cleanup()

    def create_session(self):
        return self.manager.create(
            root=self.analyses,
            document_id="document-1",
            document_cache_key="a" * 64,
            document_title="Test Paper",
            model="test-model",
            effort="high",
            language="zh-CN",
            focuses=["方法", "实验", "复现性"],
            custom_focus="检查消融实验",
            input_markdown="# Test Paper\n\n" + ("Evidence paragraph. " * 40),
        )

    def test_four_phases_persist_report_and_thread(self):
        session = self.create_session()
        wait_for(lambda: session.status == "ready")
        self.assertEqual(self.runtime.turn_count, 4)
        self.assertEqual(session.thread_id, "thread-test")
        self.assertTrue(session.report_path.is_file())
        self.assertEqual(set(session.results), {"pass1", "pass2", "pass3", "synthesis"})
        persisted = json.loads(session.metadata_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["status"], "ready")
        self.assertEqual(persisted["threadId"], "thread-test")
        self.assertIn("approximately 250 visible Chinese characters", self.manager._phase_prompt(session, "pass1"))
        self.assertIn("approximately 600 visible Chinese characters", self.manager._phase_prompt(session, "pass3"))
        self.assertEqual(session.options["phaseLengths"], default_phase_lengths())

    def test_custom_phase_lengths_are_frozen_and_language_aware(self):
        lengths = default_phase_lengths()
        lengths["pass3"] = {"level": "custom", "target": 1600}
        session = self.manager.create(
            root=self.analyses,
            document_id="document-custom",
            document_cache_key="b" * 64,
            document_title="Custom Paper",
            model="test-model",
            effort="high",
            language="en-US",
            focuses=["方法"],
            custom_focus="",
            input_markdown="# Paper\n\nEvidence.",
            phase_lengths=lengths,
        )
        lengths["pass3"]["target"] = 9999
        self.assertEqual(session.options["phaseLengths"]["pass3"]["target"], 1600)
        self.assertIn("approximately 1600 visible English words", phase_length_rule(session, "pass3"))
        wait_for(lambda: session.status == "ready")

    def test_phase_length_validation_resolves_presets_and_rejects_invalid_values(self):
        configured = default_phase_lengths()
        configured["pass1"] = {"level": "low", "target": 9999}
        configured["synthesis"] = {"level": "custom", "target": 2400}
        resolved = validate_phase_lengths(configured)
        self.assertEqual(resolved["pass1"]["target"], 150)
        self.assertEqual(resolved["synthesis"]["target"], 2400)
        with self.assertRaises(ValueError):
            validate_phase_lengths({"pass1": {"level": "medium"}})
        configured["synthesis"] = {"level": "custom", "target": 99}
        with self.assertRaises(ValueError):
            validate_phase_lengths(configured)
        configured["synthesis"] = {"level": "custom", "target": 100.5}
        with self.assertRaises(ValueError):
            validate_phase_lengths(configured)

    def test_text_runner_returns_isolated_codex_turn(self):
        runner = CodexTextRunner(Path(self.temp.name), self.runtime)
        result = runner.run(
            [{"role": "system", "content": "Return JSON."}, {"role": "user", "content": "Analyze evidence."}],
            "test-model",
            "high",
        )
        self.assertEqual(result, "这是基于论文与报告的后续回答。")
        self.assertEqual(runner.completed_turns, {})
        self.assertEqual(runner.turn_messages, {})

    def test_followup_resumes_thread_and_is_persisted(self):
        session = self.create_session()
        wait_for(lambda: session.status == "ready")
        self.manager.ask(session, "复现风险是什么？")
        wait_for(lambda: len(session.messages) == 1 and session.status == "ready")
        self.assertEqual(self.runtime.turn_count, 5)
        self.assertIn("Phase 5", session.messages[0]["answer"])
        self.assertTrue((session.root / "messages.jsonl").is_file())

    def test_api_key_authentication_fails_before_starting_thread(self):
        runtime = FakeRuntime(authenticated=False)
        manager = ThreePassAnalysisManager(Path(self.temp.name), self.skill, runtime)
        session = manager.create(
            root=self.analyses / "blocked",
            document_id="document-2",
            document_cache_key="b" * 64,
            document_title="Blocked Paper",
            model="test-model",
            effort="high",
            language="zh-CN",
            focuses=["方法"],
            custom_focus="",
            input_markdown="Readable paper " * 40,
        )
        wait_for(lambda: session.status == "failed")
        self.assertIn("ChatGPT", session.error)
        self.assertEqual(runtime.turn_count, 0)

    def test_sse_history_contains_phase_and_completion_events(self):
        session = self.create_session()
        wait_for(lambda: session.status == "ready")
        event_types = [item["event"] for item in session.events]
        self.assertIn("phase", event_types)
        self.assertEqual(event_types.count("phase_completed"), 4)
        self.assertIn("completed", event_types)

    def test_runtime_crash_fails_current_turn_without_replay(self):
        runtime = CrashingRuntime()
        manager = ThreePassAnalysisManager(Path(self.temp.name), self.skill, runtime)
        session = manager.create(
            root=self.analyses / "crash",
            document_id="document-crash",
            document_cache_key="d" * 64,
            document_title="Crash Paper",
            model="test-model",
            effort="high",
            language="zh-CN",
            focuses=["方法"],
            custom_focus="",
            input_markdown="Readable paper " * 40,
        )
        wait_for(lambda: session.status == "failed")
        self.assertEqual(runtime.turn_count, 1)
        self.assertIn("synthetic crash", session.error)

    def test_active_turn_can_be_interrupted(self):
        runtime = InterruptibleRuntime()
        manager = ThreePassAnalysisManager(Path(self.temp.name), self.skill, runtime)
        session = manager.create(
            root=self.analyses / "cancel",
            document_id="document-cancel",
            document_cache_key="e" * 64,
            document_title="Cancel Paper",
            model="test-model",
            effort="high",
            language="zh-CN",
            focuses=["实验"],
            custom_focus="",
            input_markdown="Readable paper " * 40,
        )
        wait_for(lambda: session.active_turn_id)
        manager.cancel(session)
        wait_for(lambda: session.status == "cancelled")
        self.assertEqual(runtime.interrupted, ["turn-1"])

    def test_cancelled_followup_returns_completed_analysis_to_ready(self):
        runtime = FollowupInterruptibleRuntime()
        manager = ThreePassAnalysisManager(Path(self.temp.name), self.skill, runtime)
        session = manager.create(
            root=self.analyses / "followup-cancel",
            document_id="document-followup-cancel",
            document_cache_key="9" * 64,
            document_title="Follow-up Cancel Paper",
            model="test-model",
            effort="high",
            language="zh-CN",
            focuses=["实验"],
            custom_focus="",
            input_markdown="Readable paper " * 40,
        )
        wait_for(lambda: session.status == "ready")
        manager.ask(session, "Can this follow-up be cancelled?")
        wait_for(lambda: session.active_turn_id == "turn-5")
        manager.cancel(session)
        wait_for(lambda: session.status == "ready" and not session.busy)
        self.assertEqual(runtime.interrupted, ["turn-5"])
        self.assertEqual(session.messages, [])

    def test_stale_sse_cursor_receives_full_snapshot(self):
        session = AnalysisSession(
            id="f" * 32,
            root=self.analyses / "stale",
            document_id="document-stale",
            document_cache_key="f" * 64,
            document_title="Stale Event Paper",
            model="test-model",
            effort="high",
            language="zh-CN",
            focuses=["方法"],
            custom_focus="",
        )
        for index in range(605):
            session.publish("delta", {"delta": str(index)})
        first = next(sse_events(session, after=1))
        self.assertIn("event: snapshot", first)
        self.assertIn(f"id: {session.event_sequence}", first)
        restarted = AnalysisSession(
            id="0" * 32,
            root=self.analyses / "restarted",
            document_id="document-restarted",
            document_cache_key="0" * 64,
            document_title="Restarted Event Paper",
            model="test-model",
            effort="high",
            language="zh-CN",
            focuses=["方法"],
            custom_focus="",
        )
        self.assertIn("event: snapshot", next(sse_events(restarted, after=605)))


class DirectApiThreePassTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.skill = self.root / "SKILL.md"
        self.skill.write_text("---\nname: three-pass-paper-reader\ndescription: test\n---\nEvidence only.", encoding="utf-8")
        self.provider = {
            "id": "openai",
            "name": "OpenAI",
            "baseUrl": "https://api.openai.com/v1/chat/completions",
            "apiKey": "secret-never-persist",
            "model": "test-model",
            "concurrency": 2,
            "protocol": "auto",
            "contextWindow": 128000,
        }
        self.client = FakeDirectClient()
        self.manager = ApiThreePassAnalysisManager(
            self.root,
            self.skill,
            lambda preset_id: self.provider if preset_id == "openai" else None,
            DirectApiExecutor(client_factory=lambda _config: self.client),
            max_workers=2,
        )

    def tearDown(self):
        self.manager.pool.shutdown(wait=True, cancel_futures=True)
        self.temp.cleanup()

    def create_session(self, protocol="responses"):
        source = "# Paper\n\n## [Page 1]\n\n" + ("Evidence paragraph. " * 80)
        plan, _chunks = build_chunk_plan(source, 128000, 4000)
        return self.manager.create(
            root=self.root / "analyses",
            document_id="api-document",
            document_cache_key="a" * 64,
            document_title="API Paper",
            provider=self.provider,
            protocol=protocol,
            model="test-model",
            effort="high",
            language="zh-CN",
            focuses=["方法", "实验"],
            custom_focus="检查消融",
            input_markdown=source,
            options={"reasoningEffort": "high", "maxOutputTokens": 4000, "temperature": None},
            chunk_plan=plan,
        )

    def test_responses_pipeline_is_stateless_and_never_persists_key(self):
        session = self.create_session()
        wait_for(lambda: session.status == "ready")
        self.assertEqual(len(self.client.calls), 4)
        self.assertTrue(all(kind == "responses" for kind, _request in self.client.calls))
        self.assertTrue(all(request["store"] is False for _kind, request in self.client.calls))
        metadata = session.metadata_path.read_text(encoding="utf-8")
        self.assertNotIn("secret-never-persist", metadata)
        self.assertEqual(json.loads(metadata)["backend"], "api")

    def test_chat_completions_pipeline_and_followup(self):
        session = self.create_session("chat_completions")
        wait_for(lambda: session.status == "ready")
        self.assertTrue(all(kind == "chat_completions" for kind, _request in self.client.calls))
        self.manager.ask(session, "主要复现风险是什么？")
        wait_for(lambda: len(session.messages) == 1)
        self.assertIn("Chat result", session.messages[0]["answer"])

    def test_optional_parameter_retry_happens_once(self):
        client = FakeDirectClient(fail_optional=True)
        executor = DirectApiExecutor(client_factory=lambda _config: client)
        markdown, warnings, effective, _response_id = executor.complete(
            self.provider,
            protocol="responses",
            model="test-model",
            instructions="Return JSON",
            prompt="paper",
            options={"reasoningEffort": "high", "maxOutputTokens": 4000, "temperature": 0.2},
        )
        self.assertEqual(markdown, "# API result 2")
        self.assertEqual(len(client.calls), 2)
        self.assertTrue(warnings)
        self.assertIsNone(effective["temperature"])

    def test_protocol_url_and_long_document_planning(self):
        self.assertEqual(infer_api_protocol("https://api.openai.com/v1", "auto"), "responses")
        self.assertEqual(infer_api_protocol("https://api.deepseek.com", "auto"), "chat_completions")
        self.assertEqual(normalize_api_base_url("https://api.openai.com/v1/chat/completions"), "https://api.openai.com/v1")
        plan, chunks = build_chunk_plan("# Paper\n\n" + ("long evidence " * 8000), 8000, 1000)
        self.assertGreater(plan["chunkCount"], 1)
        self.assertTrue(plan["requiresConfirmation"])
        self.assertGreater(plan["estimatedCalls"], 4)
        self.assertEqual(plan["chunkCount"], len(chunks))

    def test_chunk_prompts_use_compact_internal_notes_and_merge_uses_visible_target(self):
        session = self.create_session()
        prompt = self.manager._phase_prompt(
            session,
            "pass2",
            "source",
            "previous",
            "1/2",
            intermediate=True,
        )
        self.assertIn("internal chunk note", prompt)
        self.assertNotIn("approximately 450 visible", prompt)
        self.assertIn("approximately 450 visible Chinese characters", phase_length_rule(session, "pass2"))


class CodexClientEnvironmentTest(unittest.TestCase):
    def test_platform_api_keys_are_removed_only_from_child_environment(self):
        client = CodexAppServerClient(Path.cwd())
        with patch.dict(os.environ, {"OPENAI_API_KEY": "platform", "CODEX_API_KEY": "codex", "KEEP_ME": "yes"}):
            child = client._child_environment()
            self.assertNotIn("OPENAI_API_KEY", child)
            self.assertNotIn("CODEX_API_KEY", child)
            self.assertEqual(child["KEEP_ME"], "yes")
            self.assertEqual(os.environ["OPENAI_API_KEY"], "platform")


class CodexClientProtocolTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.server = root / "fake_codex"
        self.server.write_text(
            """#!/usr/bin/env python3
import json
import os
import sys
import threading
import time

write_lock = threading.Lock()

def send(payload):
    with write_lock:
        print(json.dumps(payload), flush=True)

print("this is deliberately invalid json", flush=True)

def respond(message):
    method = message.get("method")
    if method == "exit":
        os._exit(17)
    if method == "account/login/start":
        login_type = (message.get("params") or {}).get("type")
        login_id = "login-browser" if login_type == "chatgpt" else "login-device"
        result = {"loginId": login_id}
        if login_type == "chatgpt":
            result.update({"type": "chatgpt", "authUrl": "https://example.test/login"})
        else:
            result.update({"type": "chatgptDeviceCode", "verificationUrl": "https://example.test/device", "userCode": "ABCD-1234"})
        send({"id": message.get("id"), "result": result})
        time.sleep(0.02)
        send({"method": "account/login/completed", "params": {"loginId": login_id, "success": True, "error": None}})
        return
    if method == "account/logout":
        send({"id": message.get("id"), "result": {}})
        return
    delay = float((message.get("params") or {}).get("delay", 0))
    time.sleep(delay)
    send({"method": "fake/event", "params": {"requestId": message.get("id")}})
    send({"id": message.get("id"), "result": {"echo": (message.get("params") or {}).get("value")}})

for line in sys.stdin:
    message = json.loads(line)
    if "id" not in message:
        continue
    if message.get("method") == "initialize":
        send({"id": message["id"], "result": {"serverInfo": {"name": "fake"}}})
    else:
        threading.Thread(target=respond, args=(message,), daemon=True).start()
""",
            encoding="utf-8",
        )
        self.server.chmod(0o755)
        self.client = CodexAppServerClient(root, str(self.server))

    def tearDown(self):
        self.client.stop()
        self.temp.cleanup()

    def test_initialization_request_correlation_notifications_and_invalid_json(self):
        notifications = []
        self.client.add_listener(notifications.append)
        results = {}

        def request(name, delay):
            results[name] = self.client.request("echo", {"value": name, "delay": delay}, timeout=3)["echo"]

        slow = threading.Thread(target=request, args=("slow", 0.08))
        fast = threading.Thread(target=request, args=("fast", 0.0))
        slow.start()
        time.sleep(0.01)
        fast.start()
        slow.join()
        fast.join()
        self.assertEqual(results, {"fast": "fast", "slow": "slow"})
        self.assertEqual(sum(item.get("method") == "fake/event" for item in notifications), 2)
        self.assertTrue(any("Invalid JSON" in line for line in self.client.stderr_lines))

    def test_process_exit_fails_request_and_next_request_restarts_server(self):
        self.client.ensure_started()
        generation = self.client.generation
        with self.assertRaises(CodexProtocolError):
            self.client.request("exit", timeout=3)
        result = self.client.request("echo", {"value": "restarted"}, timeout=3)
        self.assertEqual(result["echo"], "restarted")
        self.assertGreater(self.client.generation, generation)

    def test_browser_and_device_login_protocol_state(self):
        browser = self.client.start_login("browser")
        self.assertEqual(browser["authUrl"], "https://example.test/login")
        time.sleep(0.05)
        self.assertTrue(self.client.login_status(browser["loginId"])["success"])
        device = self.client.start_login("device_code")
        self.assertEqual(device["userCode"], "ABCD-1234")
        self.client.logout()


class ThreePassApiTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.previous_jobs = web_panel.JOBS_DIR
        self.previous_cache = web_panel.READER_CACHE_DIR
        self.previous_manager = web_panel.three_pass_manager
        self.previous_api_manager = web_panel.api_three_pass_manager
        web_panel.JOBS_DIR = root / "jobs"
        web_panel.READER_CACHE_DIR = root / "reader-cache"
        web_panel.JOBS_DIR.mkdir()
        runtime = FakeRuntime()
        web_panel.three_pass_manager = ThreePassAnalysisManager(
            web_panel.PROJECT_ROOT,
            web_panel.THREE_PASS_SKILL_PATH,
            runtime,
        )
        web_panel.api_three_pass_manager = None
        with web_panel.reader_manager.lock:
            web_panel.reader_manager.documents.clear()
        document_root = web_panel.JOBS_DIR / "reader-api-document"
        (document_root / "input").mkdir(parents=True)
        (document_root / "input" / "paper.md").write_text(
            "# Paper\n\n" + ("This is source evidence for the method and experiments. " * 30),
            encoding="utf-8",
        )
        self.document = web_panel.ReaderDocument(
            id="api-document",
            root=document_root,
            title="API Paper",
            source_type="markdown",
            mode="markdown",
            render_kind="markdown",
            cache_key="c" * 64,
            source_filename="paper.md",
            status="ready",
        )
        web_panel.reader_manager.add(self.document)
        web_panel.app.config.update(TESTING=True)
        self.client = web_panel.app.test_client()

    def tearDown(self):
        web_panel.JOBS_DIR = self.previous_jobs
        web_panel.READER_CACHE_DIR = self.previous_cache
        web_panel.three_pass_manager = self.previous_manager
        if web_panel.api_three_pass_manager is not None:
            web_panel.api_three_pass_manager.pool.shutdown(wait=True, cancel_futures=True)
        web_panel.api_three_pass_manager = self.previous_api_manager
        with web_panel.reader_manager.lock:
            web_panel.reader_manager.documents.clear()
        self.temp.cleanup()

    def test_create_list_followup_and_download(self):
        status = self.client.get("/api/codex/status")
        self.assertEqual(status.status_code, 200)
        self.assertTrue(status.get_json()["chatgptAuthenticated"])
        created = self.client.post(
            "/api/reader/documents/api-document/analyses",
            json={
                "model": "test-model",
                "effort": "high",
                "language": "zh-CN",
                "focuses": ["method", "experiments", "reproducibility"],
                "customFocus": "Inspect ablations",
            },
        )
        self.assertEqual(created.status_code, 202)
        analysis_id = created.get_json()["id"]
        wait_for(lambda: web_panel.three_pass_manager.get(analysis_id).status == "ready")
        listing = self.client.get("/api/reader/documents/api-document/analyses")
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(listing.get_json()["analyses"][0]["id"], analysis_id)
        report = self.client.get(f"/api/reader/analyses/{analysis_id}/report")
        self.assertEqual(report.status_code, 200)
        self.assertIn("Phase 4", report.get_data(as_text=True))
        report.close()
        followup = self.client.post(
            f"/api/reader/analyses/{analysis_id}/messages",
            json={"question": "What is the main reproducibility risk?"},
        )
        self.assertEqual(followup.status_code, 202)
        wait_for(lambda: len(web_panel.three_pass_manager.get(analysis_id).messages) == 1)

    def test_direct_api_backend_runs_without_exposing_provider_key(self):
        provider = {
            "id": "api-test",
            "name": "API Test",
            "baseUrl": "https://api.openai.com/v1",
            "apiKey": "private-api-key",
            "model": "api-model",
            "concurrency": 2,
            "protocol": "responses",
            "contextWindow": 128000,
        }
        direct_client = FakeDirectClient()
        manager = ApiThreePassAnalysisManager(
            web_panel.PROJECT_ROOT,
            web_panel.THREE_PASS_SKILL_PATH,
            lambda preset_id: provider if preset_id == provider["id"] else None,
            DirectApiExecutor(client_factory=lambda _config: direct_client),
        )
        web_panel.api_three_pass_manager = manager
        with patch.object(web_panel, "direct_api_provider", side_effect=lambda preset_id: provider if preset_id == provider["id"] else None):
            created = self.client.post(
                "/api/reader/documents/api-document/analyses",
                json={
                    "backend": "api",
                    "api": {
                        "presetId": "api-test",
                        "reasoningEffort": "high",
                        "maxOutputTokens": 4000,
                        "confirmedApiBilling": True,
                        "confirmedChunkedCalls": False,
                    },
                    "language": "zh-CN",
                    "focuses": ["method", "experiments"],
                },
            )
        self.assertEqual(created.status_code, 202, created.get_json())
        analysis_id = created.get_json()["id"]
        wait_for(lambda: manager.get(analysis_id).status == "ready")
        snapshot = self.client.get(f"/api/reader/analyses/{analysis_id}").get_json()
        self.assertEqual(snapshot["backend"], "api")
        self.assertEqual(snapshot["protocol"], "responses")
        self.assertNotIn("private-api-key", json.dumps(snapshot))

    def test_codex_login_device_status_and_logout_endpoints(self):
        started = self.client.post("/api/codex/login", json={"flow": "device_code"})
        self.assertEqual(started.status_code, 202)
        login_id = started.get_json()["loginId"]
        progress = self.client.get(f"/api/codex/login/{login_id}")
        self.assertTrue(progress.get_json()["success"])
        logged_out = self.client.post("/api/codex/logout")
        self.assertEqual(logged_out.status_code, 200)

    def test_codex_defaults_are_validated_persisted_and_returned_to_reader(self):
        env_file = Path(self.temp.name) / "codex-settings.env"
        with (
            patch.object(web_panel, "ENV_FILE", env_file),
            patch.dict(os.environ, {"CODEX_DEFAULT_MODEL": "", "CODEX_REASONING_EFFORT": ""}, clear=False),
        ):
            initial = self.client.get("/api/codex/status")
            self.assertEqual(initial.status_code, 200)
            self.assertEqual(initial.get_json()["defaultModel"], "test-model")
            self.assertEqual(initial.get_json()["defaultReasoningEffort"], "high")

            saved = self.client.put(
                "/api/codex-config",
                json={"model": "test-model", "reasoningEffort": "medium"},
            )
            self.assertEqual(saved.status_code, 200, saved.get_json())
            self.assertEqual(saved.get_json()["codex"]["defaultReasoningEffort"], "medium")
            persisted = env_file.read_text(encoding="utf-8")
            self.assertIn("CODEX_DEFAULT_MODEL='test-model'", persisted)
            self.assertIn("CODEX_REASONING_EFFORT=medium", persisted)

            providers = self.client.get("/api/reader/analysis-providers").get_json()
            self.assertEqual(providers["codex"]["defaultReasoningEffort"], "medium")
            invalid = self.client.put(
                "/api/codex-config",
                json={"model": "test-model", "reasoningEffort": "low"},
            )
            self.assertEqual(invalid.status_code, 400)

    def test_gpt_5_6_sol_exposes_and_persists_instant_as_none(self):
        env_file = Path(self.temp.name) / "codex-instant.env"
        sol_status = {
            "available": True,
            "authMode": "chatgpt",
            "chatgptAuthenticated": True,
            "planType": "pro",
            "models": [{
                "id": "gpt-5.6-sol",
                "displayName": "GPT-5.6-Sol",
                "isDefault": True,
                "defaultEffort": "low",
                "supportedEfforts": ["low", "medium", "high", "xhigh", "max", "ultra"],
            }],
            "defaultModel": "gpt-5.6-sol",
            "rateLimits": None,
        }
        with (
            patch.object(web_panel, "ENV_FILE", env_file),
            patch.object(web_panel.three_pass_manager.runtime, "status", return_value=sol_status),
            patch.dict(os.environ, {"CODEX_DEFAULT_MODEL": "", "CODEX_REASONING_EFFORT": ""}, clear=False),
        ):
            status = self.client.get("/api/codex/status").get_json()
            self.assertEqual(status["models"][0]["supportedEfforts"][0], "none")
            saved = self.client.put(
                "/api/codex-config",
                json={"model": "gpt-5.6-sol", "reasoningEffort": "none"},
            )
            self.assertEqual(saved.status_code, 200, saved.get_json())
            self.assertEqual(saved.get_json()["codex"]["defaultReasoningEffort"], "none")
            self.assertIn("CODEX_REASONING_EFFORT=none", env_file.read_text(encoding="utf-8"))

    def test_three_pass_lengths_are_validated_persisted_and_exposed_to_reader(self):
        env_file = Path(self.temp.name) / "three-pass-settings.env"
        environment_key = web_panel.THREE_PASS_LENGTH_ENV_KEY
        with (
            patch.object(web_panel, "ENV_FILE", env_file),
            patch.dict(os.environ, {environment_key: ""}, clear=False),
        ):
            initial = self.client.get("/api/three-pass-config")
            self.assertEqual(initial.status_code, 200)
            self.assertEqual(initial.get_json()["threePass"]["phases"]["pass1"]["target"], 250)

            payload = {
                "phases": {
                    "pass1": {"level": "low", "target": 9999},
                    "pass2": {"level": "medium"},
                    "pass3": {"level": "custom", "target": 1600},
                    "synthesis": {"level": "long"},
                }
            }
            saved = self.client.put("/api/three-pass-config", json=payload)
            self.assertEqual(saved.status_code, 200, saved.get_json())
            phases = saved.get_json()["threePass"]["phases"]
            self.assertEqual(phases["pass1"], {"level": "low", "target": 150})
            self.assertEqual(phases["pass3"], {"level": "custom", "target": 1600})
            self.assertEqual(phases["synthesis"], {"level": "long", "target": 1800})
            persisted_before_invalid = env_file.read_text(encoding="utf-8")

            providers = self.client.get("/api/reader/analysis-providers").get_json()
            self.assertEqual(providers["threePass"]["phases"], phases)
            invalid = self.client.put(
                "/api/three-pass-config",
                json={"phases": {"pass1": {"level": "custom", "target": 99}}},
            )
            self.assertEqual(invalid.status_code, 400)
            self.assertEqual(env_file.read_text(encoding="utf-8"), persisted_before_invalid)

    def test_damaged_three_pass_length_environment_falls_back_to_medium(self):
        with patch.dict(os.environ, {web_panel.THREE_PASS_LENGTH_ENV_KEY: "not-json"}, clear=False):
            config = self.client.get("/api/three-pass-config").get_json()["threePass"]
        self.assertEqual(config["phases"], default_phase_lengths())

    def test_reader_page_contains_three_pass_controls(self):
        response = self.client.get("/reader")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        for control_id in (
            "threePassPanel",
            "codexStatus",
            "threePassModel",
            "threePassEffort",
            "threePassLanguage",
            "threePassLengthSummary",
            "threePassBackend",
            "threePassApiPreset",
            "threePassFocus",
            "codexLogin",
            "startThreePass",
            "cancelThreePass",
            "threePassProgress",
            "threePassFollowup",
        ):
            self.assertIn(f'id="{control_id}"', html)

    def test_api_options_fall_back_to_model_effort_and_accept_english(self):
        model, effort, language, focuses, custom = web_panel.validated_three_pass_options(
            {
                "model": "test-model",
                "effort": "ultra",
                "language": "en-US",
                "focuses": ["method", "limitations"],
                "customFocus": "Check failure modes",
            },
            web_panel.three_pass_manager.status(),
        )
        self.assertEqual((model, effort, language), ("test-model", "high", "en-US"))
        self.assertEqual(focuses, ["方法", "局限与失败模式"])
        self.assertEqual(custom, "Check failure modes")

    def test_markdown_and_mathpix_html_inputs_preserve_structure(self):
        markdown_input = web_panel.reader_analysis_input(self.document)
        self.assertIn("# Paper", markdown_input)
        self.assertIn("source evidence", markdown_input)

        html_root = web_panel.JOBS_DIR / "reader-html"
        (html_root / "output").mkdir(parents=True)
        html = """
        <html><body><h1>OCR Paper</h1>
        <p>Method evidence repeated for a stable extraction. {body}</p>
        <p>Inline <math><annotation encoding="application/x-tex">x^2 + y^2</annotation></math>.</p>
        <table><tr><th>Metric</th><th>Value</th></tr><tr><td>Accuracy</td><td>91%</td></tr></table>
        <figure><img src="figures/overview.png" alt="Overview"><figcaption>System overview</figcaption></figure>
        </body></html>
        """.format(body="evidence " * 60)
        (html_root / "output" / "paper.html").write_text(html, encoding="utf-8")
        ocr_document = web_panel.ReaderDocument(
            id="ocr-document",
            root=html_root,
            title="OCR Paper",
            source_type="pdf",
            mode="ocr",
            render_kind="html",
            document_filename="paper.html",
            status="ready",
        )
        normalized = web_panel.reader_analysis_input(ocr_document)
        self.assertIn("# OCR Paper", normalized)
        self.assertIn("$x^2 + y^2$", normalized)
        self.assertIn("| Metric | Value |", normalized)
        self.assertIn("![Overview](figures/overview.png)", normalized)
        self.assertIn("[Figure caption] System overview", normalized)

    def test_pdf_input_has_page_markers_and_rejects_weak_text_layer(self):
        pdf_root = web_panel.JOBS_DIR / "reader-pdf"
        (pdf_root / "input").mkdir(parents=True)
        (pdf_root / "input" / "paper.pdf").write_bytes(b"%PDF-1.4 synthetic")
        document = web_panel.ReaderDocument(
            id="pdf-document",
            root=pdf_root,
            title="PDF Paper",
            source_type="pdf",
            mode="pdf",
            render_kind="pdf",
            source_filename="paper.pdf",
            status="ready",
        )

        class Page:
            def __init__(self, text):
                self.text = text

            def extract_text(self):
                return self.text

        class Reader:
            def __init__(self, pages):
                self.pages = pages

        with patch.object(web_panel, "PdfReader", return_value=Reader([Page("page one " * 40), Page("page two " * 40)])):
            normalized = web_panel.reader_analysis_input(document)
        self.assertIn("## [Page 1]", normalized)
        self.assertIn("## [Page 2]", normalized)

        with patch.object(web_panel, "PdfReader", return_value=Reader([Page("tiny")])):
            with self.assertRaisesRegex(ValueError, "OCR"):
                web_panel.reader_analysis_input(document)


if __name__ == "__main__":
    unittest.main()
