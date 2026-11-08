"""Claude Code CLI runner: plan-only billing, no untrusted argv, safe errors. Never calls the real CLI."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from claude_code_cli import ClaudeCodeRunner, find_claude_executable


class FakeProcess:
    def __init__(self, stdout="", stderr="", raise_exc=None):
        self.stdout, self.stderr, self.raise_exc = stdout, stderr, raise_exc
        self.calls = []

    def __call__(self, args, **kwargs):
        system_file = args[args.index("--system-prompt-file") + 1] if "--system-prompt-file" in args else None
        self.calls.append({"args": args, **kwargs,
                           "system": Path(system_file).read_text(encoding="utf-8") if system_file else None})
        if self.raise_exc:
            raise self.raise_exc
        return SimpleNamespace(stdout=self.stdout, stderr=self.stderr, returncode=0)


def runner_with(fake):
    return ClaudeCodeRunner(finder=lambda: "C:/fake/claude.exe", runner=fake, timeout=5)


class ClaudeCodeRunnerTests(unittest.TestCase):
    MESSAGES = [{"role": "system", "content": "Rules & | <tags> %PATH%"},
                {"role": "user", "content": '{"paper": "untrusted & | text"}'}]

    def test_text_goes_through_stdin_and_file_with_tools_and_api_keys_removed(self):
        fake = FakeProcess(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": '{"ok":true}'}))
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-secret", "CLAUDECODE": "1", "KEEP": "yes"}):
            self.assertEqual(runner_with(fake).run(self.MESSAGES, "opus", "high"), '{"ok":true}')
        call = fake.calls[0]
        args = call["args"]
        self.assertEqual(args[args.index("--tools") + 1], "")
        self.assertIn("--strict-mcp-config", args)
        self.assertIn("--no-session-persistence", args)
        self.assertFalse(any("untrusted" in a or "Rules" in a for a in args))
        self.assertEqual(call["input"], self.MESSAGES[1]["content"])
        self.assertTrue(call["system"].startswith("Rules & | <tags> %PATH%"))
        self.assertNotIn("ANTHROPIC_API_KEY", call["env"])
        self.assertNotIn("CLAUDECODE", call["env"])
        self.assertEqual(call["env"]["KEEP"], "yes")
        self.assertFalse(Path(call["cwd"]).exists(), "temporary working directory is removed")

    def test_errors_become_safe_chinese_messages(self):
        limit = FakeProcess(json.dumps({"subtype": "success", "is_error": True, "result": "Claude AI usage limit reached|1700000000"}))
        with self.assertRaisesRegex(ValueError, "额度"):
            runner_with(limit).run(self.MESSAGES, "sonnet")
        with self.assertRaisesRegex(ValueError, "秒未返回"):
            runner_with(FakeProcess(raise_exc=subprocess.TimeoutExpired("claude", 5))).run(self.MESSAGES, "opus")
        with self.assertRaisesRegex(ValueError, "Claude Code：not json"):
            runner_with(FakeProcess("not json")).run(self.MESSAGES, "opus")
        with self.assertRaisesRegex(ValueError, "可解析"):
            runner_with(FakeProcess("")).run(self.MESSAGES, "opus")
        with self.assertRaisesRegex(ValueError, "无效"):
            runner_with(FakeProcess()).run(self.MESSAGES, "opus; rm -rf", "high")

    def test_status_requires_claude_ai_login_and_hides_identity(self):
        plan = FakeProcess(json.dumps({"loggedIn": True, "authMethod": "claude.ai", "subscriptionType": "max",
                                       "email": "person@example.com", "orgName": "Org"}))
        status = runner_with(plan).status()
        self.assertTrue(status["plan"])
        self.assertNotIn("person@example.com", json.dumps(status))
        api_key = FakeProcess(json.dumps({"loggedIn": True, "authMethod": "api_key"}))
        self.assertFalse(runner_with(api_key).status()["plan"])
        self.assertFalse(runner_with(FakeProcess(json.dumps({"loggedIn": False}))).status()["plan"])
        missing = ClaudeCodeRunner(finder=lambda: None, runner=FakeProcess()).status()
        self.assertFalse(missing["available"])

    def test_status_is_cached_until_refresh(self):
        fake = FakeProcess(json.dumps({"loggedIn": True, "authMethod": "claude.ai"}))
        runner = runner_with(fake)
        runner.status(); runner.status()
        self.assertEqual([c["args"][1:] for c in fake.calls], [["--version"], ["auth", "status", "--json"]])
        runner.status(refresh=True)
        self.assertEqual(len(fake.calls), 4)

    def test_global_defaults_come_from_env_with_recommendation_fallback(self):
        from claude_code_cli import current_defaults, validate_defaults
        env = {"CLAUDE_CODE_TRANSLATE_MODEL": "opus", "CLAUDE_CODE_TRANSLATE_EFFORT": "bogus",
               "CLAUDE_CODE_REVISE_MODEL": "", "CLAUDE_CODE_REVISE_EFFORT": "max"}
        with mock.patch.dict(os.environ, env):
            self.assertEqual(current_defaults(), {"translate": {"model": "opus", "effort": "medium"},
                                                  "revise": {"model": "opus", "effort": "max"}})
            status = runner_with(FakeProcess(json.dumps({"loggedIn": True, "authMethod": "claude.ai"}))).status()
            self.assertEqual(status["defaults"]["translate"]["model"], "opus")
            self.assertEqual(status["recommended"]["translate"], {"model": "sonnet", "effort": "medium"})
        self.assertEqual(validate_defaults({"translateModel": "sonnet", "translateEffort": "low",
                                            "reviseModel": "opus", "reviseEffort": "xhigh"}),
                         {"CLAUDE_CODE_TRANSLATE_MODEL": "sonnet", "CLAUDE_CODE_TRANSLATE_EFFORT": "low",
                          "CLAUDE_CODE_REVISE_MODEL": "opus", "CLAUDE_CODE_REVISE_EFFORT": "xhigh"})
        with self.assertRaises(ValueError):
            validate_defaults({"translateModel": "haiku", "translateEffort": "low", "reviseModel": "opus", "reviseEffort": "high"})

    @unittest.skipUnless(os.name == "nt", "Windows npm shim layout")
    def test_finder_prefers_native_exe_over_npm_shim(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.dict(os.environ, {"CLAUDE_CODE_CLI": ""}):
            shim = Path(folder) / "claude.cmd"
            shim.write_text("@echo off", encoding="utf-8")
            exe = Path(folder) / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
            exe.parent.mkdir(parents=True)
            exe.write_bytes(b"")
            self.assertEqual(find_claude_executable(lambda name: str(shim)), str(exe))
            exe.unlink()
            with mock.patch.object(Path, "home", return_value=Path(folder) / "nohome"):
                self.assertIsNone(find_claude_executable(lambda name: str(shim)))


if __name__ == "__main__":
    unittest.main()
