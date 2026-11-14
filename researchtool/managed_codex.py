"""Project-local Codex CLI installation, updates, and rollback support."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterator


CODEX_NPM_PACKAGE = "@openai/codex"
DEFAULT_UPDATE_INTERVAL_SECONDS = 24 * 60 * 60
INSTALL_TIMEOUT_SECONDS = 10 * 60
LOCK_TIMEOUT_SECONDS = 2 * 60
STALE_LOCK_SECONDS = 15 * 60
VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")
VERSION_OUTPUT_PATTERN = re.compile(r"(?:codex(?:-cli)?\s+)?(\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?)", re.I)


class ManagedCodexError(RuntimeError):
    """Raised when no usable managed Codex runtime can be prepared."""


@dataclass(frozen=True)
class CodexRuntimeResolution:
    executable: str
    version: str | None
    source: str
    latest_version: str | None = None
    previous_version: str | None = None
    last_checked_at: float | None = None
    update_error: str | None = None
    rolled_back_from: str | None = None

    def public(self) -> dict[str, Any]:
        payload = asdict(self)
        return {
            "executable": payload["executable"],
            "version": payload["version"],
            "source": payload["source"],
            "latestVersion": payload["latest_version"],
            "previousVersion": payload["previous_version"],
            "lastCheckedAt": payload["last_checked_at"],
            "updateError": payload["update_error"],
            "rolledBackFrom": payload["rolled_back_from"],
            "managed": payload["source"] == "managed",
        }


def codex_binary_in(release_root: Path) -> Path:
    suffix = ".cmd" if os.name == "nt" else ""
    return release_root / "node_modules" / ".bin" / f"codex{suffix}"


def read_codex_version(executable: str | Path, *, timeout: float = 15) -> str:
    try:
        result = subprocess.run(
            [str(executable), "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ManagedCodexError(f"无法验证 Codex CLI：{exc}") from exc
    output = "\n".join(part for part in (result.stdout, result.stderr) if part).strip()
    if result.returncode != 0:
        raise ManagedCodexError(f"Codex CLI 版本检查失败：{output or f'exit {result.returncode}'}")
    match = VERSION_OUTPUT_PATTERN.search(output)
    if not match:
        raise ManagedCodexError(f"无法识别 Codex CLI 版本：{output or 'empty output'}")
    return match.group(1)


class ManagedCodexInstaller:
    """Maintain an atomic, project-local installation of the stable Codex CLI."""

    def __init__(
        self,
        project_root: Path,
        *,
        update_interval_seconds: float | None = None,
        npm_bin: str | None = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.project_root = project_root.resolve()
        self.root = self.project_root / ".runtime" / "codex"
        self.releases = self.root / "releases"
        self.state_path = self.root / "state.json"
        self.lock_path = self.root / ".update.lock"
        self.npm_bin = npm_bin or os.getenv("CODEX_NPM_BIN", "").strip() or None
        self.now = now
        if update_interval_seconds is None:
            raw_interval = os.getenv("CODEX_UPDATE_INTERVAL_HOURS", "24").strip()
            try:
                update_interval_seconds = max(300.0, float(raw_interval) * 60 * 60)
            except ValueError:
                update_interval_seconds = DEFAULT_UPDATE_INTERVAL_SECONDS
        self.update_interval_seconds = float(update_interval_seconds)

    def resolve(self, *, force_check: bool = False) -> CodexRuntimeResolution:
        self.releases.mkdir(parents=True, exist_ok=True)
        state = self._read_state()
        current = self._usable_release(state.get("currentVersion"))
        if current and not self._check_due(state, force_check):
            return self._resolution(current, state)

        with self._update_lock():
            state = self._read_state()
            current = self._usable_release(state.get("currentVersion"))
            if current and not self._check_due(state, force_check):
                return self._resolution(current, state)

            checked_at = self.now()
            try:
                latest = self._latest_stable_version()
            except ManagedCodexError as exc:
                state.update({"lastCheckedAt": checked_at, "lastError": str(exc)})
                self._write_state(state)
                if current:
                    return self._resolution(current, state)
                raise

            state.update({"lastCheckedAt": checked_at, "latestVersion": latest, "lastError": None})
            if latest == state.get("failedVersion"):
                state["lastError"] = f"Codex {latest} 此前启动失败；等待下一个稳定版本。"
                self._write_state(state)
                if current and state.get("currentVersion") != latest:
                    return self._resolution(current, state)
                raise ManagedCodexError(state["lastError"])

            latest_release = self._usable_release(latest)
            if not latest_release:
                try:
                    latest_release = self._install_release(latest)
                except ManagedCodexError as exc:
                    state["lastError"] = str(exc)
                    self._write_state(state)
                    if current:
                        return self._resolution(current, state)
                    raise

            old_version = state.get("currentVersion") if current else None
            if old_version != latest:
                state["previousVersion"] = old_version
            state["currentVersion"] = latest
            if state.get("failedVersion") != latest:
                state.pop("failedVersion", None)
                state.pop("failedAt", None)
            self._write_state(state)
            self._cleanup_releases(state)
            return self._resolution(latest_release, state)

    def rollback(self, failed_version: str) -> CodexRuntimeResolution | None:
        """Atomically reactivate the previous verified release after startup failure."""
        with self._update_lock():
            state = self._read_state()
            if state.get("currentVersion") != failed_version:
                current = self._usable_release(state.get("currentVersion"))
                return self._resolution(current, state) if current else None
            previous = self._usable_release(state.get("previousVersion"))
            if not previous:
                state.update(
                    {
                        "currentVersion": None,
                        "previousVersion": None,
                        "failedVersion": failed_version,
                        "failedAt": self.now(),
                        "lastError": f"Codex {failed_version} 启动失败；等待下一个稳定版本。",
                    }
                )
                self._write_state(state)
                return None
            previous_version = str(state["previousVersion"])
            state.update(
                {
                    "currentVersion": previous_version,
                    "previousVersion": failed_version,
                    "failedVersion": failed_version,
                    "failedAt": self.now(),
                    "lastError": f"Codex {failed_version} 启动失败，已回滚到 {previous_version}。",
                }
            )
            self._write_state(state)
            return CodexRuntimeResolution(
                executable=str(previous),
                version=previous_version,
                source="managed",
                latest_version=state.get("latestVersion"),
                previous_version=failed_version,
                last_checked_at=state.get("lastCheckedAt"),
                update_error=state.get("lastError"),
                rolled_back_from=failed_version,
            )

    def _check_due(self, state: dict[str, Any], force_check: bool) -> bool:
        if force_check:
            return True
        try:
            last_checked = float(state.get("lastCheckedAt") or 0)
        except (TypeError, ValueError):
            return True
        return self.now() - last_checked >= self.update_interval_seconds

    def _npm_executable(self) -> str:
        candidate = self.npm_bin or ("npm.cmd" if os.name == "nt" else "npm")
        executable = shutil.which(candidate)
        if not executable:
            raise ManagedCodexError("未找到 npm，无法安装项目内 Codex CLI。请先安装 Node.js/npm。")
        return executable

    def _latest_stable_version(self) -> str:
        npm = self._npm_executable()
        try:
            result = subprocess.run(
                [npm, "view", f"{CODEX_NPM_PACKAGE}@latest", "version", "--json"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ManagedCodexError(f"检查 Codex 稳定版本失败：{exc}") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or f"exit {result.returncode}").strip()
            raise ManagedCodexError(f"检查 Codex 稳定版本失败：{detail[-1000:]}")
        try:
            version = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ManagedCodexError("npm 返回了无效的 Codex 版本信息。") from exc
        if isinstance(version, list):
            version = version[-1] if version else None
        version = str(version or "").strip()
        if not VERSION_PATTERN.fullmatch(version):
            raise ManagedCodexError(f"npm 返回了不安全的 Codex 版本号：{version or 'empty'}")
        return version

    def _install_release(self, version: str) -> Path:
        npm = self._npm_executable()
        staging = self.root / f".staging-{uuid.uuid4().hex}"
        target = self.releases / version
        try:
            result = subprocess.run(
                [
                    npm,
                    "install",
                    "--prefix",
                    str(staging),
                    "--no-audit",
                    "--no-fund",
                    "--package-lock=false",
                    f"{CODEX_NPM_PACKAGE}@{version}",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=INSTALL_TIMEOUT_SECONDS,
                check=False,
            )
            if result.returncode != 0:
                detail = (result.stderr or result.stdout or f"exit {result.returncode}").strip()
                raise ManagedCodexError(f"安装 Codex {version} 失败：{detail[-2000:]}")
            staged_binary = codex_binary_in(staging)
            actual_version = read_codex_version(staged_binary)
            if actual_version != version:
                raise ManagedCodexError(
                    f"Codex 安装版本不匹配：期望 {version}，实际 {actual_version}。"
                )
            if target.exists():
                shutil.rmtree(target)
            os.replace(staging, target)
            return codex_binary_in(target)
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)

    def _usable_release(self, version: Any) -> Path | None:
        version = str(version or "").strip()
        if not VERSION_PATTERN.fullmatch(version):
            return None
        executable = codex_binary_in(self.releases / version)
        return executable if executable.is_file() else None

    def _resolution(self, executable: Path, state: dict[str, Any]) -> CodexRuntimeResolution:
        return CodexRuntimeResolution(
            executable=str(executable),
            version=str(state.get("currentVersion") or "") or None,
            source="managed",
            latest_version=str(state.get("latestVersion") or "") or None,
            previous_version=str(state.get("previousVersion") or "") or None,
            last_checked_at=state.get("lastCheckedAt"),
            update_error=str(state.get("lastError") or "") or None,
        )

    def _cleanup_releases(self, state: dict[str, Any]) -> None:
        keep = {str(state.get("currentVersion") or ""), str(state.get("previousVersion") or "")}
        for child in self.releases.iterdir():
            if child.is_dir() and VERSION_PATTERN.fullmatch(child.name) and child.name not in keep:
                shutil.rmtree(child, ignore_errors=True)

    def _read_state(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _write_state(self, payload: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_name(f".{self.state_path.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, self.state_path)

    @contextmanager
    def _update_lock(self) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
        while True:
            try:
                descriptor = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                try:
                    stale = self.now() - self.lock_path.stat().st_mtime > STALE_LOCK_SECONDS
                except OSError:
                    stale = False
                if stale:
                    try:
                        self.lock_path.unlink()
                    except OSError:
                        pass
                    continue
                if time.monotonic() >= deadline:
                    raise ManagedCodexError("等待另一个 ResearchTool 进程更新 Codex 超时。")
                time.sleep(0.2)
                continue
            else:
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    handle.write(f"{os.getpid()}\n")
                break
        try:
            yield
        finally:
            try:
                self.lock_path.unlink()
            except OSError:
                pass
