"""Unit tests for the project-local Codex installer and rollback state."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from codex_three_pass import CodexAppServerClient, CodexProtocolError
from managed_codex import (
    CodexRuntimeResolution,
    ManagedCodexError,
    ManagedCodexInstaller,
    codex_binary_in,
    read_codex_version,
)


class StubInstaller(ManagedCodexInstaller):
    def __init__(self, project_root: Path, clock: list[float]) -> None:
        super().__init__(project_root, update_interval_seconds=10, now=lambda: clock[0])
        self.latest = "1.0.0"
        self.latest_error: str | None = None
        self.install_error: str | None = None
        self.registry_checks = 0
        self.installs: list[str] = []

    def _latest_stable_version(self) -> str:
        self.registry_checks += 1
        if self.latest_error:
            raise ManagedCodexError(self.latest_error)
        return self.latest

    def _install_release(self, version: str) -> Path:
        self.installs.append(version)
        if self.install_error:
            raise ManagedCodexError(self.install_error)
        executable = codex_binary_in(self.releases / version)
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.write_text("stub", encoding="utf-8")
        return executable


class ManagedCodexInstallerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.clock = [1_000.0]
        self.installer = StubInstaller(self.root, self.clock)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_installs_latest_atomically_then_skips_registry_until_due(self):
        first = self.installer.resolve()
        second = self.installer.resolve()

        self.assertEqual(first.version, "1.0.0")
        self.assertEqual(first.source, "managed")
        self.assertTrue(Path(first.executable).is_file())
        self.assertEqual(second.executable, first.executable)
        self.assertEqual(self.installer.registry_checks, 1)
        self.assertEqual(self.installer.installs, ["1.0.0"])

    def test_offline_check_keeps_last_verified_release(self):
        self.installer.resolve()
        self.clock[0] += 11
        self.installer.latest_error = "registry offline"

        resolution = self.installer.resolve()

        self.assertEqual(resolution.version, "1.0.0")
        self.assertEqual(resolution.update_error, "registry offline")

    def test_failed_update_keeps_current_release(self):
        self.installer.resolve()
        self.clock[0] += 11
        self.installer.latest = "2.0.0"
        self.installer.install_error = "install failed"

        resolution = self.installer.resolve()

        self.assertEqual(resolution.version, "1.0.0")
        self.assertEqual(resolution.update_error, "install failed")
        self.assertEqual(self.installer._read_state()["currentVersion"], "1.0.0")

    def test_rollback_blocks_failed_release_and_restores_previous(self):
        self.installer.resolve()
        self.clock[0] += 11
        self.installer.latest = "2.0.0"
        self.assertEqual(self.installer.resolve().version, "2.0.0")

        rollback = self.installer.rollback("2.0.0")
        retried = self.installer.resolve(force_check=True)

        self.assertIsNotNone(rollback)
        self.assertEqual(rollback.version, "1.0.0")
        self.assertEqual(rollback.rolled_back_from, "2.0.0")
        self.assertEqual(retried.version, "1.0.0")
        self.assertIn("此前启动失败", retried.update_error)

    def test_only_current_and_previous_releases_are_retained(self):
        self.installer.resolve()
        for version in ("2.0.0", "3.0.0"):
            self.clock[0] += 11
            self.installer.latest = version
            self.installer.resolve()

        releases = sorted(path.name for path in self.installer.releases.iterdir())
        self.assertEqual(releases, ["2.0.0", "3.0.0"])


class ManagedCodexClientTest(unittest.TestCase):
    def test_default_uses_managed_runtime_but_environment_override_wins(self):
        with patch.dict(os.environ, {"CODEX_BIN": ""}, clear=False):
            managed = CodexAppServerClient(Path.cwd())
        with patch.dict(os.environ, {"CODEX_BIN": "/opt/codex"}, clear=False):
            overridden = CodexAppServerClient(Path.cwd())

        self.assertIsNone(managed.codex_bin)
        self.assertIsNotNone(managed.managed_installer)
        self.assertEqual(overridden.codex_bin, "/opt/codex")
        self.assertIsNone(overridden.managed_installer)

    def test_app_server_start_failure_rolls_back_once(self):
        client = CodexAppServerClient(Path.cwd(), "/tmp/placeholder")
        current = CodexRuntimeResolution("/tmp/codex-2", "2.0.0", "managed")
        previous = CodexRuntimeResolution(
            "/tmp/codex-1", "1.0.0", "managed", rolled_back_from="2.0.0"
        )
        installer = Mock()
        installer.resolve.return_value = current
        installer.rollback.return_value = previous
        client.codex_bin = None
        client.managed_installer = installer

        with (
            patch.object(client, "_start_runtime", side_effect=[CodexProtocolError("bad"), None]) as start,
            patch.object(client, "stop"),
        ):
            client.ensure_started()

        self.assertEqual(start.call_count, 2)
        installer.rollback.assert_called_once_with("2.0.0")
        self.assertEqual(start.call_args_list[1].args[0], previous)

    def test_version_parser_accepts_codex_cli_output(self):
        completed = Mock(returncode=0, stdout="codex-cli 3.4.5\n", stderr="")
        with patch("managed_codex.subprocess.run", return_value=completed):
            self.assertEqual(read_codex_version("/tmp/codex"), "3.4.5")


if __name__ == "__main__":
    unittest.main()
