"""Local multi-tool web panel for the scripts in this repository."""

from __future__ import annotations

import difflib
import importlib.util
import hashlib
import base64
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import escape as html_escape, unescape as html_unescape
import io
import json
import math
import os
import queue
import random
import re
import shutil
import socket
import statistics
import subprocess
import sys
import threading
import time
import unicodedata
import uuid
import webbrowser
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterator
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

import pandas as pd
import requests
from bs4 import BeautifulSoup, Comment, Declaration, Doctype, NavigableString, ProcessingInstruction, Tag
from dotenv import load_dotenv, set_key, unset_key
from flask import Flask, jsonify, render_template, request, send_file
from werkzeug.serving import make_server
from werkzeug.utils import secure_filename

from research_gaps import ResearchGapService, create_research_gap_blueprint

try:
    from openai import OpenAI
except ImportError:  # Dependency checks keep the rest of the panel usable.
    OpenAI = None  # type: ignore[assignment,misc]

try:
    from pypdf import PdfReader
except ImportError:  # Filename fallback keeps the reader usable without it.
    PdfReader = None  # type: ignore[assignment,misc]

from sheet_to_anki import SheetToAnkiError, clean_cell, read_table, require_columns


PROJECT_ROOT = Path(__file__).resolve().parent
ENV_FILE = PROJECT_ROOT / ".env"
load_dotenv(ENV_FILE, override=False)
RUNTIME_DIR = PROJECT_ROOT / ".runtime"
UPLOAD_DIR = RUNTIME_DIR / "uploads"
JOBS_DIR = RUNTIME_DIR / "jobs"
READER_CACHE_DIR = RUNTIME_DIR / "reader-cache"
RESEARCH_GAP_DIR = RUNTIME_DIR / "research-gaps"
SCRIPTS_DIR = PROJECT_ROOT / "Potential_Scripts"
ALLOWED_TABLE_SUFFIXES = {".xlsx", ".xlsm", ".xls", ".csv", ".txt"}
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"}
READER_ASSET_SUFFIXES = IMAGE_SUFFIXES | {".gif"}
GITHUB_MARKDOWN_SUFFIXES = {".md", ".mmd"}
GITHUB_IMAGE_SUFFIXES = READER_ASSET_SUFFIXES | {".svg"}
GITHUB_PUBLISH_SUFFIXES = GITHUB_MARKDOWN_SUFFIXES | GITHUB_IMAGE_SUFFIXES
VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}
PDF_SUFFIXES = {".pdf"}
READER_SOURCE_SUFFIXES = {".pdf", ".md", ".mmd", ".html", ".htm"}
MATHPIX_BASE_URL = "https://api.mathpix.com/v3"
OCR_OPTIONAL_FORMATS = ("docx", "md", "html", "tex.zip")
MMD_BUNDLE_FORMAT = "mmd.zip"
HTML_BUNDLE_FORMAT = "html.zip"
TRANSLATABLE_SUFFIXES = {".mmd", ".md", ".html", ".htm"}
OCR_TRANSLATABLE_SUFFIXES = {".md", ".mmd", ".html", ".htm"}
MAX_OCR_WAIT_SECONDS = 20 * 60
POLL_INTERVAL_SECONDS = 5
MAX_READER_CONTEXT_LENGTH = 24_000
MAX_READER_SPEECH_LENGTH = 12_000
MAX_READER_ARCHIVE_SIZE = 250 * 1024 * 1024
READER_CACHE_SCHEMA_VERSION = 1
READER_PAIRING_VERSION = 5
TRANSLATION_MARKDOWN_POSTPROCESS_VERSION = 1
READER_ALIGNMENT_MAX_BLOCKS = 40
READER_ALIGNMENT_MAX_PAYLOAD_CHARS = 48_000
READER_ALIGNMENT_BLOCK_PREVIEW_CHARS = 1_200
READER_SOFT_ANCHOR_MIN_GAP_BLOCKS = 8
READER_SOFT_ANCHOR_LIMIT = 12
READER_SOFT_ANCHOR_MIN_SCORE = 0.52
READER_SOFT_ANCHOR_MIN_MARGIN = 0.07
TRANSLATION_RESUME_SCHEMA_VERSION = 1
TRANSLATION_PIPELINE_VERSION = "2026.08.01.3"
TRANSLATION_DEBUG_SCHEMA_VERSION = 1
# Cached files and reader behavior evolve independently. The cache schema only
# describes durable source/OCR/translation artifacts; this version describes
# the live reader capabilities applied whenever any document is opened.
READER_FEATURE_VERSION = "2026.08.01.9"
READER_UPDATE_POLICY = "latest"
AZURE_SPEECH_DEFAULT_REGION = "centralus"
AZURE_SPEECH_DEFAULT_VOICE = "zh-CN-YunxiNeural"
AZURE_SPEECH_OUTPUT_FORMAT = "audio-24khz-48kbitrate-mono-mp3"
GITHUB_API_URL = "https://api.github.com"
GITHUB_DEFAULT_BRANCH = "main"
GITHUB_DEFAULT_IMAGE_ROOT = "images"
GITHUB_API_VERSION = "2022-11-28"

app = Flask(__name__)
# The desktop panel is a long-running local process. Reload templates on each
# request so an updated reader.html cannot be paired with a newer static script.
app.config["TEMPLATES_AUTO_RELOAD"] = True
reader_cache_lock = threading.Lock()
llm_config_lock = threading.Lock()


class LlmConcurrencyRegistry:
    """Coordinate a mutable global request limit for each configured provider."""

    def __init__(self) -> None:
        self.condition = threading.Condition()
        self.limits: dict[str, int] = {}
        self.active: dict[str, int] = {}

    def configure(self, key: str, limit: int) -> None:
        with self.condition:
            self.limits[key] = limit
            self.condition.notify_all()

    @contextmanager
    def slot(self, key: str, fallback_limit: int) -> Iterator[None]:
        with self.condition:
            limit = self.limits.setdefault(key, fallback_limit)
            while self.active.get(key, 0) >= limit:
                self.condition.wait()
                limit = self.limits.get(key, fallback_limit)
            self.active[key] = self.active.get(key, 0) + 1
        try:
            yield
        finally:
            with self.condition:
                remaining = self.active.get(key, 1) - 1
                if remaining > 0:
                    self.active[key] = remaining
                else:
                    self.active.pop(key, None)
                self.condition.notify_all()


llm_concurrency_registry = LlmConcurrencyRegistry()


@dataclass(frozen=True)
class ToolSpec:
    id: str
    title: str
    category: str
    description: str
    accepts: set[str]
    dependencies: tuple[str, ...] = ()
    needs_ffmpeg: bool = False
    min_files: int = 1
    max_files: int | None = None


TOOLS = (
    ToolSpec("pdf_ocr_translate", "论文 PDF OCR", "文献处理", "将论文 PDF 识别为 Markdown、HTML、DOCX 与 LaTeX 等科研格式。", PDF_SUFFIXES, ("requests", "dotenv"), max_files=1),
    ToolSpec("document_translate", "科研文档翻译", "文献处理", "使用 AI-Markdown-Translator 后端批量翻译 Markdown、MMD 或 HTML。", TRANSLATABLE_SUFFIXES),
    ToolSpec("markdown_repair", "Markdown 修复", "文献处理", "独立修复 Markdown/MMD 的结构、图片路径与行间公式边界，不翻译正文。", {".md", ".mmd"}),
    ToolSpec("markdown_github", "Markdown 图片发布", "文献处理", "将 Markdown 引用的本地图片发布到 GitHub，并下载替换链接后的副本。", GITHUB_PUBLISH_SUFFIXES),
    ToolSpec("bibtex", "论文标题转 BibTeX", "文献处理", "根据论文标题检索并整理可直接引用的 BibTeX。", {".txt"}, ("scholarly",), max_files=1),
    ToolSpec("anki", "研究笔记转 Anki", "知识整理", "从表格选择问答字段，导出可直接导入 Anki 的复习卡片。", ALLOWED_TABLE_SUFFIXES, max_files=1),
    ToolSpec("image_crop", "实验图片裁剪缩放", "图像与演示", "批量裁剪实验图片的中心区域并统一输出尺寸。", IMAGE_SUFFIXES, ("PIL",)),
    ToolSpec("image_ppt", "图片网格 PPT", "图像与演示", "将实验图片自动排成规整的 PowerPoint 网格。", IMAGE_SUFFIXES, ("PIL", "pptx")),
    ToolSpec("video_ppt", "视频帧网格 PPT", "图像与演示", "从实验视频抽取指定帧并排版为 PowerPoint。", VIDEO_SUFFIXES, ("PIL", "pptx", "cv2")),
    ToolSpec("stack_images", "图片线性拼接 PPT", "图像与演示", "将多张实验结果横向或纵向拼接到 PowerPoint。", IMAGE_SUFFIXES, ("PIL", "pptx")),
    ToolSpec("video_crop", "实验视频裁剪缩放", "视频处理", "批量裁剪、缩放实验视频并统一转为 MP4。", VIDEO_SUFFIXES, needs_ffmpeg=True),
    ToolSpec("frames", "MP4 全帧导出", "视频处理", "将实验视频的全部帧导出为 PNG 图像序列。", {".mp4"}, ("cv2",)),
    ToolSpec("fix_frame", "修复第 16 帧", "视频处理", "将第 16 帧替换为第 15 帧并输出 16 帧视频。", {".mp4"}, ("cv2",), True, 1, 1),
    ToolSpec("stack_videos", "视频网格拼接", "视频处理", "按行列、标题和说明拼接多组实验视频。", VIDEO_SUFFIXES, ("PIL", "pptx"), True),
)
TOOL_BY_ID = {tool.id: tool for tool in TOOLS}


@dataclass
class Job:
    id: str
    tool: ToolSpec
    root: Path
    options: dict[str, Any]
    status: str = "queued"
    logs: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    download_path: Path | None = None
    download_name: str | None = None
    phase: str = "processing"
    operation: str = "default"
    source_stem: str = "document"
    local_save_dir: Path | None = None
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    pending_artifact_id: str | None = None
    translation_config: dict[str, Any] | None = None
    markdown_repair_config: dict[str, Any] | None = None
    reader_document_id: str | None = None
    translation_progress: dict[str, int] | None = None
    translation_debug_count: int = 0
    publication: dict[str, Any] | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    def log(self, message: str) -> None:
        with self.lock:
            self.logs.append(message.rstrip())
            self.logs = self.logs[-500:]

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "id": self.id,
                "tool": self.tool.id,
                "status": self.status,
                "logs": self.logs,
                "downloadReady": self.download_path is not None,
                "downloadName": self.download_name,
                "phase": self.phase,
                "warnings": self.warnings,
                "translationProgress": self.translation_progress.copy() if self.translation_progress else None,
                "translationDebugCount": self.translation_debug_count,
                "publication": self.publication.copy() if self.publication else None,
                "translationDebugUrl": (
                    f"/api/jobs/{self.id}/artifacts/translation_debug.jsonl"
                    if self.translation_debug_count
                    else None
                ),
                "translationRetryAvailable": self.tool.id == "document_translate" and self.status == "failed",
                "artifacts": [
                    {
                        **artifact,
                        "downloadUrl": f"/api/jobs/{self.id}/artifacts/{artifact['id']}",
                    }
                    for artifact in self.artifacts
                ],
                "readerPairs": [
                    {
                        "id": pair["id"],
                        "title": pair["title"],
                        "sourceName": pair["sourceName"],
                        "translatedName": pair["translatedName"],
                    }
                    for pair in job_reader_pair_records(self)
                ],
            }


class JobManager:
    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}
        self.lock = threading.Lock()
        self.queue: queue.Queue[Job] = queue.Queue()
        self.worker = threading.Thread(target=self._work, daemon=True, name="tool-job-worker")
        self.worker.start()

    def submit(self, job: Job) -> None:
        with self.lock:
            self.jobs[job.id] = job
        self.queue.put(job)

    def get(self, job_id: str) -> Job | None:
        with self.lock:
            return self.jobs.get(job_id)

    def cleanup(self) -> None:
        deadline = time.time() - 24 * 60 * 60
        with self.lock:
            expired = [job_id for job_id, job in self.jobs.items() if job.finished_at and job.finished_at < deadline]
            for job_id in expired:
                job = self.jobs.pop(job_id)
                shutil.rmtree(job.root, ignore_errors=True)

    def _work(self) -> None:
        while True:
            job = self.queue.get()
            try:
                run_job(job)
            except Exception as exc:  # pragma: no cover - final background boundary
                job.log(f"[ERROR] {exc}")
                with job.lock:
                    job.status = "failed"
                    job.finished_at = time.time()
            finally:
                self.queue.task_done()


job_manager = JobManager()


# The reader intentionally keeps documents in the local runtime directory and
# in memory.  It makes uploaded research papers private to this machine while
# still letting the browser poll an asynchronous OCR/translation job.
@dataclass
class ReaderDocument:
    id: str
    root: Path
    title: str
    source_type: str
    mode: str
    render_kind: str = "markdown"
    title_customized: bool = False
    use_ocr: bool = False
    generate_translation: bool = False
    align_translation: bool = False
    use_local_cache: bool = True
    cache_key: str | None = None
    cache_available: list[str] = field(default_factory=list)
    cache_hits: list[str] = field(default_factory=list)
    source_filename: str | None = None
    source_name: str | None = None
    document_filename: str | None = None
    translated_filename: str | None = None
    page_count: int | None = None
    status: str = "queued"
    message: str = "Waiting for local worker."
    blocks: list[dict[str, Any]] = field(default_factory=list)
    translated_blocks: list[dict[str, Any]] | None = None
    alignment_progress: dict[str, Any] | None = None
    job_id: str | None = None
    error: str | None = None
    created_at: float = field(default_factory=time.time)

    def snapshot(self) -> dict[str, Any]:
        has_translation = self.translated_blocks is not None or self.translated_filename is not None
        return {
            "id": self.id,
            "title": self.title,
            "sourceType": self.source_type,
            "mode": self.mode,
            "renderKind": self.render_kind,
            "titleCustomized": self.title_customized,
            "useOcr": self.use_ocr,
            "generateTranslation": self.generate_translation,
            "alignTranslation": self.align_translation,
            "useLocalCache": self.use_local_cache,
            "cacheKey": self.cache_key,
            "cacheAvailable": self.cache_available.copy(),
            "cacheHits": self.cache_hits.copy(),
            "pageCount": self.page_count,
            "status": self.status,
            "message": self.message,
            "alignmentProgress": self.alignment_progress.copy() if self.alignment_progress else None,
            "hasTranslation": has_translation,
            "readerFeatureVersion": READER_FEATURE_VERSION,
            "readerUpdatePolicy": READER_UPDATE_POLICY,
            "readerPairingVersion": READER_PAIRING_VERSION,
            "capabilities": reader_capabilities(self.render_kind, has_translation),
            "jobId": self.job_id,
            "error": self.error,
        }


class ReaderManager:
    def __init__(self) -> None:
        self.documents: dict[str, ReaderDocument] = {}
        self.lock = threading.Lock()

    def add(self, document: ReaderDocument) -> None:
        with self.lock:
            self.documents[document.id] = document

    def get(self, document_id: str) -> ReaderDocument | None:
        with self.lock:
            return self.documents.get(document_id)


reader_manager = ReaderManager()


def set_reader_alignment_progress(
    job: Job,
    stage: str,
    label: str,
    completed: int,
    total: int,
    percent: int,
) -> None:
    """Publish monotonic imported-pair alignment progress for reader polling."""
    document = reader_manager.get(job.reader_document_id or "")
    if not document:
        return
    safe_total = max(0, int(total))
    safe_completed = max(0, min(int(completed), safe_total)) if safe_total else 0
    previous_percent = int((document.alignment_progress or {}).get("percent") or 0)
    document.alignment_progress = {
        "stage": stage,
        "label": label,
        "completed": safe_completed,
        "total": safe_total,
        "percent": max(previous_percent, max(0, min(100, int(percent)))),
    }
    document.message = label


READER_TOOL = ToolSpec(
    "paper_reader",
    "科研论文阅读器",
    "研究",
    "对 PDF 执行 Mathpix OCR，或直接阅读 Markdown。",
    READER_SOURCE_SUFFIXES,
    ("requests", "openai"),
    max_files=1,
)


def reader_capabilities(render_kind: str, has_translation: bool) -> dict[str, bool]:
    reflowable = render_kind in {"markdown", "html"}
    return {
        "latestReader": True,
        "persistentNotes": True,
        "readingSettings": True,
        "selectionActions": True,
        "imageAssets": reflowable,
        "translationSwitch": has_translation,
        "interleavedView": reflowable and has_translation,
        "sideBySideView": reflowable and has_translation,
        "liveParagraphTranslation": reflowable,
        "pdfZoom": render_kind == "pdf",
    }


def json_error(message: str, status: int = 400):
    return jsonify({"error": message}), status


def validate_azure_speech_fields(api_key: Any, region: Any, voice: Any) -> dict[str, str]:
    key = str(api_key or "").strip()
    normalized_region = str(region or AZURE_SPEECH_DEFAULT_REGION).strip().lower()
    normalized_voice = str(voice or AZURE_SPEECH_DEFAULT_VOICE).strip()
    if not key:
        raise ValueError("请先配置 Azure Speech API Key。")
    if len(key) > 1000:
        raise ValueError("Azure Speech API Key 不能超过 1,000 个字符。")
    if not re.fullmatch(r"[a-z0-9-]{2,64}", normalized_region):
        raise ValueError("AZURE_SPEECH_REGION 配置无效。")
    if not re.fullmatch(r"[A-Za-z0-9:-]{2,100}", normalized_voice):
        raise ValueError("AZURE_SPEECH_VOICE 配置无效。")
    return {"key": key, "region": normalized_region, "voice": normalized_voice}


def azure_speech_config() -> dict[str, str]:
    return validate_azure_speech_fields(
        os.getenv("AZURE_SPEECH_KEY", ""),
        os.getenv("AZURE_SPEECH_REGION", AZURE_SPEECH_DEFAULT_REGION),
        os.getenv("AZURE_SPEECH_VOICE", AZURE_SPEECH_DEFAULT_VOICE),
    )


def public_azure_speech_config() -> dict[str, Any]:
    key_configured = bool(os.getenv("AZURE_SPEECH_KEY", "").strip())
    region = os.getenv("AZURE_SPEECH_REGION", AZURE_SPEECH_DEFAULT_REGION).strip().lower()
    voice = os.getenv("AZURE_SPEECH_VOICE", AZURE_SPEECH_DEFAULT_VOICE).strip()
    return {
        "configured": key_configured,
        "region": region or AZURE_SPEECH_DEFAULT_REGION,
        "voice": voice or AZURE_SPEECH_DEFAULT_VOICE,
    }


def save_azure_speech_config(api_key: Any, region: Any, voice: Any) -> dict[str, Any]:
    replacement_key = str(api_key or "").strip() or os.getenv("AZURE_SPEECH_KEY", "").strip()
    config = validate_azure_speech_fields(replacement_key, region, voice)
    with llm_config_lock:
        if not ENV_FILE.exists():
            ENV_FILE.touch(mode=0o600)
        for environment_key, value in (
            ("AZURE_SPEECH_KEY", config["key"]),
            ("AZURE_SPEECH_REGION", config["region"]),
            ("AZURE_SPEECH_VOICE", config["voice"]),
        ):
            set_key(str(ENV_FILE), environment_key, value, quote_mode="auto")
            os.environ[environment_key] = value
    return public_azure_speech_config()


def delete_azure_speech_config() -> dict[str, Any]:
    existing = public_azure_speech_config()
    with llm_config_lock:
        for environment_key in ("AZURE_SPEECH_KEY", "AZURE_SPEECH_REGION", "AZURE_SPEECH_VOICE"):
            if ENV_FILE.exists():
                unset_key(str(ENV_FILE), environment_key)
            os.environ.pop(environment_key, None)
    return existing


def validate_github_fields(token: Any, repository: Any, branch: Any, image_root: Any) -> dict[str, str]:
    normalized_token = str(token or "").strip()
    normalized_repository = str(repository or "").strip().removesuffix(".git")
    normalized_branch = str(branch or GITHUB_DEFAULT_BRANCH).strip()
    normalized_root = str(image_root or GITHUB_DEFAULT_IMAGE_ROOT).strip().replace("\\", "/").strip("/")
    if not normalized_token or len(normalized_token) > 1000:
        raise ValueError("请配置长度不超过 1,000 个字符的 GitHub Personal Access Token。")
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9_.-]{1,100}", normalized_repository):
        raise ValueError("GitHub 仓库必须使用 owner/repo 格式。")
    if (
        not normalized_branch
        or len(normalized_branch) > 255
        or normalized_branch.startswith(("/", "."))
        or normalized_branch.endswith(("/", ".", ".lock"))
        or ".." in normalized_branch
        or "@{" in normalized_branch
        or any(char in normalized_branch for char in " ~^:?*[\\\x00\r\n")
    ):
        raise ValueError("GitHub 分支名称无效。")
    root = PurePosixPath(normalized_root)
    if (
        not normalized_root
        or root.is_absolute()
        or any(part in {"", ".", ".."} or ":" in part for part in root.parts)
        or len(normalized_root) > 500
    ):
        raise ValueError("GitHub 图片根目录必须是安全的仓库相对路径。")
    return {
        "token": normalized_token,
        "repository": normalized_repository,
        "branch": normalized_branch,
        "imageRoot": root.as_posix(),
    }


def github_config() -> dict[str, str]:
    return validate_github_fields(
        os.getenv("GITHUB_TOKEN", ""),
        os.getenv("GITHUB_REPOSITORY", ""),
        os.getenv("GITHUB_BRANCH", GITHUB_DEFAULT_BRANCH),
        os.getenv("GITHUB_IMAGE_ROOT", GITHUB_DEFAULT_IMAGE_ROOT),
    )


def public_github_config() -> dict[str, Any]:
    repository = os.getenv("GITHUB_REPOSITORY", "").strip().removesuffix(".git")
    branch = os.getenv("GITHUB_BRANCH", GITHUB_DEFAULT_BRANCH).strip() or GITHUB_DEFAULT_BRANCH
    image_root = os.getenv("GITHUB_IMAGE_ROOT", GITHUB_DEFAULT_IMAGE_ROOT).strip().replace("\\", "/").strip("/") or GITHUB_DEFAULT_IMAGE_ROOT
    return {
        "configured": bool(os.getenv("GITHUB_TOKEN", "").strip() and repository),
        "repository": repository,
        "branch": branch,
        "imageRoot": image_root,
    }


def save_github_config(token: Any, repository: Any, branch: Any, image_root: Any) -> dict[str, Any]:
    replacement_token = str(token or "").strip() or os.getenv("GITHUB_TOKEN", "").strip()
    config = validate_github_fields(replacement_token, repository, branch, image_root)
    with llm_config_lock:
        if not ENV_FILE.exists():
            ENV_FILE.touch(mode=0o600)
        for environment_key, value in (
            ("GITHUB_TOKEN", config["token"]),
            ("GITHUB_REPOSITORY", config["repository"]),
            ("GITHUB_BRANCH", config["branch"]),
            ("GITHUB_IMAGE_ROOT", config["imageRoot"]),
        ):
            set_key(str(ENV_FILE), environment_key, value, quote_mode="auto")
            os.environ[environment_key] = value
    return public_github_config()


def delete_github_config() -> dict[str, Any]:
    existing = public_github_config()
    with llm_config_lock:
        for environment_key in ("GITHUB_TOKEN", "GITHUB_REPOSITORY", "GITHUB_BRANCH", "GITHUB_IMAGE_ROOT"):
            if ENV_FILE.exists():
                unset_key(str(ENV_FILE), environment_key)
            os.environ.pop(environment_key, None)
    return existing


def github_api_request(
    method: str,
    path: str,
    config: dict[str, str],
    *,
    json_body: dict[str, Any] | None = None,
    expected: set[int] | None = None,
) -> dict[str, Any]:
    expected_statuses = expected or {200}
    try:
        response = requests.request(
            method,
            f"{GITHUB_API_URL}{path}",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {config['token']}",
                "X-GitHub-Api-Version": GITHUB_API_VERSION,
                "User-Agent": "Research-Toolkit-Markdown-Publisher",
            },
            json=json_body,
            timeout=(10, 120),
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"无法连接 GitHub：{exc}") from exc
    if response.status_code not in expected_statuses:
        detail = response_error(response)
        if response.status_code == 401:
            raise RuntimeError("GitHub Token 无效或已过期。")
        if response.status_code == 403:
            raise RuntimeError(f"GitHub 拒绝请求；请确认 Token 具有 Contents: write 权限。{detail}")
        if response.status_code == 404:
            raise RuntimeError("GitHub 仓库、分支或对象不存在，或当前 Token 无权访问。")
        if response.status_code == 409:
            raise RuntimeError("GitHub 仓库尚未初始化或当前分支不可用。")
        if response.status_code == 422:
            raise RuntimeError("GitHub 拒绝更新；目标分支可能已变化，请重新运行任务。")
        raise RuntimeError(f"GitHub 请求失败（HTTP {response.status_code}）：{detail}")
    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError("GitHub 返回了无法解析的响应。") from exc
    if not isinstance(data, dict):
        raise RuntimeError("GitHub 返回了意外的响应结构。")
    return data


def test_github_connection() -> dict[str, Any]:
    config = github_config()
    repository = config["repository"]
    repo = github_api_request("GET", f"/repos/{repository}", config)
    if repo.get("private") is not False:
        raise RuntimeError("Raw 图片链接要求公开仓库；当前仓库不是公开仓库。")
    encoded_branch = quote(config["branch"], safe="")
    branch = github_api_request("GET", f"/repos/{repository}/branches/{encoded_branch}", config)
    return {
        "repository": str(repo.get("full_name") or repository),
        "repositoryId": repo.get("id"),
        "branch": str(branch.get("name") or config["branch"]),
        "visibility": str(repo.get("visibility") or "public"),
    }


def test_azure_speech_connection() -> dict[str, Any]:
    config = azure_speech_config()
    started = time.perf_counter()
    audio, voice = synthesize_azure_speech("这是一次语音连接测试。")
    return {
        "region": config["region"],
        "voice": voice,
        "latencyMs": round((time.perf_counter() - started) * 1000),
        "audioBytes": len(audio),
    }


def normalize_azure_speech_rate(value: Any) -> float:
    try:
        rate = float(value)
    except (TypeError, ValueError):
        raise ValueError("Azure 朗读语速必须是 0.5 至 2.0 之间的数字。") from None
    if not math.isfinite(rate) or not 0.5 <= rate <= 2.0:
        raise ValueError("Azure 朗读语速必须在 0.5× 至 2.0× 之间。")
    return round(rate, 2)


def synthesize_azure_speech(text: str, rate: Any = 1) -> tuple[bytes, str]:
    normalized = re.sub(r"\s+", " ", text).strip()
    if not normalized or len(normalized) > MAX_READER_SPEECH_LENGTH:
        raise ValueError(f"朗读段落必须包含 1 至 {MAX_READER_SPEECH_LENGTH:,} 个字符。")
    normalized_rate = normalize_azure_speech_rate(rate)
    rate_percent = round((normalized_rate - 1) * 100)
    config = azure_speech_config()
    ssml = (
        "<speak version='1.0' xml:lang='zh-CN'>"
        f"<voice name='{html_escape(config['voice'], quote=True)}'>"
        f"<prosody rate='{rate_percent:+d}%'>{html_escape(normalized, quote=False)}</prosody>"
        "</voice></speak>"
    )
    try:
        response = requests.post(
            f"https://{config['region']}.tts.speech.microsoft.com/cognitiveservices/v1",
            headers={
                "Ocp-Apim-Subscription-Key": config["key"],
                "Content-Type": "application/ssml+xml",
                "X-Microsoft-OutputFormat": AZURE_SPEECH_OUTPUT_FORMAT,
                "User-Agent": "Paper-Lens-Reader",
                "Accept": "audio/mpeg",
            },
            data=ssml.encode("utf-8"),
            timeout=(10, 90),
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"无法连接 Azure Speech：{exc}") from exc
    if not response.ok:
        if response.status_code in {401, 403}:
            raise RuntimeError("Azure Speech 密钥或区域不正确。")
        raise RuntimeError(f"Azure Speech 请求失败（HTTP {response.status_code}）：{response_error(response)}")
    if not response.content:
        raise RuntimeError("Azure Speech 返回了空音频。")
    return response.content, config["voice"]


def is_excel_file(path: Path) -> bool:
    return path.suffix.lower() in {".xlsx", ".xlsm", ".xls"}


def excel_sheets(path: Path) -> list[str]:
    engine = "xlrd" if path.suffix.lower() == ".xls" else "openpyxl"
    try:
        return pd.ExcelFile(path, engine=engine).sheet_names
    except ImportError as exc:
        raise SheetToAnkiError(f"Reading {path.suffix} files requires {engine}.") from exc


def table_columns(path: Path, sheet: str | None = None) -> list[str]:
    return [str(column) for column in read_table(path, sheet).columns]


def upload_path(token: str, filename: str) -> Path:
    return UPLOAD_DIR / f"{token}_{secure_filename(filename) or 'upload'}"


def find_upload(token: str) -> Path:
    matches = list(UPLOAD_DIR.glob(f"{token}_*"))
    if not matches:
        raise SheetToAnkiError("Uploaded file was not found. Upload it again.")
    return matches[0]


def has_module(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def missing_environment(*names: str) -> list[str]:
    return [name for name in names if not os.environ.get(name, "").strip()]


def mathpix_config_error() -> str | None:
    missing = missing_environment("MATHPIX_APP_ID", "MATHPIX_APP_KEY")
    if missing:
        return f"Mathpix is not configured. Add {', '.join(missing)} to the project .env file and restart the panel."
    return None


def github_config_error() -> str | None:
    missing = missing_environment("GITHUB_REPOSITORY", "GITHUB_TOKEN")
    if missing:
        return "请先在“AI、语音与 GitHub 配置”中完成 GitHub 仓库设置。"
    try:
        github_config()
    except ValueError as exc:
        return str(exc)
    return None


def llm_config_error() -> str | None:
    missing = missing_environment("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL")
    if missing:
        return f"LLM translation is not configured. Add {', '.join(missing)} to the project .env file and restart the panel."
    return None


def llm_concurrency(value: Any, default: int = 1) -> int:
    if value in (None, ""):
        return default
    if isinstance(value, bool) or (isinstance(value, float) and not value.is_integer()):
        raise ValueError("LLM concurrency must be an integer between 1 and 64.")
    try:
        concurrency = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("LLM concurrency must be an integer between 1 and 64.") from exc
    if not 1 <= concurrency <= 64:
        raise ValueError("LLM concurrency must be between 1 and 64.")
    return concurrency


def llm_presets() -> list[dict[str, Any]]:
    """Return safe public metadata plus in-memory credentials for .env presets."""
    presets: list[dict[str, Any]] = []
    legacy_error = llm_config_error()
    if not legacy_error:
        presets.append(
            {
                "id": "default",
                "name": os.environ.get("LLM_NAME", "默认 LLM").strip() or "默认 LLM",
                "baseUrl": os.environ["LLM_BASE_URL"],
                "model": os.environ["LLM_MODEL"],
                "apiKey": os.environ["LLM_API_KEY"],
                "concurrency": llm_concurrency(os.environ.get("LLM_CONCURRENCY")),
            }
        )
    raw_ids = re.split(r"[\s,]+", os.environ.get("LLM_PRESETS", "").strip())
    for raw_id in filter(None, raw_ids):
        key = raw_id.upper()
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", key):
            continue
        prefix = f"LLM_PRESET_{key}_"
        base_url = os.environ.get(f"{prefix}BASE_URL", "").strip()
        api_key = os.environ.get(f"{prefix}API_KEY", "").strip()
        model = os.environ.get(f"{prefix}MODEL", "").strip()
        if not base_url or not api_key or not model:
            continue
        presets.append(
            {
                "id": key.lower(),
                "name": os.environ.get(f"{prefix}NAME", raw_id).strip() or raw_id,
                "baseUrl": base_url,
                "model": model,
                "apiKey": api_key,
                "concurrency": llm_concurrency(os.environ.get(f"{prefix}CONCURRENCY")),
            }
        )
    return presets


def public_llm_presets() -> list[dict[str, Any]]:
    return [{key: preset[key] for key in ("id", "name", "baseUrl", "model", "concurrency")} for preset in llm_presets()]


def validate_llm_fields(
    name: Any,
    base_url: Any,
    api_key: Any,
    model: Any,
    concurrency: Any = None,
) -> dict[str, Any]:
    values = {
        "name": str(name or "").strip(),
        "baseUrl": str(base_url or "").strip(),
        "apiKey": str(api_key or "").strip(),
        "model": str(model or "").strip(),
        "concurrency": llm_concurrency(concurrency),
    }
    if not values["name"] or len(values["name"]) > 100 or any(ord(char) < 32 for char in values["name"]):
        raise ValueError("LLM configuration name is required and must be at most 100 characters.")
    if not values["baseUrl"].startswith(("https://", "http://")) or len(values["baseUrl"]) > 500:
        raise ValueError("LLM base URL must be a valid http(s) URL.")
    if not values["apiKey"] or len(values["apiKey"]) > 1000:
        raise ValueError("LLM API key is required.")
    if not values["model"] or len(values["model"]) > 200:
        raise ValueError("LLM model ID is required.")
    return values


def persistent_preset_id(value: Any) -> str:
    raw = re.sub(r"[^A-Za-z0-9_]+", "_", str(value or "").strip()).strip("_").upper()
    if not raw:
        raise ValueError("A preset name is required.")
    if raw[0].isdigit():
        raw = f"PROVIDER_{raw}"
    if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", raw):
        raise ValueError("The preset name must contain at most 64 letters, numbers, or underscores.")
    if raw == "DEFAULT":
        raise ValueError("'default' is reserved for the legacy LLM configuration.")
    return raw


def save_llm_preset(
    preset_name: Any,
    base_url: Any,
    api_key: Any,
    model: Any,
    concurrency: Any = 1,
) -> dict[str, Any]:
    """Persist one named OpenAI-compatible provider in the project-local .env."""
    config = validate_llm_fields(preset_name, base_url, api_key, model, concurrency)
    preset_key = persistent_preset_id(config["name"])
    with llm_config_lock:
        current_ids = [item.upper() for item in re.split(r"[\s,]+", os.environ.get("LLM_PRESETS", "").strip()) if item]
        if preset_key in current_ids:
            raise ValueError(f"A saved LLM preset named '{preset_key.lower()}' already exists.")
        if not ENV_FILE.exists():
            ENV_FILE.touch(mode=0o600)
        updated_ids = [*current_ids, preset_key]
        prefix = f"LLM_PRESET_{preset_key}_"
        # set_key handles quoting and replaces only the targeted keys, so unrelated
        # project configuration and comments remain intact.
        set_key(str(ENV_FILE), "LLM_PRESETS", ",".join(updated_ids), quote_mode="auto")
        set_key(str(ENV_FILE), f"{prefix}NAME", config["name"], quote_mode="auto")
        set_key(str(ENV_FILE), f"{prefix}BASE_URL", config["baseUrl"], quote_mode="auto")
        set_key(str(ENV_FILE), f"{prefix}API_KEY", config["apiKey"], quote_mode="auto")
        set_key(str(ENV_FILE), f"{prefix}MODEL", config["model"], quote_mode="auto")
        set_key(str(ENV_FILE), f"{prefix}CONCURRENCY", str(config["concurrency"]), quote_mode="auto")
        # Existing requests should see the provider immediately without a restart.
        os.environ["LLM_PRESETS"] = ",".join(updated_ids)
        os.environ[f"{prefix}NAME"] = config["name"]
        os.environ[f"{prefix}BASE_URL"] = config["baseUrl"]
        os.environ[f"{prefix}API_KEY"] = config["apiKey"]
        os.environ[f"{prefix}MODEL"] = config["model"]
        os.environ[f"{prefix}CONCURRENCY"] = str(config["concurrency"])
        llm_concurrency_registry.configure(f"preset:{preset_key.lower()}", config["concurrency"])
    for preset in public_llm_presets():
        if preset["id"] == preset_key.lower():
            return preset
    raise RuntimeError("The saved LLM preset could not be reloaded.")


def update_llm_preset(
    preset_id: Any,
    preset_name: Any,
    base_url: Any,
    api_key: Any,
    model: Any,
    concurrency: Any = None,
) -> dict[str, Any]:
    """Update a preset in place so saved browser mappings keep the same ID."""
    requested_id = str(preset_id or "").strip().lower()
    existing = next((preset for preset in llm_presets() if preset["id"] == requested_id), None)
    if not existing:
        raise ValueError("The selected LLM preset does not exist.")
    replacement_key = str(api_key or "").strip() or existing["apiKey"]
    config = validate_llm_fields(
        preset_name,
        base_url,
        replacement_key,
        model,
        existing["concurrency"] if concurrency in (None, "") else concurrency,
    )
    prefix = "LLM_" if requested_id == "default" else f"LLM_PRESET_{requested_id.upper()}_"
    with llm_config_lock:
        if not ENV_FILE.exists():
            ENV_FILE.touch(mode=0o600)
        for suffix, value in (
            ("NAME", config["name"]),
            ("BASE_URL", config["baseUrl"]),
            ("API_KEY", config["apiKey"]),
            ("MODEL", config["model"]),
            ("CONCURRENCY", str(config["concurrency"])),
        ):
            environment_key = f"{prefix}{suffix}"
            set_key(str(ENV_FILE), environment_key, value, quote_mode="auto")
            os.environ[environment_key] = value
        llm_concurrency_registry.configure(f"preset:{requested_id}", config["concurrency"])
    updated = next((preset for preset in public_llm_presets() if preset["id"] == requested_id), None)
    if not updated:
        raise RuntimeError("The updated LLM preset could not be reloaded.")
    return updated


def delete_llm_preset(preset_id: Any) -> dict[str, Any]:
    """Remove a preset and its credential fields from the project-local .env."""
    requested_id = str(preset_id or "").strip().lower()
    existing = next((preset for preset in llm_presets() if preset["id"] == requested_id), None)
    if not existing:
        raise ValueError("The selected LLM preset does not exist.")
    public_existing = {key: existing[key] for key in ("id", "name", "baseUrl", "model", "concurrency")}
    prefix = "LLM_" if requested_id == "default" else f"LLM_PRESET_{requested_id.upper()}_"
    with llm_config_lock:
        for suffix in ("NAME", "BASE_URL", "API_KEY", "MODEL", "CONCURRENCY"):
            environment_key = f"{prefix}{suffix}"
            if ENV_FILE.exists():
                unset_key(str(ENV_FILE), environment_key)
            os.environ.pop(environment_key, None)
        if requested_id != "default":
            preset_key = requested_id.upper()
            current_ids = [
                item.upper()
                for item in re.split(r"[\s,]+", os.environ.get("LLM_PRESETS", "").strip())
                if item and item.upper() != preset_key
            ]
            if not ENV_FILE.exists():
                ENV_FILE.touch(mode=0o600)
            set_key(str(ENV_FILE), "LLM_PRESETS", ",".join(current_ids), quote_mode="auto")
            os.environ["LLM_PRESETS"] = ",".join(current_ids)
    return public_existing


def llm_test_response_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                value = item.get("text") or item.get("content")
            else:
                value = getattr(item, "text", None) or getattr(item, "content", None)
            if isinstance(value, str) and value.strip():
                parts.append(value.strip())
        return "\n".join(parts)
    return ""


def test_llm_preset_connection(preset_id: Any, test_message: Any) -> dict[str, Any]:
    """Send one tiny chat request using a saved preset without exposing its key."""
    if OpenAI is None:
        raise RuntimeError("The OpenAI Python SDK is not installed.")
    message = str(test_message or "").strip()
    if not message:
        raise ValueError("The LLM test message cannot be empty.")
    if len(message) > 500:
        raise ValueError("The LLM test message must be at most 500 characters.")
    requested_id = str(preset_id or "").strip().lower()
    config = next((preset for preset in llm_presets() if preset["id"] == requested_id), None)
    if not config:
        raise ValueError("The selected LLM preset does not exist.")
    prepare_llm_runtime_config(config, f"preset:{requested_id}")
    started = time.perf_counter()
    client = OpenAI(
        api_key=config["apiKey"],
        base_url=config["baseUrl"],
        timeout=180.0,
        max_retries=0,
    )
    response = create_compatible_chat_completion(
        client,
        config,
        model=config["model"],
        temperature=0,
        messages=[{"role": "user", "content": message}],
    )
    if not response.choices:
        raise RuntimeError("LLM returned no choices for the test request.")
    choice = response.choices[0]
    answer = llm_test_response_text(choice.message.content)
    return {
        "name": config["name"],
        "model": config["model"],
        "latencyMs": max(0, round((time.perf_counter() - started) * 1000)),
        "response": answer[:200],
        "contentEmpty": not bool(answer),
        "finishReason": str(getattr(choice, "finish_reason", "") or ""),
    }


def safe_llm_probe_error(exc: Exception, api_key: str) -> dict[str, Any]:
    status = llm_error_status(exc)
    message = (str(exc).strip() or exc.__class__.__name__).replace(api_key, "[redacted]")[:300]
    if status == 429:
        kind = "rate_limited"
    elif status == 408 or "timeout" in exc.__class__.__name__.casefold():
        kind = "timeout"
    elif status is not None and status >= 500:
        kind = "server_error"
    elif "connection" in exc.__class__.__name__.casefold():
        kind = "connection_error"
    else:
        kind = "request_error"
    return {"kind": kind, "status": status, "message": message}


def test_llm_preset_concurrency(preset_id: Any) -> dict[str, Any]:
    """Probe the preset's saved concurrency in one burst without retrying failures."""
    if OpenAI is None:
        raise RuntimeError("The OpenAI Python SDK is not installed.")
    requested_id = str(preset_id or "").strip().lower()
    config = next((preset for preset in llm_presets() if preset["id"] == requested_id), None)
    if not config:
        raise ValueError("The selected LLM preset does not exist.")
    prepare_llm_runtime_config(config, f"preset:{requested_id}")
    target = llm_concurrency(config.get("concurrency"))
    stages: list[dict[str, Any]] = []
    level = target
    barrier = threading.Barrier(level)

    def probe_one(_index: int) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            barrier.wait(timeout=10)
            client = OpenAI(
                api_key=config["apiKey"],
                base_url=config["baseUrl"],
                timeout=180.0,
                max_retries=0,
            )
            response = create_compatible_chat_completion(
                client,
                config,
                model=config["model"],
                temperature=0,
                transient_retries=0,
                messages=[
                    {"role": "system", "content": "This is a concurrency health check. Reply with OK only."},
                    {"role": "user", "content": "OK"},
                ],
            )
            if not getattr(response, "choices", None):
                raise RuntimeError("LLM returned no choices.")
            return {"ok": True, "latencyMs": max(0, round((time.perf_counter() - started) * 1000))}
        except Exception as exc:
            return {
                "ok": False,
                "latencyMs": max(0, round((time.perf_counter() - started) * 1000)),
                "error": safe_llm_probe_error(exc, config["apiKey"]),
            }

    batch_started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=level, thread_name_prefix="llm-probe") as executor:
        results = list(executor.map(probe_one, range(level)))
    wall_seconds = max(0.001, time.perf_counter() - batch_started)
    successful = [result for result in results if result["ok"]]
    errors: dict[str, int] = {}
    examples: list[dict[str, Any]] = []
    for result in results:
        if result["ok"]:
            continue
        error = result["error"]
        errors[error["kind"]] = errors.get(error["kind"], 0) + 1
        if len(examples) < 3:
            examples.append(error)
    passed = len(successful) == level
    effective_parallelism = min(
        float(level),
        sum(float(result["latencyMs"]) for result in results) / max(1.0, wall_seconds * 1000),
    )
    stages.append({
        "concurrency": level,
        "passed": passed,
        "successCount": len(successful),
        "failureCount": level - len(successful),
        "wallMs": round(wall_seconds * 1000),
        "p50LatencyMs": round(statistics.median(result["latencyMs"] for result in results)),
        "effectiveParallelism": round(effective_parallelism, 2),
        "errors": errors,
        "errorExamples": examples,
    })

    return {
        "name": config["name"],
        "model": config["model"],
        "configuredConcurrency": target,
        "stableConcurrency": target if passed else 0,
        "recommendedConcurrency": target if passed else None,
        "reachedConfiguredLimit": passed,
        "totalRequests": target,
        "stages": stages,
        "disclaimer": (
            "This is a point-in-time burst test. Provider quotas, account limits, load, and shared traffic can change the result."
        ),
    }


def safe_llm_test_error(exc: Exception, preset_id: Any) -> str:
    message = str(exc).strip() or exc.__class__.__name__
    requested_id = str(preset_id or "").strip().lower()
    config = next((preset for preset in llm_presets() if preset["id"] == requested_id), None)
    if config and config["apiKey"]:
        message = message.replace(config["apiKey"], "[redacted]")
    return message[:500]


def prepare_llm_runtime_config(config: dict[str, Any], limiter_key: str | None = None) -> dict[str, Any]:
    config["concurrency"] = llm_concurrency(config.get("concurrency"))
    if not limiter_key:
        identity = "\0".join(
            str(config.get(key) or "") for key in ("baseUrl", "model", "apiKey")
        )
        limiter_key = f"custom:{hashlib.sha256(identity.encode()).hexdigest()}"
    config["_limiterKey"] = limiter_key
    config.setdefault("_capabilityLock", threading.Lock())
    llm_concurrency_registry.configure(limiter_key, config["concurrency"])
    return config


def translation_config_from_request(data: dict[str, Any]) -> dict[str, Any]:
    config = data.get("llm")
    if not isinstance(config, dict):
        raise ValueError("Choose an LLM preset or enter a custom OpenAI-compatible configuration.")
    mode = str(config.get("mode", "")).strip()
    if mode == "preset":
        requested_id = str(config.get("presetId", "")).strip()
        for preset in llm_presets():
            if preset["id"] == requested_id:
                selected = validate_llm_fields(
                    preset["name"],
                    preset["baseUrl"],
                    preset["apiKey"],
                    preset["model"],
                    preset["concurrency"],
                )
                return prepare_llm_runtime_config(selected, f"preset:{requested_id}")
        raise ValueError("The selected LLM preset is unavailable. Check .env and restart the panel.")
    if mode == "custom":
        selected = validate_llm_fields(
            config.get("name"),
            config.get("baseUrl"),
            config.get("apiKey"),
            config.get("model"),
            config.get("concurrency", 1),
        )
        return prepare_llm_runtime_config(selected)
    raise ValueError("Invalid LLM configuration mode.")


def managed_translation_config_from_request(data: dict[str, Any]) -> dict[str, Any]:
    """Resolve whole-document translation credentials from the shared preset manager only."""
    config = data.get("llm")
    if not isinstance(config, dict) or str(config.get("mode", "")).strip() != "preset":
        raise ValueError("Choose a saved LLM configuration from the global LLM manager.")
    return translation_config_from_request(data)


def dependency_error(tool: ToolSpec) -> str | None:
    missing = [name for name in tool.dependencies if not has_module(name)]
    if missing:
        return (
            f"Missing Python dependency: {', '.join(missing)}. "
            "Run the project launcher for your operating system."
        )
    if tool.needs_ffmpeg:
        missing_bins = [binary for binary in ("ffmpeg", "ffprobe") if shutil.which(binary) is None]
        if missing_bins:
            return f"Missing system dependency: {', '.join(missing_bins)}. Install FFmpeg and add it to PATH."
    return None


def tool_public(tool: ToolSpec) -> dict[str, Any]:
    error = dependency_error(tool)
    ocr_error = mathpix_config_error() if tool.id == "pdf_ocr_translate" else None
    if not error and ocr_error:
        error = ocr_error
    github_error = github_config_error() if tool.id == "markdown_github" else None
    if not error and github_error:
        error = github_error
    return {
        "id": tool.id,
        "title": tool.title,
        "category": tool.category,
        "description": tool.description,
        "accepts": sorted(tool.accepts),
        "minFiles": tool.min_files,
        "maxFiles": tool.max_files,
        "available": error is None,
        "unavailableReason": error,
        "translationAvailable": tool.id != "pdf_ocr_translate" or error is None,
        "translationUnavailableReason": None,
        # Both document translation and the follow-up translation step in PDF OCR
        # use the same local .env presets. Keep credentials server-side; only the
        # safe display fields are returned to the browser.
        "llmPresets": public_llm_presets() if tool.id in {"document_translate", "pdf_ocr_translate", "markdown_repair"} else [],
    }


def safe_relative_path(value: str) -> Path:
    parts = [secure_filename(part) for part in Path(value.replace("\\", "/")).parts if part not in {".", "..", "/"}]
    parts = [part for part in parts if part]
    if not parts:
        raise ValueError("Invalid uploaded file name.")
    return Path(*parts)


def safe_work_name(value: str, suffix: str) -> str:
    raw = Path(value).stem if value else "file"
    cleaned = secure_filename(raw) or "file"
    return f"{cleaned}{suffix.lower()}"


def safe_output_stem(value: str) -> str:
    raw = Path(value).stem if value else "document"
    cleaned = re.sub(r'[\\/:*?"<>|#%\x00-\x1f]', "_", raw).strip(". ")
    return cleaned[:180] or "document"


def relative_path_within(path: Path, root: Path) -> Path:
    """Return a relative path while accepting equivalent symlinked roots.

    macOS commonly aliases ``/var`` to ``/private/var``. Comparing the original
    spelling first avoids changing normal paths, while the resolved fallback
    keeps internal task and cache paths interoperable across that alias.
    """
    try:
        return path.relative_to(root)
    except ValueError:
        return path.resolve().relative_to(root.resolve())


def local_pdf_source_path(options: dict[str, Any]) -> Path:
    value = str(options.get("localSourcePath", "")).strip()
    if not value:
        raise ValueError("Enter the original PDF's absolute local path so outputs can be saved beside it.")
    if len(value) > 4096:
        raise ValueError("The local PDF path is too long.")
    source = Path(value).expanduser().resolve()
    if source.suffix.lower() != ".pdf" or not source.is_file():
        raise ValueError("The local source path must point to an existing PDF file.")
    return source


def positive_int(value: Any, name: str, default: int, minimum: int = 1, maximum: int = 10000) -> int:
    if value in (None, ""):
        return default
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer.") from exc
    if not minimum <= result <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}.")
    return result


def number(value: Any, name: str, default: float, minimum: float = 0, maximum: float = 100000) -> float:
    if value in (None, ""):
        return default
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number.") from exc
    if not minimum <= result <= maximum:
        raise ValueError(f"{name} is outside the allowed range.")
    return result


def option_text(options: dict[str, Any], name: str, default: str = "") -> str:
    value = str(options.get(name, default)).strip()
    if len(value) > 500:
        raise ValueError(f"{name} is too long.")
    return value


def run_process(job: Job, command: list[str]) -> None:
    job.log("[COMMAND] " + " ".join(f'"{part}"' if " " in part else part for part in command))
    process = subprocess.Popen(
        command,
        cwd=job.root,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert process.stdout is not None
    with process.stdout:
        for line in process.stdout:
            job.log(line)
    if process.wait() != 0:
        raise RuntimeError(f"Tool exited with code {process.returncode}.")


def generate_anki(job: Job, source: Path, output: Path) -> None:
    options = job.options
    front = option_text(options, "front")
    back = option_text(options, "back")
    if not front or not back:
        raise ValueError("Choose both the front and back columns.")
    front_sheet = option_text(options, "frontSheet") or None
    back_sheet = option_text(options, "backSheet") or None
    front_df = read_table(source, front_sheet)
    back_df = read_table(source, back_sheet)
    require_columns(front_df, front)
    require_columns(back_df, back)
    written = 0
    with output.open("w", encoding="utf-8", newline="\n") as handle:
        for index in range(min(len(front_df), len(back_df))):
            front_value = clean_cell(front_df.iloc[index][front])
            back_value = clean_cell(back_df.iloc[index][back])
            if front_value and back_value:
                handle.write(f"{front_value}\t{back_value}\n")
                written += 1
    if not written:
        raise ValueError("No cards were generated. Check for empty selected columns.")
    job.log(f"Exported {written} card(s).")


def create_stack_readme(work_dir: Path, options: dict[str, Any], rows: int, cols: int, mode: str) -> None:
    titles = options.get("cellTitles") or []
    captions = options.get("captions") or []
    if not isinstance(titles, list) or not isinstance(captions, list):
        raise ValueError("Invalid caption data.")
    count = rows * cols
    values = [str(item).replace("\n", " ").strip() for item in titles[:count]]
    values += [""] * (count - len(values))
    cap_count = rows if mode == "h" else cols
    cap_values = [str(item).replace("\n", " ").strip() for item in captions[:cap_count]]
    cap_values += [""] * (cap_count - len(cap_values))
    (work_dir / "readme.txt").write_text("\n".join(values + ["", ""] + cap_values) + "\n", encoding="utf-8")


def add_artifact(job: Job, path: Path, kind: str, format_name: str) -> None:
    artifact_id = path.name
    translation_supported = kind in {"ocr", "ocr_legacy"} and path.suffix.lower() in OCR_TRANSLATABLE_SUFFIXES
    with job.lock:
        job.artifacts = [item for item in job.artifacts if item["id"] != artifact_id]
        job.artifacts.append(
            {
                "id": artifact_id,
                "name": path.name,
                "kind": kind,
                "format": format_name,
                "translationSupported": translation_supported,
            }
        )


def artifact_for_id(job: Job, artifact_id: str) -> tuple[dict[str, Any], Path] | None:
    for artifact in job.artifacts:
        if artifact["id"] == artifact_id:
            # Preserve the original path spelling for callers. macOS exposes
            # both /var and /private/var, and returning only the resolved form
            # makes a valid task file fail equality checks against its root.
            output_dir = job.root / "output"
            path = output_dir / artifact["name"]
            if path.resolve().parent == output_dir.resolve() and path.is_file():
                return artifact, path
    return None


def package_pdf_artifacts(job: Job) -> tuple[Path, str]:
    output_dir = job.root / "output"
    files = sorted(path for path in output_dir.rglob("*") if path.is_file())
    if not files:
        raise RuntimeError("OCR completed but no output files were downloaded.")
    archive = job.root / "output.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        for file in files:
            bundle.write(file, file.relative_to(output_dir))
    return archive, f"{job.source_stem}_ocr_results.zip"


def save_local_artifact(job: Job, path: Path) -> None:
    save_local_artifact_with_log(job, path)


def save_local_artifact_with_log(job: Job, path: Path, *, log: bool = True) -> None:
    if not job.local_save_dir:
        return
    destination = job.local_save_dir / path.name
    shutil.copy2(path, destination)
    if log:
        job.log(f"[LOCAL] Saved {destination.name} beside the original PDF.")


def save_local_directory(job: Job, directory: Path) -> None:
    if not job.local_save_dir:
        return
    destination = job.local_save_dir / directory.name
    shutil.copytree(directory, destination, dirs_exist_ok=True)
    job.log(f"[LOCAL] Saved {destination.name} beside the original PDF.")


def prepare_local_pdf_folder(job: Job) -> None:
    source = local_pdf_source_path(job.options)
    destination = source.parent / source.stem
    if destination.exists():
        raise FileExistsError(f"The output folder already exists: {destination}. Rename the source PDF or move/remove that folder before retrying.")
    input_files = [path for path in (job.root / "input").iterdir() if path.is_file()]
    if len(input_files) != 1:
        raise RuntimeError("The uploaded PDF could not be prepared for local saving.")
    if safe_output_stem(input_files[0].name) != safe_output_stem(source.name):
        raise ValueError("The selected upload and the local PDF path must have the same file name.")
    destination.mkdir()
    try:
        shutil.copy2(input_files[0], destination / source.name)
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise
    job.source_stem = safe_output_stem(source.name)
    job.local_save_dir = destination
    job.log(f"[LOCAL] Created {destination} and copied {source.name}.")


def mathpix_headers() -> dict[str, str]:
    error = mathpix_config_error()
    if error:
        raise RuntimeError(error)
    return {
        "app_id": os.environ["MATHPIX_APP_ID"],
        "app_key": os.environ["MATHPIX_APP_KEY"],
    }


def response_error(response: requests.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text.strip()[:500] or response.reason
    if isinstance(payload, dict):
        return str(payload.get("error") or payload.get("error_info") or payload)
    return str(payload)


def checked_ocr_formats(options: dict[str, Any]) -> list[str]:
    selected = options.get("ocrFormats", list(OCR_OPTIONAL_FORMATS))
    if not isinstance(selected, list):
        raise ValueError("OCR format selection is invalid.")
    invalid = [value for value in selected if value not in OCR_OPTIONAL_FORMATS]
    if invalid:
        raise ValueError(f"Unsupported OCR format: {', '.join(map(str, invalid))}.")
    return [value for value in OCR_OPTIONAL_FORMATS if value in selected]


def wait_for_mathpix_status(job: Job, pdf_id: str) -> None:
    deadline = time.monotonic() + MAX_OCR_WAIT_SECONDS
    last_status = ""
    while time.monotonic() < deadline:
        response = requests.get(f"{MATHPIX_BASE_URL}/pdf/{pdf_id}", headers=mathpix_headers(), timeout=30)
        if not response.ok:
            raise RuntimeError(f"Mathpix status request failed: {response_error(response)}")
        payload = response.json()
        status = str(payload.get("status", "unknown"))
        if status != last_status:
            progress = payload.get("percent_done")
            progress_text = f" ({progress}% complete)" if progress is not None else ""
            job.log(f"[OCR] Mathpix status: {status}{progress_text}")
            last_status = status
        if status == "completed":
            return
        if status == "error":
            raise RuntimeError(f"Mathpix OCR failed: {payload.get('error') or payload}")
        time.sleep(POLL_INTERVAL_SECONDS)
    raise RuntimeError("Mathpix OCR timed out after 20 minutes.")


def wait_for_conversion_status(job: Job, pdf_id: str, formats: list[str]) -> set[str]:
    if not formats:
        return set()
    deadline = time.monotonic() + MAX_OCR_WAIT_SECONDS
    last_states: dict[str, str] = {}
    while time.monotonic() < deadline:
        response = requests.get(f"{MATHPIX_BASE_URL}/converter/{pdf_id}", headers=mathpix_headers(), timeout=30)
        if not response.ok:
            raise RuntimeError(f"Mathpix conversion status request failed: {response_error(response)}")
        payload = response.json()
        status_map = payload.get("conversion_status") or {}
        completed: set[str] = set()
        unresolved = False
        for extension in formats:
            state = str((status_map.get(extension) or {}).get("status", "pending"))
            if state != last_states.get(extension):
                job.log(f"[OCR] {extension} conversion: {state}")
                last_states[extension] = state
            if state == "completed":
                completed.add(extension)
            elif state == "error":
                warning = f"Mathpix could not convert {extension}."
                if warning not in job.warnings:
                    job.warnings.append(warning)
                    job.log(f"[WARNING] {warning}")
            else:
                unresolved = True
        if not unresolved:
            return completed
        time.sleep(POLL_INTERVAL_SECONDS)
    raise RuntimeError("Mathpix format conversion timed out after 20 minutes.")


MARKDOWN_IMAGE_PATTERN = re.compile(r"(!\[[^\]]*\]\()\s*(<?)([^)\s>]+)(>?)([^)]*\))")
LATEX_IMAGE_PATTERN = re.compile(r"(\\includegraphics(?:\[[^\]]*\])?\{)([^}]+)(\})")
MATHPIX_CROPPED_URL_PATTERN = re.compile(r"https?://cdn\.mathpix\.com/cropped/[^\s\"'<>(){}]+")


def safe_bundle_path(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise RuntimeError(f"Mathpix ZIP contains an unsafe path: {name}")
    return path


@dataclass
class LocalAssetStore:
    directory: Path
    url_paths: dict[str, str | None] = field(default_factory=dict)
    asset_paths: set[str] = field(default_factory=set)

    @property
    def reference_prefix(self) -> str:
        return self.directory.name


def canonical_mathpix_url(url: str) -> str:
    parsed = urlsplit(html_unescape(unquote(url)))
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True))), ""))


def mathpix_asset_filename(url: str) -> str:
    parsed = urlsplit(html_unescape(unquote(url)))
    source_name = PurePosixPath(parsed.path).name
    stem, suffix = os.path.splitext(source_name)
    params = dict(parse_qsl(parsed.query, keep_blank_values=True))
    crop_keys = ("height", "width", "top_left_y", "top_left_x")
    if stem and suffix and all(params.get(key) for key in crop_keys):
        return secure_filename(f"{stem}_{'_'.join(params[key] for key in crop_keys)}{suffix}")
    digest = hashlib.sha256(canonical_mathpix_url(url).encode("utf-8")).hexdigest()[:16]
    return secure_filename(f"{stem or 'mathpix-image'}-{digest}{suffix or '.img'}")


def relative_asset_reference(target: str, assets: LocalAssetStore) -> str | None:
    decoded = unquote(target)
    normalized = decoded[2:] if decoded.startswith("./") else decoded
    if normalized.startswith("images/"):
        relative = normalized.removeprefix("images/")
        if relative in assets.asset_paths:
            return f"{assets.reference_prefix}/{relative}"
    return None


def warn_asset_download_failure(job: Job, url: str, detail: str) -> None:
    warning = f"Could not localize Mathpix image {url}: {detail}"
    if warning not in job.warnings:
        job.warnings.append(warning)
        job.log(f"[WARNING] {warning}")


def ensure_mathpix_asset(job: Job, url: str, assets: LocalAssetStore) -> str | None:
    canonical = canonical_mathpix_url(url)
    if canonical in assets.url_paths:
        return assets.url_paths[canonical]
    filename = mathpix_asset_filename(url)
    if filename in assets.asset_paths or (assets.directory / filename).is_file():
        assets.asset_paths.add(filename)
        assets.url_paths[canonical] = filename
        return filename
    try:
        response = requests.get(html_unescape(url), timeout=120)
    except requests.RequestException as exc:
        warn_asset_download_failure(job, url, str(exc))
        assets.url_paths[canonical] = None
        return None
    if not response.ok:
        warn_asset_download_failure(job, url, response_error(response))
        assets.url_paths[canonical] = None
        return None
    destination = assets.directory / filename
    destination.write_bytes(response.content)
    assets.asset_paths.add(filename)
    assets.url_paths[canonical] = filename
    job.log(f"[OCR] Downloaded image {filename}.")
    return filename


def rewrite_text_asset_references(job: Job, text: str, assets: LocalAssetStore) -> str:
    def replace_url(match: re.Match[str]) -> str:
        filename = ensure_mathpix_asset(job, match.group(0), assets)
        return f"{assets.reference_prefix}/{filename}" if filename else match.group(0)

    text = MATHPIX_CROPPED_URL_PATTERN.sub(replace_url, text)

    def replace_markdown(match: re.Match[str]) -> str:
        reference = relative_asset_reference(match.group(3), assets)
        return f"{match.group(1)}{match.group(2)}{reference or match.group(3)}{match.group(4)}{match.group(5)}"

    def replace_latex(match: re.Match[str]) -> str:
        reference = relative_asset_reference(match.group(2), assets)
        return f"{match.group(1)}{reference or match.group(2)}{match.group(3)}"

    return LATEX_IMAGE_PATTERN.sub(replace_latex, MARKDOWN_IMAGE_PATTERN.sub(replace_markdown, text))


MARKDOWN_REPAIR_SUFFIXES = {"md", "mmd"}
TRANSLATED_MARKDOWN_SUFFIXES = {".md", ".mmd"}
MARKDOWN_IMAGE_TOKEN_PATTERNS = (
    re.compile(r"!\[[^\]\n]*\]\((?:<[^>\n]+>|(?:\\.|[^)\n])*)\)"),
    re.compile(r"!\[\[[^\]\n]+\]\]"),
    re.compile(r"\\includegraphics(?:\[[^\]\n]*\])?\{[^}\n]+\}"),
)
FOOTNOTE_DEFINITION_PATTERN = re.compile(r"(?m)^ {0,3}\[\^([^\]\r\n]+)\]:")
FOOTNOTE_REFERENCE_PATTERN = re.compile(r"\[\^([^\]\r\n]+)\]")
STANDALONE_FOOTNOTE_PATTERN = re.compile(r"(?m)^\s*\[\^[^\]\r\n]+\]\s*$")
VISUAL_FOOTNOTE_PATTERN = re.compile(r"\$\{\s*\}\^\{\s*[0-9A-Za-z]+\s*\}\$")
FENCE_LINE_PATTERN = re.compile(r"^ {0,3}(`{3,}|~{3,})(?:[^`~]*)$")
# A malformed backtick fence has prose immediately before its marker. The
# prefix cannot contain an earlier triple-backtick run, so inline triple
# backticks are not mistaken for a fence.
GLUED_BACKTICK_FENCE_PATTERN = re.compile(
    r"^(?P<prefix>(?:(?!`{3})[^\r\n])+)(?P<marker>`{3,})(?P<suffix>[^`]*)$"
)
GLUED_BACKTICK_CLOSING_PATTERN = re.compile(
    r"^(?P<prefix>(?:(?!`{3})[^\r\n])+)(?P<marker>`{3,})(?P<suffix>[ \t]*)$"
)
LATEX_ENVIRONMENT_PATTERN = re.compile(r"\\(begin|end)\{([A-Za-z*]+)\}")
ALGORITHM_LABEL_PATTERN = re.compile(r"\b(?:algorithm|pseudo-?code)\b", re.IGNORECASE)
ALGORITHM_MATH_PATTERN = re.compile(
    r"(?:\\(?:pi|alpha|beta|gamma|arg|max|min|sum|prod|gets|leftarrow|in|mathbb|mathcal)|"
    r"\$[^$\n]+\$|\\\([^\n]+\\\))"
)
TRANSLATION_MATH_MARKDOWN_BLOCK_PATTERN = re.compile(
    r"^\s*(?:#{1,6}\s|!\[|\[\^[^\]]+\]:|[-+*]\s+|\d+[.)]\s+|>\s+|\|)"
)
TRANSLATION_CHINESE_PROSE_START_PATTERN = re.compile(r"^[\u3400-\u9fff（【“‘]")
TRANSLATION_FORMULA_START_PATTERN = re.compile(
    r"\\(?:operatorname|mathcal|mathrm|mathbf|mathbb|frac|sqrt|begin|left|sum|prod|int|nabla|partial)\b"
)


@dataclass(frozen=True)
class MarkdownRepairRegion:
    start: int
    end: int
    reason: str


class TranslationMarkdownPostprocessError(RuntimeError):
    """A generated Markdown translation failed structural math validation."""

    def __init__(self, diagnostics: dict[Path, list[str]], preserved_outputs: list[Path]) -> None:
        self.diagnostics = diagnostics
        self.preserved_outputs = preserved_outputs
        detail = "; ".join(
            f"{path.name}: {', '.join(errors)}"
            for path, errors in diagnostics.items()
        )
        names = ", ".join(path.name for path in preserved_outputs)
        super().__init__(f"Markdown math validation failed; kept raw output as {names}. {detail}")


class MarkdownRepairOutputError(RuntimeError):
    """An independently repaired Markdown file could not pass final validation."""

    def __init__(self, diagnostics: dict[Path, list[str]], preserved_outputs: list[Path]) -> None:
        self.diagnostics = diagnostics
        self.preserved_outputs = preserved_outputs
        detail = "; ".join(f"{path.name}: {', '.join(errors)}" for path, errors in diagnostics.items())
        super().__init__(f"Markdown repair failed validation; kept original input as {', '.join(path.name for path in preserved_outputs)}. {detail}")


def markdown_image_references(text: str) -> list[str]:
    """Return complete image tokens in document order for exact invariants."""
    matches: list[tuple[int, int, str]] = []
    for pattern in MARKDOWN_IMAGE_TOKEN_PATTERNS:
        matches.extend((match.start(), match.end(), match.group(0)) for match in pattern.finditer(text))
    matches.sort(key=lambda item: (item[0], item[1]))
    result: list[str] = []
    previous_end = -1
    for start, end, value in matches:
        if start < previous_end:
            continue
        result.append(value)
        previous_end = end
    return result


@dataclass(frozen=True)
class GithubMarkdownReference:
    start: int
    end: int
    target: str


def mask_markdown_code(text: str) -> str:
    """Keep source offsets stable while hiding fenced and inline code."""
    characters = list(text)
    fence: tuple[str, int] | None = None
    offset = 0
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        fence_match = re.match(r"^ {0,3}(`{3,}|~{3,})", body)
        hide_line = fence is not None or fence_match is not None
        if fence is None and fence_match:
            marker = fence_match.group(1)
            fence = (marker[0], len(marker))
        elif fence is not None:
            marker_char, marker_length = fence
            if re.match(rf"^ {{0,3}}{re.escape(marker_char)}{{{marker_length},}}\s*$", body):
                fence = None
        if hide_line:
            for index in range(offset, offset + len(line)):
                if characters[index] not in "\r\n":
                    characters[index] = " "
        else:
            for match in re.finditer(r"(?<!\\)(`+)([^\r\n]*?)(?<!`)\1(?!`)", line):
                for index in range(offset + match.start(), offset + match.end()):
                    characters[index] = " "
        offset += len(line)
    return "".join(characters)


def normalized_markdown_label(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip()).casefold()


def contains_unescaped_whitespace(value: str) -> bool:
    escaped = False
    for character in value:
        if escaped:
            escaped = False
        elif character == "\\":
            escaped = True
        elif character.isspace():
            return True
    return False


def is_unbracketed_local_image_destination(value: str) -> bool:
    """Return whether a Markdown destination can be safely wrapped in ``<...>``."""
    if not contains_unescaped_whitespace(value):
        return False
    decoded = local_markdown_asset_path(value)
    return decoded is not None and PurePosixPath(decoded).suffix.lower() in GITHUB_IMAGE_SUFFIXES


def github_inline_markdown_image_references(text: str) -> list[GithubMarkdownReference]:
    """Extract inline image destinations, including unbracketed local paths with spaces."""
    masked = mask_markdown_code(text)
    references: list[GithubMarkdownReference] = []

    inline_start_pattern = re.compile(r"!\[(?:\\.|[^\]\r\n])*\]\(\s*")
    for match in inline_start_pattern.finditer(masked):
        cursor = match.end()
        if cursor >= len(masked):
            continue
        if masked[cursor] == "<":
            end = masked.find(">", cursor + 1)
            if end > cursor + 1:
                references.append(GithubMarkdownReference(cursor + 1, end, text[cursor + 1:end]))
            continue
        start = cursor
        depth = 0
        escaped = False
        closing = -1
        while cursor < len(masked):
            char = masked[cursor]
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == "(":
                depth += 1
            elif char == ")":
                if depth == 0:
                    closing = cursor
                    break
                depth -= 1
            cursor += 1
        content_end = closing if closing >= 0 else cursor
        raw_end = start + len(text[start:content_end].rstrip())
        if raw_end <= start:
            continue
        raw_target = text[start:raw_end]
        if is_unbracketed_local_image_destination(raw_target):
            references.append(GithubMarkdownReference(start, raw_end, raw_target))
            continue

        target_end = start
        depth = 0
        escaped = False
        while target_end < raw_end:
            char = masked[target_end]
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == "(":
                depth += 1
            elif char == ")" and depth:
                depth -= 1
            elif char.isspace() and depth == 0:
                break
            target_end += 1
        if target_end > start:
            references.append(GithubMarkdownReference(start, target_end, text[start:target_end]))
    return references


def github_markdown_references(text: str) -> list[GithubMarkdownReference]:
    masked = mask_markdown_code(text)
    references = github_inline_markdown_image_references(text)

    def add_span(match: re.Match[str], group: str | int) -> None:
        start, end = match.span(group)
        if start >= 0 and end > start:
            references.append(GithubMarkdownReference(start, end, text[start:end]))

    latex_pattern = re.compile(r"\\includegraphics(?:\[[^\]\r\n]*\])?\{(?P<path>[^}\r\n]+)\}")
    for match in latex_pattern.finditer(masked):
        add_span(match, "path")

    wiki_pattern = re.compile(r"!\[\[(?P<path>[^\]|\r\n]+)(?:\|[^\]\r\n]*)?\]\]")
    for match in wiki_pattern.finditer(masked):
        add_span(match, "path")

    image_labels: set[str] = set()
    image_reference_pattern = re.compile(r"!\[(?P<alt>[^\]\r\n]*)\](?:\[(?P<label>[^\]\r\n]*)\])?")
    for match in image_reference_pattern.finditer(masked):
        if masked[match.end():match.end() + 1] == "(" or masked[match.start() + 2:match.start() + 3] == "[":
            continue
        label = match.group("label")
        image_labels.add(normalized_markdown_label(label if label not in {None, ""} else match.group("alt")))
    definition_pattern = re.compile(
        r"(?m)^ {0,3}\[(?P<label>[^\]\r\n]+)\]:\s*(?:<(?P<angle>[^>\r\n]+)>|(?P<plain>\S+))"
    )
    for match in definition_pattern.finditer(masked):
        if normalized_markdown_label(match.group("label")) in image_labels:
            add_span(match, "angle" if match.group("angle") is not None else "plain")

    tag_pattern = re.compile(r"<img\b[^>]*>", flags=re.IGNORECASE)
    attribute_pattern = re.compile(
        r"\b(?P<name>src|srcset)\s*=\s*(?:(?P<quote>['\"])(?P<quoted>.*?)\2|(?P<bare>[^\s>]+))",
        flags=re.IGNORECASE,
    )
    for tag_match in tag_pattern.finditer(masked):
        original_tag = text[tag_match.start():tag_match.end()]
        for attribute in attribute_pattern.finditer(original_tag):
            value_group = "quoted" if attribute.group("quoted") is not None else "bare"
            value_start, _value_end = attribute.span(value_group)
            value = attribute.group(value_group)
            absolute_start = tag_match.start() + value_start
            if attribute.group("name").casefold() == "src":
                references.append(GithubMarkdownReference(absolute_start, absolute_start + len(value), value))
                continue
            if "data:" in value.casefold():
                continue
            for candidate in re.finditer(r"(?:^|,)\s*(?P<path>[^,\s]+)", value):
                start, end = candidate.span("path")
                references.append(GithubMarkdownReference(absolute_start + start, absolute_start + end, candidate.group("path")))

    unique: dict[tuple[int, int], GithubMarkdownReference] = {}
    for reference in references:
        unique[(reference.start, reference.end)] = reference
    ordered = sorted(unique.values(), key=lambda item: (item.start, item.end))
    previous_end = -1
    for reference in ordered:
        if reference.start < previous_end:
            raise ValueError("Markdown 图片引用存在无法安全解析的重叠结构。")
        previous_end = reference.end
    return ordered


def local_markdown_asset_path(target: str) -> str | None:
    value = html_unescape(target.strip())
    value = re.sub(r"\\([ !#$%&'()*+,\-./:;<=>?@\[\\\]^_`{|}~])", r"\1", value)
    if not value or value.startswith(("/", "#", "//")):
        return None
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc:
        return None
    decoded = unquote(parsed.path).replace("\\", "/")
    return decoded or None


def repair_unbracketed_markdown_image_paths(text: str) -> tuple[str, int]:
    """Wrap unambiguous local image destinations containing spaces in angle brackets."""
    replacements: list[tuple[int, int, str]] = []
    for reference in github_inline_markdown_image_references(text):
        if reference.start > 0 and text[reference.start - 1] == "<":
            continue
        if is_unbracketed_local_image_destination(reference.target):
            replacements.append((reference.start, reference.end, f"<{reference.target}>"))
    for start, end, replacement in reversed(replacements):
        text = text[:start] + replacement + text[end:]
    return text, len(replacements)


def github_markdown_asset_relative_path(source_relative: Path, target: str) -> Path | None:
    """Convert one local Markdown target to a validated task-relative path."""
    decoded = local_markdown_asset_path(target)
    if decoded is None:
        return None
    combined = PurePosixPath(source_relative.parent.as_posix()) / PurePosixPath(decoded)
    parts: list[str] = []
    for part in combined.parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if not parts:
                raise ValueError(f"图片引用越出了上传目录：{target}")
            parts.pop()
            continue
        if ":" in part or "\x00" in part:
            raise ValueError(f"图片引用路径无效：{target}")
        parts.append(part)
    if not parts:
        raise ValueError(f"图片引用路径无效：{target}")
    return Path(*parts)


def unicode_equivalent_uploaded_path(input_root: Path, relative: Path) -> Path:
    """Resolve a validated upload path while tolerating NFC/NFD filename differences.

    Markdown generated on one platform may spell an accented filename with a
    single code point while browsers upload the same visible filename as a base
    character plus combining mark. Only exact Unicode-normalized component
    matches are considered, and ambiguous matches are rejected.
    """
    current = input_root
    for index, part in enumerate(relative.parts):
        exact = current / part
        if exact.exists():
            current = exact
            continue
        if not current.is_dir():
            return current.joinpath(*relative.parts[index:])
        normalized_part = unicodedata.normalize("NFC", part)
        matches = [
            child for child in current.iterdir()
            if unicodedata.normalize("NFC", child.name) == normalized_part
        ]
        if len(matches) == 1:
            current = matches[0]
            continue
        if len(matches) > 1:
            raise ValueError(f"图片引用路径存在无法确定的 Unicode 名称匹配：{relative.as_posix()}")
        return current.joinpath(*relative.parts[index:])
    return current


def resolve_github_markdown_asset(input_root: Path, source_relative: Path, target: str) -> tuple[Path, Path] | None:
    relative = github_markdown_asset_relative_path(source_relative, target)
    if relative is None:
        return None
    candidate = unicode_equivalent_uploaded_path(input_root, relative).resolve()
    resolved_root = input_root.resolve()
    if resolved_root not in candidate.parents:
        raise ValueError(f"图片引用越出了上传目录：{target}")
    if candidate.suffix.lower() not in GITHUB_IMAGE_SUFFIXES:
        raise ValueError(f"不支持的 Markdown 图片格式：{target}")
    if not candidate.is_file():
        raise ValueError(f"Markdown 引用的图片未随任务提供：{target}")
    return candidate, relative


def log_github_asset_resolution_diagnostics(
    job: Job,
    input_root: Path,
    source_relative: Path,
    target: str,
) -> None:
    """Write evidence from the task's uploaded files when an asset cannot resolve."""
    job.log(f"[GITHUB][DIAGNOSTIC] Markdown image target (literal): {target!r}")
    job.log(f"[GITHUB][DIAGNOSTIC] Markdown source in task: {source_relative.as_posix()!r}")
    try:
        relative = github_markdown_asset_relative_path(source_relative, target)
    except ValueError as exc:
        job.log(f"[GITHUB][DIAGNOSTIC] Target cannot form a safe task-relative path: {exc}")
        return
    if relative is None:
        job.log("[GITHUB][DIAGNOSTIC] Target is remote, root-relative, fragment-only, or empty; no local file is expected.")
        return
    candidate = unicode_equivalent_uploaded_path(input_root, relative)
    job.log(f"[GITHUB][DIAGNOSTIC] Expected uploaded path: {relative.as_posix()!r}")
    job.log(
        "[GITHUB][DIAGNOSTIC] Resolved candidate status: "
        f"exists={candidate.exists()}, is_file={candidate.is_file()}, is_dir={candidate.is_dir()}."
    )
    uploaded_images = [
        path for path in input_root.rglob("*")
        if path.is_file() and path.suffix.lower() in GITHUB_IMAGE_SUFFIXES
    ]
    job.log(f"[GITHUB][DIAGNOSTIC] Image files actually uploaded with this task: {len(uploaded_images)}.")
    exact_name_matches = [
        path.relative_to(input_root).as_posix()
        for path in uploaded_images
        if path.name == relative.name
    ]
    normalized_name_matches = [
        path.relative_to(input_root).as_posix()
        for path in uploaded_images
        if unicodedata.normalize("NFC", path.name) == unicodedata.normalize("NFC", relative.name)
    ]
    if exact_name_matches:
        job.log(
            "[GITHUB][DIAGNOSTIC] Uploaded files with the same filename: "
            + ", ".join(exact_name_matches[:10])
            + (" (additional matches omitted)" if len(exact_name_matches) > 10 else "")
        )
    elif normalized_name_matches:
        job.log(
            "[GITHUB][DIAGNOSTIC] Uploaded files with a Unicode-equivalent filename: "
            + ", ".join(normalized_name_matches[:10])
            + (" (additional matches omitted)" if len(normalized_name_matches) > 10 else "")
        )
    else:
        job.log("[GITHUB][DIAGNOSTIC] No uploaded image has the requested filename.")


def github_raw_url(repository: str, commit_sha: str, path: str) -> str:
    owner, repo = repository.split("/", 1)
    encoded_path = "/".join(quote(part, safe="") for part in PurePosixPath(path).parts)
    return f"https://raw.githubusercontent.com/{quote(owner, safe='')}/{quote(repo, safe='')}/{commit_sha}/{encoded_path}"


def publish_github_image_blobs(
    config: dict[str, str],
    images: dict[str, bytes],
    source_name: str,
) -> dict[str, Any]:
    repository = config["repository"]
    repository_info = github_api_request("GET", f"/repos/{repository}", config)
    if repository_info.get("private") is not False:
        raise RuntimeError("Raw 图片链接要求公开仓库；当前仓库不是公开仓库。")
    encoded_branch = quote(config["branch"], safe="")
    reference = github_api_request("GET", f"/repos/{repository}/git/ref/heads/{encoded_branch}", config)
    head_sha = str((reference.get("object") or {}).get("sha") or "")
    if not head_sha:
        raise RuntimeError("GitHub 分支没有可用的 HEAD commit。")
    commit = github_api_request("GET", f"/repos/{repository}/git/commits/{head_sha}", config)
    base_tree_sha = str((commit.get("tree") or {}).get("sha") or "")
    if not base_tree_sha:
        raise RuntimeError("GitHub HEAD commit 没有可用的 tree。")
    current_tree = github_api_request("GET", f"/repos/{repository}/git/trees/{base_tree_sha}?recursive=1", config)
    current_paths = {
        str(item.get("path")): str(item.get("sha"))
        for item in current_tree.get("tree", [])
        if isinstance(item, dict) and item.get("type") == "blob" and item.get("path") and item.get("sha")
    }
    blob_shas: dict[str, str] = {}
    for path, content in images.items():
        blob = github_api_request(
            "POST",
            f"/repos/{repository}/git/blobs",
            config,
            json_body={"content": base64.b64encode(content).decode("ascii"), "encoding": "base64"},
            expected={201},
        )
        sha = str(blob.get("sha") or "")
        if not sha:
            raise RuntimeError(f"GitHub 未返回图片 blob SHA：{path}")
        blob_shas[path] = sha
    changed = {path: sha for path, sha in blob_shas.items() if current_paths.get(path) != sha}
    reused_count = len(blob_shas) - len(changed)
    final_sha = head_sha
    commit_created = False
    if changed:
        tree = github_api_request(
            "POST",
            f"/repos/{repository}/git/trees",
            config,
            json_body={
                "base_tree": base_tree_sha,
                "tree": [
                    {"path": path, "mode": "100644", "type": "blob", "sha": sha}
                    for path, sha in sorted(changed.items())
                ],
            },
            expected={201},
        )
        tree_sha = str(tree.get("sha") or "")
        if not tree_sha:
            raise RuntimeError("GitHub 未返回新 tree SHA。")
        created_commit = github_api_request(
            "POST",
            f"/repos/{repository}/git/commits",
            config,
            json_body={
                "message": f"Publish Markdown images for {source_name}",
                "tree": tree_sha,
                "parents": [head_sha],
            },
            expected={201},
        )
        final_sha = str(created_commit.get("sha") or "")
        if not final_sha:
            raise RuntimeError("GitHub 未返回新 commit SHA。")
        github_api_request(
            "PATCH",
            f"/repos/{repository}/git/refs/heads/{encoded_branch}",
            config,
            json_body={"sha": final_sha, "force": False},
        )
        commit_created = True
    return {
        "commitSha": final_sha,
        "commitUrl": f"https://github.com/{repository}/commit/{final_sha}",
        "uploadedImageCount": len(changed),
        "reusedImageCount": reused_count,
        "commitCreated": commit_created,
    }


def run_github_markdown_publish(job: Job) -> None:
    input_root = job.root / "input"
    markdown_sources = [
        path for path in input_root.rglob("*")
        if path.is_file() and path.suffix.lower() in GITHUB_MARKDOWN_SUFFIXES
    ]
    if len(markdown_sources) != 1:
        raise ValueError("Markdown 图片发布要求恰好上传一份 .md 或 .mmd 文档。")
    source = markdown_sources[0]
    source_relative = source.relative_to(input_root)
    text = source.read_text(encoding="utf-8")
    references = github_markdown_references(text)
    resolved: list[tuple[GithubMarkdownReference, Path]] = []
    for reference in references:
        try:
            asset = resolve_github_markdown_asset(input_root, source_relative, reference.target)
        except ValueError:
            log_github_asset_resolution_diagnostics(job, input_root, source_relative, reference.target)
            raise
        if asset is not None:
            resolved.append((reference, asset[0]))
    if not resolved:
        raise ValueError("Markdown 中没有找到可发布的本地图片引用。")
    config = github_config()
    publication_date = datetime.fromtimestamp(job.created_at).astimezone().strftime("%Y/%m/%d")
    remote_by_file: dict[Path, str] = {}
    images: dict[str, bytes] = {}
    for _reference, asset in resolved:
        if asset in remote_by_file:
            continue
        content = asset.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        remote_path = PurePosixPath(
            config["imageRoot"],
            publication_date,
            f"{digest}{asset.suffix.lower()}",
        ).as_posix()
        existing = images.get(remote_path)
        if existing is not None and existing != content:
            raise RuntimeError(f"图片哈希路径发生内容冲突：{remote_path}")
        images[remote_path] = content
        remote_by_file[asset] = remote_path
    job.phase = "github_publish"
    job.log(f"[GITHUB] 已验证 {len(resolved)} 个引用，对应 {len(images)} 张唯一图片。")
    publication = publish_github_image_blobs(config, images, source.name)
    replacements = [
        (reference.start, reference.end, github_raw_url(config["repository"], publication["commitSha"], remote_by_file[asset]))
        for reference, asset in resolved
    ]
    rewritten = text
    for start, end, replacement in sorted(replacements, reverse=True):
        rewritten = rewritten[:start] + replacement + rewritten[end:]
    output_root = job.root / "output"
    output_root.mkdir(exist_ok=True)
    output = output_root / f"{safe_output_stem(source.name)}_github{source.suffix.lower()}"
    write_durable_text(output, rewritten)
    job.publication = publication
    job.download_path = output
    job.download_name = output.name
    job.phase = "github_publish_complete"
    action = "已创建单次提交" if publication["commitCreated"] else "远端内容已存在，未创建空提交"
    job.log(
        f"[GITHUB] {action}：上传 {publication['uploadedImageCount']}，复用 {publication['reusedImageCount']}。"
    )


def markdown_line_and_ending(raw_line: str) -> tuple[str, str]:
    """Split one physical line without normalizing the document's line ending."""
    if raw_line.endswith("\r\n"):
        return raw_line[:-2], "\r\n"
    if raw_line.endswith("\n") or raw_line.endswith("\r"):
        return raw_line[:-1], raw_line[-1]
    return raw_line, ""


def glued_backtick_fence_match(line: str, opening: tuple[str, int] | None) -> re.Match[str] | None:
    """Recognize a prose-glued backtick opening or an unambiguous closing fence."""
    pattern = GLUED_BACKTICK_FENCE_PATTERN if opening is None else GLUED_BACKTICK_CLOSING_PATTERN
    match = pattern.match(line)
    if not match or not match.group("prefix").strip():
        return None
    if opening is not None and len(match.group("marker")) < opening[1]:
        return None
    return match


def deterministic_markdown_repairs(text: str) -> str:
    """Apply only repairs whose intended output is structurally unambiguous."""
    text, _image_path_repairs = repair_unbracketed_markdown_image_paths(text)
    text = re.sub(
        r"(\[\^[^\]\r\n]+\])[ \t]*(?=(?:```|~~~))",
        r"\1\n\n",
        text,
    )
    separator = "\r\n" if "\r\n" in text else "\n"
    output: list[str] = []
    opening: tuple[str, int] | None = None
    for raw_line in text.splitlines(keepends=True):
        line, ending = markdown_line_and_ending(raw_line)
        if opening is None:
            valid_fence = FENCE_LINE_PATTERN.match(line.rstrip())
            if valid_fence:
                marker = valid_fence.group(1)
                opening = (marker[0], len(marker))
                output.append(raw_line)
                continue
            glued = glued_backtick_fence_match(line, None)
            if glued:
                output.extend((
                    glued.group("prefix") + (ending or separator),
                    separator,
                    glued.group("marker") + glued.group("suffix") + ending,
                ))
                opening = ("`", len(glued.group("marker")))
                continue
            output.append(raw_line)
            continue

        if opening[0] == "`":
            glued = glued_backtick_fence_match(line, opening)
            if glued:
                output.extend((
                    glued.group("prefix") + (ending or separator),
                    glued.group("marker") + glued.group("suffix") + ending,
                ))
                opening = None
                continue
        output.append(raw_line)
        valid_fence = FENCE_LINE_PATTERN.match(line.rstrip())
        if valid_fence:
            marker = valid_fence.group(1)
            if marker[0] == opening[0] and len(marker) >= opening[1]:
                opening = None
    return "".join(output)


def glued_backtick_fence_lines(text: str) -> list[int]:
    """Return malformed prose-glued backtick fence line numbers outside code content."""
    lines: list[int] = []
    opening: tuple[str, int] | None = None
    for index, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.rstrip()
        if opening is None:
            valid_fence = FENCE_LINE_PATTERN.match(line)
            if valid_fence:
                marker = valid_fence.group(1)
                opening = (marker[0], len(marker))
                continue
            glued = glued_backtick_fence_match(line, None)
            if glued:
                lines.append(index)
                opening = ("`", len(glued.group("marker")))
            continue

        if opening[0] == "`" and glued_backtick_fence_match(line, opening):
            lines.append(index)
            opening = None
            continue
        valid_fence = FENCE_LINE_PATTERN.match(line)
        if valid_fence:
            marker = valid_fence.group(1)
            if marker[0] == opening[0] and len(marker) >= opening[1]:
                opening = None
    return lines


def markdown_fence_blocks(text: str) -> tuple[list[tuple[int, int]], int | None]:
    """Return fenced line ranges and the opening line of an unmatched fence."""
    blocks: list[tuple[int, int]] = []
    opening: tuple[str, int, int] | None = None
    for index, raw_line in enumerate(text.splitlines()):
        match = FENCE_LINE_PATTERN.match(raw_line.rstrip())
        if not match:
            continue
        marker = match.group(1)
        if opening is None:
            opening = (marker[0], len(marker), index)
        elif marker[0] == opening[0] and len(marker) >= opening[1]:
            blocks.append((opening[2], index))
            opening = None
    return blocks, opening[2] if opening else None


def markdown_without_fenced_code(text: str) -> str:
    output: list[str] = []
    opening: tuple[str, int] | None = None
    for raw_line in text.splitlines():
        match = FENCE_LINE_PATTERN.match(raw_line.rstrip())
        if opening is None:
            if match:
                opening = (match.group(1)[0], len(match.group(1)))
                output.append("")
            else:
                output.append(raw_line)
            continue
        if match and match.group(1)[0] == opening[0] and len(match.group(1)) >= opening[1]:
            opening = None
        output.append("")
    return "\n".join(output)


def markdown_repair_errors(original: str, repaired: str) -> list[str]:
    errors: list[str] = []
    normalized_original, _image_path_repairs = repair_unbracketed_markdown_image_paths(original)
    if markdown_image_references(normalized_original) != markdown_image_references(repaired):
        errors.append("localized image references changed")

    blocks, unmatched_fence = markdown_fence_blocks(repaired)
    if unmatched_fence is not None:
        errors.append(f"unclosed fenced block at line {unmatched_fence + 1}")
    for line_number in glued_backtick_fence_lines(repaired):
        errors.append(f"text precedes a backtick fence at line {line_number}")

    lines = repaired.splitlines()
    for start, end in blocks:
        content = "\n".join(lines[start:end + 1])
        if ALGORITHM_LABEL_PATTERN.search(content) and ALGORITHM_MATH_PATTERN.search(content):
            errors.append(f"algorithm with math remains fenced at line {start + 1}")

    structural_text = markdown_without_fenced_code(repaired)
    if len(re.findall(r"(?<!\\)\$\$", structural_text)) % 2:
        errors.append("display-math $$ delimiters are unbalanced")
    if structural_text.count(r"\[") != structural_text.count(r"\]"):
        errors.append("display-math \\[ and \\] delimiters are unbalanced")

    environment_stack: list[str] = []
    for match in LATEX_ENVIRONMENT_PATTERN.finditer(structural_text):
        action, name = match.groups()
        if action == "begin":
            environment_stack.append(name)
        elif not environment_stack or environment_stack[-1] != name:
            errors.append(f"LaTeX environment closes out of order: {name}")
            break
        else:
            environment_stack.pop()
    if environment_stack:
        errors.append(f"unclosed LaTeX environment: {environment_stack[-1]}")

    definitions = FOOTNOTE_DEFINITION_PATTERN.findall(repaired)
    if len(definitions) != len(set(definitions)):
        errors.append("footnote definition IDs are not unique")
    reference_text = FOOTNOTE_DEFINITION_PATTERN.sub("", repaired)
    references = set(FOOTNOTE_REFERENCE_PATTERN.findall(reference_text))
    if references != set(definitions):
        missing = sorted(references - set(definitions))
        unused = sorted(set(definitions) - references)
        detail = []
        if missing:
            detail.append(f"missing definitions: {', '.join(missing)}")
        if unused:
            detail.append(f"unreferenced definitions: {', '.join(unused)}")
        errors.append("footnote references and definitions differ" + (f" ({'; '.join(detail)})" if detail else ""))
    if STANDALONE_FOOTNOTE_PATTERN.search(repaired):
        errors.append("standalone footnote references remain")
    if re.search(r"\[\^[^\]\r\n]+\][ \t]*(?:```|~~~)", repaired):
        errors.append("a footnote reference remains attached to a fence")
    if VISUAL_FOOTNOTE_PATTERN.search(repaired):
        errors.append("visual superscript footnote markers remain")
    return errors


def markdown_fence_opening(line: str, opening: tuple[str, int] | None) -> tuple[str, int] | None:
    """Advance a CommonMark-style fenced-code state without parsing its contents."""
    match = FENCE_LINE_PATTERN.match(line.rstrip("\r\n"))
    if opening is None:
        return (match.group(1)[0], len(match.group(1))) if match else None
    if match and match.group(1)[0] == opening[0] and len(match.group(1)) >= opening[1]:
        return None
    return opening


def translation_line_ending(raw_line: str) -> tuple[str, str]:
    if raw_line.endswith("\r\n"):
        return raw_line[:-2], "\r\n"
    if raw_line.endswith("\n") or raw_line.endswith("\r"):
        return raw_line[:-1], raw_line[-1]
    return raw_line, ""


def translation_looks_like_prose(line: str) -> bool:
    """Conservatively identify translated Markdown prose that cannot be math."""
    text = line.strip()
    if not text:
        return False
    if TRANSLATION_MATH_MARKDOWN_BLOCK_PATTERN.match(text):
        return True
    return bool(TRANSLATION_CHINESE_PROSE_START_PATTERN.match(text)) and bool(re.search(r"[。；：，]", text))


def translation_next_nonblank(lines: list[str], start: int) -> tuple[int | None, str | None]:
    for index in range(start, len(lines)):
        stripped = lines[index].strip()
        if stripped:
            return index, stripped
    return None, None


def normalize_translation_math_boundaries(lines: list[str]) -> tuple[list[str], list[str]]:
    """Split only line-boundary ``$$`` delimiters that are glued to content."""
    output: list[str] = []
    changes: list[str] = []
    fence: tuple[str, int] | None = None
    for line_number, raw_line in enumerate(lines, 1):
        prior_fence = fence
        fence = markdown_fence_opening(raw_line, fence)
        if prior_fence is not None or fence is not None:
            output.append(raw_line)
            continue
        line, ending = translation_line_ending(raw_line)
        if line.strip() == "$$":
            output.append(raw_line)
            continue
        parts = [line]
        prefix = re.match(r"^(\s*)\$\$(\S.*)$", parts[0])
        if prefix:
            parts = [prefix.group(1) + "$$", prefix.group(2).rstrip()]
            changes.append(f"L{line_number}: split a line-start $$ delimiter from adjacent content")
        suffix = re.match(r"^(.*\S)\$\$\s*$", parts[-1])
        if suffix and not parts[-1].lstrip().startswith("$$"):
            parts[-1:] = [suffix.group(1).rstrip(), "$$"]
            changes.append(f"L{line_number}: split a line-end $$ delimiter from adjacent content")
        for index, part in enumerate(parts):
            part_ending = ending if index == len(parts) - 1 else (ending or "\n")
            output.append(part + part_ending)
    return output, changes


def split_translation_prose_and_formula(line: str) -> tuple[str, str] | None:
    """Find a prose colon followed by a bare display formula, outside inline math."""
    in_dollar_math = False
    in_parenthesis_math = False
    index = 0
    while index < len(line):
        if line.startswith(r"\(", index):
            in_parenthesis_math = True
            index += 2
            continue
        if line.startswith(r"\)", index):
            in_parenthesis_math = False
            index += 2
            continue
        if line.startswith("$$", index):
            index += 2
            continue
        character = line[index]
        if character == "$" and (index == 0 or line[index - 1] != "\\"):
            in_dollar_math = not in_dollar_math
            index += 1
            continue
        if character in "：:" and not in_dollar_math and not in_parenthesis_math:
            formula = line[index + 1:].lstrip()
            if TRANSLATION_FORMULA_START_PATTERN.match(formula):
                return line[:index + 1].rstrip(), formula.rstrip()
        index += 1
    return None


def repair_translation_math_structure(lines: list[str]) -> tuple[list[str], list[str]]:
    """Apply high-confidence display-math repairs while tracking Markdown state."""
    output: list[str] = []
    changes: list[str] = []
    fence: tuple[str, int] | None = None
    in_math = False
    index = 0
    while index < len(lines):
        raw_line = lines[index]
        prior_fence = fence
        fence = markdown_fence_opening(raw_line, fence)
        if prior_fence is not None or fence is not None:
            output.append(raw_line)
            index += 1
            continue
        line, ending = translation_line_ending(raw_line)
        stripped = line.strip()
        if not in_math:
            split = split_translation_prose_and_formula(line)
            if split is not None:
                _, next_line = translation_next_nonblank(lines, index + 1)
                if next_line == "$$":
                    prose, formula = split
                    output.extend([prose + (ending or "\n"), "\n", "$$\n", formula + (ending or "\n")])
                    in_math = True
                    changes.append(f"L{index + 1}: inserted a missing display-math opening $$")
                    index += 1
                    continue
        if stripped == "$$":
            if not in_math:
                _, next_line = translation_next_nonblank(lines, index + 1)
                if next_line is not None and translation_looks_like_prose(next_line):
                    changes.append(f"L{index + 1}: removed a stray display-math opening $$ before prose")
                    index += 1
                    continue
                in_math = True
            else:
                in_math = False
            output.append(raw_line)
            index += 1
            continue
        output.append(raw_line)
        index += 1
    return output, changes


def lint_translation_math_structure(lines: list[str]) -> list[str]:
    """Report remaining ambiguous math damage without attempting a speculative edit."""
    errors: list[str] = []
    fence: tuple[str, int] | None = None
    in_math = False
    opener_line: int | None = None
    for line_number, raw_line in enumerate(lines, 1):
        prior_fence = fence
        fence = markdown_fence_opening(raw_line, fence)
        if prior_fence is not None or fence is not None:
            continue
        stripped = raw_line.strip()
        if stripped == "$$":
            in_math = not in_math
            opener_line = line_number if in_math else None
            continue
        if not in_math:
            continue
        if TRANSLATION_MATH_MARKDOWN_BLOCK_PATTERN.match(stripped):
            errors.append(f"L{line_number}: Markdown block structure appears inside display math: {stripped[:80]}")
        if re.search(r"(?<!\\)#", raw_line):
            errors.append(f"L{line_number}: unescaped # appears inside display math")
        if translation_looks_like_prose(raw_line):
            errors.append(f"L{line_number}: prose appears inside display math: {stripped[:80]}")
    if in_math:
        errors.append(f"L{opener_line}: display math is missing a closing $$ delimiter")
    return errors


def postprocess_translated_markdown_text(text: str) -> tuple[str, list[str], list[str]]:
    """Return normalized Markdown plus its repair log and remaining validation errors."""
    lines = text.splitlines(keepends=True)
    lines, boundary_changes = normalize_translation_math_boundaries(lines)
    lines, structure_changes = repair_translation_math_structure(lines)
    return "".join(lines), boundary_changes + structure_changes, lint_translation_math_structure(lines)


FOOTNOTE_REPAIR_DEFINITION_PATTERN = re.compile(r"^ {0,3}\[\^([^\]\r\n]+)\]:[ \t]*(.*)$", re.MULTILINE)
FOOTNOTE_REPAIR_REFERENCE_PATTERN = re.compile(r"\[\^([^\]\r\n]+)\](?!:)")
FOOTNOTE_REPAIR_MARKER_PATTERNS = (
    re.compile(r"\$[ \t]*(?:\{[ \t]*\}[ \t]*)?\^[ \t]*\{[ \t]*(?P<number>\d+)[ \t]*\}[ \t]*\$"),
    re.compile(r"\\\([ \t]*(?:\{[ \t]*\}[ \t]*)?\^[ \t]*\{[ \t]*(?P<number>\d+)[ \t]*\}[ \t]*\\\)"),
    re.compile(r"\\textsuperscript[ \t]*\{[ \t]*(?P<number>\d+)[ \t]*\}"),
    re.compile(r"<sup>[ \t]*(?P<number>\d+)[ \t]*</sup>", re.IGNORECASE),
)
FOOTNOTE_REPAIR_UNICODE_SUPERSCRIPTS = str.maketrans(
    "\u2070\u00b9\u00b2\u00b3\u2074\u2075\u2076\u2077\u2078\u2079", "0123456789"
)
FOOTNOTE_REPAIR_UNICODE_MARKER_PATTERN = re.compile(r"[\u2070\u00b9\u00b2\u00b3\u2074\u2075\u2076\u2077\u2078\u2079]+")
CHINESE_PUNCTUATION_TRANSLATION = str.maketrans({
    "\uff0c": ",", "\u3002": ".", "\uff1b": ";", "\uff1a": ":", "\uff01": "!", "\uff1f": "?", "\u3001": ",",
    "\uff08": "(", "\uff09": ")", "\u3010": "[", "\u3011": "]", "\uff3b": "[", "\uff3d": "]", "\uff5b": "{", "\uff5d": "}",
    "\u201c": '"', "\u201d": '"', "\u2018": "'", "\u2019": "'", "\u300c": '"', "\u300d": '"', "\u300e": '"', "\u300f": '"',
    "\u300a": "<", "\u300b": ">", "\u3008": "<", "\u3009": ">", "\u2014": "-", "\u2013": "-", "\uff0d": "-", "\uff5e": "~", "\u2026": "...",
    "\uff0e": ".", "\uff0f": "/", "\uff02": '"', "\uff07": "'", "\uff06": "&", "\uff0a": "*", "\uff0b": "+", "\uff1d": "=",
})


def footnote_repair_markers(text: str) -> list[tuple[str, int, int, str]]:
    markers: list[tuple[str, int, int, str]] = []
    for pattern in FOOTNOTE_REPAIR_MARKER_PATTERNS:
        markers.extend((match.group("number"), match.start(), match.end(), match.group(0)) for match in pattern.finditer(text))
    markers.extend(
        (match.group(0).translate(FOOTNOTE_REPAIR_UNICODE_SUPERSCRIPTS), match.start(), match.end(), match.group(0))
        for match in FOOTNOTE_REPAIR_UNICODE_MARKER_PATTERN.finditer(text)
    )
    markers.sort(key=lambda marker: (marker[1], -(marker[2] - marker[1])))
    result: list[tuple[str, int, int, str]] = []
    for marker in markers:
        if not result or marker[1] >= result[-1][2]:
            result.append(marker)
    return result


def footnote_repair_is_marker_line(line: str) -> bool:
    stripped = line.lstrip(" \t")
    return bool(footnote_repair_markers(stripped) and footnote_repair_markers(stripped)[0][1] == 0)


def merged_footnote_definitions(text: str) -> list[tuple[int, int, str, list[tuple[str, str]]]]:
    """Find only unambiguous OCR definitions containing multiple printed footnotes."""
    lines, offsets = markdown_line_offsets(text)
    groups: list[tuple[int, int, str, list[tuple[str, str]]]] = []
    index = 0
    while index < len(lines):
        line = lines[index].rstrip("\r\n")
        match = FOOTNOTE_REPAIR_DEFINITION_PATTERN.match(line)
        if not match:
            index += 1
            continue
        body_lines = [match.group(2)]
        following = index + 1
        while following < len(lines):
            candidate = lines[following].rstrip("\r\n")
            if FOOTNOTE_REPAIR_DEFINITION_PATTERN.match(candidate):
                break
            if not candidate.strip():
                body_lines.append("")
                following += 1
                continue
            if not (re.match(r"^(?:[ \t]{2,}|\t)", candidate) or footnote_repair_is_marker_line(candidate)):
                break
            body_lines.append(candidate)
            following += 1
        body = "\n".join(body_lines)
        markers = footnote_repair_markers(body)
        if len(markers) >= 2 and not body[:markers[0][1]].strip():
            accepted = [markers[0]]
            for marker in markers[1:]:
                line_start = body.rfind("\n", 0, marker[1]) + 1
                before_marker = body[line_start:marker[1]]
                if not before_marker.strip() or re.search(r"[ \t]{2,}$", body[max(0, marker[1] - 4):marker[1]]):
                    accepted.append(marker)
            parts: list[tuple[str, str]] = []
            for marker_index, marker in enumerate(accepted):
                end = accepted[marker_index + 1][1] if marker_index + 1 < len(accepted) else len(body)
                segment = body[marker[2]:end].strip()
                segment_lines = segment.splitlines()
                nonempty = [line for line in segment_lines if line.strip()]
                if nonempty:
                    indent = min(len(line) - len(line.lstrip(" \t")) for line in nonempty)
                    segment_lines = [line[indent:].rstrip() if line.strip() else "" for line in segment_lines]
                cleaned = "\n".join(segment_lines).strip()
                if not cleaned:
                    parts = []
                    break
                parts.append((marker[0], cleaned))
            if len(parts) >= 2:
                groups.append((offsets[index], offsets[following], match.group(1), parts))
        index = max(following, index + 1)
    return groups


def footnote_repair_spans(text: str, definitions: list[tuple[int, int, str, list[tuple[str, str]]]]) -> list[tuple[int, int]]:
    lines, offsets = markdown_line_offsets(text)
    blocks, _unmatched = markdown_fence_blocks(text)
    spans = [(start, end) for start, end, _identifier, _parts in definitions]
    spans.extend((match.start(), match.end()) for match in FOOTNOTE_REPAIR_DEFINITION_PATTERN.finditer(text))
    spans.extend((offsets[start], offsets[end + 1]) for start, end in blocks)
    spans.extend((match.start(), match.end()) for match in re.finditer(r"(?<!\\)`+[^`\r\n]*`+", text))
    return sorted(spans)


def footnote_repair_in_spans(position: int, spans: list[tuple[int, int]]) -> bool:
    return any(start <= position < end for start, end in spans)


def footnote_repair_suffix(index: int) -> str:
    result: list[str] = []
    while index:
        index -= 1
        result.append(chr(ord("a") + index % 26))
        index //= 26
    return "".join(reversed(result))


def footnote_repair_render_definition(identifier: str, body: str) -> str:
    paragraphs = re.split(r"\n\s*\n", body)
    rendered: list[str] = []
    for paragraph in paragraphs:
        lines = [line.strip() for line in paragraph.splitlines() if line.strip()]
        if not lines:
            continue
        structured = any(re.match(r"^(?:[-+*]|\d+[.)]|>|#{1,6}\s)", line) for line in lines)
        rendered.append("\n".join(lines) if structured else " ".join(lines))
    if not rendered:
        return f"[^{identifier}]:"
    return f"[^{identifier}]: {rendered[0]}" + "".join(f"\n\n    {paragraph.replace(chr(10), chr(10) + '    ')}" for paragraph in rendered[1:])


def repair_merged_markdown_footnotes(text: str) -> tuple[str, list[str]]:
    """Strictly split OCR-merged footnotes when every replacement is identifiable."""
    groups = merged_footnote_definitions(text)
    if not groups:
        return text, []
    protected = footnote_repair_spans(text, groups)
    occupied = {match.group(1) for match in FOOTNOTE_REPAIR_DEFINITION_PATTERN.finditer(text)}
    occupied.update(match.group(1) for match in FOOTNOTE_REPAIR_REFERENCE_PATTERN.finditer(text))
    all_markers = footnote_repair_markers(text)
    edits: list[tuple[int, int, str]] = []
    used_marker_spans: set[tuple[int, int]] = set()
    changes: list[str] = []
    for start, end, base_id, parts in groups:
        references = [
            match for match in FOOTNOTE_REPAIR_REFERENCE_PATTERN.finditer(text)
            if match.group(1) == base_id and not footnote_repair_in_spans(match.start(), protected)
        ]
        detached = [
            match for match in references
            if not text[text.rfind("\n", 0, match.start()) + 1:match.start()].strip()
        ]
        anchor = (detached[0].start() if detached else (references[0].start() if references else start))
        generated_ids = [base_id]
        suffix_index = 1
        while len(generated_ids) < len(parts):
            candidate = base_id + footnote_repair_suffix(suffix_index)
            suffix_index += 1
            if candidate not in occupied and candidate not in generated_ids:
                generated_ids.append(candidate)
        selected: list[tuple[str, int, int, str]] = []
        upper_bound = anchor
        for number, _body in reversed(parts):
            candidates = [
                marker for marker in all_markers
                if marker[0] == number
                and marker[2] <= upper_bound
                and (marker[1], marker[2]) not in used_marker_spans
                and not footnote_repair_in_spans(marker[1], protected)
            ]
            if not candidates:
                selected = []
                break
            marker = max(candidates, key=lambda item: item[1])
            selected.append(marker)
            upper_bound = marker[1]
        if len(selected) != len(parts):
            continue
        selected.reverse()
        for identifier, marker in zip(generated_ids, selected):
            edits.append((marker[1], marker[2], f"[^{identifier}]"))
            used_marker_spans.add((marker[1], marker[2]))
        for match in detached:
            edits.append((match.start(), match.end(), ""))
        rendered = "\n\n".join(
            footnote_repair_render_definition(identifier, body)
            for identifier, (_number, body) in zip(generated_ids, parts)
        ) + "\n"
        edits.append((start, end, rendered))
        occupied.update(generated_ids)
        changes.append(f"split merged footnote [^{base_id}] into {len(parts)} definitions")
    if not edits:
        return text, []
    for start, end, replacement in sorted(edits, reverse=True):
        text = text[:start] + replacement + text[end:]
    return text, changes


def normalize_chinese_punctuation(text: str) -> tuple[str, list[str]]:
    """Use ASCII punctuation in Chinese translations without touching protected Markdown literals."""
    if not re.search(r"[\u4e00-\u9fff\u3400-\u4dbf]", text):
        return text, []
    protected_text, protected = protect_literals(text)
    normalized = protected_text.translate(CHINESE_PUNCTUATION_TRANSLATION)
    normalized = restore_literals(normalized, protected)
    return normalized, (["normalized Chinese punctuation to ASCII"] if normalized != text else [])


def translation_unfixed_path(output: Path) -> Path:
    return output.with_name(f"{output.stem}_unfixed{output.suffix}")


def finalize_translated_markdown_outputs(job: Job, outputs: list[Path]) -> list[Path]:
    """Atomically publish validated Markdown translations, or retain raw diagnostics."""
    markdown_outputs = [path for path in outputs if path.suffix.lower() in TRANSLATED_MARKDOWN_SUFFIXES]
    if not markdown_outputs:
        return outputs
    candidates: dict[Path, tuple[str, list[str]]] = {}
    diagnostics: dict[Path, list[str]] = {}
    for output in markdown_outputs:
        text = output.read_text(encoding="utf-8")
        repaired, changes, errors = postprocess_translated_markdown_text(text)
        if not errors:
            repaired, punctuation_changes = normalize_chinese_punctuation(repaired)
            changes.extend(punctuation_changes)
        if errors:
            diagnostics[output] = errors
        else:
            candidates[output] = (repaired, changes)
    if diagnostics:
        preserved: list[Path] = []
        for output in markdown_outputs:
            raw_output = translation_unfixed_path(output)
            os.replace(output, raw_output)
            preserved.append(raw_output)
        for output, errors in diagnostics.items():
            job.log(f"[MARKDOWN POSTPROCESS] {output.name} failed validation: {'; '.join(errors)}")
        raise TranslationMarkdownPostprocessError(diagnostics, preserved)
    for output, (repaired, changes) in candidates.items():
        if repaired != output.read_text(encoding="utf-8"):
            write_durable_text(output, repaired)
        for change in changes:
            job.log(f"[MARKDOWN POSTPROCESS] {output.name} {change}")
        job.log(f"[MARKDOWN POSTPROCESS] {output.name} passed display-math validation.")
    return outputs


def markdown_line_offsets(text: str) -> tuple[list[str], list[int]]:
    lines = text.splitlines(keepends=True)
    if not lines:
        return [""], [0, 0]
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    return lines, offsets


def markdown_repair_regions(text: str) -> list[MarkdownRepairRegion]:
    """Find bounded semantic regions that may be changed by the repair model."""
    lines, offsets = markdown_line_offsets(text)
    plain_lines = [line.rstrip("\r\n") for line in lines]
    ranges: list[tuple[int, int, str]] = []

    def add(start: int, end: int, reason: str, context: int = 4) -> None:
        start = max(0, start - context)
        end = min(len(lines) - 1, end + context)
        ranges.append((start, end, reason))

    blocks, unmatched_fence = markdown_fence_blocks(text)
    for start, end in blocks:
        content = "\n".join(plain_lines[start:end + 1])
        if ALGORITHM_LABEL_PATTERN.search(content) and ALGORITHM_MATH_PATTERN.search(content):
            add(start, end, "algorithm or pseudocode contains mathematical notation", context=6)
    if unmatched_fence is not None:
        # A missing close near the start of a paper must not make the entire
        # remainder editable. A bounded window is enough for the model to infer
        # the intended boundary while the splice keeps later prose immutable.
        add(unmatched_fence, min(len(lines) - 1, unmatched_fence + 80), "unclosed fenced block", context=4)

    for index, line in enumerate(plain_lines):
        if STANDALONE_FOOTNOTE_PATTERN.fullmatch(line):
            add(index, index, "footnote reference is isolated from its sentence", context=6)
        if VISUAL_FOOTNOTE_PATTERN.search(line):
            add(index, index, "visual superscript must be reconciled with Markdown footnotes", context=6)
        if FOOTNOTE_DEFINITION_PATTERN.match(line):
            following = "\n".join(plain_lines[index:min(len(lines), index + 5)])
            if VISUAL_FOOTNOTE_PATTERN.search(following) or re.search(r"(?m)^ {4,}\S", following):
                add(index, min(len(lines) - 1, index + 5), "merged or incorrectly indented footnote definitions", context=5)

    errors = markdown_repair_errors(text, text)
    if any("footnote" in error for error in errors):
        footnote_lines = [
            index for index, line in enumerate(plain_lines)
            if FOOTNOTE_REFERENCE_PATTERN.search(line) or VISUAL_FOOTNOTE_PATTERN.search(line)
        ]
        for index in footnote_lines:
            add(index, index, "footnote reference/definition structure is inconsistent", context=5)
    # "algorithm with math remains fenced" describes a code-fence repair; it
    # must not make every formula in a long document an editable LLM region.
    # Only actual delimiter/environment balance failures warrant collecting
    # all mathematical lines.
    math_structure_errors = any(
        error.startswith("display-math ")
        or error.startswith("LaTeX environment ")
        or error.startswith("unclosed LaTeX environment")
        for error in errors
    )
    if math_structure_errors:
        math_lines = [
            index for index, line in enumerate(plain_lines)
            if "$$" in line or r"\[" in line or r"\]" in line or LATEX_ENVIRONMENT_PATTERN.search(line)
        ]
        for index in math_lines:
            add(index, index, "mathematical delimiters or environments are unbalanced", context=6)

    if not ranges:
        return []
    ranges.sort(key=lambda item: (item[0], item[1]))
    merged: list[tuple[int, int, set[str]]] = []
    for start, end, reason in ranges:
        if merged and start <= merged[-1][1] + 8:
            old_start, old_end, reasons = merged[-1]
            reasons.add(reason)
            merged[-1] = (old_start, max(old_end, end), reasons)
        else:
            merged.append((start, end, {reason}))
    merged_regions = [
        MarkdownRepairRegion(offsets[start], offsets[end + 1], "; ".join(sorted(reasons)))
        for start, end, reasons in merged
    ]
    split_regions: list[MarkdownRepairRegion] = []
    for region in merged_regions:
        split_regions.extend(split_markdown_repair_region(text, region))
    return split_regions


def split_markdown_repair_region(
    text: str,
    region: MarkdownRepairRegion,
    maximum: int = 12_000,
) -> list[MarkdownRepairRegion]:
    """Split oversized editable windows only at safe, non-fenced blank lines."""
    source = text[region.start:region.end]
    if len(source) <= maximum:
        return [region]
    lines = source.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    safe_breaks: list[int] = []
    opening: tuple[str, int] | None = None
    for index, line in enumerate(lines, start=1):
        match = FENCE_LINE_PATTERN.match(line.rstrip("\r\n").rstrip())
        if opening is None and match:
            opening = (match.group(1)[0], len(match.group(1)))
        elif opening and match and match.group(1)[0] == opening[0] and len(match.group(1)) >= opening[1]:
            opening = None
        if opening is None and not line.strip():
            safe_breaks.append(offsets[index])
    if not safe_breaks:
        return [region]
    result: list[MarkdownRepairRegion] = []
    relative_start = 0
    while len(source) - relative_start > maximum:
        choices = [value for value in safe_breaks if relative_start < value <= relative_start + maximum]
        if not choices:
            later = next((value for value in safe_breaks if value > relative_start), None)
            if later is None:
                break
            relative_end = later
        else:
            relative_end = choices[-1]
        result.append(MarkdownRepairRegion(region.start + relative_start, region.start + relative_end, region.reason))
        relative_start = relative_end
    if relative_start < len(source):
        result.append(MarkdownRepairRegion(region.start + relative_start, region.end, region.reason))
    return result or [region]


def protect_markdown_repair_images(text: str) -> tuple[str, dict[str, str]]:
    matches: list[tuple[int, int, str]] = []
    for pattern in MARKDOWN_IMAGE_TOKEN_PATTERNS:
        matches.extend((match.start(), match.end(), match.group(0)) for match in pattern.finditer(text))
    matches.sort(key=lambda item: (item[0], item[1]))
    selected: list[tuple[int, int, str]] = []
    previous_end = -1
    for match in matches:
        if match[0] < previous_end:
            continue
        selected.append(match)
        previous_end = match[1]
    protected = {f"[[[OCR_IMAGE_{index:04d}]]]": value for index, (_, _, value) in enumerate(selected)}
    result = text
    for index in range(len(selected) - 1, -1, -1):
        start, end, _value = selected[index]
        key = f"[[[OCR_IMAGE_{index:04d}]]]"
        result = result[:start] + key + result[end:]
    return result, protected


def restore_markdown_repair_images(text: str, protected: dict[str, str]) -> str:
    actual = re.findall(r"\[\[\[OCR_IMAGE_\d{4}\]\]\]", text)
    if sorted(actual) != sorted(protected):
        raise RuntimeError("Markdown repair changed protected image placeholders.")
    for key, value in protected.items():
        text = text.replace(key, value)
    return text


def markdown_repair_related_context(text: str, region: MarkdownRepairRegion) -> str:
    snippets: list[str] = []
    for line in text.splitlines():
        if FOOTNOTE_REFERENCE_PATTERN.search(line) or VISUAL_FOOTNOTE_PATTERN.search(line) or ALGORITHM_LABEL_PATTERN.search(line):
            snippets.append(line)
        if sum(len(item) + 1 for item in snippets) >= 6000:
            break
    return "\n".join(snippets)[:6000]


def parse_markdown_repair_response(content: Any) -> str:
    value = str(content or "").strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", value, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        value = fenced.group(1)
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Markdown repair API did not return valid JSON.") from exc
    replacement = payload.get("replacement") if isinstance(payload, dict) else None
    if not isinstance(replacement, str):
        raise RuntimeError("Markdown repair API response is missing a string replacement.")
    return replacement


def llm_repair_markdown_region(
    job: Job,
    source: str,
    reason: str,
    related_context: str,
    diagnostics: str = "",
) -> str:
    if OpenAI is None:
        raise RuntimeError("The OpenAI Python SDK is not installed.")
    if not job.markdown_repair_config:
        raise RuntimeError("No LLM configuration was selected for Markdown repair.")
    protected_source, protected_images = protect_markdown_repair_images(source)
    config = job.markdown_repair_config
    client = OpenAI(api_key=config["apiKey"], base_url=config["baseUrl"], timeout=180.0, max_retries=0)
    prompt = (
        "Repair only the supplied Markdown/MMD region. Preserve its language, facts, prose order, formulas, "
        "and every [[[OCR_IMAGE_0000]]] placeholder. Restore Markdown structure without rewriting style. "
        "Fix footnote placement/definitions, page-split continuations, fence boundaries, and duplicated visual "
        "superscripts when present. Convert only algorithm or pseudocode fences containing math into a MathJax-"
        "renderable $$\\begin{aligned}...\\end{aligned}$$ structure; keep ordinary code fenced. "
        "Return exactly one JSON object with a string field named replacement and no explanation."
    )
    user_parts = [f"Reason: {reason}", f"Editable region:\n{protected_source}"]
    if related_context:
        user_parts.append(f"Read-only related context (do not reproduce unless already in the editable region):\n{related_context}")
    if diagnostics:
        user_parts.append(f"The previous attempt failed validation. Correct these issues:\n{diagnostics}")
    response = create_compatible_chat_completion(
        client,
        config,
        job=job,
        model=config["model"],
        temperature=0.1,
        messages=[{"role": "system", "content": prompt}, {"role": "user", "content": "\n\n".join(user_parts)}],
    )
    content = response.choices[0].message.content if response.choices else None
    replacement = parse_markdown_repair_response(content)
    maximum = max(12_000, len(source) * 4 + 4_000)
    if len(replacement) > maximum:
        raise RuntimeError("Markdown repair replacement is unexpectedly large.")
    return restore_markdown_repair_images(replacement, protected_images)


def repair_markdown_text(
    job: Job,
    localized_text: str,
    extension: str,
    repair_footnotes: bool = True,
) -> str:
    if extension not in MARKDOWN_REPAIR_SUFFIXES:
        return localized_text
    base = deterministic_markdown_repairs(localized_text)
    if repair_footnotes:
        base, footnote_changes = repair_merged_markdown_footnotes(base)
        for change in footnote_changes:
            job.log(f"[MARKDOWN REPAIR] {change}.")
    regions = markdown_repair_regions(base)
    if not regions:
        errors = markdown_repair_errors(localized_text, base)
        if errors:
            raise RuntimeError("; ".join(errors))
        return base

    job.log(
        f"[MARKDOWN REPAIR] Deep structure repair will process {len(regions)} region(s) "
        "with the selected LLM."
    )
    diagnostics = ""
    last_error: Exception | None = None
    for attempt in range(2):
        candidate = base
        try:
            ordered_regions = list(reversed(regions))
            replacements: list[str | None] = [None] * len(ordered_regions)

            def repair_region(position: int, region: MarkdownRepairRegion) -> Callable[[], str]:
                def task() -> str:
                    job.log(
                        f"[MARKDOWN REPAIR] LLM region {position}/{len(ordered_regions)} "
                        f"(attempt {attempt + 1}/2): {region.reason}."
                    )
                    return llm_repair_markdown_region(
                        job,
                        base[region.start:region.end],
                        region.reason,
                        markdown_repair_related_context(base, region),
                        diagnostics,
                    )

                return task

            tasks = [
                repair_region(position, region)
                for position, region in enumerate(ordered_regions, start=1)
            ]
            run_ordered_translation_tasks(job, tasks, lambda index, replacement: replacements.__setitem__(index, replacement))
            for region, replacement in zip(ordered_regions, replacements):
                if replacement is None:
                    raise RuntimeError("Markdown repair did not return a region replacement.")
                candidate = candidate[:region.start] + replacement + candidate[region.end:]
            errors = markdown_repair_errors(localized_text, candidate)
            if not errors:
                return candidate
            diagnostics = "\n".join(f"- {error}" for error in errors)
            last_error = RuntimeError("; ".join(errors))
        except Exception as exc:
            last_error = exc
            diagnostics = f"- {exc}"
        if attempt == 0:
            job.log(f"[MARKDOWN REPAIR] Validation failed; retrying once: {diagnostics.replace(chr(10), ' ')}")
    raise RuntimeError(str(last_error or "Markdown repair failed validation."))


def extract_mmd_bundle(job: Job, bundle: bytes, output: Path) -> tuple[str, LocalAssetStore]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(bundle))
    except zipfile.BadZipFile as exc:
        raise RuntimeError("Mathpix mmd.zip is not a valid ZIP archive.") from exc
    with archive:
        files = [info for info in archive.infolist() if not info.is_dir()]
        paths = {info.filename: safe_bundle_path(info.filename) for info in files}
        documents = [info for info in files if paths[info.filename].suffix.lower() == ".mmd"]
        if len(documents) != 1:
            raise RuntimeError("Mathpix mmd.zip must contain exactly one .mmd file.")
        document = documents[0]
        assets_dir = output.with_name(f"{output.stem}.assets")
        assets_dir.mkdir(exist_ok=True)
        assets = LocalAssetStore(assets_dir)
        for info in files:
            bundled_path = paths[info.filename]
            if bundled_path.parts[0] != "images":
                continue
            relative = PurePosixPath(*bundled_path.parts[1:])
            if not relative.parts:
                continue
            destination = assets_dir.joinpath(*relative.parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, destination.open("wb") as target:
                shutil.copyfileobj(source, target)
            assets.asset_paths.add(relative.as_posix())
        text = archive.read(document).decode("utf-8")
    return text, assets


def extract_html_bundle(bundle: bytes, output_dir: Path) -> Path:
    """Safely extract one self-contained Mathpix HTML document and its assets."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(bundle))
    except zipfile.BadZipFile as exc:
        raise RuntimeError("Mathpix html.zip is not a valid ZIP archive.") from exc
    with archive:
        files = [
            info for info in archive.infolist()
            if not info.is_dir() and not info.filename.startswith("__MACOSX/")
        ]
        if sum(info.file_size for info in files) > MAX_READER_ARCHIVE_SIZE:
            raise RuntimeError("Mathpix html.zip is larger than the reader safety limit.")
        paths = {info.filename: safe_bundle_path(info.filename) for info in files}
        documents = [
            info for info in files
            if paths[info.filename].suffix.lower() in {".html", ".htm"}
        ]
        if len(documents) != 1:
            raise RuntimeError("Mathpix html.zip must contain exactly one HTML document.")
        output_root = output_dir.resolve()
        for info in files:
            mode = (info.external_attr >> 16) & 0o170000
            if mode == 0o120000:
                raise RuntimeError(f"Mathpix ZIP contains a symbolic link: {info.filename}")
            relative = paths[info.filename]
            destination = output_dir.joinpath(*relative.parts)
            resolved = destination.resolve()
            if output_root != resolved and output_root not in resolved.parents:
                raise RuntimeError(f"Mathpix ZIP contains an unsafe path: {info.filename}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, destination.open("wb") as target:
                shutil.copyfileobj(source, target)
        return output_dir.joinpath(*paths[documents[0].filename].parts)


def write_registered_ocr_text(
    job: Job,
    output: Path,
    text: str,
    extension: str,
    *,
    kind: str = "ocr",
    label: str | None = None,
) -> None:
    write_durable_text(output, text)
    save_local_artifact(job, output)
    labels = {"mmd": "Mathpix Markdown", "md": "Markdown", "html": "HTML", "lines.json": "Lines JSON"}
    add_artifact(job, output, kind, label or labels.get(extension, extension))
    job.log(f"[OCR] Downloaded {output.name}.")


def add_markdown_repair_warning(job: Job, output: Path, exc: Exception) -> None:
    warning = f"Could not repair {output.name}; only the localized legacy file was kept: {exc}"
    if warning not in job.warnings:
        job.warnings.append(warning)
        job.log(f"[WARNING] {warning}")


def write_ocr_text_artifact(job: Job, output: Path, extension: str, text: str, assets: LocalAssetStore) -> None:
    localized_text = rewrite_text_asset_references(job, text, assets)
    repair_enabled = bool(job.options.get("repairMarkdown", True))
    if extension not in MARKDOWN_REPAIR_SUFFIXES or not repair_enabled:
        write_registered_ocr_text(job, output, localized_text, extension)
        return

    legacy_output = output.with_name(f"{output.stem}_legacy{output.suffix}")
    legacy_label = "Localized legacy Markdown" if extension == "md" else "Localized legacy Mathpix Markdown"
    write_registered_ocr_text(
        job,
        legacy_output,
        localized_text,
        extension,
        kind="ocr_legacy",
        label=legacy_label,
    )
    previous_phase = job.phase
    job.phase = "markdown_repair"
    job.log(f"[MARKDOWN REPAIR] Repairing {output.name} after image localization.")
    try:
        repaired = repair_markdown_text(job, localized_text, extension)
        errors = markdown_repair_errors(localized_text, repaired)
        if errors:
            raise RuntimeError("; ".join(errors))
        write_registered_ocr_text(
            job,
            output,
            repaired,
            extension,
            label="Repaired Markdown" if extension == "md" else "Repaired Mathpix Markdown",
        )
        job.log(f"[MARKDOWN REPAIR] {output.name} passed all structural invariants.")
    except Exception as exc:
        output.unlink(missing_ok=True)
        if job.local_save_dir:
            local_output = job.local_save_dir / output.name
            if local_output.parent == job.local_save_dir and local_output.is_file():
                local_output.unlink()
        with job.lock:
            job.artifacts = [artifact for artifact in job.artifacts if artifact["id"] != output.name]
        add_markdown_repair_warning(job, output, exc)
    finally:
        job.phase = previous_phase


def download_mmd_bundle(job: Job, pdf_id: str, output: Path) -> tuple[str, LocalAssetStore]:
    response = requests.get(f"{MATHPIX_BASE_URL}/pdf/{pdf_id}.{MMD_BUNDLE_FORMAT}", headers=mathpix_headers(), timeout=120)
    if not response.ok:
        raise RuntimeError(f"Could not download {MMD_BUNDLE_FORMAT}: {response_error(response)}")
    return extract_mmd_bundle(job, response.content, output)


def download_mathpix_text_artifact(job: Job, pdf_id: str, extension: str, output: Path, assets: LocalAssetStore) -> None:
    response = requests.get(f"{MATHPIX_BASE_URL}/pdf/{pdf_id}.{extension}", headers=mathpix_headers(), timeout=120)
    if not response.ok:
        raise RuntimeError(f"Could not download {extension}: {response_error(response)}")
    try:
        text = response.content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError(f"Mathpix {extension} output is not UTF-8 text.") from exc
    write_ocr_text_artifact(job, output, extension, text, assets)


def download_mathpix_artifact(job: Job, pdf_id: str, extension: str, output: Path, kind: str = "ocr") -> None:
    response = requests.get(f"{MATHPIX_BASE_URL}/pdf/{pdf_id}.{extension}", headers=mathpix_headers(), timeout=120)
    if not response.ok:
        raise RuntimeError(f"Could not download {extension}: {response_error(response)}")
    output.write_bytes(response.content)
    save_local_artifact(job, output)
    labels = {
        "mmd": "Mathpix Markdown",
        "md": "Markdown",
        "html": "HTML",
        "docx": "DOCX",
        "tex.zip": "LaTeX ZIP",
        "lines.json": "Lines JSON",
    }
    add_artifact(job, output, kind, labels.get(extension, extension))
    job.log(f"[OCR] Downloaded {output.name}.")


def submit_mathpix_pdf(job: Job, source: Path, requested_formats: list[str]) -> str:
    job.phase = "ocr"
    job.log("[OCR] Uploading PDF to Mathpix.")
    with source.open("rb") as handle:
        response = requests.post(
            f"{MATHPIX_BASE_URL}/pdf",
            headers=mathpix_headers(),
            files={"file": handle},
            data={"options_json": json.dumps({"conversion_formats": {item: True for item in requested_formats}})},
            timeout=120,
        )
    if not response.ok:
        raise RuntimeError(f"Mathpix upload failed: {response_error(response)}")
    payload = response.json()
    pdf_id = str(payload.get("pdf_id", ""))
    if not pdf_id:
        raise RuntimeError(f"Mathpix did not return a PDF ID: {payload}")
    job.log("[OCR] Upload accepted; waiting for OCR.")
    wait_for_mathpix_status(job, pdf_id)
    return pdf_id


def run_pdf_ocr(job: Job, files: list[Path]) -> None:
    if len(files) != 1:
        raise ValueError("PDF OCR requires exactly one PDF.")
    selected_formats = checked_ocr_formats(job.options)
    requested_formats = [*selected_formats, MMD_BUNDLE_FORMAT]
    source = files[0]
    output_dir = job.root / "output"
    output_dir.mkdir(exist_ok=True)
    pdf_id = submit_mathpix_pdf(job, source, requested_formats)
    completed_formats = wait_for_conversion_status(job, pdf_id, requested_formats)
    if MMD_BUNDLE_FORMAT not in completed_formats:
        raise RuntimeError("Mathpix could not create the required self-contained MMD bundle.")
    mmd_output = output_dir / f"{job.source_stem}.mmd"
    mmd_text, assets = download_mmd_bundle(job, pdf_id, mmd_output)
    write_ocr_text_artifact(job, mmd_output, "mmd", mmd_text, assets)
    save_local_directory(job, assets.directory)
    lines_output = output_dir / f"{job.source_stem}.lines.json"
    download_mathpix_text_artifact(job, pdf_id, "lines.json", lines_output, assets)
    for extension in completed_formats:
        if extension == MMD_BUNDLE_FORMAT:
            continue
        output = output_dir / f"{job.source_stem}.{extension}"
        if extension in {"md", "html"}:
            download_mathpix_text_artifact(job, pdf_id, extension, output, assets)
        else:
            download_mathpix_artifact(job, pdf_id, extension, output)
    save_local_directory(job, assets.directory)
    job.log(f"[OCR] Saved {len(assets.asset_paths)} image(s) to {assets.reference_prefix}.")
    job.download_path, job.download_name = package_pdf_artifacts(job)
    job.phase = "ocr_complete"


def run_reader_html_ocr(job: Job, source: Path) -> Path:
    """OCR a reader PDF directly to the self-contained browser format."""
    output_dir = job.root / "output"
    output_dir.mkdir(exist_ok=True)
    pdf_id = submit_mathpix_pdf(job, source, [HTML_BUNDLE_FORMAT])
    completed = wait_for_conversion_status(job, pdf_id, [HTML_BUNDLE_FORMAT])
    if HTML_BUNDLE_FORMAT not in completed:
        raise RuntimeError("Mathpix could not create the required self-contained HTML bundle.")
    response = requests.get(
        f"{MATHPIX_BASE_URL}/pdf/{pdf_id}.{HTML_BUNDLE_FORMAT}",
        headers=mathpix_headers(),
        timeout=120,
    )
    if not response.ok:
        raise RuntimeError(f"Could not download {HTML_BUNDLE_FORMAT}: {response_error(response)}")
    html_root = output_dir / f"{job.source_stem}.html-assets"
    html_root.mkdir()
    html_path = extract_html_bundle(response.content, html_root)
    job.log(f"[OCR] Extracted self-contained HTML to {html_path.relative_to(output_dir).as_posix()}.")
    job.phase = "ocr_complete"
    return html_path


PLACEHOLDER_PATTERN = re.compile(r"\[\[\[KEEP_\d{4}\]\]\]")
PROTECTED_MARKDOWN_PATTERNS = (
    re.compile(r"```.*?```", re.DOTALL),
    re.compile(r"~~~.*?~~~", re.DOTALL),
    re.compile(r"(?<!\\)\$\$.*?(?<!\\)\$\$", re.DOTALL),
    re.compile(
        r"\\begin\{(equation\*?|align\*?|aligned|gather\*?|multline\*?|cases|matrix|pmatrix|bmatrix|vmatrix|Vmatrix)\}"
        r".*?\\end\{\1\}",
        re.DOTALL,
    ),
    re.compile(r"\\\[.*?\\\]", re.DOTALL),
    re.compile(r"\\\(.*?\\\)", re.DOTALL),
    re.compile(r"(?<!\\)`[^`\n]+`"),
    re.compile(r"\\(?:label|ref|eqref|cite\w*|begin|end|includegraphics)\{[^{}]*\}"),
    re.compile(r"(?<=\]\()[^)]*(?=\))"),
    re.compile(r"https?://[^\s)>]+"),
    re.compile(r"(?<!\\)\$[^$\n]+\$"),
)


def protect_literals(text: str) -> tuple[str, dict[str, str]]:
    # Match against the untouched source, then keep only non-overlapping
    # outermost spans. Sequential substitutions can nest placeholders when an
    # inline formula contains an environment or text resembling a Markdown
    # link destination, causing an inner KEEP marker to leak after restoration.
    candidates: list[tuple[int, int, int]] = []
    for priority, pattern in enumerate(PROTECTED_MARKDOWN_PATTERNS):
        candidates.extend((match.start(), match.end(), priority) for match in pattern.finditer(text))
    selected: list[tuple[int, int, int]] = []
    for candidate in sorted(candidates, key=lambda item: (item[0], -item[1], item[2])):
        start, end, _priority = candidate
        if selected and start < selected[-1][1]:
            continue
        selected.append(candidate)
    selected.sort(key=lambda item: item[0])

    protected: dict[str, str] = {}
    rendered: list[str] = []
    cursor = 0
    for start, end, _priority in selected:
        rendered.append(text[cursor:start])
        key = f"[[[KEEP_{len(protected):04d}]]]"
        protected[key] = text[start:end]
        rendered.append(key)
        cursor = end
    rendered.append(text[cursor:])
    return "".join(rendered), protected


def restore_literals(text: str, protected: dict[str, str]) -> str:
    expected = sorted(protected)
    actual = sorted(PLACEHOLDER_PATTERN.findall(text))
    if actual != expected:
        raise RuntimeError("Translation changed protected formulas, code, links, or citations.")
    for key, value in protected.items():
        text = text.replace(key, value)
    return text


def text_chunks(text: str, maximum: int = 6000) -> list[str]:
    parts = re.split(r"(\n\s*\n)", text)
    chunks: list[str] = []
    current = ""
    for part in parts:
        if current and len(current) + len(part) > maximum:
            chunks.append(current)
            current = ""
        if len(part) > maximum:
            index = 0
            while index < len(part):
                end = min(index + maximum, len(part))
                # Never split a protection marker across two API requests.
                marker = PLACEHOLDER_PATTERN.search(part, max(index, end - 15), end + 15)
                if marker and marker.start() < end < marker.end():
                    end = marker.start()
                if end == index:
                    end = marker.end() if marker else min(index + maximum, len(part))
                chunks.append(part[index:end])
                index = end
        else:
            current += part
    if current:
        chunks.append(current)
    return chunks


def llm_translate(job: Job, text: str) -> str:
    if OpenAI is None:
        raise RuntimeError("The OpenAI Python SDK is not installed.")
    if not job or not job.translation_config:
        raise RuntimeError("No LLM configuration was selected for this translation.")
    config = job.translation_config
    client = OpenAI(api_key=config["apiKey"], base_url=config["baseUrl"], timeout=180.0, max_retries=0)
    has_placeholders = bool(PLACEHOLDER_PATTERN.search(text))
    is_segment_batch = '"task":"translate_markdown_segments_v1"' in text[:200]
    if is_segment_batch:
        structure_instruction = (
            "The user supplies a JSON object containing ordered Markdown parts. Translate only each part's "
            "text value. Preserve every id, key, array order, syntax, boundary, and literal value. Return the "
            "same JSON shape with no Markdown fence. "
        )
    else:
        structure_instruction = (
            "Preserve Markdown or HTML structure, every reserved placeholder exactly, numbers, and punctuation. "
            if has_placeholders
            else "Preserve Markdown or HTML structure, numbers, and punctuation. "
        )
    response = create_compatible_chat_completion(
        client,
        config,
        job=job,
        model=config["model"],
        temperature=0.1,
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a professional academic translator. Translate into Simplified Chinese only. "
                    + structure_instruction
                    + "Return only the translated content with no explanation."
                ),
            },
            {"role": "user", "content": text},
        ],
    )
    result = response.choices[0].message.content if response.choices else None
    if not result or not result.strip():
        raise RuntimeError("Translation API returned empty content.")
    return result


def unsupported_temperature_error(exc: Exception) -> bool:
    message = str(exc).casefold()
    return (
        "temperature" in message
        and (
            "unsupported" in message
            or "does not support" in message
            or "not supported" in message
            or "only the default" in message
        )
    )


def llm_error_status(exc: Exception) -> int | None:
    value = getattr(exc, "status_code", None)
    if value is None:
        value = getattr(getattr(exc, "response", None), "status_code", None)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def retryable_llm_error(exc: Exception) -> bool:
    status = llm_error_status(exc)
    if status in {408, 429} or (status is not None and status >= 500):
        return True
    name = exc.__class__.__name__.casefold()
    return any(token in name for token in ("connection", "timeout", "temporarilyunavailable"))


def llm_retry_delay(exc: Exception, retry_index: int) -> float:
    headers = getattr(getattr(exc, "response", None), "headers", None)
    retry_after = None
    if headers is not None:
        try:
            raw_retry_after = str(headers.get("retry-after") or headers.get("Retry-After") or "").strip()
            retry_after = float(raw_retry_after)
        except (AttributeError, TypeError, ValueError):
            try:
                retry_at = parsedate_to_datetime(raw_retry_after)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                retry_after = max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())
            except (TypeError, ValueError, UnboundLocalError):
                retry_after = None
    if retry_after is not None and retry_after >= 0:
        return min(30.0, retry_after)
    base = float(2 ** retry_index)
    return min(30.0, base + random.uniform(0.0, base * 0.25))


def adjust_translation_active(job: Job | None, delta: int) -> None:
    if job is None:
        return
    with job.lock:
        if not job.translation_progress:
            return
        active = max(0, int(job.translation_progress.get("active") or 0) + delta)
        job.translation_progress["active"] = active


def clear_translation_active(job: Job) -> None:
    with job.lock:
        if job.translation_progress:
            job.translation_progress["active"] = 0


def log_translation_debug_summary(job: Job) -> None:
    with job.lock:
        count = job.translation_debug_count
    if not count:
        return
    debug_path = job.root / "output" / "translation_debug.jsonl"
    if job.local_save_dir and debug_path.is_file():
        try:
            save_local_artifact_with_log(job, debug_path, log=False)
        except OSError as exc:
            job.log(f"[TRANSLATION] Could not copy translation diagnostics beside the source PDF: {exc}")
    job.log(
        f"[TRANSLATION] Captured {count} protected-literal mismatch event(s) in translation_debug.jsonl. "
        "It contains affected source/model text but never the configured API key; review it before sharing."
    )


def create_compatible_chat_completion(
    client: Any,
    config: dict[str, Any],
    *,
    job: Job | None = None,
    temperature: float,
    transient_retries: int = 3,
    **request: Any,
) -> Any:
    """Apply provider-wide concurrency, compatibility fallback, and transient retries."""
    if not config.get("_limiterKey"):
        prepare_llm_runtime_config(config)
    limiter_key = str(config["_limiterKey"])
    concurrency = llm_concurrency(config.get("concurrency"))
    capability_lock = config.setdefault("_capabilityLock", threading.Lock())

    def one_attempt() -> Any:
        attempt_request = dict(request)
        with capability_lock:
            omit_temperature = bool(config.get("_omitTemperature"))
        if not omit_temperature:
            attempt_request["temperature"] = temperature
        with llm_concurrency_registry.slot(limiter_key, concurrency):
            adjust_translation_active(job, 1)
            try:
                try:
                    return client.chat.completions.create(**attempt_request)
                except Exception as exc:
                    if "temperature" not in attempt_request or not unsupported_temperature_error(exc):
                        raise
                    with capability_lock:
                        config["_omitTemperature"] = True
                    attempt_request.pop("temperature", None)
                    return client.chat.completions.create(**attempt_request)
            finally:
                adjust_translation_active(job, -1)

    transient_retries = max(0, min(3, int(transient_retries)))
    for retry_index in range(transient_retries + 1):
        try:
            return one_attempt()
        except Exception as exc:
            if retry_index >= transient_retries or not retryable_llm_error(exc):
                raise
            delay = llm_retry_delay(exc, retry_index)
            if job is not None:
                job.log(
                    f"[LLM] Transient request failure; retrying in {delay:.1f}s "
                    f"({retry_index + 1}/{transient_retries})."
                )
            time.sleep(delay)
    raise RuntimeError("LLM retry loop exited unexpectedly.")


def translation_unit_count(text: str) -> int:
    protected_text, _protected = protect_literals(text)
    return len(text_chunks(protected_text))


def translation_concurrency(job: Job | None) -> int:
    if job is None:
        return 1
    config = job.translation_config or job.markdown_repair_config
    if not config:
        return 1
    return llm_concurrency(config.get("concurrency"))


def run_ordered_translation_tasks(
    job: Job | None,
    tasks: list[Callable[[], str]],
    on_result: Callable[[int, str], None],
) -> None:
    """Run a bounded window of requests and commit only the continuous source prefix."""
    if not tasks:
        return
    concurrency = min(translation_concurrency(job), len(tasks))
    if concurrency <= 1:
        for index, task in enumerate(tasks):
            on_result(index, task())
        return

    executor = ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="translation")
    futures: dict[Future[str], int] = {}
    buffered: dict[int, str] = {}
    next_to_submit = 0
    next_to_commit = 0

    def submit_next() -> None:
        nonlocal next_to_submit
        future = executor.submit(tasks[next_to_submit])
        futures[future] = next_to_submit
        next_to_submit += 1

    try:
        while next_to_submit < concurrency:
            submit_next()
        while futures:
            done, _pending = wait(futures, return_when=FIRST_COMPLETED)
            failure: Exception | None = None
            for future in sorted(done, key=futures.__getitem__):
                index = futures.pop(future)
                try:
                    buffered[index] = future.result()
                except Exception as exc:
                    if failure is None:
                        failure = exc
            if failure is not None:
                for future in futures:
                    future.cancel()
                executor.shutdown(wait=True, cancel_futures=True)
                raise failure
            while next_to_commit in buffered:
                on_result(next_to_commit, buffered.pop(next_to_commit))
                next_to_commit += 1
            while next_to_submit < len(tasks) and len(futures) + len(buffered) < concurrency:
                submit_next()
    except Exception:
        for future in futures:
            future.cancel()
        executor.shutdown(wait=True, cancel_futures=True)
        raise
    else:
        executor.shutdown(wait=True)


def prepared_translation_chunks(text: str) -> tuple[list[str], dict[str, str]]:
    protected_text, protected = protect_literals(text)
    return text_chunks(protected_text), protected


def localize_chunk_markers(chunk: str, protected: dict[str, str]) -> tuple[str, dict[str, str]]:
    """Renumber a chunk's document-wide markers to a compact request-local sequence."""
    ordered = list(dict.fromkeys(PLACEHOLDER_PATTERN.findall(chunk)))
    mapping = {marker: f"[[[KEEP_{index:04d}]]]" for index, marker in enumerate(ordered)}
    localized = PLACEHOLDER_PATTERN.sub(lambda match: mapping[match.group(0)], chunk)
    return localized, {mapping[marker]: protected[marker] for marker in ordered}


def restore_missing_boundary_markers(source: str, translated: str, expected: list[str]) -> str:
    """Restore omitted standalone markers at chunk boundaries where placement is unambiguous."""
    actual = Counter(PLACEHOLDER_PATTERN.findall(translated))
    expected_counts = Counter(expected)
    leading_match = re.match(rf"^(?:\s*{PLACEHOLDER_PATTERN.pattern}\s*)+", source)
    trailing_match = re.search(rf"(?:\s*{PLACEHOLDER_PATTERN.pattern}\s*)+$", source)
    leading = PLACEHOLDER_PATTERN.findall(leading_match.group(0)) if leading_match else []
    trailing = PLACEHOLDER_PATTERN.findall(trailing_match.group(0)) if trailing_match else []
    prepend: list[str] = []
    append: list[str] = []
    for marker in leading:
        if actual[marker] < expected_counts[marker]:
            prepend.append(marker)
            actual[marker] += 1
    for marker in trailing:
        if actual[marker] < expected_counts[marker]:
            append.append(marker)
            actual[marker] += 1
    if prepend:
        translated = "\n\n".join(prepend) + "\n\n" + translated.lstrip()
    if append:
        translated = translated.rstrip() + "\n\n" + "\n\n".join(append)
    return translated


def safely_duplicated_inline_math_markers(
    expected: list[str],
    actual: list[str],
    protected: dict[str, str],
) -> bool:
    expected_counts = Counter(expected)
    actual_counts = Counter(actual)
    if expected_counts - actual_counts:
        return False
    extras = actual_counts - expected_counts
    if not extras:
        return True
    for marker in extras:
        literal = str(protected.get(marker) or "").strip()
        if not (
            re.fullmatch(r"(?<!\\)\$(?!\$)[^$\n]+(?<!\\)\$", literal)
            or re.fullmatch(r"\\\(.+?\\\)", literal, flags=re.DOTALL)
        ):
            return False
    return True


def repair_protected_marker_sequence(
    translated: str,
    expected: list[str],
) -> str | None:
    """Repair a small monotonic marker error without retranslating the Markdown block.

    Models commonly repeat one neighboring marker in place of another while
    otherwise preserving their order. Reusing the first translation is safer
    than translating every prose fragment independently because that fallback
    can collapse headings, footnote definitions, and paragraph breaks.
    """
    actual = PLACEHOLDER_PATTERN.findall(translated)
    if actual == expected:
        return translated
    if not expected or len(actual) != len(expected):
        return None

    mismatches = sum(left != right for left, right in zip(actual, expected))
    if not 0 < mismatches <= 3:
        return None
    replacements = iter(expected)
    return PLACEHOLDER_PATTERN.sub(lambda _match: next(replacements), translated)


def translation_debug_excerpt(value: str, maximum: int = 12_000) -> tuple[str, bool]:
    return value[:maximum], len(value) > maximum


def markdown_structure_signature(text: str) -> dict[str, Any]:
    """Return Markdown syntax that translation must preserve exactly."""
    return {
        "headings": [len(match) for match in re.findall(r"(?m)^(#{1,6})[ \t]+", text)],
        "footnotes": re.findall(r"(?m)^\[\^([^\]\r\n]+)\]:", text),
        "paragraphBreaks": len(re.findall(r"\r?\n[ \t]*\r?\n", text)),
        "lists": re.findall(r"(?m)^ {0,3}((?:[-+*]|\d+[.)]))[ \t]+", text),
        "quotes": [match.count(">") for match in re.findall(r"(?m)^([ \t]*>+)[ \t]?", text)],
    }


def boundary_blank_lines(text: str) -> tuple[str, str, str]:
    """Separate blank lines at request boundaries so the model cannot trim them."""
    leading_match = re.match(r"^(?:[ \t]*\r?\n)+", text)
    leading = leading_match.group(0) if leading_match else ""
    remainder = text[len(leading) :]
    trailing_match = re.search(r"(?:\r?\n[ \t]*)+$", remainder)
    trailing = trailing_match.group(0) if trailing_match else ""
    body = remainder[: len(remainder) - len(trailing)] if trailing else remainder
    return leading, body, trailing


def parsed_translation_json(value: str) -> dict[str, Any] | None:
    candidate = value.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?\s*", "", candidate, count=1, flags=re.IGNORECASE)
        candidate = re.sub(r"\s*```$", "", candidate, count=1)
    if not candidate.startswith("{") or not candidate.endswith("}"):
        return None
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def translate_structured_markdown_chunk(
    job: Job | None,
    chunk: str,
    protected: dict[str, str],
) -> str | None:
    """Translate prose as a JSON batch while retaining all Markdown assembly locally."""
    parts: list[dict[str, str]] = []
    assembly: list[tuple[str, str]] = []
    segment_ids: list[str] = []

    def append_fixed(kind: str, value: str) -> None:
        parts.append({kind: value})
        assembly.append(("fixed", value))

    def append_text(value: str) -> None:
        if not value:
            return
        if not re.search(r"[A-Za-z]", value):
            append_fixed("literal", value)
            return
        segment_id = f"S{len(segment_ids):04d}"
        segment_ids.append(segment_id)
        parts.append({"id": segment_id, "text": value})
        assembly.append(("text", segment_id))

    split_pattern = rf"({PLACEHOLDER_PATTERN.pattern}|\r?\n[ \t]*\r?\n)"
    for piece in re.split(split_pattern, chunk):
        if not piece:
            continue
        if PLACEHOLDER_PATTERN.fullmatch(piece):
            parts.append({"literal": protected[piece]})
            assembly.append(("marker", piece))
            continue
        if re.fullmatch(r"\r?\n[ \t]*\r?\n", piece):
            append_fixed("boundary", piece)
            continue
        prefix_match = re.match(r"(?: {0,3}#{1,6}[ \t]+|[ \t]*\[\^[^\]\r\n]+\]:[ \t]*)", piece)
        if prefix_match:
            append_fixed("syntax", prefix_match.group(0))
            piece = piece[prefix_match.end() :]
        append_text(piece)

    if not segment_ids:
        return chunk
    request_payload = json.dumps(
        {
            "task": "translate_markdown_segments_v1",
            "target": "Simplified Chinese",
            "parts": parts,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    response_payload = parsed_translation_json(llm_translate(job, request_payload))
    if response_payload is None or not isinstance(response_payload.get("parts"), list):
        return None
    translations: dict[str, str] = {}
    for part in response_payload["parts"]:
        if not isinstance(part, dict) or not isinstance(part.get("id"), str):
            continue
        if isinstance(part.get("text"), str):
            translations[part["id"]] = part["text"]
    if set(translations) != set(segment_ids):
        return None
    rendered: list[str] = []
    for kind, value in assembly:
        if kind == "text":
            translated = translations[value]
            if PLACEHOLDER_PATTERN.search(translated):
                return None
            rendered.append(translated)
        else:
            rendered.append(value)
    return "".join(rendered)


def record_translation_marker_mismatch(
    job: Job | None,
    context: str,
    chunk: str,
    translated_chunk: str,
    protected: dict[str, str],
    expected: list[str],
    actual: list[str],
) -> int:
    """Write a shareable diagnostic event without serializing configured credentials."""
    if job is None:
        return 0
    config = job.translation_config or job.markdown_repair_config or {}
    parsed_base = urlsplit(str(config.get("baseUrl") or ""))
    provider = f"{parsed_base.scheme}://{parsed_base.netloc}" if parsed_base.scheme and parsed_base.netloc else ""
    input_excerpt, input_truncated = translation_debug_excerpt(chunk)
    output_excerpt, output_truncated = translation_debug_excerpt(translated_chunk)
    missing = list((Counter(expected) - Counter(actual)).elements())
    unexpected = list((Counter(actual) - Counter(expected)).elements())
    output = job.root / "output" / "translation_debug.jsonl"
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        with job.lock:
            event_number = job.translation_debug_count + 1
            payload = {
                "schemaVersion": TRANSLATION_DEBUG_SCHEMA_VERSION,
                "pipelineVersion": TRANSLATION_PIPELINE_VERSION,
                "event": "protected_literal_mismatch",
                "eventNumber": event_number,
                "timestamp": time.time(),
                "jobId": job.id,
                "context": context,
                "thread": threading.current_thread().name,
                "provider": provider,
                "model": str(config.get("model") or ""),
                "configuredConcurrency": llm_concurrency(config.get("concurrency")),
                "inputLength": len(chunk),
                "inputSha256": hashlib.sha256(chunk.encode("utf-8")).hexdigest(),
                "outputLength": len(translated_chunk),
                "outputSha256": hashlib.sha256(translated_chunk.encode("utf-8")).hexdigest(),
                "expectedMarkers": expected,
                "actualMarkers": actual,
                "missingMarkers": missing,
                "unexpectedMarkers": unexpected,
                "protectedLiterals": {key: protected[key] for key in expected if key in protected},
                "protectedInput": input_excerpt,
                "protectedInputTruncated": input_truncated,
                "modelOutput": output_excerpt,
                "modelOutputTruncated": output_truncated,
            }
            with output.open("a", encoding="utf-8", newline="") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            job.translation_debug_count = event_number
    except OSError as exc:
        job.log(f"[TRANSLATION] Could not write translation diagnostics: {exc}")
        return 0
    add_artifact(job, output, "translation_debug", "Translation debug JSONL")
    return event_number


def translate_prepared_chunk(
    job: Job | None,
    chunk: str,
    protected: dict[str, str],
    debug_context: str = "chunk",
    preserve_markdown_structure: bool = False,
) -> str:
    chunk, protected = localize_chunk_markers(chunk, protected)

    def translate_preserving_boundary_blank_lines(value: str) -> str:
        if not re.search(r"[A-Za-z]", value):
            return value
        leading, body, trailing = boundary_blank_lines(value)
        if not body:
            return value
        translated = llm_translate(job, body).strip("\r\n")
        return leading + translated + trailing

    def translate_without_markers(value: str) -> str:
        prefix = ""
        prefix_match = re.match(r"(?: {0,3}#{1,6}[ \t]+|\s*\[\^[^\]\r\n]+\]:[ \t]*)", value)
        if prefix_match:
            prefix = prefix_match.group(0)
            value = value[prefix_match.end() :]
        pieces = re.split(f"({PLACEHOLDER_PATTERN.pattern})", value)
        rendered: list[str] = []
        for piece in pieces:
            if PLACEHOLDER_PATTERN.fullmatch(piece):
                rendered.append(piece)
                continue
            for prose in text_chunks(piece):
                translated_prose = translate_preserving_boundary_blank_lines(prose)
                emitted = PLACEHOLDER_PATTERN.findall(translated_prose)
                if emitted:
                    if job is not None:
                        job.log("[TRANSLATION] Removed reserved placeholder text emitted while translating plain prose.")
                    translated_prose = PLACEHOLDER_PATTERN.sub("", translated_prose)
                    if re.search(r"[A-Za-z]", prose) and not translated_prose.strip():
                        raise RuntimeError("Translation API returned only reserved placeholder text for plain prose.")
                rendered.append(translated_prose)
        return prefix + "".join(rendered)

    def repair_missing_marker_blocks(model_output: str, missing: list[str]) -> str | None:
        """Retranslate only source blocks that lost markers, keeping other model output intact."""
        source_parts = re.split(r"(\n\s*\n)", chunk)
        output_parts = re.split(r"(\n\s*\n)", model_output)
        used_output_parts: set[int] = set()
        repaired_any = False
        for source_part in source_parts:
            source_markers = PLACEHOLDER_PATTERN.findall(source_part)
            if not source_markers or not any(marker in missing for marker in source_markers):
                continue
            source_marker_set = set(source_markers)
            candidates: list[tuple[int, int]] = []
            for output_index, output_part in enumerate(output_parts):
                if output_index in used_output_parts or re.fullmatch(r"\n\s*\n", output_part):
                    continue
                output_markers = PLACEHOLDER_PATTERN.findall(output_part)
                if not output_markers or not set(output_markers).issubset(source_marker_set):
                    continue
                overlap = sum((Counter(source_markers) & Counter(output_markers)).values())
                if overlap:
                    candidates.append((overlap, output_index))
            if not candidates:
                return None
            best_overlap = max(score for score, _index in candidates)
            best = [index for score, index in candidates if score == best_overlap]
            if len(best) != 1:
                return None
            output_index = best[0]
            output_parts[output_index] = translate_without_markers(source_part)
            used_output_parts.add(output_index)
            repaired_any = True
        if not repaired_any:
            return None
        repaired = "".join(output_parts)
        repaired_markers = PLACEHOLDER_PATTERN.findall(repaired)
        return repaired if safely_duplicated_inline_math_markers(expected, repaired_markers, protected) else None

    def translate_markdown_blocks() -> str:
        return "".join(
            part if re.fullmatch(r"\n\s*\n", part) else translate_without_markers(part)
            for part in re.split(r"(\n\s*\n)", chunk)
        )

    translated_chunk = (
        translate_structured_markdown_chunk(job, chunk, protected)
        if preserve_markdown_structure
        else None
    )
    if translated_chunk is None:
        translated_chunk = translate_preserving_boundary_blank_lines(chunk)
    expected = PLACEHOLDER_PATTERN.findall(chunk)
    translated_chunk = restore_missing_boundary_markers(chunk, translated_chunk, expected)
    actual = PLACEHOLDER_PATTERN.findall(translated_chunk)
    if not safely_duplicated_inline_math_markers(expected, actual, protected):
        event_number = 0
        if job is not None:
            event_number = record_translation_marker_mismatch(
                job,
                debug_context,
                chunk,
                translated_chunk,
                protected,
                expected,
                actual,
            )
        repaired_chunk = repair_protected_marker_sequence(translated_chunk, expected)
        repair_description = "correcting its marker sequence"
        if repaired_chunk is None:
            missing = list((Counter(expected) - Counter(actual)).elements())
            repaired_chunk = repair_missing_marker_blocks(translated_chunk, missing)
            repair_description = "retranslating only the affected Markdown block"
        if repaired_chunk is not None:
            translated_chunk = repaired_chunk
            if job is not None and (not event_number or event_number <= 3 or event_number % 10 == 0):
                detail = f" #{event_number}" if event_number else ""
                job.log(
                    f"[TRANSLATION] Repaired protected literal mismatch{detail} at {debug_context} "
                    f"by {repair_description}."
                )
        else:
            if job is not None:
                if event_number and (event_number <= 3 or event_number % 10 == 0):
                    job.log(
                        f"[TRANSLATION] Protected literal mismatch #{event_number} at {debug_context}; "
                        "retrying without markers. Download translation_debug.jsonl for details."
                    )
                elif not event_number:
                    job.log("[TRANSLATION] Retrying a section without protected literals.")
            translated_chunk = translate_without_markers(chunk)
    if (
        preserve_markdown_structure
        and markdown_structure_signature(translated_chunk) != markdown_structure_signature(chunk)
    ):
        if job is not None:
            job.log(
                f"[TRANSLATION] Markdown structure changed at {debug_context}; "
                "retranslating its blocks independently."
            )
        translated_chunk = translate_markdown_blocks()
        if markdown_structure_signature(translated_chunk) != markdown_structure_signature(chunk):
            raise RuntimeError(f"Translation changed Markdown structure at {debug_context}.")
    chunk_literals = {key: protected[key] for key in expected}
    for key, value in chunk_literals.items():
        translated_chunk = translated_chunk.replace(key, value)
    if PLACEHOLDER_PATTERN.search(translated_chunk):
        raise RuntimeError("Translation output still contains an internal protected-literal marker.")
    return translated_chunk


def translate_text_block(
    job: Job | None,
    text: str,
    on_chunk: Callable[[str], None] | None = None,
    start_chunk: int = 0,
    debug_context: str = "text-block",
    preserve_markdown_structure: bool = False,
) -> str:
    translated_chunks: list[str] = []
    chunks, protected = prepared_translation_chunks(text)
    if not 0 <= start_chunk <= len(chunks):
        raise ValueError("Translation resume position is outside the document.")
    remaining = chunks[start_chunk:]
    tasks = [
        lambda chunk=chunk, chunk_index=chunk_index: translate_prepared_chunk(
            job,
            chunk,
            protected,
            f"{debug_context}:chunk:{chunk_index + 1}/{len(chunks)}",
            preserve_markdown_structure,
        )
        for chunk_index, chunk in enumerate(remaining, start=start_chunk)
    ]

    def commit(_index: int, translated_chunk: str) -> None:
        translated_chunks.append(translated_chunk)
        if on_chunk:
            on_chunk(translated_chunk)

    run_ordered_translation_tasks(job, tasks, commit)
    return "".join(translated_chunks)


def set_translation_progress(job: Job | None, total: int, completed: int = 0) -> None:
    if job is None:
        return
    with job.lock:
        active = int(job.translation_progress.get("active") or 0) if job.translation_progress else 0
        job.translation_progress = {
            "completed": completed,
            "total": total,
            "active": active,
            "concurrency": translation_concurrency(job),
        }


def checkpoint_partial_translation(job: Job | None, output: Path, completed: int, total: int) -> None:
    if job is None:
        return
    add_artifact(job, output, "translation_partial", "Partial Simplified Chinese translation")
    save_local_artifact_with_log(job, output, log=False)
    set_translation_progress(job, total, completed)
    if completed == total or completed % 5 == 0:
        job.log(f"[TRANSLATION] Saved {completed}/{total} section(s).")


def write_checkpoint(handle: io.TextIOBase, job: Job | None, output: Path, completed: int, total: int) -> None:
    handle.flush()
    os.fsync(handle.fileno())
    checkpoint_partial_translation(job, output, completed, total)


def translation_resume_manifest_path(output: Path) -> Path:
    return output.with_name(f".{output.name}.resume.json")


def translation_source_hash(source: Path) -> str:
    digest = hashlib.sha256()
    with source.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def translation_resume_completed(
    output: Path,
    source_hash: str,
    total: int,
) -> int:
    manifest_path = translation_resume_manifest_path(output)
    if not output.exists():
        manifest_path.unlink(missing_ok=True)
        return 0
    manifest: dict[str, Any] | None = None
    if manifest_path.is_file():
        try:
            candidate = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest = candidate if isinstance(candidate, dict) else None
        except (OSError, json.JSONDecodeError):
            manifest = None
    if manifest is not None:
        completed = int(manifest.get("completed") or 0)
        if (
            manifest.get("schemaVersion") == TRANSLATION_RESUME_SCHEMA_VERSION
            and manifest.get("pipelineVersion") == TRANSLATION_PIPELINE_VERSION
            and manifest.get("sourceHash") == source_hash
            and int(manifest.get("total") or 0) == total
            and 0 <= completed <= total
        ):
            return completed
        raise RuntimeError("The partial translation does not match the current source document.")
    if output.stat().st_size == 0:
        return 0
    raise RuntimeError("The partial translation has no valid resume metadata.")


def write_translation_resume_manifest(
    output: Path,
    source_hash: str,
    completed: int,
    total: int,
) -> None:
    write_durable_text(
        translation_resume_manifest_path(output),
        json.dumps(
            {
                "schemaVersion": TRANSLATION_RESUME_SCHEMA_VERSION,
                "pipelineVersion": TRANSLATION_PIPELINE_VERSION,
                "sourceHash": source_hash,
                "completed": completed,
                "total": total,
            },
            separators=(",", ":"),
            sort_keys=True,
        ),
    )


def translate_markdown(job: Job | None, source: Path, output: Path) -> None:
    text = source.read_text(encoding="utf-8")
    total = translation_unit_count(text)
    if not total:
        raise RuntimeError("The Markdown file contains no text to translate.")
    source_hash = translation_source_hash(source)
    completed = translation_resume_completed(output, source_hash, total) if output.exists() else 0
    set_translation_progress(job, total, completed)
    if completed:
        write_translation_resume_manifest(output, source_hash, completed, total)
        if job is not None:
            job.log(f"[TRANSLATION] Resuming Markdown at section {completed + 1}/{total}.")
    with output.open("a" if completed else "w", encoding="utf-8", newline="") as handle:
        def save_chunk(translated_chunk: str) -> None:
            nonlocal completed
            handle.write(translated_chunk)
            completed += 1
            handle.flush()
            os.fsync(handle.fileno())
            write_translation_resume_manifest(output, source_hash, completed, total)
            write_checkpoint(handle, job, output, completed, total)

        translate_text_block(
            job,
            text,
            save_chunk,
            start_chunk=completed,
            debug_context=source.name,
            preserve_markdown_structure=True,
        )
    translated_text = output.read_text(encoding="utf-8")
    if PLACEHOLDER_PATTERN.search(translated_text):
        write_translation_resume_manifest(output, source_hash, 0, total)
        raise RuntimeError("Translation output contains an internal protected-literal marker.")
    if markdown_structure_signature(translated_text) != markdown_structure_signature(text):
        # Keep the invalid file available for diagnostics, but make an explicit
        # retry start cleanly instead of treating the corrupt output as complete.
        write_translation_resume_manifest(output, source_hash, 0, total)
        raise RuntimeError("Translation changed the document's Markdown structure; the partial output was preserved.")
    translation_resume_manifest_path(output).unlink(missing_ok=True)


READER_TRANSLATABLE_BLOCK_TYPES = {"heading", "paragraph", "quote", "list", "table"}


def reader_blocks_to_markdown(blocks: list[dict[str, Any]]) -> str:
    rendered: list[str] = []
    for block in blocks:
        kind = str(block.get("type") or "paragraph")
        content = str(block.get("content") or "")
        if kind == "heading":
            level = max(1, min(6, int(block.get("level") or 1)))
            rendered.append(f"{'#' * level} {content}")
        elif kind == "quote":
            rendered.append("\n".join(f"> {line}" if line else ">" for line in content.splitlines()))
        elif kind == "rule":
            rendered.append("---")
        else:
            rendered.append(content)
    return "\n\n".join(rendered).rstrip() + "\n"


def translate_reader_markdown_blocks(
    job: Job | None,
    blocks: list[dict[str, Any]],
    output: Path,
) -> list[dict[str, Any]]:
    """Translate flattened reader chunks concurrently while preserving exact pair IDs."""
    prepared: dict[int, tuple[list[str], dict[str, str]]] = {}
    units: list[tuple[int, int, str, dict[str, str]]] = []
    for block_index, block in enumerate(blocks):
        content = str(block.get("content") or "")
        if block.get("type") not in READER_TRANSLATABLE_BLOCK_TYPES or not re.search(r"[A-Za-z]", content):
            continue
        chunks, protected = prepared_translation_chunks(content)
        prepared[block_index] = (chunks, protected)
        units.extend((block_index, chunk_index, chunk, protected) for chunk_index, chunk in enumerate(chunks))
    total = len(units)
    set_translation_progress(job, total)
    translated_parts: dict[int, list[str]] = {index: [] for index in prepared}
    completed = 0

    def rendered_blocks(include_untranslated_suffix: bool) -> list[dict[str, Any]]:
        rendered: list[dict[str, Any]] = []
        translated_section = ""
        for index, source_block in enumerate(blocks):
            block = dict(source_block)
            content = str(source_block.get("content") or "")
            if index in prepared:
                chunks, protected = prepared[index]
                parts = translated_parts[index]
                if include_untranslated_suffix:
                    suffix = [
                        restore_literals(chunk, {key: protected[key] for key in PLACEHOLDER_PATTERN.findall(chunk)})
                        for chunk in chunks[len(parts):]
                    ]
                    content = "".join([*parts, *suffix])
                else:
                    content = "".join(parts)
            block["content"] = content
            block["pairId"] = str(source_block.get("id") or f"b{index + 1}")
            if block.get("type") == "heading":
                translated_section = content
            block["section"] = translated_section
            rendered.append(block)
        return rendered

    tasks = [
        lambda block_index=block_index, chunk_index=chunk_index, chunk=chunk, protected=protected: translate_prepared_chunk(
            job,
            chunk,
            protected,
            f"reader:block:{blocks[block_index].get('id') or block_index + 1}:chunk:{chunk_index + 1}",
        )
        for block_index, chunk_index, chunk, protected in units
    ]

    def commit(unit_index: int, translated_chunk: str) -> None:
        nonlocal completed
        block_index, chunk_index, _chunk, _protected = units[unit_index]
        if chunk_index != len(translated_parts[block_index]):
            raise RuntimeError("Reader translation chunks completed outside the source order.")
        translated_parts[block_index].append(translated_chunk)
        completed += 1
        write_durable_text(output, reader_blocks_to_markdown(rendered_blocks(True)))
        checkpoint_partial_translation(job, output, completed, total)

    run_ordered_translation_tasks(job, tasks, commit)
    translated_blocks = rendered_blocks(False)
    write_durable_text(output, reader_blocks_to_markdown(translated_blocks))
    return translated_blocks


def normalized_reader_anchor_path(value: str) -> str:
    path = unquote(value.strip().strip("<>").split("?", 1)[0].split("#", 1)[0]).replace("\\", "/")
    return PurePosixPath(path).name.casefold()


def reader_block_anchor_tokens(block: dict[str, Any]) -> list[tuple[str, float, str]]:
    content = str(block.get("content") or "")
    kind = str(block.get("type") or "paragraph")
    tokens: dict[str, tuple[float, str]] = {}

    def add(key: str, weight: float, anchor_kind: str) -> None:
        if key and (key not in tokens or tokens[key][0] < weight):
            tokens[key] = (weight, anchor_kind)

    if kind == "heading":
        chapter = re.match(
            r"^\s*(?:(?:chapter|section)\s+|第\s*)?(\d+(?:\.\d+)*)(?:\s*[章节.]|\b)",
            content,
            flags=re.IGNORECASE,
        )
        if chapter:
            add(f"heading-number:{chapter.group(1)}", 9.0, "heading-number")
    html_id = re.sub(r"\s+", "", str(block.get("htmlId") or "")).casefold()
    if html_id:
        add(f"element-id:{html_id}", 9.0, "element-id")

    image_patterns = (
        r"!\[[^\]]*\]\(\s*<([^>]+)>",
        r"!\[[^\]]*\]\(\s*([^\s)]+)",
        r"!\[\[([^]|]+)",
    )
    for pattern in image_patterns:
        for match in re.finditer(pattern, content):
            name = normalized_reader_anchor_path(match.group(1))
            if name:
                add(f"image:{name}", 12.0, "image")

    for match in re.finditer(r"\b(?:fig(?:ure)?|图)\s*[.:#-]?\s*(\d+[a-z]?)\b", content, flags=re.IGNORECASE):
        add(f"figure-number:{match.group(1).casefold()}", 7.0, "figure-number")
    for match in re.finditer(r"\b(?:table|表)\s*[.:#-]?\s*(\d+[a-z]?)\b", content, flags=re.IGNORECASE):
        add(f"table-number:{match.group(1).casefold()}", 7.0, "table-number")
    for match in re.finditer(r"\b10\.\d{4,9}/[-._;()/:a-z0-9]+\b", content, flags=re.IGNORECASE):
        add(f"doi:{match.group(0).rstrip('.,;').casefold()}", 11.0, "doi")
    for match in re.finditer(r"https?://[^\s<>)\]]+", content, flags=re.IGNORECASE):
        add(f"url:{match.group(0).rstrip('.,;').casefold()}", 10.0, "url")

    if kind == "math":
        normalized_math = re.sub(r"\s+", "", content)
        if normalized_math:
            digest = hashlib.sha256(normalized_math.encode("utf-8")).hexdigest()[:20]
            add(f"formula:{digest}", 12.0, "formula")
    return [(key, weight, anchor_kind) for key, (weight, anchor_kind) in tokens.items()]


def reader_alignment_payload_block(block: dict[str, Any], prefix: str) -> dict[str, Any]:
    content = str(block.get("content") or "")
    if len(content) > READER_ALIGNMENT_BLOCK_PREVIEW_CHARS:
        content = f"{content[:READER_ALIGNMENT_BLOCK_PREVIEW_CHARS]}…"
    return {
        "id": f"{prefix}:{block.get('id')}",
        "type": str(block.get("type") or "paragraph"),
        "text": content,
        "anchors": [token for token, _weight, _kind in reader_block_anchor_tokens(block)],
    }


def weighted_monotonic_reader_anchors(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not candidates:
        return []
    ordered = sorted(
        candidates,
        key=lambda item: (item["sourceIndex"], item["translationIndex"], -float(item["weight"])),
    )
    scores: list[float] = []
    previous: list[int | None] = []
    for index, candidate in enumerate(ordered):
        best_score = float(candidate["weight"])
        best_previous = None
        for earlier_index, earlier in enumerate(ordered[:index]):
            if (
                earlier["sourceIndex"] < candidate["sourceIndex"]
                and earlier["translationIndex"] < candidate["translationIndex"]
                and scores[earlier_index] + float(candidate["weight"]) > best_score
            ):
                best_score = scores[earlier_index] + float(candidate["weight"])
                best_previous = earlier_index
        scores.append(best_score)
        previous.append(best_previous)
    cursor: int | None = max(range(len(ordered)), key=scores.__getitem__)
    chain: list[dict[str, Any]] = []
    while cursor is not None:
        chain.append(ordered[cursor])
        cursor = previous[cursor]
    return list(reversed(chain))


def reader_hard_alignment_anchors(
    source_blocks: list[dict[str, Any]],
    translated_blocks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    source_tokens: dict[str, list[tuple[int, float, str]]] = {}
    translated_tokens: dict[str, list[tuple[int, float, str]]] = {}
    for index, block in enumerate(source_blocks):
        for token, weight, kind in reader_block_anchor_tokens(block):
            source_tokens.setdefault(token, []).append((index, weight, kind))
    for index, block in enumerate(translated_blocks):
        for token, weight, kind in reader_block_anchor_tokens(block):
            translated_tokens.setdefault(token, []).append((index, weight, kind))

    aggregated: dict[tuple[int, int], dict[str, Any]] = {}
    for token, source_matches in source_tokens.items():
        target_matches = translated_tokens.get(token, [])
        if len(source_matches) != 1 or len(target_matches) != 1:
            continue
        source_index, source_weight, kind = source_matches[0]
        target_index, target_weight, _target_kind = target_matches[0]
        key = (source_index, target_index)
        anchor = aggregated.setdefault(key, {
            "sourceIndex": source_index,
            "translationIndex": target_index,
            "weight": 0.0,
            "kinds": [],
            "strength": "exact",
        })
        anchor["weight"] += min(source_weight, target_weight)
        anchor["kinds"].append(kind)

    source_headings = [
        (index, int(block.get("level") or 1))
        for index, block in enumerate(source_blocks)
        if block.get("type") == "heading"
    ]
    target_headings = [
        (index, int(block.get("level") or 1))
        for index, block in enumerate(translated_blocks)
        if block.get("type") == "heading"
    ]
    if (
        source_headings
        and len(source_headings) == len(target_headings)
        and [level for _index, level in source_headings] == [level for _index, level in target_headings]
    ):
        for (source_index, _level), (target_index, _target_level) in zip(source_headings, target_headings):
            key = (source_index, target_index)
            anchor = aggregated.setdefault(key, {
                "sourceIndex": source_index,
                "translationIndex": target_index,
                "weight": 0.0,
                "kinds": [],
                "strength": "strong",
            })
            anchor["weight"] += 5.0
            anchor["kinds"].append("heading-structure")

    return weighted_monotonic_reader_anchors(list(aggregated.values()))


def reader_special_sentence(block: dict[str, Any]) -> tuple[str, float] | None:
    if block.get("type") not in {"paragraph", "quote", "list"}:
        return None
    content = re.sub(r"!\[[^\]]*\]\([^)]+\)", " ", str(block.get("content") or ""))
    sentences = [
        sentence.strip()
        for sentence in re.split(r"(?<=[.!?。！？])(?:\s+|(?=[A-Z]))|\n+", content)
        if sentence.strip()
    ]
    best: tuple[str, float] | None = None
    for sentence in sentences:
        plain = re.sub(r"\s+", " ", sentence).strip()
        if len(plain) < 35 or len(plain) > 420:
            continue
        numbers = re.findall(r"\b\d+(?:\.\d+)?(?:%|[kKmMgG])?\b", plain)
        acronyms = re.findall(r"\b[A-Z][A-Za-z0-9]*(?:[-/][A-Za-z0-9]+)+\b|\b[A-Z]{2,}\d*\b", plain)
        references = re.findall(r"\b(?:fig(?:ure)?|table|section|appendix|algorithm)\s+\w+", plain, flags=re.IGNORECASE)
        citations = re.findall(r"\[[0-9,\s-]+\]|\([A-Z][A-Za-z-]+(?:\s+et al\.)?,?\s+\d{4}\)", plain)
        formula_count = len(re.findall(r"\$[^$]+\$|\\[A-Za-z]+\{", plain))
        score = (
            min(4, len(numbers)) * 1.2
            + min(3, len(acronyms)) * 1.8
            + min(2, len(references)) * 2.0
            + min(2, len(citations)) * 1.5
            + min(2, formula_count) * 2.0
        )
        if 55 <= len(plain) <= 260:
            score += 1.0
        if score >= 4.0 and (best is None or score > best[1]):
            best = (plain, score)
    return best


def reader_soft_anchor_candidates(
    source_blocks: list[dict[str, Any]],
    translated_blocks: list[dict[str, Any]],
    hard_anchors: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    boundaries = [
        {"sourceIndex": -1, "translationIndex": -1},
        *hard_anchors,
        {"sourceIndex": len(source_blocks), "translationIndex": len(translated_blocks)},
    ]
    candidates: list[dict[str, Any]] = []
    for left, right in zip(boundaries, boundaries[1:]):
        source_start = int(left["sourceIndex"]) + 1
        source_end = int(right["sourceIndex"])
        target_start = int(left["translationIndex"]) + 1
        target_end = int(right["translationIndex"])
        source_count = source_end - source_start
        target_count = target_end - target_start
        if (
            source_count < READER_SOFT_ANCHOR_MIN_GAP_BLOCKS
            or target_count < READER_SOFT_ANCHOR_MIN_GAP_BLOCKS
        ):
            continue
        subdivision_count = min(3, max(1, (max(source_count, target_count) + 19) // 20))
        for subdivision in range(subdivision_count):
            bucket_start = source_start + round(subdivision / subdivision_count * source_count)
            bucket_end = source_start + round((subdivision + 1) / subdivision_count * source_count)
            midpoint = (bucket_start + bucket_end - 1) / 2
            choices: list[tuple[float, int, str]] = []
            for source_index in range(bucket_start, bucket_end):
                special = reader_special_sentence(source_blocks[source_index])
                if not special:
                    continue
                sentence, feature_score = special
                distance_penalty = abs(source_index - midpoint) / max(1, bucket_end - bucket_start)
                choices.append((feature_score - distance_penalty, source_index, sentence))
            if not choices:
                continue
            _score, source_index, sentence = max(choices)
            expected_target_start = target_start + round(subdivision / subdivision_count * target_count)
            expected_target_end = target_start + round((subdivision + 1) / subdivision_count * target_count)
            candidates.append({
                "id": f"A{len(candidates) + 1}",
                "sourceIndex": source_index,
                "sourceBlockId": str(source_blocks[source_index].get("id")),
                "text": sentence,
                "targetStart": max(target_start, expected_target_start - 2),
                "targetEnd": min(target_end, expected_target_end + 2),
            })
            if len(candidates) >= READER_SOFT_ANCHOR_LIMIT:
                return candidates
    return candidates


def validate_reader_anchor_translations(
    payload: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> dict[str, str]:
    raw_signals = payload.get("signals")
    if not isinstance(raw_signals, list):
        raise RuntimeError("Anchor translation LLM returned no signals array.")
    expected = [candidate["id"] for candidate in candidates]
    ids: list[str] = []
    translations: dict[str, str] = {}
    for raw_signal in raw_signals:
        if not isinstance(raw_signal, dict):
            raise RuntimeError("Anchor translation signals must be JSON objects.")
        signal_id = str(raw_signal.get("id") or "")
        translation = str(raw_signal.get("translation") or "").strip()
        if not signal_id or not translation:
            raise RuntimeError("Every anchor signal must contain an ID and translation.")
        ids.append(signal_id)
        translations[signal_id] = translation
    if ids != expected:
        raise RuntimeError("Anchor translation LLM changed, skipped, duplicated, or reordered signal IDs.")
    return translations


def request_reader_anchor_translations(
    job: Job,
    candidates: list[dict[str, Any]],
) -> dict[str, str]:
    if OpenAI is None:
        raise RuntimeError("The OpenAI Python SDK is not installed.")
    if not job.translation_config:
        raise RuntimeError("Soft alignment anchors require an LLM configuration.")
    compact = [{"id": candidate["id"], "text": candidate["text"]} for candidate in candidates]
    prompt = (
        "Translate each academic signal sentence into concise Simplified Chinese. Preserve every number, "
        "formula, model name, dataset name, acronym, figure/table/section reference, and citation. "
        "Do not merge or split signals. "
        'Return only JSON in this exact shape: {"signals":[{"id":"A1","translation":"..."}]}. Input:\n'
        f"{json.dumps(compact, ensure_ascii=False, separators=(',', ':'))}"
    )
    config = job.translation_config
    last_error: RuntimeError | None = None
    for attempt in range(2):
        client = OpenAI(api_key=config["apiKey"], base_url=config["baseUrl"], timeout=180.0, max_retries=0)
        response = create_compatible_chat_completion(
            client,
            config,
            job=job,
            model=config["model"],
            temperature=0,
            messages=[
                {
                    "role": "system",
                    "content": "You translate alignment signals. Output strict JSON only and preserve all signal IDs.",
                },
                {
                    "role": "user",
                    "content": prompt if attempt == 0 else f"{prompt}\nYour previous response was invalid. Recheck every ID.",
                },
            ],
        )
        content = response.choices[0].message.content if response.choices else None
        if not content or not content.strip():
            last_error = RuntimeError("Anchor translation LLM returned empty content.")
            continue
        try:
            return validate_reader_anchor_translations(parse_reader_alignment_json(content), candidates)
        except RuntimeError as exc:
            last_error = exc
    raise last_error or RuntimeError("Soft anchor translation failed.")


def normalized_reader_alignment_text(value: str) -> str:
    return re.sub(r"[\W_]+", "", value, flags=re.UNICODE).casefold()


def reader_alignment_text_similarity(signal: str, candidate: str) -> float:
    normalized_signal = normalized_reader_alignment_text(signal)
    normalized_candidate = normalized_reader_alignment_text(candidate)
    if not normalized_signal or not normalized_candidate:
        return 0.0
    if normalized_signal in normalized_candidate:
        return 1.0
    signal_bigrams = {
        normalized_signal[index:index + 2]
        for index in range(max(1, len(normalized_signal) - 1))
    }
    candidate_bigrams = {
        normalized_candidate[index:index + 2]
        for index in range(max(1, len(normalized_candidate) - 1))
    }
    coverage = len(signal_bigrams & candidate_bigrams) / max(1, len(signal_bigrams))
    sequence = difflib.SequenceMatcher(None, normalized_signal, normalized_candidate).ratio()
    signal_features = set(re.findall(r"[a-z]+\d*(?:[-/][a-z0-9]+)*|\d+(?:\.\d+)?%?", signal.casefold()))
    candidate_features = set(re.findall(r"[a-z]+\d*(?:[-/][a-z0-9]+)*|\d+(?:\.\d+)?%?", candidate.casefold()))
    feature_score = (
        len(signal_features & candidate_features) / len(signal_features)
        if signal_features
        else 0.0
    )
    return 0.55 * coverage + 0.30 * sequence + 0.15 * feature_score


def match_reader_soft_alignment_anchors(
    candidates: list[dict[str, Any]],
    translations: dict[str, str],
    translated_blocks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    anchors: list[dict[str, Any]] = []
    last_target_index = -1
    for candidate in candidates:
        signal = translations.get(candidate["id"], "")
        scored: list[tuple[float, int, int]] = []
        for target_index in range(max(last_target_index + 1, candidate["targetStart"]), candidate["targetEnd"]):
            for width in (1, 2, 3):
                if target_index + width > candidate["targetEnd"]:
                    continue
                text = "\n".join(
                    str(translated_blocks[index].get("content") or "")
                    for index in range(target_index, target_index + width)
                )
                scored.append((reader_alignment_text_similarity(signal, text), target_index, width))
        if not scored:
            continue
        ranked = sorted(scored, reverse=True)
        best_score, best_index, best_width = ranked[0]
        best_range = range(best_index, best_index + best_width)
        second_score = next(
            (
                score
                for score, index, width in ranked[1:]
                if set(best_range).isdisjoint(range(index, index + width))
            ),
            0.0,
        )
        if (
            best_score < READER_SOFT_ANCHOR_MIN_SCORE
            or best_score - second_score < READER_SOFT_ANCHOR_MIN_MARGIN
        ):
            continue
        anchors.append({
            "sourceIndex": candidate["sourceIndex"],
            "translationIndex": best_index,
            "weight": 3.0 + best_score,
            "kinds": ["translated-signal"],
            "strength": "soft",
            "score": best_score,
        })
        last_target_index = best_index
    return anchors


def reader_alignment_anchors(
    job: Job,
    source_blocks: list[dict[str, Any]],
    translated_blocks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    set_reader_alignment_progress(job, "anchors", "正在提取标题、图片、公式等结构锚点。", 0, 1, 5)
    hard_anchors = reader_hard_alignment_anchors(source_blocks, translated_blocks)
    set_reader_alignment_progress(job, "anchors", "结构锚点提取完成。", 1, 1, 12)
    candidates = reader_soft_anchor_candidates(source_blocks, translated_blocks, hard_anchors)
    soft_anchors: list[dict[str, Any]] = []
    if candidates:
        set_reader_alignment_progress(
            job,
            "signals",
            f"正在调用 LLM 翻译 {len(candidates)} 个特征句以建立软锚点。",
            0,
            len(candidates),
            15,
        )
        try:
            translations = request_reader_anchor_translations(job, candidates)
            soft_anchors = match_reader_soft_alignment_anchors(candidates, translations, translated_blocks)
            set_reader_alignment_progress(
                job,
                "signals",
                f"已匹配 {len(soft_anchors)} 个特征句锚点。",
                len(candidates),
                len(candidates),
                25,
            )
        except RuntimeError as exc:
            job.log(f"[ALIGNMENT] Optional translated-sentence anchors were skipped: {exc}")
            set_reader_alignment_progress(
                job,
                "signals",
                "特征句锚点不可用，继续使用结构锚点。",
                len(candidates),
                len(candidates),
                25,
            )
    else:
        set_reader_alignment_progress(job, "signals", "无需额外特征句锚点。", 0, 0, 25)
    combined = weighted_monotonic_reader_anchors([*hard_anchors, *soft_anchors])
    job.log(
        f"[ALIGNMENT] Selected {len(hard_anchors)} structural/exact anchor(s) "
        f"and {len(soft_anchors)} translated-sentence anchor(s)."
    )
    return combined


def reader_alignment_batch_size(
    source_blocks: list[dict[str, Any]],
    translated_blocks: list[dict[str, Any]],
) -> int:
    payload = {
        "source": [reader_alignment_payload_block(block, "S") for block in source_blocks],
        "translation": [reader_alignment_payload_block(block, "T") for block in translated_blocks],
    }
    return len(json.dumps(payload, ensure_ascii=False))


def split_reader_alignment_batch(
    source_blocks: list[dict[str, Any]],
    translated_blocks: list[dict[str, Any]],
) -> list[tuple[list[dict[str, Any]], list[dict[str, Any]]]]:
    if (
        len(source_blocks) + len(translated_blocks) <= READER_ALIGNMENT_MAX_BLOCKS
        and reader_alignment_batch_size(source_blocks, translated_blocks) <= READER_ALIGNMENT_MAX_PAYLOAD_CHARS
    ):
        return [(source_blocks, translated_blocks)]
    if len(source_blocks) <= 1 or len(translated_blocks) <= 1:
        return [(source_blocks, translated_blocks)]
    source_midpoint = max(1, min(len(source_blocks) - 1, len(source_blocks) // 2))
    target_midpoint = round(source_midpoint / len(source_blocks) * len(translated_blocks))
    target_midpoint = max(1, min(len(translated_blocks) - 1, target_midpoint))
    return [
        *split_reader_alignment_batch(source_blocks[:source_midpoint], translated_blocks[:target_midpoint]),
        *split_reader_alignment_batch(source_blocks[source_midpoint:], translated_blocks[target_midpoint:]),
    ]


def reader_alignment_plan(
    job: Job,
    source_blocks: list[dict[str, Any]],
    translated_blocks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    anchors = reader_alignment_anchors(job, source_blocks, translated_blocks)
    plan: list[dict[str, Any]] = []
    source_start = 0
    target_start = 0

    def append_batches(source_end: int, target_end: int) -> bool:
        source_section = source_blocks[source_start:source_end]
        target_section = translated_blocks[target_start:target_end]
        if not source_section and not target_section:
            return True
        if not source_section or not target_section:
            return False
        for source_batch, target_batch in split_reader_alignment_batch(source_section, target_section):
            plan.append({
                "type": "batch",
                "sourceBlocks": source_batch,
                "translationBlocks": target_batch,
            })
        return True

    for anchor in anchors:
        source_index = int(anchor["sourceIndex"])
        target_index = int(anchor["translationIndex"])
        if source_index < source_start or target_index < target_start:
            continue
        if anchor.get("strength") in {"exact", "strong"}:
            if not append_batches(source_index, target_index):
                continue
            plan.append({
                "type": "locked",
                "group": {
                    "sourceIds": [str(source_blocks[source_index].get("id"))],
                    "translationIds": [str(translated_blocks[target_index].get("id"))],
                    "confidence": 1.0 if anchor.get("strength") == "exact" else 0.97,
                    "anchorKind": ",".join(anchor.get("kinds") or []),
                },
            })
            source_start = source_index + 1
            target_start = target_index + 1
            continue
        if source_index > source_start and target_index > target_start:
            append_batches(source_index, target_index)
            source_start = source_index
            target_start = target_index
    append_batches(len(source_blocks), len(translated_blocks))
    return plan


def reader_alignment_batches(
    job: Job,
    source_blocks: list[dict[str, Any]],
    translated_blocks: list[dict[str, Any]],
) -> list[tuple[list[dict[str, Any]], list[dict[str, Any]]]]:
    plan = reader_alignment_plan(job, source_blocks, translated_blocks)
    batches: list[tuple[list[dict[str, Any]], list[dict[str, Any]]]] = []
    for item in plan:
        if item["type"] == "batch":
            batches.append((item["sourceBlocks"], item["translationBlocks"]))
    return batches


def parse_reader_alignment_json(content: str) -> dict[str, Any]:
    stripped = content.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped, flags=re.IGNORECASE)
        stripped = re.sub(r"\s*```$", "", stripped)
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start < 0 or end <= start:
            raise RuntimeError("Alignment LLM did not return a JSON object.")
        try:
            payload = json.loads(stripped[start:end + 1])
        except json.JSONDecodeError as exc:
            raise RuntimeError("Alignment LLM returned invalid JSON.") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Alignment LLM response must be a JSON object.")
    return payload


def coalesce_reader_alignment_empty_groups(
    raw_groups: list[Any],
) -> list[dict[str, Any]]:
    """Turn LLM-style one-sided groups into adjacent bilateral groups."""
    groups: list[dict[str, Any]] = []
    pending_source: list[str] = []
    pending_translation: list[str] = []
    pending_confidences: list[float] = []

    def confidence(raw_group: dict[str, Any]) -> float:
        try:
            value = float(raw_group.get("confidence", 0.5))
        except (TypeError, ValueError):
            value = 0.5
        return max(0.0, min(1.0, value))

    def append_group(source_ids: list[str], translation_ids: list[str], value: float) -> None:
        groups.append({
            "sourceIds": source_ids,
            "translationIds": translation_ids,
            "confidence": value,
        })

    for raw_group in raw_groups:
        if not isinstance(raw_group, dict):
            raise RuntimeError("Alignment groups must be JSON objects.")
        source_ids = raw_group.get("sourceIds")
        translation_ids = raw_group.get("translationIds")
        if (
            not isinstance(source_ids, list)
            or not all(isinstance(value, str) and value for value in source_ids)
            or not isinstance(translation_ids, list)
            or not all(isinstance(value, str) and value for value in translation_ids)
        ):
            raise RuntimeError("Every alignment group must contain sourceIds and translationIds arrays of valid IDs.")
        group_confidence = confidence(raw_group)
        if source_ids and translation_ids:
            if pending_source and pending_translation:
                append_group(
                    pending_source,
                    pending_translation,
                    min(pending_confidences, default=0.5),
                )
                pending_source = []
                pending_translation = []
                pending_confidences = []
            elif pending_source:
                source_ids = [*pending_source, *source_ids]
                group_confidence = min(group_confidence, *pending_confidences)
                pending_source = []
                pending_confidences = []
            elif pending_translation:
                translation_ids = [*pending_translation, *translation_ids]
                group_confidence = min(group_confidence, *pending_confidences)
                pending_translation = []
                pending_confidences = []
            append_group(source_ids, translation_ids, group_confidence)
            continue
        pending_source.extend(source_ids)
        pending_translation.extend(translation_ids)
        pending_confidences.append(group_confidence)

    if pending_source and pending_translation:
        append_group(
            pending_source,
            pending_translation,
            min(pending_confidences, default=0.5),
        )
    elif pending_source or pending_translation:
        if not groups:
            raise RuntimeError("Alignment LLM returned no group containing content from both documents.")
        groups[-1]["sourceIds"].extend(pending_source)
        groups[-1]["translationIds"].extend(pending_translation)
        groups[-1]["confidence"] = min(
            float(groups[-1]["confidence"]),
            min(pending_confidences, default=0.5),
        )
    if not groups:
        raise RuntimeError("Alignment LLM returned no usable alignment groups.")
    return groups


def validate_reader_alignment_groups(
    payload: dict[str, Any],
    source_blocks: list[dict[str, Any]],
    translated_blocks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    raw_groups = payload.get("groups")
    if not isinstance(raw_groups, list) or not raw_groups:
        raise RuntimeError("Alignment LLM returned no alignment groups.")
    normalized_groups = coalesce_reader_alignment_empty_groups(raw_groups)
    expected_source = [f"S:{block.get('id')}" for block in source_blocks]
    expected_translation = [f"T:{block.get('id')}" for block in translated_blocks]
    source_flat: list[str] = []
    translation_flat: list[str] = []
    groups: list[dict[str, Any]] = []
    for raw_group in normalized_groups:
        source_ids = raw_group["sourceIds"]
        translation_ids = raw_group["translationIds"]
        source_flat.extend(source_ids)
        translation_flat.extend(translation_ids)
        groups.append({
            "sourceIds": [value.removeprefix("S:") for value in source_ids],
            "translationIds": [value.removeprefix("T:") for value in translation_ids],
            "confidence": raw_group["confidence"],
        })
    if source_flat != expected_source or translation_flat != expected_translation:
        raise RuntimeError(
            "Alignment LLM response changed, skipped, duplicated, or reordered one or more block IDs."
        )
    return groups


def request_reader_alignment_groups(
    job: Job,
    source_blocks: list[dict[str, Any]],
    translated_blocks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if OpenAI is None:
        raise RuntimeError("The OpenAI Python SDK is not installed.")
    if not job.translation_config:
        raise RuntimeError("Translation alignment requires an LLM configuration.")
    payload = {
        "source": [reader_alignment_payload_block(block, "S") for block in source_blocks],
        "translation": [reader_alignment_payload_block(block, "T") for block in translated_blocks],
    }
    prompt = (
        "Align the source blocks with their translated counterparts. The two documents are in the same order, "
        "but either side may contain erroneous paragraph breaks, split sentences, or merged paragraphs. "
        "Create monotonic consecutive groups supporting 1:1, 1:N, and N:1 alignment. "
        "Use every supplied ID exactly once and never change an ID. Every group must contain at least one "
        "source ID and at least one translation ID; never output an empty array. If a block appears to exist "
        "on only one side, attach its ID to the nearest neighboring group. Do not translate or rewrite text. "
        'Return only JSON in this exact shape: {"groups":[{"sourceIds":["S:b1"],'
        '"translationIds":["T:b1","T:b2"],"confidence":0.95}]}. Input:\n'
        f"{json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}"
    )
    config = job.translation_config
    last_error: RuntimeError | None = None
    for attempt in range(2):
        client = OpenAI(api_key=config["apiKey"], base_url=config["baseUrl"], timeout=180.0, max_retries=0)
        response = create_compatible_chat_completion(
            client,
            config,
            job=job,
            model=config["model"],
            temperature=0,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You align bilingual academic documents. Output strict JSON only. "
                        "All mappings must be monotonic, consecutive, complete, and based only on supplied IDs."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        prompt
                        if attempt == 0
                        else (
                            f"{prompt}\nYour previous response failed validation: {last_error}. "
                            "Return no empty arrays and recheck exact ID coverage."
                        )
                    ),
                },
            ],
        )
        content = response.choices[0].message.content if response.choices else None
        if not content or not content.strip():
            last_error = RuntimeError("Alignment LLM returned empty content.")
            continue
        try:
            response_payload = parse_reader_alignment_json(content)
            response_groups = response_payload.get("groups")
            one_sided_count = (
                sum(
                    isinstance(group, dict)
                    and (
                        not group.get("sourceIds")
                        or not group.get("translationIds")
                    )
                    for group in response_groups
                )
                if isinstance(response_groups, list)
                else 0
            )
            validated = validate_reader_alignment_groups(
                response_payload,
                source_blocks,
                translated_blocks,
            )
            if one_sided_count:
                job.log(
                    f"[ALIGNMENT] Coalesced {one_sided_count} one-sided LLM group(s) "
                    "into adjacent monotonic groups."
                )
            return validated
        except RuntimeError as exc:
            last_error = exc
    raise last_error or RuntimeError("Translation alignment failed.")


def merge_reader_alignment_groups(
    groups: list[dict[str, Any]],
    source_blocks: list[dict[str, Any]],
    translated_blocks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    source_by_id = {str(block.get("id")): block for block in source_blocks}
    translated_by_id = {str(block.get("id")): block for block in translated_blocks}
    aligned: list[dict[str, Any]] = []
    for index, group in enumerate(groups, start=1):
        source_ids = [str(value) for value in group["sourceIds"]]
        translation_ids = [str(value) for value in group["translationIds"]]
        targets = [translated_by_id[value] for value in translation_ids]
        first = targets[0]
        content = "\n\n".join(str(block.get("content") or "") for block in targets)
        aligned.append({
            **first,
            "id": f"a{index}",
            "pairId": source_ids[0],
            "sourceIds": source_ids,
            "translationIds": translation_ids,
            "content": content,
            "section": str(first.get("section") or source_by_id[source_ids[0]].get("section") or ""),
            "alignmentConfidence": group["confidence"],
            "alignmentAnchor": str(group.get("anchorKind") or ""),
        })
    return aligned


def validate_reader_alignment_coverage(
    groups: list[dict[str, Any]],
    source_blocks: list[dict[str, Any]],
    translated_blocks: list[dict[str, Any]],
) -> None:
    source_ids = [str(value) for group in groups for value in group["sourceIds"]]
    translation_ids = [str(value) for group in groups for value in group["translationIds"]]
    expected_source = [str(block.get("id")) for block in source_blocks]
    expected_translation = [str(block.get("id")) for block in translated_blocks]
    if source_ids != expected_source or translation_ids != expected_translation:
        raise RuntimeError("Anchor alignment plan did not preserve complete monotonic block coverage.")


def build_reader_alignment_groups(
    job: Job,
    source_blocks: list[dict[str, Any]],
    translated_blocks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    plan = reader_alignment_plan(job, source_blocks, translated_blocks)
    set_reader_alignment_progress(job, "planning", "单调对齐计划已生成。", 1, 1, 27)
    batch_count = sum(item["type"] == "batch" for item in plan)
    locked_count = sum(item["type"] == "locked" for item in plan)
    total_items = len(plan)
    set_reader_alignment_progress(
        job,
        "aligning",
        f"对齐计划已生成：{batch_count} 个 LLM 区间，{locked_count} 个锁定锚点。",
        0,
        total_items,
        30,
    )
    job.log(
        f"[ALIGNMENT] Aligning imported translation in {batch_count} monotonic batch(es) "
        f"between {locked_count} locked anchor pair(s)."
    )
    batch_index = 0
    for item_index, item in enumerate(plan, start=1):
        if item["type"] == "locked":
            groups.append(item["group"])
            set_reader_alignment_progress(
                job,
                "aligning",
                f"已应用结构锚点，正在处理第 {item_index}/{total_items} 个对齐区间。",
                item_index,
                total_items,
                30 + round(60 * item_index / max(1, total_items)),
            )
            continue
        batch_index += 1
        set_reader_alignment_progress(
            job,
            "aligning",
            f"正在调用 LLM 对齐第 {batch_index}/{batch_count} 个文本区间。",
            item_index - 1,
            total_items,
            30 + round(60 * (item_index - 1) / max(1, total_items)),
        )
        groups.extend(
            request_reader_alignment_groups(
                job,
                item["sourceBlocks"],
                item["translationBlocks"],
            )
        )
        job.log(f"[ALIGNMENT] Validated batch {batch_index}/{batch_count}.")
        set_reader_alignment_progress(
            job,
            "aligning",
            f"已完成第 {batch_index}/{batch_count} 个 LLM 文本区间。",
            item_index,
            total_items,
            30 + round(60 * item_index / max(1, total_items)),
        )
    validate_reader_alignment_coverage(groups, source_blocks, translated_blocks)
    set_reader_alignment_progress(job, "writing", "对齐已验证，正在写入结果。", 1, 1, 92)
    return groups


def align_reader_markdown_blocks(
    job: Job,
    source_blocks: list[dict[str, Any]],
    translated_blocks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    groups = build_reader_alignment_groups(job, source_blocks, translated_blocks)
    aligned = merge_reader_alignment_groups(groups, source_blocks, translated_blocks)
    set_reader_alignment_progress(job, "writing", "正在写入 Markdown 对照关系。", 1, 1, 95)
    return aligned


HTML_TRANSLATION_SKIPPED_TAGS = {"script", "style", "code", "pre", "math", "svg", "kbd", "samp"}
HTML_ALIGNMENT_WHOLE_TAGS = {
    "h1", "h2", "h3", "h4", "h5", "h6",
    "p", "blockquote", "pre", "table", "figure", "ul", "ol",
}
HTML_ALIGNMENT_CONTAINER_TAGS = {
    "body", "main", "article", "section", "div", "header",
}
HTML_ALIGNMENT_IGNORED_TAGS = {
    "script", "style", "noscript", "template", "link", "meta",
    "nav", "footer", "aside",
}


def html_alignment_has_content(tag: Tag) -> bool:
    return bool(tag.get_text(" ", strip=True) or tag.find(["img", "svg", "math", "canvas", "video"]))


def html_alignment_is_hidden(tag: Tag) -> bool:
    style = re.sub(r"\s+", "", str(tag.get("style") or "")).casefold()
    return (
        tag.has_attr("hidden")
        or str(tag.get("aria-hidden") or "").casefold() == "true"
        or "display:none" in style
        or "visibility:hidden" in style
    )


def html_alignment_has_own_inline_content(tag: Tag) -> bool:
    for child in tag.children:
        if isinstance(child, NavigableString):
            if str(child).strip():
                return True
            continue
        if (
            not isinstance(child, Tag)
            or child.name in HTML_ALIGNMENT_IGNORED_TAGS
            or html_alignment_is_hidden(child)
        ):
            continue
        if child.name not in HTML_ALIGNMENT_CONTAINER_TAGS and child.name not in HTML_ALIGNMENT_WHOLE_TAGS:
            return True
    return False


def html_alignment_semantic_tags(soup: BeautifulSoup) -> list[Tag]:
    root = soup.body or soup
    blocks: list[Tag] = []

    def visit(parent: Tag | BeautifulSoup) -> None:
        for child in list(parent.children):
            if isinstance(child, NavigableString):
                text = str(child).strip()
                if not text or parent is not root:
                    continue
                wrapper = soup.new_tag("div")
                wrapper.string = text
                child.replace_with(wrapper)
                blocks.append(wrapper)
                continue
            if (
                not isinstance(child, Tag)
                or child.name in HTML_ALIGNMENT_IGNORED_TAGS
                or html_alignment_is_hidden(child)
                or not html_alignment_has_content(child)
            ):
                continue
            if child.name in HTML_ALIGNMENT_WHOLE_TAGS:
                blocks.append(child)
            elif (
                child.name in HTML_ALIGNMENT_CONTAINER_TAGS
                and not html_alignment_has_own_inline_content(child)
            ):
                visit(child)
            else:
                blocks.append(child)

    visit(root)
    return blocks


def html_tag_reader_block(tag: Tag, index: int, section: str) -> dict[str, Any]:
    name = str(tag.name or "div").lower()
    content = tag.get_text(" ", strip=True)
    image_references = []
    for image in tag.find_all("img"):
        src = str(image.get("src") or "").strip()
        alt = str(image.get("alt") or "").strip()
        if src:
            image_references.append(f"![{alt}]({src})")
    if image_references:
        content = "\n".join(filter(None, [content, *image_references]))
    if re.fullmatch(r"h[1-6]", name):
        kind = "heading"
        level = int(name[1])
    elif name == "blockquote":
        kind = "quote"
        level = None
    elif name in {"ul", "ol"}:
        kind = "list"
        level = None
    elif name == "table":
        kind = "table"
        level = None
    elif name == "figure":
        kind = "figure"
        level = None
    elif name == "pre":
        kind = "code"
        level = None
    elif name == "math" or tag.find("math"):
        kind = "math"
        level = None
    else:
        kind = "paragraph"
        level = None
    block: dict[str, Any] = {
        "id": f"b{index}",
        "type": kind,
        "section": section,
        "content": content,
        "htmlTag": name,
        "htmlId": str(tag.get("id") or ""),
    }
    if level is not None:
        block["level"] = level
    return block


def html_to_reader_alignment_blocks(
    soup: BeautifulSoup,
) -> tuple[list[dict[str, Any]], dict[str, Tag]]:
    blocks: list[dict[str, Any]] = []
    tags_by_id: dict[str, Tag] = {}
    section = ""
    for index, tag in enumerate(html_alignment_semantic_tags(soup), start=1):
        block = html_tag_reader_block(tag, index, section)
        if block["type"] == "heading":
            section = str(block["content"])
            block["section"] = section
        blocks.append(block)
        tags_by_id[str(block["id"])] = tag
    if not blocks:
        raise RuntimeError("The HTML document contains no semantic reading blocks.")
    return blocks, tags_by_id


def align_reader_html_documents(job: Job, source: Path, translated: Path) -> list[dict[str, Any]]:
    source_soup = BeautifulSoup(source.read_text(encoding="utf-8"), "html.parser")
    translated_soup = BeautifulSoup(translated.read_text(encoding="utf-8"), "html.parser")
    source_blocks, source_tags = html_to_reader_alignment_blocks(source_soup)
    translated_blocks, translated_tags = html_to_reader_alignment_blocks(translated_soup)
    groups = build_reader_alignment_groups(job, source_blocks, translated_blocks)
    for index, group in enumerate(groups, start=1):
        pair_id = f"reader-aligned-{index}"
        confidence = f"{float(group.get('confidence', 0.5)):.3f}"
        anchor_kind = str(group.get("anchorKind") or "")
        for block_id in group["sourceIds"]:
            tag = source_tags[str(block_id)]
            tag["data-reader-pair-id"] = pair_id
            tag["data-reader-alignment-confidence"] = confidence
            if anchor_kind:
                tag["data-reader-alignment-anchor"] = anchor_kind
        for block_id in group["translationIds"]:
            tag = translated_tags[str(block_id)]
            tag["data-reader-pair-id"] = pair_id
            tag["data-reader-alignment-confidence"] = confidence
            if anchor_kind:
                tag["data-reader-alignment-anchor"] = anchor_kind
    write_durable_text(source, str(source_soup))
    write_durable_text(translated, str(translated_soup))
    set_reader_alignment_progress(job, "writing", "正在写入 HTML DOM 对照关系。", 1, 1, 95)
    return groups


def html_translation_nodes(soup: BeautifulSoup) -> list[Any]:
    return [
        node for node in soup.find_all(string=True)
        if not isinstance(node, (Comment, Declaration, Doctype, ProcessingInstruction))
        and node.parent
        and not any(parent.name in HTML_TRANSLATION_SKIPPED_TAGS for parent in node.parents)
        and str(node).strip() and re.search(r"[A-Za-z]", str(node))
    ]


def html_translation_parts_path(output: Path) -> Path:
    return output.with_name(f".{output.name}.parts")


def write_durable_text(output: Path, text: str) -> None:
    temporary = output.with_name(f".{output.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)


def merge_html_translation_parts(
    source: Path,
    output: Path,
    staged_parts: dict[int, list[Path]],
    expected_parts: list[int] | None = None,
) -> None:
    soup = BeautifulSoup(source.read_text(encoding="utf-8"), "html.parser")
    nodes = html_translation_nodes(soup)
    if expected_parts is not None and len(nodes) != len(expected_parts):
        raise RuntimeError("The HTML structure changed while translation parts were being merged.")
    if expected_parts is not None and set(staged_parts) != set(range(len(nodes))):
        raise RuntimeError("One or more HTML sections have no staged translation parts.")
    for node_index, part_paths in staged_parts.items():
        if node_index >= len(nodes):
            raise RuntimeError("A staged HTML translation part no longer matches the source document.")
        if expected_parts is not None and len(part_paths) != expected_parts[node_index]:
            raise RuntimeError(f"HTML section {node_index + 1} is missing one or more translated parts.")
        node = nodes[node_index]
        value = str(node)
        leading = value[: len(value) - len(value.lstrip())]
        trailing = value[len(value.rstrip()):]
        translated = "".join(path.read_text(encoding="utf-8") for path in part_paths)
        if expected_parts is None:
            protected_text, protected = protect_literals(value.strip())
            untranslated_chunks = text_chunks(protected_text)[len(part_paths):]
            translated += "".join(
                restore_literals(
                    chunk,
                    {key: protected[key] for key in PLACEHOLDER_PATTERN.findall(chunk)},
                )
                for chunk in untranslated_chunks
            )
        node.replace_with(soup.new_string(f"{leading}{translated}{trailing}"))
    write_durable_text(output, str(soup))


def annotate_reader_html_pair_ids(source: Path) -> None:
    """Attach deterministic IDs before reader translation so both DOMs align."""
    soup = BeautifulSoup(source.read_text(encoding="utf-8"), "html.parser")
    root = soup.body or soup
    candidates = [
        tag for tag in root.find_all(True)
        if tag.name not in {"script", "style", "noscript", "template", "link", "meta"}
    ]
    for index, tag in enumerate(candidates, start=1):
        tag["data-reader-pair-id"] = f"reader-pair-{index}"
    write_durable_text(source, str(soup))


def translate_html(job: Job | None, source: Path, output: Path) -> None:
    soup = BeautifulSoup(source.read_text(encoding="utf-8"), "html.parser")
    nodes = html_translation_nodes(soup)
    prepared_nodes = [prepared_translation_chunks(str(node).strip()) for node in nodes]
    expected_parts = [len(chunks) for chunks, _protected in prepared_nodes]
    total = sum(expected_parts)
    if not total:
        raise RuntimeError("The HTML file contains no visible text to translate.")
    set_translation_progress(job, total)
    source_hash = translation_source_hash(source)
    parts_dir = html_translation_parts_path(output)
    parts_dir.mkdir(parents=True, exist_ok=True)
    staged_parts: dict[int, list[Path]] = {}
    completed = 0
    found_uncommitted = False
    for node_index, node in enumerate(nodes):
        prefix = f"{node_index:06d}-"
        existing_parts = sorted(
            path
            for path in parts_dir.glob(f"{prefix}*.txt")
            if re.fullmatch(rf"{node_index:06d}-(\d{{6}})\.txt", path.name)
        )
        expected_names = [f"{node_index:06d}-{part_index:06d}.txt" for part_index in range(len(existing_parts))]
        if [path.name for path in existing_parts] != expected_names or len(existing_parts) > expected_parts[node_index]:
            raise RuntimeError(f"HTML section {node_index + 1} has invalid resume parts.")
        if found_uncommitted and existing_parts:
            raise RuntimeError("HTML resume parts are not a continuous source prefix.")
        staged_parts[node_index] = existing_parts
        completed += len(existing_parts)
        if len(existing_parts) < expected_parts[node_index]:
            found_uncommitted = True
    if completed:
        if not output.is_file():
            raise RuntimeError("HTML resume parts exist without a partial document.")
        recorded_completed = translation_resume_completed(output, source_hash, total)
        if recorded_completed != completed:
            raise RuntimeError("HTML resume metadata does not match its saved translation parts.")
        set_translation_progress(job, total, completed)
        merge_html_translation_parts(source, output, staged_parts)
        if job is not None:
            job.log(f"[TRANSLATION] Resuming HTML at section {completed + 1}/{total}.")
    units: list[tuple[int, int, str, dict[str, str]]] = []
    for node_index, (chunks, protected) in enumerate(prepared_nodes):
        units.extend(
            (node_index, part_index, chunk, protected)
            for part_index, chunk in enumerate(chunks[len(staged_parts[node_index]):], start=len(staged_parts[node_index]))
        )
    tasks = [
        lambda node_index=node_index, part_index=part_index, chunk=chunk, protected=protected: translate_prepared_chunk(
            job,
            chunk,
            protected,
            f"{source.name}:html-node:{node_index + 1}:chunk:{part_index + 1}/{expected_parts[node_index]}",
        )
        for node_index, part_index, chunk, protected in units
    ]

    def save_unit(unit_index: int, translated_chunk: str) -> None:
        nonlocal completed
        node_index, part_index, _chunk, _protected = units[unit_index]
        if part_index != len(staged_parts[node_index]):
            raise RuntimeError("HTML translation chunks completed outside the source order.")
        part_path = parts_dir / f"{node_index:06d}-{part_index:06d}.txt"
        write_durable_text(part_path, translated_chunk)
        staged_parts[node_index].append(part_path)
        merge_html_translation_parts(source, output, staged_parts)
        completed += 1
        write_translation_resume_manifest(output, source_hash, completed, total)
        checkpoint_partial_translation(job, output, completed, total)

    run_ordered_translation_tasks(job, tasks, save_unit)
    merge_html_translation_parts(source, output, staged_parts, expected_parts)
    shutil.rmtree(parts_dir, ignore_errors=True)
    translation_resume_manifest_path(output).unlink(missing_ok=True)


def partial_translation_path(source: Path) -> Path:
    stem = source.stem if source.stem.endswith("_zh-CN") else f"{source.stem}_zh-CN"
    return source.with_name(f"{stem}.partial{source.suffix}")


def translation_source_sort_key(source: Path) -> tuple[int, str]:
    """Use a stable Markdown-first order for backend task creation."""
    priority = {".md": 0, ".mmd": 1, ".html": 2, ".htm": 3}
    return priority.get(source.suffix.lower(), 99), source.as_posix().casefold()


def package_document_translations(job: Job, outputs: list[Path]) -> tuple[Path, str]:
    if not outputs:
        raise RuntimeError("No translation output was produced.")
    if len(outputs) == 1:
        return outputs[0], outputs[0].name
    output_dir = job.root / "output"
    archive = job.root / "document_translate_results.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        for output in outputs:
            bundle.write(output, output.relative_to(output_dir))
    return archive, archive.name


def markdown_repair_output_path(output_root: Path, relative: Path) -> Path:
    return output_root / relative.parent / f"{relative.stem}_fixed{relative.suffix}"


def package_markdown_repair_outputs(job: Job, outputs: list[Path]) -> tuple[Path, str]:
    if not outputs:
        raise RuntimeError("Markdown repair produced no output.")
    if len(outputs) == 1:
        return outputs[0], outputs[0].name
    output_dir = job.root / "output"
    archive = job.root / "markdown_repair_results.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        for output in outputs:
            bundle.write(output, output.relative_to(output_dir))
    return archive, archive.name


def run_markdown_repair_tool(job: Job) -> None:
    """Expose the existing pre/post translation Markdown repairs without translating prose."""
    input_dir = job.root / "input"
    sources = sorted(
        (path for path in input_dir.rglob("*") if path.is_file() and path.suffix.lower() in TRANSLATED_MARKDOWN_SUFFIXES),
        key=lambda path: path.relative_to(input_dir).as_posix().casefold(),
    )
    if not sources:
        raise RuntimeError("No Markdown or MMD files were uploaded.")
    deep_repair = bool(job.options.get("deepRepair", False))
    math_repair = bool(job.options.get("mathRepair", True))
    footnote_repair = bool(job.options.get("footnoteRepair", True))
    normalize_punctuation = bool(job.options.get("normalizeChinesePunctuation", False))
    output_root = job.root / "output"
    output_root.mkdir(exist_ok=True)
    candidates: dict[Path, tuple[str, list[str]]] = {}
    originals: dict[Path, str] = {}
    diagnostics: dict[Path, list[str]] = {}
    job.phase = "markdown_repair"
    enabled_modes = ", ".join(
        mode for enabled, mode in (
            (deep_repair, "deep structure"),
            (math_repair, "display math"),
            (footnote_repair, "merged footnotes"),
            (normalize_punctuation, "Chinese punctuation"),
        ) if enabled
    )
    job.log(f"[MARKDOWN REPAIR] Processing {len(sources)} file(s): {enabled_modes} repair enabled.")
    for source in sources:
        relative = source.relative_to(input_dir)
        output = markdown_repair_output_path(output_root, relative)
        original = source.read_text(encoding="utf-8")
        originals[output] = original
        try:
            job.log(f"[MARKDOWN REPAIR] Analyzing {relative.as_posix()}.")
            changes: list[str] = []
            repaired, image_path_repairs = repair_unbracketed_markdown_image_paths(original)
            if image_path_repairs:
                changes.append(f"wrapped {image_path_repairs} local image path(s) containing spaces in angle brackets")
            if deep_repair:
                repaired = repair_markdown_text(
                    job,
                    repaired,
                    source.suffix.lower().lstrip("."),
                    repair_footnotes=footnote_repair,
                )
            else:
                if footnote_repair:
                    repaired, footnote_changes = repair_merged_markdown_footnotes(repaired)
                    changes.extend(footnote_changes)
            if normalize_punctuation:
                repaired, punctuation_changes = normalize_chinese_punctuation(repaired)
                changes.extend(punctuation_changes)
            if math_repair:
                repaired, math_changes, errors = postprocess_translated_markdown_text(repaired)
                changes.extend(math_changes)
                if errors:
                    diagnostics[output] = errors
                    continue
            candidates[output] = (repaired, changes)
        except Exception as exc:
            diagnostics[output] = [str(exc)]
    if diagnostics:
        preserved: list[Path] = []
        for output, original in originals.items():
            raw_output = translation_unfixed_path(output)
            raw_output.parent.mkdir(parents=True, exist_ok=True)
            write_durable_text(raw_output, original)
            preserved.append(raw_output)
        for output, errors in diagnostics.items():
            job.log(f"[MARKDOWN REPAIR] {output.name} failed validation: {'; '.join(errors)}")
        job.download_path, job.download_name = package_markdown_repair_outputs(job, preserved)
        job.phase = "markdown_repair_failed"
        raise MarkdownRepairOutputError(diagnostics, preserved)
    outputs: list[Path] = []
    for output, (repaired, changes) in candidates.items():
        output.parent.mkdir(parents=True, exist_ok=True)
        write_durable_text(output, repaired)
        for change in changes:
            job.log(f"[MARKDOWN REPAIR] {output.name} {change}")
        job.log(f"[MARKDOWN REPAIR] Saved {output.name}.")
        outputs.append(output)
    job.download_path, job.download_name = package_markdown_repair_outputs(job, outputs)
    job.phase = "markdown_repair_complete"


TRANSLATION_BACKEND_SYSTEM_PROMPT = (
    "You are a professional translator. Translate the visible natural-language prose in the entire "
    "user-provided Markdown, MMD, or raw HTML chunk into Simplified Chinese. Preserve all markup, "
    "HTML tags, attributes, URLs, DOCTYPE declarations, scripts, styles, code, and formatting exactly; "
    "do not add wrappers or explanations. Return only the translated chunk."
)


def translation_backend_executable() -> Path:
    configured = os.environ.get("TRANSLATION_BACKEND_BINARY", "").strip()
    if configured:
        return Path(configured).expanduser()
    binary_name = "ai-markdown-translator.exe" if os.name == "nt" else "ai-markdown-translator"
    return RUNTIME_DIR / "translation-backend" / binary_name


def translated_backend_output(output_root: Path, relative: Path) -> Path:
    return output_root / relative.parent / f"{relative.stem}_zh-CN{relative.suffix}"


def run_translation_backend(
    job: Job,
    source_root: Path,
    output_root: Path,
    sources: list[Path],
) -> list[Path]:
    """Run the backend-only AI-Markdown-Translator adapter.

    The selected API key is serialized only to the child process stdin. The Go
    backend persists task/chunk state in SQLite but its schema has no credential
    fields, keeping the existing LLM manager authoritative.
    """
    if not job.translation_config:
        raise RuntimeError("No LLM configuration was selected for this translation.")
    executable = translation_backend_executable()
    if not executable.is_file():
        raise RuntimeError(
            "AI-Markdown-Translator backend is not built. Run the project launcher again "
            "so it can build translation_backend."
        )
    # Keep the caller's absolute spelling intact. On macOS, /var is commonly a
    # symlink to /private/var; resolving only one side turns otherwise identical
    # task paths into paths that cannot be compared with ``relative_to``.
    source_root = source_root.absolute()
    output_root.mkdir(parents=True, exist_ok=True)
    output_root = output_root.absolute()
    relative_sources: list[Path] = []
    for source in sources:
        source = source.absolute()
        try:
            relative = source.relative_to(source_root)
        except ValueError as exc:
            raise ValueError(f"Translation source is outside its task directory: {source.name}") from exc
        if relative.suffix.lower() not in TRANSLATABLE_SUFFIXES:
            raise ValueError(f"AI-Markdown-Translator only accepts Markdown, MMD, or HTML: {relative.as_posix()}")
        relative_sources.append(relative)
    config = job.translation_config
    task_identity = "\0".join(relative.as_posix() for relative in relative_sources)
    backend_task_id = f"{job.id}-{hashlib.sha256(task_identity.encode()).hexdigest()[:16]}"
    payload = {
        "taskId": backend_task_id,
        "database": str(job.root / "translation_backend.sqlite3"),
        "sourceDir": str(source_root),
        "targetDir": str(output_root),
        "files": [relative.as_posix() for relative in relative_sources],
        "concurrency": llm_concurrency(config.get("concurrency")),
        "llm": {
            "baseUrl": config["baseUrl"],
            "apiKey": config["apiKey"],
            "model": config["model"],
            "systemPrompt": TRANSLATION_BACKEND_SYSTEM_PROMPT,
            "timeoutSeconds": 300,
            # The upstream splitter estimates one token per rune and applies a
            # 90% safety factor. 8k therefore produces roughly 7.2k-character
            # chunks, leaving ample output/reasoning budget for Chinese text.
            "maxInputTokens": 8_000,
            "maxOutputTokens": 32_000,
        },
    }
    command = [str(executable)]
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(PROJECT_ROOT),
        creationflags=creation_flags,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    process.stdin.write(json.dumps(payload, ensure_ascii=False))
    process.stdin.close()
    backend_error = ""
    try:
        for raw_line in process.stdout:
            line = raw_line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                safe_line = line.replace(str(config["apiKey"]), "[redacted]")
                job.log(f"[TRANSLATION BACKEND] {safe_line[:1000]}")
                continue
            event_type = event.get("type")
            if event_type == "progress":
                progress = {
                    "total": max(0, int(event.get("total") or 0)),
                    "completed": max(0, int(event.get("completed") or 0)),
                    "active": max(0, int(event.get("active") or 0)),
                    "concurrency": max(1, int(event.get("concurrency") or 1)),
                }
                with job.lock:
                    job.translation_progress = progress
            elif event_type in {"log", "error"}:
                message = str(event.get("message") or "").replace(str(config["apiKey"]), "[redacted]")
                if message:
                    job.log(f"[TRANSLATION BACKEND] {message[:1000]}")
                if event_type == "error":
                    backend_error = message
    finally:
        process.stdout.close()
    return_code = process.wait()
    if return_code != 0:
        raise RuntimeError(backend_error or f"AI-Markdown-Translator backend exited with code {return_code}.")
    outputs = [translated_backend_output(output_root, relative) for relative in relative_sources]
    missing = [output.relative_to(output_root).as_posix() for output in outputs if not output.is_file()]
    if missing:
        raise RuntimeError(f"AI-Markdown-Translator did not produce: {', '.join(missing)}")
    return outputs


def run_document_translation(job: Job) -> None:
    input_dir = job.root / "input"
    sources = sorted(
        (path for path in input_dir.rglob("*") if path.is_file() and path.suffix.lower() in TRANSLATABLE_SUFFIXES),
        key=lambda path: translation_source_sort_key(path.relative_to(input_dir)),
    )
    if not sources:
        raise RuntimeError("No Markdown, MMD, or HTML files were uploaded.")
    output_dir = job.root / "output"
    output_dir.mkdir(exist_ok=True)
    job.phase = "translation"
    job.log(f"[TRANSLATION] Sending {len(sources)} file(s) to the AI-Markdown-Translator backend.")
    try:
        outputs = run_translation_backend(job, input_dir, output_dir, sources)
        outputs = finalize_translated_markdown_outputs(job, outputs)
    except TranslationMarkdownPostprocessError as exc:
        job.phase = "translation_postprocess_failed"
        job.download_path, job.download_name = package_artifacts(job, exc.preserved_outputs)
        raise
    except Exception:
        if (job.translation_progress or {}).get("completed", 0):
            job.phase = "translation_partial"
        raise
    job.download_path, job.download_name = package_document_translations(job, outputs)
    job.phase = "translation_complete"


def run_pdf_translation(job: Job) -> None:
    if not job.pending_artifact_id:
        raise RuntimeError("No OCR output was selected for translation.")
    selected = artifact_for_id(job, job.pending_artifact_id)
    if not selected:
        raise RuntimeError("The selected OCR output is no longer available.")
    artifact, source = selected
    if not artifact.get("translationSupported"):
        raise ValueError("AI-Markdown-Translator only supports Markdown, MMD, or HTML OCR outputs.")
    output = source.with_name(f"{source.stem}_zh-CN{source.suffix}")
    if output.exists():
        raise FileExistsError(f"A translation already exists: {output.name}")
    job.phase = "translation"
    job.log(f"[TRANSLATION] Sending {source.name} to the AI-Markdown-Translator backend.")
    try:
        [produced] = run_translation_backend(job, source.parent, source.parent, [source])
        if produced != output:
            raise RuntimeError("AI-Markdown-Translator returned an unexpected output path.")
        finalize_translated_markdown_outputs(job, [output])
        save_local_artifact(job, output)
        add_artifact(job, output, "translation", "Simplified Chinese translation")
        job.download_path, job.download_name = package_pdf_artifacts(job)
        job.phase = "translation_complete"
        job.pending_artifact_id = None
    except TranslationMarkdownPostprocessError as exc:
        for raw_output in exc.preserved_outputs:
            save_local_artifact(job, raw_output)
            add_artifact(job, raw_output, "translation_unfixed", "Unfixed Markdown translation")
        job.download_path, job.download_name = package_pdf_artifacts(job)
        job.phase = "translation_postprocess_failed"
        raise
    except Exception:
        if (job.translation_progress or {}).get("completed", 0):
            job.phase = "translation_partial"
            progress = job.translation_progress or {"completed": 0, "total": 0}
            job.log(
                "[TRANSLATION] Failed after persisting "
                f"{progress['completed']}/{progress['total']} chunk(s) in the backend database."
            )
        raise


def markdown_to_reader_blocks(text: str) -> list[dict[str, Any]]:
    """Create stable reading units for common Markdown and Mathpix MMD output.

    Mathpix uses a mixture of Markdown and LaTeX environments, so a paragraph
    splitter is not enough: it would split ``align`` equations, tables, and
    nested lists into unrelated blocks.  This deliberately keeps source syntax
    intact for the browser renderer instead of attempting lossy conversion.
    """
    blocks: list[dict[str, Any]] = []
    paragraph: list[str] = []
    fenced: list[str] | None = None
    math: list[str] | None = None
    latex: list[str] | None = None
    section = ""

    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if lines and lines[0].strip() == "---":
        try:
            closing = next(index for index, line in enumerate(lines[1:], start=1) if line.strip() in {"---", "..."})
            lines = lines[closing + 1:]
        except StopIteration:
            pass

    def emit_paragraph() -> None:
        nonlocal paragraph
        content = "\n".join(paragraph).strip()
        if content:
            blocks.append({"id": f"b{len(blocks) + 1}", "type": "paragraph", "section": section, "content": content})
        paragraph = []

    def emit(kind: str, content: str, **extra: Any) -> None:
        blocks.append({"id": f"b{len(blocks) + 1}", "type": kind, "section": section, "content": content, **extra})

    def is_table_divider(line: str) -> bool:
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell or "") for cell in cells)

    index = 0
    while index < len(lines):
        raw_line = lines[index]
        line = raw_line.rstrip()
        if fenced is not None:
            fenced.append(raw_line)
            if line.startswith("```") or line.startswith("~~~"):
                emit("code", "\n".join(fenced))
                fenced = None
            index += 1
            continue
        if math is not None:
            math.append(raw_line)
            if line == "$$" or line == r"\]":
                emit("math", "\n".join(math))
                math = None
            index += 1
            continue
        if latex is not None:
            latex.append(raw_line)
            if re.match(r"^\\end\{(?:equation\*?|align\*?|aligned|alignat\*?|gather\*?|multline\*?|cases|matrix|pmatrix|bmatrix|vmatrix|Vmatrix|array|smallmatrix|split)\}", line):
                emit("math", "\\[\n" + "\n".join(latex) + "\n\\]")
                latex = None
            index += 1
            continue
        if line.startswith(("```", "~~~")):
            emit_paragraph()
            fenced = [raw_line]
            index += 1
            continue
        if line in {"$$", r"\["}:
            emit_paragraph()
            math = [raw_line]
            index += 1
            continue
        if re.match(r"^\\begin\{(?:equation\*?|align\*?|aligned|alignat\*?|gather\*?|multline\*?|cases|matrix|pmatrix|bmatrix|vmatrix|Vmatrix|array|smallmatrix|split)\}", line):
            emit_paragraph()
            latex = [raw_line]
            index += 1
            continue
        heading = re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", line)
        latex_heading = re.match(r"^\\(?:sub)*section\*?\{(.+?)\}\s*$", line)
        if heading or latex_heading:
            emit_paragraph()
            section = heading.group(2) if heading else latex_heading.group(1)
            level = len(heading.group(1)) if heading else max(1, line.count("sub") + 1)
            emit("heading", section, level=level)
            index += 1
            continue
        if index + 1 < len(lines) and line.strip() and re.fullmatch(r"[=-]{3,}\s*", lines[index + 1]):
            emit_paragraph()
            section = line.strip()
            emit("heading", section, level=1 if lines[index + 1].lstrip().startswith("=") else 2)
            index += 2
            continue
        if re.fullmatch(r"\s{0,3}(?:[-*_]\s*){3,}", line):
            emit_paragraph()
            emit("rule", "")
            index += 1
            continue
        if line.strip() and index + 1 < len(lines) and "|" in line and is_table_divider(lines[index + 1]):
            emit_paragraph()
            table = [raw_line, lines[index + 1]]
            index += 2
            while index < len(lines) and lines[index].strip() and "|" in lines[index]:
                table.append(lines[index])
                index += 1
            emit("table", "\n".join(table))
            continue
        if line.lstrip().startswith(">"):
            emit_paragraph()
            quote: list[str] = []
            while index < len(lines) and lines[index].lstrip().startswith(">"):
                quote.append(re.sub(r"^\s*>\s?", "", lines[index]))
                index += 1
            emit("quote", "\n".join(quote))
            continue
        if re.match(r"^\s*(?:[-+*]|\d+[.)])\s+", line):
            emit_paragraph()
            list_lines: list[str] = []
            while index < len(lines) and (not lines[index].strip() or re.match(r"^\s*(?:[-+*]|\d+[.)])\s+", lines[index])):
                list_lines.append(lines[index])
                index += 1
            emit("list", "\n".join(list_lines).strip())
            continue
        if not line.strip():
            emit_paragraph()
            index += 1
            continue
        paragraph.append(raw_line)
        index += 1
    emit_paragraph()
    if fenced:
        emit("code", "\n".join(fenced))
    if math:
        emit("math", "\n".join(math))
    if latex:
        emit("math", "\\[\n" + "\n".join(latex) + "\n\\]")
    if not blocks:
        raise RuntimeError("The document contains no readable Markdown content.")
    return blocks


def reader_source_artifact(job: Job) -> Path:
    output_dir = job.root / "output"
    candidates = [output_dir / f"{job.source_stem}.mmd", output_dir / f"{job.source_stem}.md"]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise RuntimeError("OCR finished without a Markdown document for the reader.")


def reader_source_cache_key(source: Path) -> str:
    digest = hashlib.sha256()
    digest.update(f"reader-cache-v{READER_CACHE_SCHEMA_VERSION}\0{source.suffix.lower()}\0".encode())
    with source.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def clean_reader_title(value: Any) -> str | None:
    title = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value or ""))
    title = re.sub(r"\s+", " ", title).strip(" \t\r\n-–—_|")
    if not title or len(title) > 500:
        return None
    if (
        re.fullmatch(
            r"[{(]?[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}[})]?",
            title,
            flags=re.IGNORECASE,
        )
        or re.fullmatch(r"[0-9a-f]{24,64}", title, flags=re.IGNORECASE)
    ):
        return None
    if title.casefold() in {"untitled", "document", "pdf", "mathpix", "ocr document", "ocr 文档"}:
        return None
    return title


def reader_source_display_title(source_name: str) -> str | None:
    normalized = source_name.replace("\\", "/").strip("/")
    if not normalized:
        return None
    for part in PurePosixPath(normalized).parts:
        assets_match = re.fullmatch(r"(.+?)\.(?:html?|md|mmd|pdf)-assets", part, flags=re.IGNORECASE)
        if assets_match:
            return clean_reader_title(assets_match.group(1).replace("_", " "))
    return clean_reader_title(PurePosixPath(normalized).stem)


def pdf_document_title(source: Path) -> str | None:
    if PdfReader is None:
        return None
    try:
        metadata = PdfReader(str(source), strict=False).metadata
        return clean_reader_title(metadata.title if metadata else None)
    except Exception:
        return None


def cached_ocr_html_title(source: Path) -> str | None:
    try:
        with source.open("r", encoding="utf-8", errors="replace") as handle:
            soup = BeautifulSoup(handle.read(512 * 1024), "html.parser")
    except OSError:
        return None
    for selector, attribute in (
        ('meta[name="citation_title"]', "content"),
        ('meta[name="dc.title"]', "content"),
        ("title", None),
        ("h1", None),
    ):
        node = soup.select_one(selector)
        value = node.get(attribute) if node and attribute else node.get_text(" ", strip=True) if node else None
        title = clean_reader_title(value)
        if title:
            return title
    return None


def paired_reader_title(source: Path) -> str:
    stem = re.sub(r"^\d{4}_", "", source.stem)
    return clean_reader_title(stem) or stem or "阅读文档"


def reader_pair_record(source: Path, translated: Path, *, base: Path) -> dict[str, Any]:
    source_relative = source.relative_to(base).as_posix()
    translated_relative = translated.relative_to(base).as_posix()
    pair_id = hashlib.sha256(f"{source_relative}\0{translated_relative}".encode()).hexdigest()[:24]
    return {
        "id": pair_id,
        "title": paired_reader_title(source),
        "sourceName": source_relative,
        "translatedName": translated_relative,
        "source": source,
        "translated": translated,
    }


def job_reader_pair_records(job: Job) -> list[dict[str, Any]]:
    """Return completed source/translation pairs that can open in the reader."""
    if job.status not in {"completed", "completed_with_warnings"} or job.phase != "translation_complete":
        return []
    if job.tool.id == "pdf_ocr_translate":
        output_dir = job.root / "output"
        sources = {
            (Path(str(item["name"])).stem, Path(str(item["name"])).suffix.lower()): item
            for item in job.artifacts
            if item.get("kind") == "ocr"
        }
        pairs: list[dict[str, Any]] = []
        for item in job.artifacts:
            if item.get("kind") != "translation":
                continue
            translated = output_dir / str(item["name"])
            translated_stem = translated.stem
            if not translated_stem.endswith("_zh-CN"):
                continue
            source_item = sources.get((translated_stem[:-6], translated.suffix.lower()))
            if not source_item:
                continue
            source = output_dir / str(source_item["name"])
            if source.is_file() and translated.is_file():
                pairs.append(reader_pair_record(source, translated, base=output_dir))
        return pairs
    if job.tool.id == "document_translate":
        input_dir = job.root / "input"
        output_dir = job.root / "output"
        pairs = []
        for translated in sorted(path for path in output_dir.rglob("*") if path.is_file()):
            if translated.suffix.lower() not in TRANSLATABLE_SUFFIXES or not translated.stem.endswith("_zh-CN"):
                continue
            relative = translated.relative_to(output_dir)
            source = input_dir / relative.parent / f"{translated.stem[:-6]}{translated.suffix}"
            if source.is_file():
                pair = reader_pair_record(source, translated, base=job.root)
                pair["sourceName"] = source.relative_to(input_dir).as_posix()
                pair["translatedName"] = translated.relative_to(output_dir).as_posix()
                pairs.append(pair)
        return pairs
    return []


def reader_translation_cache_key(config: dict[str, str]) -> str:
    identity = {
        "schemaVersion": READER_CACHE_SCHEMA_VERSION,
        "readerPairingVersion": READER_PAIRING_VERSION,
        "markdownPostprocessVersion": TRANSLATION_MARKDOWN_POSTPROCESS_VERSION,
        "targetLanguage": "zh-CN",
        "baseUrl": config.get("baseUrl", "").rstrip("/"),
        "model": config.get("model", ""),
    }
    return hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def reader_translation_cache_entry_is_current(entry: Any) -> bool:
    return (
        isinstance(entry, dict)
        and entry.get("pairingVersion") == READER_PAIRING_VERSION
        and entry.get("markdownPostprocessVersion") == TRANSLATION_MARKDOWN_POSTPROCESS_VERSION
    )


def reader_cache_root(cache_key: str) -> Path:
    return READER_CACHE_DIR / cache_key[:2] / cache_key


def read_reader_cache_manifest(cache_key: str) -> dict[str, Any]:
    manifest_path = reader_cache_root(cache_key) / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if (
        not isinstance(manifest, dict)
        or manifest.get("schemaVersion") != READER_CACHE_SCHEMA_VERSION
        or manifest.get("cacheKey") != cache_key
    ):
        return {}
    return manifest


def write_reader_cache_manifest(cache_key: str, manifest: dict[str, Any]) -> None:
    root = reader_cache_root(cache_key)
    root.mkdir(parents=True, exist_ok=True)
    manifest.update({
        "schemaVersion": READER_CACHE_SCHEMA_VERSION,
        "cacheKey": cache_key,
        "updatedAt": time.time(),
    })
    write_durable_text(root / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))


def read_reader_persistent_state(cache_key: str) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9a-f]{64}", cache_key) or not read_reader_cache_manifest(cache_key):
        return {"version": 2, "updatedAt": 0, "notes": [], "textFormats": [], "liveTranslations": []}
    try:
        state = json.loads((reader_cache_root(cache_key) / "reader-state.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": 2, "updatedAt": 0, "notes": [], "textFormats": [], "liveTranslations": []}
    if not isinstance(state, dict):
        return {"version": 2, "updatedAt": 0, "notes": [], "textFormats": [], "liveTranslations": []}
    notes = state.get("notes") if isinstance(state.get("notes"), list) else []
    text_formats = state.get("textFormats") if isinstance(state.get("textFormats"), list) else []
    live_translations = normalize_reader_live_translations(state.get("liveTranslations"))
    return {
        "version": 2,
        "updatedAt": float(state.get("updatedAt") or 0),
        "notes": [item for item in notes if isinstance(item, dict)][-200:],
        "textFormats": [item for item in text_formats if isinstance(item, dict)][-500:],
        "liveTranslations": live_translations,
    }


def normalize_reader_live_translations(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    translations: list[dict[str, Any]] = []
    for item in value[-200:]:
        if not isinstance(item, dict):
            continue
        block_id = str(item.get("blockId") or "").strip()
        source_hash = str(item.get("sourceHash") or "").strip()
        translation = str(item.get("translation") or "").strip()
        if (
            not re.fullmatch(r"[A-Za-z0-9._:-]{1,200}", block_id)
            or not re.fullmatch(r"\d{1,8}:[0-9a-f]{8}", source_hash)
            or not translation
            or len(translation) > 120_000
        ):
            continue
        try:
            updated_at = float(item.get("updatedAt") or 0)
        except (TypeError, ValueError):
            updated_at = 0
        translations.append({
            "blockId": block_id,
            "sourceHash": source_hash,
            "translation": translation,
            "cached": item.get("cached") is True,
            "updatedAt": updated_at,
        })
    return translations


def write_reader_persistent_state(cache_key: str, payload: dict[str, Any]) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9a-f]{64}", cache_key) or not read_reader_cache_manifest(cache_key):
        raise ValueError("The reader document does not have a persistent local cache.")
    notes = payload.get("notes") if isinstance(payload.get("notes"), list) else []
    text_formats = payload.get("textFormats") if isinstance(payload.get("textFormats"), list) else []
    state = {
        "version": 2,
        "updatedAt": time.time() * 1000,
        "notes": [item for item in notes if isinstance(item, dict)][-200:],
        "textFormats": [item for item in text_formats if isinstance(item, dict)][-500:],
        "liveTranslations": normalize_reader_live_translations(payload.get("liveTranslations")),
    }
    serialized = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
    if len(serialized.encode("utf-8")) > 4 * 1024 * 1024:
        raise ValueError("Reader notes, annotations, and saved translations exceed the 4 MB local-state limit.")
    with reader_cache_lock:
        write_durable_text(reader_cache_root(cache_key) / "reader-state.json", serialized)
    return state


def reader_cached_file(root: Path, relative: str) -> Path | None:
    try:
        candidate = (root / safe_relative_path(relative)).resolve()
    except ValueError:
        return None
    resolved_root = root.resolve()
    if resolved_root not in candidate.parents or not candidate.is_file():
        return None
    return candidate


def reader_document_manifest(
    document: ReaderDocument,
    *,
    storage_root: str,
    source_filename: str | None = None,
    document_filename: str | None = None,
) -> dict[str, Any]:
    return {
        "storageRoot": storage_root,
        "sourceFilename": source_filename,
        "documentFilename": document_filename,
        "title": document.title,
        "titleCustomized": document.title_customized,
        "sourceName": document.source_name or document.source_filename or document.title,
        "sourceType": document.source_type,
        "renderKind": document.render_kind,
        "useOcr": document.use_ocr,
        "createdAt": time.time(),
    }


def apply_reader_cached_title_override(
    document: ReaderDocument,
    manifest: dict[str, Any],
) -> None:
    for key in ("document", "ocr"):
        entry = manifest.get(key)
        title = clean_reader_title(entry.get("title")) if isinstance(entry, dict) else None
        if isinstance(entry, dict) and entry.get("titleCustomized") is True and title:
            document.title = title
            document.title_customized = True
            return


def store_reader_source_cache(document: ReaderDocument, source: Path) -> None:
    if not document.cache_key or not source.is_file():
        return
    cache_root = reader_cache_root(document.cache_key)
    cached_root = cache_root / "source"
    cached_name = f"source{source.suffix.lower()}"
    cached = cached_root / cached_name
    with reader_cache_lock:
        cached_root.mkdir(parents=True, exist_ok=True)
        if not cached.exists():
            temporary = cached_root / f".{cached_name}.{uuid.uuid4().hex}.tmp"
            try:
                shutil.copyfile(source, temporary)
                os.replace(temporary, cached)
            finally:
                temporary.unlink(missing_ok=True)
        if document.render_kind == "markdown":
            output_root = document.root / "output"
            if output_root.is_dir():
                for asset in output_root.rglob("*"):
                    if not asset.is_file() or asset.suffix.lower() not in READER_ASSET_SUFFIXES:
                        continue
                    relative = asset.relative_to(output_root)
                    cached_asset = cached_root / relative
                    cached_asset.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(asset, cached_asset)
        manifest = read_reader_cache_manifest(document.cache_key)
        apply_reader_cached_title_override(document, manifest)
        existing_document = manifest.get("document")
        keep_richer_ocr = (
            document.source_type == "pdf"
            and document.render_kind == "pdf"
            and isinstance(existing_document, dict)
            and existing_document.get("renderKind") == "html"
            and existing_document.get("useOcr") is True
        )
        if not keep_richer_ocr:
            manifest["document"] = reader_document_manifest(
                document,
                storage_root="source",
                source_filename=cached_name,
            )
        write_reader_cache_manifest(document.cache_key, manifest)


def restore_reader_source_cache(root: Path, cached: dict[str, Any]) -> Path | None:
    cache_key = str(cached.get("id") or "")
    source_filename = str(cached.get("sourceFilename") or "")
    cached_source = reader_cached_file(reader_cache_root(cache_key) / "source", source_filename)
    if not cached_source:
        return None
    input_root = root / "input"
    input_root.mkdir(parents=True, exist_ok=True)
    source_name = safe_work_name(str(cached.get("sourceName") or cached_source.name), cached_source.suffix)
    destination = input_root / source_name
    shutil.copyfile(cached_source, destination)
    cached_root = cached_source.parent
    for asset in cached_root.rglob("*"):
        if not asset.is_file() or asset == cached_source or asset.suffix.lower() not in READER_ASSET_SUFFIXES:
            continue
        relative = asset.relative_to(cached_root)
        restored_asset = root / "output" / relative
        restored_asset.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(asset, restored_asset)
    return destination


def refresh_cached_document_for_current_reader(
    document: ReaderDocument,
    primary_source: Path,
) -> None:
    """Rebuild derived reader data with today's code, never a cached UI snapshot."""
    if document.render_kind == "markdown":
        document.blocks = markdown_to_reader_blocks(primary_source.read_text(encoding="utf-8"))
    elif document.render_kind == "html":
        annotate_reader_html_pair_ids(primary_source)
        current_title = cached_ocr_html_title(primary_source)
        if current_title and not document.title_customized:
            document.title = current_title


def reader_cache_availability(cache_key: str, translation_config: dict[str, str] | None) -> list[str]:
    manifest = read_reader_cache_manifest(cache_key)
    available: list[str] = []
    document = manifest.get("document")
    if isinstance(document, dict) and document.get("storageRoot") == "source":
        source_root = reader_cache_root(cache_key) / "source"
        if reader_cached_file(source_root, str(document.get("sourceFilename", ""))):
            available.append("document")
    ocr = manifest.get("ocr")
    if isinstance(ocr, dict):
        ocr_root = reader_cache_root(cache_key) / "ocr"
        if reader_cached_file(ocr_root, str(ocr.get("documentFilename", ""))):
            available.append("ocr")
    if translation_config:
        translations = manifest.get("translations")
        translation_key = reader_translation_cache_key(translation_config)
        entry = translations.get(translation_key) if isinstance(translations, dict) else None
        if reader_translation_cache_entry_is_current(entry):
            translation_root = reader_cache_root(cache_key) / "translations" / translation_key
            if reader_cached_file(translation_root, str(entry.get("filename", ""))):
                available.append("translation")
    return available


def reader_cache_library_entry(cache_key: str) -> dict[str, Any] | None:
    if not re.fullmatch(r"[0-9a-f]{64}", cache_key):
        return None
    manifest = read_reader_cache_manifest(cache_key)
    document = manifest.get("document")
    if not isinstance(document, dict):
        # Backward compatibility for OCR caches created before the generic
        # persistent-reader manifest was introduced.
        ocr = manifest.get("ocr")
        if not isinstance(ocr, dict):
            return None
        document = {
            "storageRoot": "ocr",
            "documentFilename": ocr.get("documentFilename"),
            "title": ocr.get("title"),
            "titleCustomized": ocr.get("titleCustomized", False),
            "sourceName": ocr.get("sourceName"),
            "sourceType": ocr.get("sourceType") or "pdf",
            "renderKind": "html",
            "useOcr": ocr.get("useOcr", True),
            "createdAt": ocr.get("createdAt"),
        }
    storage_root = str(document.get("storageRoot") or "")
    if storage_root not in {"source", "ocr"}:
        return None
    render_kind = str(document.get("renderKind") or "html").strip().lower()
    source_filename = str(document.get("sourceFilename") or "")
    document_filename = str(document.get("documentFilename") or "")
    primary_filename = document_filename if render_kind == "html" else source_filename
    cached_document = reader_cached_file(reader_cache_root(cache_key) / storage_root, primary_filename)
    if not cached_document:
        return None
    translations = manifest.get("translations")
    valid_translations = 0
    if isinstance(translations, dict):
        for translation_key, entry in translations.items():
            if not re.fullmatch(r"[0-9a-f]{64}", str(translation_key)) or not isinstance(entry, dict):
                continue
            if not reader_translation_cache_entry_is_current(entry):
                continue
            translation_root = reader_cache_root(cache_key) / "translations" / translation_key
            if reader_cached_file(translation_root, str(entry.get("filename", ""))):
                valid_translations += 1
    source_name = str(document.get("sourceName") or "").strip()
    source_title = reader_source_display_title(source_name)
    html_title = cached_ocr_html_title(cached_document) if render_kind == "html" else None
    fallback_title = html_title or source_title or Path(primary_filename).stem or "阅读文档"
    source_type = str(document.get("sourceType") or ("pdf" if render_kind == "pdf" else "markdown")).strip().lower()
    use_ocr = bool(document.get("useOcr", source_type == "pdf" and render_kind == "html"))
    return {
        "id": cache_key,
        "title": clean_reader_title(document.get("title")) or fallback_title,
        "titleCustomized": document.get("titleCustomized") is True,
        "sourceName": source_name or primary_filename,
        "sourceType": source_type,
        "renderKind": render_kind,
        "useOcr": use_ocr,
        "storageRoot": storage_root,
        "sourceFilename": source_filename or None,
        "documentFilename": document_filename,
        "updatedAt": float(manifest.get("updatedAt") or document.get("createdAt") or 0),
        "hasTranslation": valid_translations > 0,
        "translationCount": valid_translations,
        "canTranslateDocument": render_kind in {"markdown", "html"},
        "readerFeatureVersion": READER_FEATURE_VERSION,
        "readerUpdatePolicy": READER_UPDATE_POLICY,
        "readerPairingVersion": READER_PAIRING_VERSION,
        "capabilities": reader_capabilities(render_kind, valid_translations > 0),
    }


def reader_cache_library() -> list[dict[str, Any]]:
    if not READER_CACHE_DIR.is_dir():
        return []
    entries: list[dict[str, Any]] = []
    for manifest_path in READER_CACHE_DIR.glob("*/*/manifest.json"):
        entry = reader_cache_library_entry(manifest_path.parent.name)
        if entry:
            entries.append(entry)
    return sorted(entries, key=lambda item: (item["updatedAt"], item["title"].lower()), reverse=True)


def latest_reader_cached_translation(cache_key: str) -> Path | None:
    manifest = read_reader_cache_manifest(cache_key)
    translations = manifest.get("translations")
    if not isinstance(translations, dict):
        return None
    candidates: list[tuple[float, Path]] = []
    for translation_key, entry in translations.items():
        if not re.fullmatch(r"[0-9a-f]{64}", str(translation_key)) or not isinstance(entry, dict):
            continue
        if not reader_translation_cache_entry_is_current(entry):
            continue
        root = reader_cache_root(cache_key) / "translations" / translation_key
        cached = reader_cached_file(root, str(entry.get("filename", "")))
        if cached:
            candidates.append((float(entry.get("createdAt") or 0), cached))
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


def reader_cached_aligned_blocks(cache_key: str, translated: Path) -> list[dict[str, Any]] | None:
    manifest = read_reader_cache_manifest(cache_key)
    translations = manifest.get("translations")
    if not isinstance(translations, dict):
        return None
    resolved_translation = translated.resolve()
    for translation_key, entry in translations.items():
        if not isinstance(entry, dict):
            continue
        if not reader_translation_cache_entry_is_current(entry):
            continue
        root = reader_cache_root(cache_key) / "translations" / str(translation_key)
        cached = reader_cached_file(root, str(entry.get("filename") or ""))
        if not cached or cached.resolve() != resolved_translation:
            continue
        blocks = reader_cached_file(root, str(entry.get("blocksFilename") or ""))
        if not blocks:
            return None
        try:
            payload = json.loads(blocks.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if isinstance(payload, list) and all(isinstance(item, dict) for item in payload):
            return payload
    return None


def reader_cached_aligned_html_source(cache_key: str, translated: Path) -> Path | None:
    manifest = read_reader_cache_manifest(cache_key)
    translations = manifest.get("translations")
    if not isinstance(translations, dict):
        return None
    resolved_translation = translated.resolve()
    for translation_key, entry in translations.items():
        if not isinstance(entry, dict):
            continue
        if not reader_translation_cache_entry_is_current(entry):
            continue
        root = reader_cache_root(cache_key) / "translations" / str(translation_key)
        cached = reader_cached_file(root, str(entry.get("filename") or ""))
        if not cached or cached.resolve() != resolved_translation:
            continue
        return reader_cached_file(root, str(entry.get("sourceFilename") or ""))
    return None


def restore_reader_ocr_cache(job: Job, document: ReaderDocument) -> Path | None:
    if not document.use_local_cache or not document.cache_key:
        return None
    manifest = read_reader_cache_manifest(document.cache_key)
    entry = manifest.get("ocr")
    if not isinstance(entry, dict):
        return None
    cached_root = reader_cache_root(document.cache_key) / "ocr"
    relative = str(entry.get("documentFilename", ""))
    if not reader_cached_file(cached_root, relative):
        return None
    staging = job.root / f".reader-cache-restore-{uuid.uuid4().hex}"
    output = job.root / "output"
    try:
        shutil.copytree(cached_root, staging)
        restored_document = reader_cached_file(staging, relative)
        if not restored_document:
            return None
        if output.exists():
            return None
        os.replace(staging, output)
        return output / safe_relative_path(relative)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def store_reader_ocr_cache(job: Job, document: ReaderDocument) -> None:
    if not document.cache_key or not document.document_filename:
        return
    output = job.root / "output"
    if not reader_cached_file(output, document.document_filename):
        return
    cache_root = reader_cache_root(document.cache_key)
    cached_output = cache_root / "ocr"
    with reader_cache_lock:
        manifest = read_reader_cache_manifest(document.cache_key)
        apply_reader_cached_title_override(document, manifest)
        if not cached_output.exists():
            staging = cache_root / f".ocr-{uuid.uuid4().hex}.tmp"
            cache_root.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copytree(output, staging)
                os.replace(staging, cached_output)
            finally:
                shutil.rmtree(staging, ignore_errors=True)
        else:
            # Refresh the document and its relative image tree. This matters for
            # directly uploaded HTML whose source text may be unchanged while
            # an accompanying images/assets directory has been added or updated.
            shutil.copytree(output, cached_output, dirs_exist_ok=True)
        manifest["ocr"] = {
            "documentFilename": document.document_filename,
            "title": document.title,
            "titleCustomized": document.title_customized,
            "sourceName": document.source_name or document.source_filename or f"{document.title}.pdf",
            "sourceType": document.source_type,
            "useOcr": document.use_ocr,
            "createdAt": time.time(),
        }
        manifest["document"] = reader_document_manifest(
            document,
            storage_root="ocr",
            document_filename=document.document_filename,
        )
        write_reader_cache_manifest(document.cache_key, manifest)


def restore_reader_translation_cache(
    document: ReaderDocument,
    config: dict[str, str],
    destination: Path,
) -> bool:
    if not document.use_local_cache or not document.cache_key:
        return False
    translation_key = reader_translation_cache_key(config)
    manifest = read_reader_cache_manifest(document.cache_key)
    translations = manifest.get("translations")
    entry = translations.get(translation_key) if isinstance(translations, dict) else None
    if not reader_translation_cache_entry_is_current(entry):
        return False
    cached_root = reader_cache_root(document.cache_key) / "translations" / translation_key
    cached = reader_cached_file(cached_root, str(entry.get("filename", "")))
    if not cached:
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(cached, destination)
    if document.render_kind == "html" and document.document_filename:
        cached_source = reader_cached_file(cached_root, str(entry.get("sourceFilename") or ""))
        source_destination = reader_cached_file(document.root / "output", document.document_filename)
        if cached_source and source_destination:
            shutil.copyfile(cached_source, source_destination)
    blocks_filename = str(entry.get("blocksFilename") or "")
    cached_blocks = reader_cached_file(cached_root, blocks_filename) if blocks_filename else None
    if document.render_kind == "markdown" and cached_blocks:
        try:
            blocks = json.loads(cached_blocks.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            blocks = None
        if isinstance(blocks, list) and all(isinstance(item, dict) for item in blocks):
            document.translated_blocks = blocks
    return True


def store_reader_translation_cache(
    document: ReaderDocument,
    config: dict[str, str],
    translated: Path,
) -> None:
    if not document.cache_key or not translated.is_file():
        return
    translation_key = reader_translation_cache_key(config)
    cache_root = reader_cache_root(document.cache_key)
    translation_root = cache_root / "translations" / translation_key
    cached_name = f"translated{translated.suffix.lower()}"
    cached = translation_root / cached_name
    with reader_cache_lock:
        translation_root.mkdir(parents=True, exist_ok=True)
        if not cached.exists():
            temporary = translation_root / f".{cached_name}.{uuid.uuid4().hex}.tmp"
            try:
                shutil.copyfile(translated, temporary)
                os.replace(temporary, cached)
            finally:
                temporary.unlink(missing_ok=True)
        blocks_filename = None
        source_filename = None
        if document.render_kind == "markdown" and document.translated_blocks is not None:
            blocks_filename = "aligned-blocks.json"
            write_durable_text(
                translation_root / blocks_filename,
                json.dumps(document.translated_blocks, ensure_ascii=False, separators=(",", ":")),
            )
        elif document.render_kind == "html" and document.document_filename:
            source = reader_cached_file(document.root / "output", document.document_filename)
            if source:
                source_filename = f"aligned-source{source.suffix.lower()}"
                source_cached = translation_root / source_filename
                temporary = translation_root / f".{source_filename}.{uuid.uuid4().hex}.tmp"
                try:
                    shutil.copyfile(source, temporary)
                    os.replace(temporary, source_cached)
                finally:
                    temporary.unlink(missing_ok=True)
        manifest = read_reader_cache_manifest(document.cache_key)
        translations = manifest.setdefault("translations", {})
        translations[translation_key] = {
            "filename": cached_name,
            "blocksFilename": blocks_filename,
            "sourceFilename": source_filename,
            "pairingVersion": READER_PAIRING_VERSION,
            "markdownPostprocessVersion": TRANSLATION_MARKDOWN_POSTPROCESS_VERSION,
            "baseUrl": config.get("baseUrl", "").rstrip("/"),
            "model": config.get("model", ""),
            "createdAt": time.time(),
        }
        write_reader_cache_manifest(document.cache_key, manifest)


def copy_reader_pair_assets(job: Job, destination_root: Path) -> None:
    """Copy image assets that may be referenced by either side of a paired document."""
    for source_root_name in ("input", "output"):
        source_root = job.root / source_root_name
        if not source_root.is_dir():
            continue
        for asset in source_root.rglob("*"):
            if not asset.is_file() or asset.suffix.lower() not in READER_ASSET_SUFFIXES:
                continue
            destination = destination_root / asset.relative_to(source_root)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if not destination.exists():
                shutil.copyfile(asset, destination)


def reader_pair_relative_path(value: Any) -> Path:
    relative = PurePosixPath(str(value or "").replace("\\", "/"))
    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in {"", ".", ".."} or ":" in part for part in relative.parts)
    ):
        raise ValueError("The paired document has an invalid relative path.")
    return Path(*relative.parts)


def import_job_pair_into_reader(job: Job, pair: dict[str, Any]) -> ReaderDocument:
    source = pair["source"]
    translated = pair["translated"]
    suffix = source.suffix.lower()
    if suffix not in {".md", ".mmd", ".html", ".htm"} or translated.suffix.lower() != suffix:
        raise ValueError("Only matching Markdown, MMD, or HTML source/translation pairs can be imported.")
    if not source.is_file() or not translated.is_file():
        raise FileNotFoundError("The source/translation pair is no longer available.")

    document_id = uuid.uuid4().hex
    root = JOBS_DIR / f"reader-{document_id}"
    input_root = root / "input"
    output_root = root / "output"
    source_relative = reader_pair_relative_path(pair["sourceName"])
    translated_relative = reader_pair_relative_path(pair["translatedName"])
    source_copy = input_root / source_relative
    translated_copy = output_root / translated_relative
    source_copy.parent.mkdir(parents=True, exist_ok=True)
    translated_copy.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, source_copy)
    shutil.copyfile(translated, translated_copy)
    copy_reader_pair_assets(job, output_root)

    is_html = suffix in {".html", ".htm"}
    imported_from_ocr = job.tool.id == "pdf_ocr_translate"
    document = ReaderDocument(
        document_id,
        root,
        str(pair["title"]),
        "pdf" if imported_from_ocr and is_html else "html" if is_html else "markdown",
        "ocr_translate" if imported_from_ocr and is_html else "html_translate" if is_html else "markdown_translate",
        render_kind="html" if is_html else "markdown",
        use_ocr=imported_from_ocr and is_html,
        generate_translation=True,
        use_local_cache=True,
        status="ready",
        message="已从处理结果导入原文与译文，可直接对照阅读。",
    )
    document.source_filename = source_relative.as_posix()
    document.source_name = source_relative.name
    document.cache_key = reader_source_cache_key(source_copy)
    if is_html:
        original_copy = output_root / source_relative
        original_copy.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, original_copy)
        document.document_filename = source_relative.as_posix()
        document.translated_filename = translated_relative.as_posix()
        document.title = cached_ocr_html_title(original_copy) or document.title
    else:
        document.blocks = markdown_to_reader_blocks(source_copy.read_text(encoding="utf-8"))
        document.translated_blocks = markdown_to_reader_blocks(translated_copy.read_text(encoding="utf-8"))

    reader_job = Job(
        document_id,
        READER_TOOL,
        root,
        {},
        status="completed",
        phase="reader_ready",
        operation="reader_import",
        reader_document_id=document_id,
        finished_at=time.time(),
    )
    reader_job.download_path = source_copy
    reader_job.download_name = source_copy.name
    document.job_id = reader_job.id
    try:
        if is_html:
            store_reader_ocr_cache(reader_job, document)
        else:
            store_reader_source_cache(document, source_copy)
        store_reader_translation_cache(
            document,
            {"baseUrl": "local://paired-result", "model": f"{job.tool.id}-import"},
            translated_copy,
        )
    except Exception as exc:
        reader_job.log(f"[CACHE] Could not persist imported reader pair: {exc}")
    reader_manager.add(document)
    with job_manager.lock:
        job_manager.jobs[reader_job.id] = reader_job
    return document


def note_reader_cache_failure(job: Job, operation: str, exc: Exception) -> None:
    job.log(f"[CACHE] Could not {operation} local reader cache: {exc}")


def translate_reader_html_with_backend(job: Job, document: ReaderDocument, html_source: Path) -> Path:
    if not job.translation_config:
        raise RuntimeError("HTML translation requires a managed LLM configuration.")
    output_root = job.root / "output"
    translated = html_source.with_name(f"{html_source.stem}_zh-CN{html_source.suffix}")
    document.message = "正在由 AI-Markdown-Translator 翻译原始 HTML 分块。"
    try:
        translation_restored = restore_reader_translation_cache(document, job.translation_config, translated)
    except Exception as exc:
        note_reader_cache_failure(job, "restore translation from", exc)
        translated.unlink(missing_ok=True)
        translation_restored = False
    if translation_restored:
        document.cache_hits.append("translation")
        job.log("[CACHE] Reused cached HTML translation; the LLM was not called.")
    else:
        [produced] = run_translation_backend(job, html_source.parent, html_source.parent, [html_source])
        if produced != translated:
            raise RuntimeError("AI-Markdown-Translator returned an unexpected HTML output path.")
        try:
            store_reader_translation_cache(document, job.translation_config, translated)
            job.log("[CACHE] Saved HTML translation to the persistent local reader cache.")
        except Exception as exc:
            note_reader_cache_failure(job, "save HTML translation to", exc)
    document.translated_filename = relative_path_within(translated, output_root).as_posix()
    return translated


def run_reader_document(job: Job) -> None:
    if not job.reader_document_id:
        raise RuntimeError("Reader job is missing its document ID.")
    document = reader_manager.get(job.reader_document_id)
    if not document:
        raise RuntimeError("Reader document was not found.")
    source_files = [path for path in (job.root / "input").rglob("*") if path.is_file()]
    if len(source_files) != 1:
        raise RuntimeError("A reader document requires exactly one source file.")
    source = source_files[0]
    document.status = "processing"
    document.message = "正在解析文档。"
    suffix = source.suffix.lower()
    if suffix == ".pdf":
        if not document.use_ocr:
            document.render_kind = "pdf"
            try:
                store_reader_source_cache(document, source)
                job.log("[CACHE] Saved the original PDF to the persistent local reader cache.")
            except Exception as exc:
                note_reader_cache_failure(job, "save PDF document to", exc)
            document.status = "ready"
            document.message = "原始 PDF 已准备好，可划词提问。"
            job.phase = "reader_ready"
            job.download_path = source
            job.download_name = source.name
            return
        document.message = "正在由 Mathpix 识别 PDF、公式与版式。"
        try:
            html_source = restore_reader_ocr_cache(job, document)
        except Exception as exc:
            note_reader_cache_failure(job, "restore OCR from", exc)
            html_source = None
        if html_source:
            document.cache_hits.append("ocr")
            job.log("[CACHE] Reused cached OCR output; Mathpix was not called.")
            document.message = "已命中本地 OCR 缓存，正在准备论文。"
        else:
            job.log("[CACHE] No reusable OCR output found; starting Mathpix OCR.")
            html_source = run_reader_html_ocr(job, source)
        document.render_kind = "html"
        document.document_filename = relative_path_within(html_source, job.root / "output").as_posix()
        try:
            store_reader_ocr_cache(job, document)
            if "ocr" in document.cache_hits:
                job.log("[CACHE] Refreshed cached document title and source filename metadata.")
            else:
                job.log("[CACHE] Saved OCR output to the persistent local reader cache.")
        except Exception as exc:
            note_reader_cache_failure(job, "save OCR to", exc)
        if document.generate_translation:
            translate_reader_html_with_backend(job, document, html_source)
        document.status = "ready"
        if document.cache_hits:
            reused = "、".join("OCR" if item == "ocr" else "译文" for item in document.cache_hits)
            document.message = f"已复用本地缓存（{reused}），论文已准备好。"
        else:
            document.message = "OCR 论文已准备好，可划词提问。"
        job.phase = "reader_ready"
        job.download_path = html_source
        job.download_name = html_source.name
        return

    if suffix in {".html", ".htm"}:
        document.render_kind = "html"
        document.message = "正在准备 HTML 阅读视图。"
        output_dir = job.root / "output"
        output_dir.mkdir(parents=True, exist_ok=True)
        html_source = output_dir / source.name
        # Normalize to UTF-8 so title extraction, translation, and browser
        # rendering all observe the same text even for BOM-prefixed files.
        html_text = source.read_text(encoding="utf-8-sig", errors="replace")
        write_durable_text(html_source, html_text)
        document.title = cached_ocr_html_title(html_source) or clean_reader_title(document.source_name and Path(document.source_name).stem) or source.stem
        document.document_filename = html_source.relative_to(output_dir).as_posix()
        if document.align_translation:
            if not job.translation_config or not document.translated_filename:
                raise RuntimeError("Imported HTML translation alignment requires an LLM configuration and translation file.")
            translated = reader_cached_file(output_dir, document.translated_filename)
            if not translated:
                raise RuntimeError("The imported HTML translation file is missing.")
            document.message = "正在使用结构锚点和 LLM 修复 HTML 原文与译文的块对齐。"
            align_reader_html_documents(job, html_source, translated)
        try:
            store_reader_ocr_cache(job, document)
            job.log("[CACHE] Saved the uploaded HTML document to the persistent local reader cache.")
        except Exception as exc:
            note_reader_cache_failure(job, "save HTML document to", exc)
        if document.generate_translation:
            translate_reader_html_with_backend(job, document, html_source)
        elif document.align_translation:
            translated = reader_cached_file(output_dir, document.translated_filename or "")
            if translated:
                try:
                    store_reader_translation_cache(document, job.translation_config or {}, translated)
                    job.log("[CACHE] Saved validated imported HTML alignment.")
                except Exception as exc:
                    note_reader_cache_failure(job, "save aligned HTML translation to", exc)
        document.status = "ready"
        if document.align_translation:
            set_reader_alignment_progress(
                job, "complete", "HTML 原文与译文已完成锚点辅助的 LLM 块对齐。", 1, 1, 100
            )
        else:
            document.message = "已复用本地译文缓存，HTML 已准备好。" if document.cache_hits else "HTML 已准备好，可划词提问。"
        job.phase = "reader_ready"
        job.download_path = html_source
        job.download_name = html_source.name
        return

    document.render_kind = "markdown"
    original_text = source.read_text(encoding="utf-8")
    document.blocks = markdown_to_reader_blocks(original_text)
    try:
        store_reader_source_cache(document, source)
        job.log("[CACHE] Saved the Markdown document to the persistent local reader cache.")
    except Exception as exc:
        note_reader_cache_failure(job, "save Markdown document to", exc)
    if document.align_translation:
        if not job.translation_config or not document.translated_filename:
            raise RuntimeError("Imported translation alignment requires an LLM configuration and translation file.")
        translated = reader_cached_file(job.root / "output", document.translated_filename)
        if not translated:
            raise RuntimeError("The imported translation file is missing.")
        document.message = "正在使用 LLM 修复原文与译文的段落对齐。"
        translated_blocks = markdown_to_reader_blocks(translated.read_text(encoding="utf-8"))
        document.translated_blocks = align_reader_markdown_blocks(job, document.blocks, translated_blocks)
        try:
            store_reader_translation_cache(document, job.translation_config, translated)
            job.log("[CACHE] Saved validated imported-translation alignment.")
        except Exception as exc:
            note_reader_cache_failure(job, "save aligned translation to", exc)
    if document.generate_translation:
        if not job.translation_config:
            raise RuntimeError("Document translation requires an LLM configuration.")
        document.message = "正在按论文段落生成对照译文。"
        translated = job.root / "output" / f"{source.stem}_zh-CN{source.suffix}"
        translated.parent.mkdir(exist_ok=True)
        try:
            translation_restored = restore_reader_translation_cache(document, job.translation_config, translated)
        except Exception as exc:
            note_reader_cache_failure(job, "restore translation from", exc)
            translated.unlink(missing_ok=True)
            translation_restored = False
        if translation_restored:
            document.cache_hits.append("translation")
            job.log("[CACHE] Reused cached translation; the LLM was not called.")
        else:
            [translated] = run_translation_backend(job, source.parent, translated.parent, [source])
        translated_before_postprocess = translated.read_text(encoding="utf-8")
        finalize_translated_markdown_outputs(job, [translated])
        if (
            not translation_restored
            or document.translated_blocks is None
            or translated.read_text(encoding="utf-8") != translated_before_postprocess
        ):
            document.translated_blocks = markdown_to_reader_blocks(translated.read_text(encoding="utf-8"))
        if not translation_restored:
            try:
                store_reader_translation_cache(document, job.translation_config, translated)
                job.log("[CACHE] Saved translation to the persistent local reader cache.")
            except Exception as exc:
                note_reader_cache_failure(job, "save translation to", exc)
    document.status = "ready"
    if document.align_translation:
        set_reader_alignment_progress(job, "complete", "原文与译文已完成 LLM 段落对齐。", 1, 1, 100)
    else:
        document.message = "已复用本地译文缓存，论文已准备好。" if document.cache_hits else "论文已准备好，可划词提问。"
    job.phase = "reader_ready"
    job.download_path = source
    job.download_name = source.name


def run_cached_reader_translation(job: Job) -> None:
    if not job.reader_document_id or not job.translation_config:
        raise RuntimeError("Cached reader translation is missing its document or managed LLM configuration.")
    document = reader_manager.get(job.reader_document_id)
    if not document or not document.document_filename:
        raise RuntimeError("Cached reader document was not found.")
    output_root = job.root / "output"
    html_source = reader_cached_file(output_root, document.document_filename)
    if not html_source or html_source.suffix.lower() not in {".html", ".htm"}:
        raise RuntimeError("Cached reader HTML is incomplete.")
    document.status = "processing"
    translate_reader_html_with_backend(job, document, html_source)
    document.generate_translation = True
    document.mode = "html_translate" if document.source_type in {"html", "htm"} else "ocr_translate"
    document.status = "ready"
    document.message = "HTML 译文已准备好，可以继续阅读。"
    job.phase = "reader_ready"
    job.download_path = html_source
    job.download_name = html_source.name


def build_command(job: Job, files: list[Path]) -> tuple[list[str] | None, list[Path]]:
    tool_id = job.tool.id
    options = job.options
    output_dir = job.root / "output"
    output_dir.mkdir(exist_ok=True)
    input_dir = job.root / "input"
    ordered_dir = job.root / "ordered"
    python = sys.executable

    if tool_id == "anki":
        output = output_dir / "anki_cards.txt"
        generate_anki(job, files[0], output)
        return None, [output]
    if tool_id == "image_crop":
        crop = positive_int(options.get("crop"), "Crop size", 256)
        out = positive_int(options.get("out"), "Output size", 512)
        return [python, str(SCRIPTS_DIR / "crop_center_resize.py"), "--input_dir", str(input_dir), "--crop", str(crop), "--out", str(out), "--recursive"], [job.root / "input_resized"]
    if tool_id == "video_crop":
        crop = positive_int(options.get("crop"), "Crop size", 256)
        out = positive_int(options.get("out"), "Output size", 512)
        offset = positive_int(options.get("offsetY"), "Vertical offset", -80, -10000, 10000)
        crf = positive_int(options.get("crf"), "CRF", 18, 0, 51)
        return [python, str(SCRIPTS_DIR / "crop_center_resize_video.py"), "--input_dir", str(input_dir), "--crop", str(crop), "--out", str(out), "--offset_y", str(offset), "--crf", str(crf), "--recursive"], [job.root / "input_resized"]
    if tool_id == "frames":
        return [python, str(SCRIPTS_DIR / "mp42png.py"), str(input_dir)], [path.with_suffix("") for path in files]
    if tool_id == "fix_frame":
        output = output_dir / "fixed_frame16.mp4"
        return [python, str(SCRIPTS_DIR / "fix_15_video.py"), str(files[0]), str(output)], [output]
    if tool_id in {"image_ppt", "video_ppt"}:
        rows = positive_int(options.get("rows"), "Rows", 3, 1, 100)
        cols = positive_int(options.get("cols"), "Columns", 5, 1, 100)
        size = number(options.get("cellSize"), "Cell size", 5, 0.1, 100)
        gap = number(options.get("gap"), "Gap", 4, 0, 1000)
        margin = number(options.get("margin"), "Margin", 1, 0, 100)
        fit = option_text(options, "fit", "fit")
        if fit not in {"fit", "fill"}:
            raise ValueError("Fit mode must be fit or fill.")
        output = output_dir / ("image_grid.pptx" if tool_id == "image_ppt" else "video_grid.pptx")
        script = "sort_images_ppt.py" if tool_id == "image_ppt" else "sort_video.py"
        command = [python, str(SCRIPTS_DIR / script)]
        if tool_id == "image_ppt":
            command += ["--images", str(ordered_dir)]
        else:
            frame_indexes = [str(positive_int(value, "Frame index", 0, 0, 1000000)) for value in str(options.get("frameIndexes", "0")).replace(",", " ").split()]
            if not frame_indexes:
                raise ValueError("Provide at least one frame index.")
            command += ["--video-dir", str(ordered_dir), "--frame-idx", *frame_indexes]
        command += ["--rows", str(rows), "--cols", str(cols), "--cell-size-cm", str(size), "--gap-px", str(gap), "--margin-cm", str(margin), "--fit", fit, "--sort", "name", "--out", str(output)]
        return command, [output]
    if tool_id == "stack_images":
        direction = option_text(options, "direction", "horizontal")
        if direction not in {"horizontal", "vertical"}:
            raise ValueError("Direction must be horizontal or vertical.")
        output = output_dir / "stacked_images.pptx"
        gap = positive_int(options.get("gap"), "Gap", 5, 0, 1000)
        border = number(options.get("border"), "Border", 1, 0, 100)
        ordered_files = sorted(path for path in ordered_dir.iterdir() if path.is_file())
        return [python, str(SCRIPTS_DIR / "stack_cli.py"), "images", "--files", *[str(file) for file in ordered_files], "--direction", direction, "--gap-px", str(gap), "--border-pt", str(border), "--out", str(output)], [output]
    if tool_id == "stack_videos":
        rows = positive_int(options.get("rows"), "Rows", 1, 1, 50)
        cols = positive_int(options.get("cols"), "Columns", 1, 1, 50)
        mode = option_text(options, "mode", "h")
        if mode not in {"h", "v"}:
            raise ValueError("Layout mode must be h or v.")
        create_stack_readme(ordered_dir, options, rows, cols, mode)
        output = output_dir / "stacked_videos.mp4"
        command = [python, str(SCRIPTS_DIR / "stack_cli.py"), "videos", "--dir", str(ordered_dir), "--rows", str(rows), "--cols", str(cols), "--mode", mode, "--out", str(output)]
        for key, flag, default, minimum, maximum in (
            ("gap", "--gap-px", 5, 0, 1000), ("outerBorder", "--outer-border-px", 5, 0, 1000),
            ("titleBand", "--title-band-px", 40, 0, 10000), ("captionBand", "--rowcap-band-px" if mode == "h" else "--colcap-band-px", 150, 0, 10000),
            ("titleFont", "--title-fontsize", 26, 1, 1000), ("captionFont", "--rowcap-fontsize" if mode == "h" else "--colcap-fontsize", 30, 1, 1000),
        ):
            command += [flag, str(positive_int(options.get(key), key, default, minimum, maximum))]
        audio = option_text(options, "audio", "first")
        if audio not in {"first", "none"}:
            raise ValueError("Audio must be first or none.")
        command += ["--keep-audio", audio]
        fontfile = option_text(options, "fontFile")
        if fontfile:
            raise ValueError("Custom font paths are not accepted by the web panel. Install a usable system font instead.")
        return command, [output]
    if tool_id == "bibtex":
        output = output_dir / "references.bib"
        return [python, str(SCRIPTS_DIR / "get_bibtex.py"), str(files[0]), str(output)], [output]
    raise ValueError(f"Unsupported tool: {tool_id}")


def package_artifacts(job: Job, artifacts: list[Path]) -> tuple[Path, str]:
    existing = [path for path in artifacts if path.exists()]
    if not existing:
        raise RuntimeError("Tool completed but no output was produced.")
    if len(existing) == 1 and existing[0].is_file():
        return existing[0], existing[0].name
    archive = job.root / "output.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        for artifact in existing:
            if artifact.is_file():
                bundle.write(artifact, artifact.name)
            else:
                for file in artifact.rglob("*"):
                    if file.is_file():
                        bundle.write(file, file.relative_to(job.root))
    return archive, f"{job.tool.id}_results.zip"


def run_job(job: Job) -> None:
    with job.lock:
        job.status = "running"
    job.log(f"[INFO] Starting {job.tool.title}{' translation' if job.operation == 'translate' else ''}.")
    if job.tool.id == "paper_reader":
        try:
            if job.operation == "reader_cached_translate":
                run_cached_reader_translation(job)
            else:
                run_reader_document(job)
        except Exception as exc:
            document = reader_manager.get(job.reader_document_id or "")
            if document:
                document.status = "failed"
                document.error = str(exc)
                document.message = "文档处理失败。"
                if document.align_translation:
                    previous = document.alignment_progress or {}
                    document.alignment_progress = {
                        "stage": "failed",
                        "label": "LLM 对齐失败。",
                        "completed": int(previous.get("completed") or 0),
                        "total": int(previous.get("total") or 0),
                        "percent": int(previous.get("percent") or 0),
                    }
            with job.lock:
                job.finished_at = time.time()
            raise
        finally:
            log_translation_debug_summary(job)
            clear_translation_active(job)
            job.translation_config = None
        with job.lock:
            job.status = "completed_with_warnings" if job.warnings else "completed"
            job.finished_at = time.time()
        job.log("[INFO] Reader document is ready.")
        return
    if job.tool.id == "pdf_ocr_translate":
        try:
            if job.operation == "translate":
                run_pdf_translation(job)
            else:
                files = sorted(path for path in (job.root / "input").rglob("*") if path.is_file())
                run_pdf_ocr(job, files)
        except Exception:
            with job.lock:
                job.finished_at = time.time()
            raise
        finally:
            log_translation_debug_summary(job)
            clear_translation_active(job)
            job.markdown_repair_config = None
            if job.operation == "translate":
                job.translation_config = None
        with job.lock:
            job.status = "completed_with_warnings" if job.warnings else "completed"
            job.finished_at = time.time()
        job.log("[INFO] Completed. Download is ready.")
        return
    if job.tool.id == "document_translate":
        try:
            run_document_translation(job)
        except Exception:
            with job.lock:
                job.finished_at = time.time()
            raise
        finally:
            # The selected provider credential is needed only while this job runs.
            log_translation_debug_summary(job)
            clear_translation_active(job)
            job.translation_config = None
        with job.lock:
            job.status = "completed"
            job.finished_at = time.time()
        job.log("[INFO] Completed. Download is ready.")
        return
    if job.tool.id == "markdown_repair":
        try:
            run_markdown_repair_tool(job)
        except Exception:
            with job.lock:
                job.finished_at = time.time()
            raise
        finally:
            clear_translation_active(job)
            job.markdown_repair_config = None
        with job.lock:
            job.status = "completed"
            job.finished_at = time.time()
        job.log("[INFO] Markdown repair completed. Download is ready.")
        return
    if job.tool.id == "markdown_github":
        try:
            run_github_markdown_publish(job)
        except Exception:
            with job.lock:
                job.finished_at = time.time()
            raise
        with job.lock:
            job.status = "completed"
            job.finished_at = time.time()
        job.log("[INFO] GitHub 图片发布完成，可下载替换后的 Markdown。")
        return
    files = sorted((job.root / "input").rglob("*"))
    files = [path for path in files if path.is_file()]
    command, artifacts = build_command(job, files)
    if command:
        run_process(job, command)
    download_path, download_name = package_artifacts(job, artifacts)
    with job.lock:
        job.status = "completed"
        job.finished_at = time.time()
        job.download_path = download_path
        job.download_name = download_name
    job.log("[INFO] Completed. Download is ready.")


def store_job_uploads(job: Job, uploaded_files: list[Any], manifest: list[dict[str, Any]]) -> None:
    if len(uploaded_files) != len(manifest):
        raise ValueError("Upload manifest does not match uploaded files.")
    input_dir = job.root / "input"
    input_dir.mkdir(parents=True)
    ordered_dir = job.root / "ordered"
    needs_ordered_copies = job.tool.id in {"image_ppt", "video_ppt", "stack_images", "stack_videos"}
    if needs_ordered_copies:
        ordered_dir.mkdir()
    preserved_paths: set[Path] = set()
    for index, (upload, item) in enumerate(zip(uploaded_files, manifest), start=1):
        if not isinstance(item, dict):
            raise ValueError("Invalid upload manifest.")
        relative = safe_relative_path(str(item.get("relativePath") or upload.filename or "file"))
        suffix = Path(upload.filename or relative.name).suffix.lower()
        if suffix not in job.tool.accepts:
            raise ValueError(f"{relative.name}: unsupported file type for {job.tool.title}.")
        if job.tool.id == "markdown_github":
            preserved = reader_pair_relative_path(str(item.get("relativePath") or upload.filename or "file"))
            if preserved.suffix.lower() != suffix:
                raise ValueError(f"{preserved.name}: 文件扩展名与上传内容不一致。")
            if preserved in preserved_paths:
                raise ValueError(f"{preserved.as_posix()}: 上传路径重复。")
            preserved_paths.add(preserved)
            target = input_dir / preserved
            target.parent.mkdir(parents=True, exist_ok=True)
            upload.save(target)
            continue
        if job.tool.id == "pdf_ocr_translate":
            job.source_stem = safe_output_stem(upload.filename or relative.name)
            target = input_dir / f"{job.source_stem}.pdf"
            upload.save(target)
            continue
        work_name = safe_work_name(str(item.get("workName") or relative.stem), suffix)
        target = input_dir / relative.parent / f"{index:04d}_{work_name}"
        target.parent.mkdir(parents=True, exist_ok=True)
        upload.save(target)
        if needs_ordered_copies:
            shutil.copyfile(target, ordered_dir / target.name)


def research_gap_llm_request(preset_id: str, messages: list[dict[str, str]]) -> str:
    """Run research extraction through the shared preset runtime."""
    if OpenAI is None:
        raise RuntimeError("The OpenAI Python SDK is not installed.")
    config = managed_translation_config_from_request({
        "llm": {"mode": "preset", "presetId": preset_id},
    })
    client = OpenAI(
        api_key=config["apiKey"],
        base_url=config["baseUrl"],
        timeout=180.0,
        max_retries=0,
    )
    response = create_compatible_chat_completion(
        client,
        config,
        model=config["model"],
        temperature=0.1,
        messages=messages,
    )
    content = response.choices[0].message.content if response.choices else None
    result = llm_test_response_text(content)
    if not result:
        raise RuntimeError("LLM returned an empty research extraction.")
    return result


def research_gap_reader_source(cache_key: str) -> dict[str, Any] | None:
    """Resolve the richest durable reader artifact for research analysis."""
    entry = reader_cache_library_entry(cache_key)
    if not entry:
        return None
    storage_root = str(entry.get("storageRoot") or "")
    render_kind = str(entry.get("renderKind") or "")
    filename = entry.get("documentFilename") if render_kind == "html" else entry.get("sourceFilename")
    if not storage_root or not filename:
        return None
    source = reader_cached_file(reader_cache_root(cache_key) / storage_root, str(filename))
    if not source:
        return None
    return {"entry": entry, "path": str(source)}


research_gap_service = ResearchGapService(
    RESEARCH_GAP_DIR,
    llm_request=research_gap_llm_request,
    llm_presets=public_llm_presets,
    reader_source=research_gap_reader_source,
)
app.register_blueprint(create_research_gap_blueprint(research_gap_service))


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/reader")
def reader():
    return render_template("reader.html")


@app.get("/research-gaps")
def research_gaps():
    return render_template("research_gaps.html")


@app.get("/health")
def health():
    return render_template("health.html")


READER_ACTIONS = {
    "translate": "翻译选区：忠实翻译为简体中文，保留 LaTeX、变量名、引用编号、代码和专有名词；必要时在译文后用一句话说明关键术语。",
    "explain": "解释选区：先给一句结论，再逐句解释术语、变量和逻辑；优先给机器学习论文中的具体含义。",
    "formula": "解释公式：列出所有符号及张量形状/取值域，说明每一项的作用、输入输出、假设和训练/推理时的意义。",
    "geometry": "解释公式的几何意义：明确向量/参数/概率分布所处的空间，说明方向、距离、角度、投影、流形或优化几何；没有严格几何解释时要直说并给直觉。",
    "derivation": "推导这一步：从可见的前提开始，逐行给出代数、概率或微积分变形；指出使用的恒等式、近似或条件，不能凭空补全未给出的前提。",
    "summary": "总结选区和相邻上下文：说明问题、方法、关键机制、结论以及它在整篇论文中的作用。",
    "intuition": "给出直觉解释：使用一个小型数值、二维几何或训练过程例子，但不要改变原公式或暗示未证实结论。",
    "assumptions": "批判性阅读：识别显式/隐式假设、潜在失效情形、计算代价、数据分布要求与需要实验验证的主张。",
    "implementation": "转换为实现视角：说明张量形状、伪代码步骤、数值稳定性、常用默认值和在 PyTorch/JAX 中容易出错的位置。",
}


def reader_llm_config(data: dict[str, Any]) -> dict[str, Any]:
    return translation_config_from_request(data)


def reader_context(document: ReaderDocument, block_id: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    blocks = document.blocks
    index = next((i for i, block in enumerate(blocks) if block["id"] == block_id), None)
    if index is None:
        return None, []
    start, end = max(0, index - 2), min(len(blocks), index + 3)
    return blocks[index], blocks[start:end]


def reader_live_paragraph_text(document: ReaderDocument, block_id: str) -> str | None:
    if document.render_kind == "markdown":
        block = next((item for item in document.blocks if item.get("id") == block_id), None)
        return str(block.get("content") or "").strip() if block else None
    if document.render_kind != "html" or not document.document_filename:
        return None
    source = reader_cached_file(document.root / "output", document.document_filename)
    if not source:
        return None
    try:
        soup = BeautifulSoup(source.read_text(encoding="utf-8"), "html.parser")
    except OSError:
        return None
    node = soup.find(attrs={"data-reader-pair-id": block_id})
    return node.get_text(" ", strip=True) if node else None


def reader_live_translation_cache_path(
    document: ReaderDocument,
    config: dict[str, str],
    block_id: str,
    source_text: str,
) -> Path | None:
    if (
        not document.cache_key
        or not re.fullmatch(r"[0-9a-f]{64}", document.cache_key)
        or not read_reader_cache_manifest(document.cache_key)
    ):
        return None
    model_key = reader_translation_cache_key(config)
    paragraph_key = hashlib.sha256(
        f"{READER_PAIRING_VERSION}\0{block_id}\0{source_text}".encode("utf-8")
    ).hexdigest()
    return reader_cache_root(document.cache_key) / "live-translations" / model_key / f"{paragraph_key}.json"


def read_reader_live_translation(
    document: ReaderDocument,
    config: dict[str, str],
    block_id: str,
    source_text: str,
) -> str | None:
    path = reader_live_translation_cache_path(document, config, block_id, source_text)
    if not path:
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    translation = payload.get("translation") if isinstance(payload, dict) else None
    return str(translation).strip() if translation else None


def write_reader_live_translation(
    document: ReaderDocument,
    config: dict[str, str],
    block_id: str,
    source_text: str,
    translation: str,
) -> None:
    path = reader_live_translation_cache_path(document, config, block_id, source_text)
    if not path:
        return
    payload = {
        "version": READER_PAIRING_VERSION,
        "blockId": block_id,
        "sourceHash": hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
        "translation": translation,
        "updatedAt": time.time(),
    }
    with reader_cache_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_durable_text(path, json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def ask_reader_llm(config: dict[str, Any], action: str, selection: str, block: dict[str, Any], context: list[dict[str, Any]], custom_question: str) -> str:
    if OpenAI is None:
        raise RuntimeError("The OpenAI Python SDK is not installed.")
    instruction = READER_ACTIONS.get(action)
    if not instruction:
        raise ValueError("Unsupported reader action.")
    context_text = "\n\n".join(
        f"[{item['id']} · {item.get('section') or '未命名章节'}]\n{item['content']}" for item in context
    )
    user_prompt = (
        f"任务：{instruction}\n\n"
        f"所在章节：{block.get('section') or '未命名章节'}\n"
        f"用户划选文本：\n{selection}\n\n"
        f"相邻原文（只能作为上下文，不能把未出现内容说成论文事实）：\n{context_text}"
    )
    if custom_question.strip():
        user_prompt += f"\n\n用户的补充问题：{custom_question.strip()}"
    client = OpenAI(api_key=config["apiKey"], base_url=config["baseUrl"], timeout=180.0, max_retries=0)
    response = create_compatible_chat_completion(
        client,
        config,
        model=config["model"],
        temperature=0.2,
        messages=[
            {
                "role": "system",
                "content": (
                    "你是严谨的计算机科学研究导师，专长为深度学习、机器学习、扩散模型、概率建模、"
                    "优化与相关数学理论。用简体中文作答。只依据给定文本和明确标注的通用数学知识；"
                    "区分论文原文、你的推导和合理推断。保留全部 LaTeX 符号与变量命名，不编造实验、"
                    "引用或作者意图。使用标准 Markdown 输出；答案先给直接结论，随后按需使用小标题、列表、表格或代码块；"
                    "若上下文不足，明确说明缺少什么。"
                ),
            },
            {"role": "user", "content": user_prompt},
        ],
    )
    answer = response.choices[0].message.content if response.choices else None
    if not answer or not answer.strip():
        raise RuntimeError("LLM returned an empty explanation.")
    return answer.strip()


@app.get("/api/reader/config")
def reader_config():
    return jsonify({
        "llmPresets": public_llm_presets(),
        "actions": [{"id": key, "label": label.split("：", 1)[0]} for key, label in READER_ACTIONS.items()],
        "speech": public_azure_speech_config(),
        "readerFeatureVersion": READER_FEATURE_VERSION,
        "readerUpdatePolicy": READER_UPDATE_POLICY,
        "readerPairingVersion": READER_PAIRING_VERSION,
    })


@app.get("/api/reader/library")
def reader_library():
    return jsonify({"documents": reader_cache_library()})


@app.patch("/api/reader/library/<cache_key>")
def rename_reader_library_document(cache_key: str):
    cached = reader_cache_library_entry(cache_key)
    if not cached:
        return json_error("Cached reader document not found.", 404)
    data = request.get_json(silent=True) or {}
    raw_title = data.get("title")
    if not isinstance(raw_title, str):
        return json_error("Document title must be a string.")
    title = clean_reader_title(raw_title)
    if not title or len(title) > 160:
        return json_error("Document title must contain 1 to 160 readable characters.")
    with reader_cache_lock:
        manifest = read_reader_cache_manifest(cache_key)
        document = manifest.get("document")
        if isinstance(document, dict):
            document["title"] = title
            document["titleCustomized"] = True
        ocr = manifest.get("ocr")
        if isinstance(ocr, dict):
            ocr["title"] = title
            ocr["titleCustomized"] = True
        if not isinstance(document, dict) and not isinstance(ocr, dict):
            return json_error("Cached reader document metadata is incomplete.", 409)
        write_reader_cache_manifest(cache_key, manifest)
    with reader_manager.lock:
        for active_document in reader_manager.documents.values():
            if active_document.cache_key == cache_key:
                active_document.title = title
                active_document.title_customized = True
    updated = reader_cache_library_entry(cache_key)
    return jsonify({"document": updated})


@app.post("/api/reader/library/<cache_key>/open")
def open_reader_library_document(cache_key: str):
    cached = reader_cache_library_entry(cache_key)
    if not cached:
        return json_error("Cached reader document not found.", 404)
    data = request.get_json(silent=True) or {}
    include_translation = data.get("includeTranslation") is True
    if include_translation and not cached["hasTranslation"] and not cached.get("canTranslateDocument"):
        return json_error("This cached document cannot generate a full-document translation without OCR.")
    translation_config = None
    if include_translation and not cached["hasTranslation"]:
        try:
            translation_config = managed_translation_config_from_request({"llm": data.get("llm")})
        except ValueError as exc:
            return json_error(str(exc))
    cached_source_type = str(cached.get("sourceType") or "pdf")
    cached_render_kind = str(cached.get("renderKind") or "html")
    cached_is_html = cached_render_kind == "html"
    cached_is_markdown = cached_render_kind == "markdown"
    if cached_is_html:
        cached_mode = (
            "ocr_translate" if include_translation else "ocr"
        ) if cached.get("useOcr") else (
            "html_translate" if include_translation else "html"
        )
    elif cached_is_markdown:
        cached_mode = "markdown_translate" if include_translation else "markdown"
    else:
        cached_mode = "pdf_pair" if include_translation and cached["hasTranslation"] else "pdf"
    document_id = uuid.uuid4().hex
    root = JOBS_DIR / f"reader-{document_id}"
    document = ReaderDocument(
        document_id,
        root,
        cached["title"],
        cached_source_type,
        cached_mode,
        render_kind=cached_render_kind,
        title_customized=cached.get("titleCustomized") is True,
        use_ocr=bool(cached.get("useOcr", True)),
        generate_translation=include_translation,
        use_local_cache=True,
        cache_key=cache_key,
        cache_available=[cached.get("storageRoot") or "document"] + (["translation"] if cached["hasTranslation"] else []),
    )
    document.source_name = cached.get("sourceName")
    operation = "reader_cached_translate" if translation_config and cached_is_html else "reader"
    job = Job(
        document_id,
        READER_TOOL,
        root,
        {},
        status="queued" if translation_config else "completed",
        phase="translation_queued" if translation_config else "reader_ready",
        operation=operation,
        translation_config=translation_config,
        reader_document_id=document_id,
        finished_at=None if translation_config else time.time(),
    )
    try:
        root.mkdir(parents=True, exist_ok=True)
        if cached_is_html:
            primary_source = restore_reader_ocr_cache(job, document)
            if not primary_source:
                raise RuntimeError("Cached HTML reader document is incomplete.")
            document.document_filename = relative_path_within(primary_source, root / "output").as_posix()
            document.cache_hits.append("ocr" if document.use_ocr else "document")
        else:
            primary_source = restore_reader_source_cache(root, cached)
            if not primary_source:
                raise RuntimeError("Cached source reader document is incomplete.")
            document.source_filename = primary_source.name
            job.source_stem = safe_output_stem(primary_source.name)
            document.cache_hits.append("document")
        refresh_cached_document_for_current_reader(document, primary_source)
        if cached_is_html:
            try:
                store_reader_ocr_cache(job, document)
            except Exception as exc:
                note_reader_cache_failure(job, "refresh aligned HTML in", exc)
        cached_translation = latest_reader_cached_translation(cache_key) if include_translation else None
        if include_translation and cached_translation:
            if cached_is_html:
                translated = primary_source.with_name(f"{primary_source.stem}_zh-CN.html")
                shutil.copyfile(cached_translation, translated)
                aligned_source = reader_cached_aligned_html_source(cache_key, cached_translation)
                if aligned_source:
                    shutil.copyfile(aligned_source, primary_source)
                document.translated_filename = relative_path_within(translated, root / "output").as_posix()
            elif cached_is_markdown:
                output_root = root / "output"
                output_root.mkdir(parents=True, exist_ok=True)
                translated = output_root / f"{primary_source.stem}_zh-CN{primary_source.suffix}"
                shutil.copyfile(cached_translation, translated)
                document.translated_blocks = (
                    reader_cached_aligned_blocks(cache_key, cached_translation)
                    or markdown_to_reader_blocks(translated.read_text(encoding="utf-8"))
                )
            else:
                output_root = root / "output"
                output_root.mkdir(parents=True, exist_ok=True)
                translated = output_root / f"{primary_source.stem}_translated.pdf"
                shutil.copyfile(cached_translation, translated)
                document.translated_filename = translated.relative_to(output_root).as_posix()
            document.cache_hits.append("translation")
        document.status = "queued" if translation_config else "ready"
        document.message = (
            "已恢复本地文档，整篇翻译任务正在排队。"
            if translation_config
            else "已载入本地文档内容，并应用当前版本的全部阅读功能。"
        )
        document.job_id = job.id
        job.download_path = primary_source
        job.download_name = primary_source.name
    except Exception as exc:
        shutil.rmtree(root, ignore_errors=True)
        return json_error(f"Unable to restore cached reader document: {exc}", 409)
    reader_manager.add(document)
    if translation_config:
        job.log("[CACHE] Restored the cached reader document; queued translation without repeating document processing.")
        job_manager.submit(job)
        return jsonify(document.snapshot()), 202
    with job_manager.lock:
        job_manager.jobs[job.id] = job
    return jsonify(document.snapshot()), 201


@app.get("/api/llm-presets")
def list_llm_presets():
    return jsonify({"presets": public_llm_presets()})


@app.get("/api/speech-config")
def get_speech_config():
    return jsonify({"speech": public_azure_speech_config()})


@app.put("/api/speech-config")
def update_speech_config():
    data = request.get_json(silent=True) or {}
    try:
        speech = save_azure_speech_config(
            data.get("apiKey"),
            data.get("region"),
            data.get("voice"),
        )
    except (ValueError, OSError) as exc:
        return json_error(str(exc))
    return jsonify({"speech": speech})


@app.post("/api/speech-config/test")
def test_speech_config():
    try:
        result = test_azure_speech_connection()
    except ValueError as exc:
        return json_error(str(exc))
    except RuntimeError as exc:
        return json_error(str(exc), 502)
    return jsonify({"result": result})


@app.delete("/api/speech-config")
def remove_speech_config():
    try:
        speech = delete_azure_speech_config()
    except OSError as exc:
        return json_error(str(exc))
    return jsonify({"speech": speech})


@app.get("/api/github-config")
def get_github_config():
    return jsonify({"github": public_github_config()})


@app.put("/api/github-config")
def update_github_config():
    data = request.get_json(silent=True) or {}
    try:
        github = save_github_config(
            data.get("token"),
            data.get("repository"),
            data.get("branch"),
            data.get("imageRoot"),
        )
    except (ValueError, OSError) as exc:
        return json_error(str(exc))
    return jsonify({"github": github})


@app.post("/api/github-config/test")
def test_saved_github_config():
    try:
        result = test_github_connection()
    except ValueError as exc:
        return json_error(str(exc))
    except RuntimeError as exc:
        return json_error(str(exc), 502)
    return jsonify({"result": result})


@app.delete("/api/github-config")
def remove_github_config():
    try:
        github = delete_github_config()
    except OSError as exc:
        return json_error(str(exc))
    return jsonify({"github": github})


@app.post("/api/llm-presets/<preset_id>/test")
def test_saved_llm_preset(preset_id: str):
    data = request.get_json(silent=True) or {}
    try:
        result = test_llm_preset_connection(preset_id, data.get("message"))
    except ValueError as exc:
        return json_error(str(exc), 404 if "does not exist" in str(exc) else 400)
    except Exception as exc:
        return json_error(safe_llm_test_error(exc, preset_id), 503 if OpenAI is None else 502)
    return jsonify({"result": result})


@app.post("/api/llm-presets/<preset_id>/concurrency-test")
def test_saved_llm_preset_concurrency(preset_id: str):
    try:
        result = test_llm_preset_concurrency(preset_id)
    except ValueError as exc:
        return json_error(str(exc), 404 if "does not exist" in str(exc) else 400)
    except Exception as exc:
        return json_error(safe_llm_test_error(exc, preset_id), 503 if OpenAI is None else 502)
    return jsonify({"result": result})


@app.post("/api/llm-presets")
@app.post("/api/reader/llm-presets")  # Backward-compatible reader alias.
def create_reader_llm_preset():
    data = request.get_json(silent=True) or {}
    try:
        preset = save_llm_preset(
            data.get("name"),
            data.get("baseUrl"),
            data.get("apiKey"),
            data.get("model"),
            data.get("concurrency", 1),
        )
    except (ValueError, OSError) as exc:
        return json_error(str(exc))
    return jsonify({"preset": preset}), 201


@app.put("/api/llm-presets/<preset_id>")
def edit_llm_preset(preset_id: str):
    data = request.get_json(silent=True) or {}
    try:
        preset = update_llm_preset(
            preset_id,
            data.get("name"),
            data.get("baseUrl"),
            data.get("apiKey"),
            data.get("model"),
            data.get("concurrency"),
        )
    except (ValueError, OSError) as exc:
        return json_error(str(exc), 404 if "does not exist" in str(exc) else 400)
    return jsonify({"preset": preset})


@app.delete("/api/llm-presets/<preset_id>")
def remove_llm_preset(preset_id: str):
    try:
        preset = delete_llm_preset(preset_id)
    except (ValueError, OSError) as exc:
        return json_error(str(exc), 404 if "does not exist" in str(exc) else 400)
    return jsonify({"preset": preset})


def reader_form_boolean(name: str, default: bool = False) -> bool:
    value = request.form.get(name)
    if value is None:
        return default
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false.")


def reader_format_family(suffix: str) -> str | None:
    normalized = suffix.lower()
    if normalized == ".pdf":
        return "pdf"
    if normalized in {".md", ".mmd"}:
        return "markdown"
    if normalized in {".html", ".htm"}:
        return "html"
    return None


@app.post("/api/reader/documents")
def create_reader_document():
    uploaded = request.files.get("file")
    if not uploaded or not uploaded.filename:
        return json_error("Choose a PDF, Markdown, MMD, or HTML file.")
    suffix = Path(uploaded.filename).suffix.lower()
    if suffix not in READER_SOURCE_SUFFIXES:
        return json_error("Reader accepts .pdf, .md, .mmd, .html, and .htm files.")
    translated_upload = request.files.get("translationFile")
    has_existing_translation = bool(translated_upload and translated_upload.filename)
    translated_suffix = Path(translated_upload.filename).suffix.lower() if has_existing_translation else ""
    if has_existing_translation and (
        translated_suffix not in READER_SOURCE_SUFFIXES
        or reader_format_family(translated_suffix) != reader_format_family(suffix)
    ):
        return json_error("The existing translation must use the same document format as the source.")
    legacy_mode = request.form.get("mode")
    if legacy_mode is not None and legacy_mode not in {"ocr", "ocr_translate"}:
        return json_error("Choose OCR only or OCR + translation.")
    try:
        if "useOcr" in request.form or "generateTranslation" in request.form:
            use_ocr = reader_form_boolean("useOcr")
            generate_translation = reader_form_boolean("generateTranslation")
        else:
            use_ocr = suffix == ".pdf" and legacy_mode in {"ocr", "ocr_translate"}
            generate_translation = legacy_mode == "ocr_translate"
    except ValueError as exc:
        return json_error(str(exc))
    if has_existing_translation:
        use_ocr = False
        generate_translation = False
    if suffix != ".pdf":
        use_ocr = False
    if suffix == ".pdf" and generate_translation and not use_ocr:
        return json_error("Full-document PDF translation requires OCR. Leave it off to use selection translation.")
    if generate_translation and suffix not in {".pdf", ".md", ".mmd", ".html", ".htm"}:
        return json_error("AI-Markdown-Translator full-document translation supports Markdown, MMD, and HTML.")
    try:
        use_local_cache = reader_form_boolean("useLocalCache", True)
        align_translation = reader_form_boolean("alignTranslation", False)
    except ValueError as exc:
        return json_error(str(exc))
    if align_translation and not has_existing_translation:
        return json_error("Translation alignment requires an uploaded source/translation pair.")
    if align_translation and reader_format_family(suffix) not in {"markdown", "html"}:
        return json_error("LLM block alignment currently supports Markdown/MMD and HTML pairs.")
    translation_config = None
    if generate_translation or align_translation:
        try:
            llm_payload = json.loads(request.form.get("llm", "{}"))
            resolver = managed_translation_config_from_request if generate_translation else reader_llm_config
            translation_config = resolver({"llm": llm_payload})
        except (ValueError, json.JSONDecodeError) as exc:
            return json_error(str(exc))
    if suffix == ".pdf":
        mode = "pdf_pair" if has_existing_translation else "ocr_translate" if generate_translation else "ocr" if use_ocr else "pdf"
        render_kind = "html" if use_ocr else "pdf"
        source_type = "pdf"
    elif suffix in {".html", ".htm"}:
        mode = "html_pair" if has_existing_translation else "html_translate" if generate_translation else "html"
        render_kind = "html"
        source_type = "html"
    else:
        mode = "markdown_pair" if has_existing_translation else "markdown_translate" if generate_translation else "markdown"
        render_kind = "markdown"
        source_type = "markdown"
    document_id = uuid.uuid4().hex
    root = JOBS_DIR / f"reader-{document_id}"
    document = ReaderDocument(
        document_id,
        root,
        Path(uploaded.filename).stem,
        source_type,
        mode,
        render_kind=render_kind,
        use_ocr=use_ocr,
        generate_translation=generate_translation,
        align_translation=align_translation,
        use_local_cache=use_local_cache,
    )
    job = Job(document_id, READER_TOOL, root, {}, operation="reader", translation_config=translation_config, reader_document_id=document_id)
    mathpix_error = None
    try:
        input_dir = root / "input"
        input_dir.mkdir(parents=True, exist_ok=True)
        destination = input_dir / safe_work_name(uploaded.filename, suffix)
        uploaded.save(destination)
        asset_uploads = request.files.getlist("assets")
        if asset_uploads and source_type not in {"markdown", "html"}:
            raise ValueError("Only Markdown, MMD, and HTML documents can include a separate image directory.")
        if len(asset_uploads) > 3000:
            raise ValueError("A reader document can include at most 3,000 image files.")
        asset_manifest_raw = request.form.get("assetManifest", "[]")
        try:
            asset_manifest = json.loads(asset_manifest_raw)
        except json.JSONDecodeError as exc:
            raise ValueError("Invalid reader image manifest.") from exc
        if not isinstance(asset_manifest, list) or len(asset_manifest) != len(asset_uploads):
            raise ValueError("The reader image manifest does not match the uploaded files.")
        output_dir = root / "output"
        translated_destination = None
        if has_existing_translation and translated_upload:
            translated_name = safe_work_name(translated_upload.filename, translated_suffix)
            if translated_name.casefold() == destination.name.casefold():
                translated_name = f"{Path(translated_name).stem}_translated{translated_suffix}"
            translated_destination = output_dir / translated_name
            translated_destination.parent.mkdir(parents=True, exist_ok=True)
            translated_upload.save(translated_destination)
            document.translated_filename = translated_destination.relative_to(output_dir).as_posix()
        saved_assets: set[Path] = set()
        for asset, item in zip(asset_uploads, asset_manifest):
            if not asset.filename or not isinstance(item, dict):
                raise ValueError("Invalid reader image upload.")
            relative = reader_pair_relative_path(str(item.get("relativePath") or asset.filename))
            if relative.suffix.lower() not in READER_ASSET_SUFFIXES:
                raise ValueError(f"{relative.name}: unsupported reader image type.")
            if relative in saved_assets:
                raise ValueError(f"{relative.as_posix()}: duplicate reader image path.")
            saved_assets.add(relative)
            asset_destination = output_dir / relative
            asset_destination.parent.mkdir(parents=True, exist_ok=True)
            asset.save(asset_destination)
        job.source_stem = safe_output_stem(destination.name)
        document.source_filename = destination.name
        document.source_name = Path(uploaded.filename.replace("\\", "/")).name
        if suffix == ".pdf":
            document.title = pdf_document_title(destination) or Path(document.source_name).stem
        elif suffix in {".html", ".htm"}:
            document.title = cached_ocr_html_title(destination) or Path(document.source_name).stem
        document.cache_key = reader_source_cache_key(destination)
        if use_local_cache:
            document.cache_available = reader_cache_availability(document.cache_key, translation_config)
        if suffix == ".pdf" and use_ocr and "ocr" not in document.cache_available:
            mathpix_error = mathpix_config_error()
    except Exception as exc:
        shutil.rmtree(root, ignore_errors=True)
        return json_error(str(exc))
    if mathpix_error:
        shutil.rmtree(root, ignore_errors=True)
        return json_error(mathpix_error, 503)
    document.job_id = job.id
    if has_existing_translation and translated_destination and align_translation:
        document.status = "queued"
        document.message = "原文与译文已导入，正在等待 LLM 段落对齐。"
        document.alignment_progress = {
            "stage": "queued",
            "label": document.message,
            "completed": 0,
            "total": 0,
            "percent": 0,
        }
        reader_manager.add(document)
        job.log("[INFO] Imported pair queued for LLM paragraph alignment.")
        job_manager.submit(job)
        return jsonify(document.snapshot()), 202
    if has_existing_translation and translated_destination:
        try:
            if render_kind == "markdown":
                document.blocks = markdown_to_reader_blocks(destination.read_text(encoding="utf-8"))
                document.translated_blocks = markdown_to_reader_blocks(
                    translated_destination.read_text(encoding="utf-8")
                )
            elif render_kind == "html":
                original_html = output_dir / destination.name
                shutil.copyfile(destination, original_html)
                document.document_filename = original_html.relative_to(output_dir).as_posix()
                document.translated_filename = translated_destination.relative_to(output_dir).as_posix()
            else:
                document.translated_filename = translated_destination.relative_to(output_dir).as_posix()
            document.status = "ready"
            document.message = "已有原文与译文已导入，可直接对照阅读。"
            job.status = "completed"
            job.phase = "reader_ready"
            job.finished_at = time.time()
            job.download_path = destination
            job.download_name = destination.name
            try:
                if render_kind == "html":
                    store_reader_ocr_cache(job, document)
                else:
                    store_reader_source_cache(document, destination)
                store_reader_translation_cache(
                    document,
                    {"baseUrl": "local://uploaded-pair", "model": "existing-translation"},
                    translated_destination,
                )
            except Exception as exc:
                job.log(f"[CACHE] Could not persist uploaded reader pair: {exc}")
        except Exception as exc:
            shutil.rmtree(root, ignore_errors=True)
            return json_error(f"Unable to import the source/translation pair: {exc}", 409)
        reader_manager.add(document)
        with job_manager.lock:
            job_manager.jobs[job.id] = job
        return jsonify(document.snapshot()), 201
    reader_manager.add(document)
    job.log("[INFO] Reader document queued.")
    job_manager.submit(job)
    return jsonify(document.snapshot()), 202


@app.get("/api/reader/documents/<document_id>")
def reader_document_status(document_id: str):
    document = reader_manager.get(document_id)
    if not document:
        return json_error("Reader document not found.", 404)
    return jsonify(document.snapshot())


@app.get("/api/reader/documents/<document_id>/state")
def reader_document_state(document_id: str):
    document = reader_manager.get(document_id)
    if not document or not document.cache_key:
        return json_error("Reader document state is unavailable.", 404)
    return jsonify(read_reader_persistent_state(document.cache_key))


@app.put("/api/reader/documents/<document_id>/state")
def update_reader_document_state(document_id: str):
    document = reader_manager.get(document_id)
    if not document or not document.cache_key:
        return json_error("Reader document state is unavailable.", 404)
    if request.content_length and request.content_length > 4 * 1024 * 1024:
        return json_error("Reader notes, annotations, and saved translations exceed the 4 MB local-state limit.", 413)
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return json_error("Reader state must be a JSON object.")
    try:
        state = write_reader_persistent_state(document.cache_key, data)
    except ValueError as exc:
        return json_error(str(exc), 413 if "4 MB" in str(exc) else 409)
    return jsonify(state)


@app.get("/api/reader/documents/<document_id>/content")
def reader_document_content(document_id: str):
    document = reader_manager.get(document_id)
    if not document:
        return json_error("Reader document not found.", 404)
    if document.status != "ready":
        return json_error("The reader document is not ready.", 409)
    asset_base = f"/api/reader/documents/{document.id}/assets/"

    def asset_url(relative: str | None) -> str | None:
        if not relative:
            return None
        return asset_base + "/".join(quote(part, safe="") for part in PurePosixPath(relative).parts)

    document_url = (
        f"/api/reader/documents/{document.id}/source"
        if document.render_kind == "pdf"
        else asset_url(document.document_filename)
    )
    return jsonify({
        "title": document.title,
        "renderKind": document.render_kind,
        "documentUrl": document_url,
        "translatedDocumentUrl": asset_url(document.translated_filename),
        "pageCount": document.page_count,
        "blocks": document.blocks,
        "translatedBlocks": document.translated_blocks,
        "assetBase": asset_base,
        "readerFeatureVersion": READER_FEATURE_VERSION,
        "readerUpdatePolicy": READER_UPDATE_POLICY,
        "readerPairingVersion": READER_PAIRING_VERSION,
        "capabilities": reader_capabilities(
            document.render_kind,
            document.translated_blocks is not None or document.translated_filename is not None,
        ),
    })


@app.get("/api/reader/documents/<document_id>/source")
def reader_source(document_id: str):
    document = reader_manager.get(document_id)
    if not document or document.source_type != "pdf" or not document.source_filename:
        return json_error("Reader PDF source not found.", 404)
    input_root = (document.root / "input").resolve()
    candidate = (input_root / document.source_filename).resolve()
    if input_root not in candidate.parents or not candidate.is_file() or candidate.suffix.lower() != ".pdf":
        return json_error("Reader PDF source not found.", 404)
    return send_file(candidate, mimetype="application/pdf", conditional=True)


@app.get("/api/reader/documents/<document_id>/assets/<path:asset_path>")
def reader_asset(document_id: str, asset_path: str):
    document = reader_manager.get(document_id)
    if not document:
        return json_error("Reader document not found.", 404)
    try:
        relative = reader_pair_relative_path(asset_path)
    except ValueError:
        return json_error("Reader asset not found.", 404)
    candidate = (document.root / "output" / relative).resolve()
    output_root = (document.root / "output").resolve()
    if output_root not in candidate.parents or not candidate.is_file():
        return json_error("Reader asset not found.", 404)
    response = send_file(candidate, conditional=True)
    if candidate.suffix.lower() in {".html", ".htm"}:
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; img-src 'self' data: blob:; "
            "style-src 'self' 'unsafe-inline'; font-src 'self' data:; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'self'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.post("/api/reader/documents/<document_id>/questions")
def reader_question(document_id: str):
    document = reader_manager.get(document_id)
    if not document or document.status != "ready":
        return json_error("Reader document is not ready.", 404)
    data = request.get_json(silent=True) or {}
    selection = str(data.get("selection", "")).strip()
    action = str(data.get("action", "")).strip()
    block_id = str(data.get("blockId", "")).strip()
    if not selection or len(selection) > 12000:
        return json_error("Select between 1 and 12,000 characters of text.")
    supplied_context = str(data.get("context", "")).strip()
    if len(supplied_context) > MAX_READER_CONTEXT_LENGTH:
        return json_error(f"Reader context cannot exceed {MAX_READER_CONTEXT_LENGTH:,} characters.")
    section = str(data.get("section", "")).strip()[:500]
    page_number = data.get("pageNumber")
    if page_number is not None:
        try:
            page_number = int(page_number)
        except (TypeError, ValueError):
            return json_error("pageNumber must be an integer.")
        if page_number < 1:
            return json_error("pageNumber must be positive.")
    block, context = reader_context(document, block_id)
    if supplied_context:
        location = f"第 {page_number} 页" if page_number else section or "当前选区"
        block = {"id": block_id or "selection", "section": section or location, "content": selection}
        context = [{"id": f"page-{page_number}" if page_number else "context", "section": section or location, "content": supplied_context}]
    if not block:
        return json_error("The selected paragraph is unavailable.")
    try:
        config = reader_llm_config(data)
        answer = ask_reader_llm(config, action, selection, block, context, str(data.get("question", "")))
    except (ValueError, RuntimeError) as exc:
        return json_error(str(exc), 503 if "SDK" in str(exc) else 400)
    return jsonify({
        "answer": answer,
        "action": action,
        "sourceBlockId": block_id or block["id"],
        "contextBlockIds": [item["id"] for item in context],
    })


@app.post("/api/reader/documents/<document_id>/speech")
def reader_speech(document_id: str):
    document = reader_manager.get(document_id)
    if not document or document.status != "ready":
        return json_error("Reader document is not ready.", 404)
    if document.source_type not in {"pdf", "markdown", "html"}:
        return json_error("段落朗读仅支持 PDF、Markdown 与 HTML 阅读。", 409)
    data = request.get_json(silent=True) or {}
    text = data.get("text")
    if not isinstance(text, str):
        return json_error("朗读文本必须是字符串。")
    try:
        rate = normalize_azure_speech_rate(data.get("rate", 1))
        audio, voice = synthesize_azure_speech(text, rate)
    except ValueError as exc:
        return json_error(str(exc))
    except RuntimeError as exc:
        return json_error(str(exc), 502)
    response = app.response_class(audio, mimetype="audio/mpeg")
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Speech-Voice"] = voice
    response.headers["X-Speech-Rate"] = str(rate)
    return response


@app.post("/api/reader/documents/<document_id>/paragraph-translations")
def reader_paragraph_translation(document_id: str):
    document = reader_manager.get(document_id)
    if not document or document.status != "ready":
        return json_error("Reader document is not ready.", 404)
    if document.render_kind not in {"markdown", "html"}:
        return json_error("Live paragraph translation requires Markdown or HTML.", 409)
    data = request.get_json(silent=True) or {}
    block_id = str(data.get("blockId") or "").strip()
    if not block_id or len(block_id) > 200 or not re.fullmatch(r"[A-Za-z0-9._:-]+", block_id):
        return json_error("A valid paragraph ID is required.")
    source_text = reader_live_paragraph_text(document, block_id)
    if not source_text:
        source_text = str(data.get("sourceText") or "").strip()
    if not source_text or len(source_text) > 12000:
        return json_error("The paragraph must contain between 1 and 12,000 characters.")
    try:
        config = reader_llm_config(data)
        cached = read_reader_live_translation(document, config, block_id, source_text)
        if cached:
            return jsonify({"blockId": block_id, "translation": cached, "cached": True})
        translation_job = Job(
            f"reader-live-{uuid.uuid4().hex}",
            READER_TOOL,
            document.root,
            {},
            operation="reader_live_translate",
            translation_config=config,
            reader_document_id=document.id,
        )
        translation = translate_text_block(
            translation_job,
            source_text,
            debug_context=f"reader-paragraph:{block_id}",
        ).strip()
        if not translation:
            raise RuntimeError("Translation API returned empty content.")
        write_reader_live_translation(document, config, block_id, source_text, translation)
    except (ValueError, RuntimeError) as exc:
        return json_error(str(exc), 503 if "SDK" in str(exc) else 400)
    return jsonify({"blockId": block_id, "translation": translation, "cached": False})


@app.get("/api/tools")
def tools_index():
    return jsonify({"tools": [tool_public(tool) for tool in TOOLS]})


@app.post("/api/inspect")
def inspect_file():
    uploaded = request.files.get("file")
    if not uploaded or not uploaded.filename:
        return json_error("No file uploaded.")
    suffix = Path(uploaded.filename).suffix.lower()
    if suffix not in ALLOWED_TABLE_SUFFIXES:
        return json_error("Upload .xlsx, .xlsm, .xls, .csv, or .txt.")
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    path = upload_path(token, uploaded.filename)
    uploaded.save(path)
    try:
        sheets = excel_sheets(path) if is_excel_file(path) else []
        columns = table_columns(path, sheets[0] if sheets else None)
    except Exception as exc:
        path.unlink(missing_ok=True)
        return json_error(str(exc))
    return jsonify({"token": token, "filename": uploaded.filename, "sheets": sheets, "columns": columns})


@app.post("/api/columns")
def columns_for_sheet():
    data = request.get_json(silent=True) or {}
    try:
        return jsonify({"columns": table_columns(find_upload(str(data.get("token", ""))), str(data.get("sheet")) if data.get("sheet") else None)})
    except Exception as exc:
        return json_error(str(exc))


@app.post("/api/jobs")
def create_job():
    job_manager.cleanup()
    tool = TOOL_BY_ID.get(request.form.get("tool", ""))
    if not tool:
        return json_error("Unknown tool.")
    error = dependency_error(tool)
    if error:
        return json_error(error, 503)
    if tool.id == "pdf_ocr_translate":
        error = mathpix_config_error()
        if error:
            return json_error(error, 503)
    if tool.id == "markdown_github":
        error = github_config_error()
        if error:
            return json_error(error, 503)
    try:
        options = json.loads(request.form.get("options", "{}"))
        manifest = json.loads(request.form.get("manifest", "[]"))
    except json.JSONDecodeError:
        return json_error("Invalid job options.")
    if not isinstance(options, dict) or not isinstance(manifest, list):
        return json_error("Invalid job payload.")
    translation_config = None
    markdown_repair_config = None
    if tool.id == "document_translate":
        try:
            # Do not retain a custom API key in job.options, which may be used by
            # generic tooling or future diagnostics. The worker receives it only
            # through the in-memory translation configuration.
            translation_config = managed_translation_config_from_request({"llm": options.pop("llm", None)})
        except ValueError as exc:
            return json_error(str(exc))
    elif tool.id == "pdf_ocr_translate":
        repair_enabled = options.get("repairMarkdown", True)
        if not isinstance(repair_enabled, bool):
            return json_error("Markdown repair selection is invalid.")
        options["repairMarkdown"] = repair_enabled
        repair_llm = options.pop("repairLlm", None)
        if repair_enabled:
            if OpenAI is None:
                return json_error("The OpenAI Python SDK is required for automatic Markdown repair.", 503)
            try:
                markdown_repair_config = translation_config_from_request({"llm": repair_llm})
            except ValueError as exc:
                return json_error(str(exc))
    elif tool.id == "markdown_repair":
        deep_repair = options.get("deepRepair", False)
        math_repair = options.get("mathRepair", True)
        footnote_repair = options.get("footnoteRepair", True)
        normalize_punctuation = options.get("normalizeChinesePunctuation", False)
        if not all(isinstance(value, bool) for value in (deep_repair, math_repair, footnote_repair, normalize_punctuation)):
            return json_error("Markdown repair selection is invalid.")
        if not any((deep_repair, math_repair, footnote_repair, normalize_punctuation)):
            return json_error("Choose at least one Markdown repair mode.")
        options["deepRepair"] = deep_repair
        options["mathRepair"] = math_repair
        options["footnoteRepair"] = footnote_repair
        options["normalizeChinesePunctuation"] = normalize_punctuation
        repair_llm = options.pop("repairLlm", None)
        if deep_repair:
            if OpenAI is None:
                return json_error("The OpenAI Python SDK is required for deep Markdown repair.", 503)
            try:
                markdown_repair_config = managed_translation_config_from_request({"llm": repair_llm})
            except ValueError as exc:
                return json_error(str(exc))
    uploaded_files = request.files.getlist("files")
    if len(uploaded_files) < tool.min_files:
        return json_error(f"{tool.title} requires at least {tool.min_files} file(s).")
    if tool.max_files is not None and len(uploaded_files) > tool.max_files:
        return json_error(f"{tool.title} accepts at most {tool.max_files} file(s).")
    if tool.id == "markdown_github":
        markdown_count = sum(
            1 for uploaded in uploaded_files
            if Path(uploaded.filename or "").suffix.lower() in GITHUB_MARKDOWN_SUFFIXES
        )
        if markdown_count != 1:
            return json_error("Markdown 图片发布要求恰好上传一份 .md 或 .mmd 文档。")
    job_id = uuid.uuid4().hex
    job = Job(
        job_id,
        tool,
        JOBS_DIR / job_id,
        options,
        operation="ocr" if tool.id == "pdf_ocr_translate" else "default",
        translation_config=translation_config,
        markdown_repair_config=markdown_repair_config,
    )
    try:
        store_job_uploads(job, uploaded_files, manifest)
        if tool.id == "pdf_ocr_translate":
            prepare_local_pdf_folder(job)
    except Exception as exc:
        shutil.rmtree(job.root, ignore_errors=True)
        return json_error(str(exc))
    job.log("[INFO] Queued. Waiting for the local worker.")
    job_manager.submit(job)
    return jsonify({"id": job.id, "status": job.status}), 202


@app.get("/api/jobs/<job_id>")
def job_status(job_id: str):
    job = job_manager.get(job_id)
    if not job:
        return json_error("Job not found.", 404)
    return jsonify(job.snapshot())


@app.get("/api/jobs/<job_id>/download")
def job_download(job_id: str):
    job = job_manager.get(job_id)
    if not job or not job.download_path or not job.download_path.exists():
        return json_error("Download is not ready.", 404)
    return send_file(job.download_path, as_attachment=True, download_name=job.download_name)


@app.get("/api/jobs/<job_id>/artifacts/<artifact_id>")
def artifact_download(job_id: str, artifact_id: str):
    job = job_manager.get(job_id)
    if not job:
        return json_error("Job not found.", 404)
    selected = artifact_for_id(job, artifact_id)
    if not selected:
        return json_error("Output file not found.", 404)
    artifact, path = selected
    return send_file(path, as_attachment=True, download_name=artifact["name"])


@app.post("/api/jobs/<job_id>/translations")
def create_translation(job_id: str):
    job = job_manager.get(job_id)
    if not job or job.tool.id != "pdf_ocr_translate":
        return json_error("PDF OCR job not found.", 404)
    error = dependency_error(job.tool)
    if error:
        return json_error(error, 503)
    data = request.get_json(silent=True) or {}
    try:
        selected_llm = managed_translation_config_from_request(data)
    except ValueError as exc:
        return json_error(str(exc))
    artifact_id = str(data.get("artifactId", ""))
    selected = artifact_for_id(job, artifact_id)
    if not selected:
        return json_error("Choose an OCR output from this job.")
    artifact, source = selected
    if not artifact.get("translationSupported"):
        return json_error("AI-Markdown-Translator only supports Markdown, MMD, or HTML OCR outputs.")
    with job.lock:
        retrying_postprocess_failure = job.status == "failed" and job.phase == "translation_postprocess_failed"
        if job.status not in {"completed", "completed_with_warnings"} and not retrying_postprocess_failure:
            return json_error("Wait for the current OCR or translation task to finish.", 409)
        output = source.with_name(f"{source.stem}_zh-CN{source.suffix}")
        if output.exists():
            return json_error(f"A translation already exists: {output.name}", 409)
        job.operation = "translate"
        job.phase = "translation_queued"
        job.pending_artifact_id = artifact_id
        job.translation_config = selected_llm
        job.translation_progress = None
        job.status = "queued"
        job.finished_at = None
    job.log(f"[INFO] Queued Simplified Chinese translation for {source.name} using {selected_llm['name']}.")
    job_manager.submit(job)
    return jsonify({"id": job.id, "status": job.status}), 202


@app.post("/api/jobs/<job_id>/translation-retry")
def retry_document_translation(job_id: str):
    job = job_manager.get(job_id)
    if not job or job.tool.id != "document_translate":
        return json_error("Document translation job not found.", 404)
    data = request.get_json(silent=True) or {}
    try:
        selected_llm = managed_translation_config_from_request(data)
    except ValueError as exc:
        return json_error(str(exc))
    with job.lock:
        if job.status != "failed":
            return json_error("Only a failed document translation can be resumed.", 409)
        job.translation_config = selected_llm
        job.status = "queued"
        job.phase = "translation_queued"
        job.finished_at = None
    progress = job.translation_progress or {"completed": 0, "total": 0}
    job.log(
        "[INFO] Queued AI-Markdown-Translator resume using "
        f"{selected_llm['name']} from {progress['completed']}/{progress['total']} persisted chunk(s)."
    )
    job_manager.submit(job)
    return jsonify({"id": job.id, "status": job.status}), 202


@app.post("/api/jobs/<job_id>/reader-imports")
def create_reader_import(job_id: str):
    job = job_manager.get(job_id)
    if not job:
        return json_error("Job not found.", 404)
    data = request.get_json(silent=True) or {}
    pairs = job_reader_pair_records(job)
    pair_id = str(data.get("pairId") or "")
    if not pair_id and len(pairs) == 1:
        pair_id = pairs[0]["id"]
    pair = next((item for item in pairs if item["id"] == pair_id), None)
    if not pair:
        return json_error("Choose an available completed source/translation pair.", 409)
    try:
        document = import_job_pair_into_reader(job, pair)
    except Exception as exc:
        return json_error(f"Unable to import the pair into the reader: {exc}", 409)
    payload = document.snapshot()
    payload["readerUrl"] = f"/reader?document={quote(document.id, safe='')}"
    return jsonify(payload), 201


def open_browser_when_ready(url: str) -> None:
    if os.environ.get("WEB_PANEL_OPEN_BROWSER", "1") != "0":
        threading.Timer(0.35, webbrowser.open, args=(url,)).start()


def configure_console_output() -> None:
    """Keep localized Windows errors from crashing an otherwise valid fallback."""
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(errors="backslashreplace")
        except (OSError, ValueError):
            # Redirected, detached, or embedded streams may not be reconfigurable.
            pass


def main() -> None:
    configure_console_output()
    host = "127.0.0.1"
    requested_port = int(os.environ.get("WEB_PANEL_PORT", "8765"))
    port = requested_port
    if requested_port:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as test_socket:
                test_socket.bind((host, requested_port))
        except OSError as exc:
            print(f"[toolbox] Port {requested_port} is unavailable ({exc}). Using another port.", flush=True)
            port = 0
    server = make_server(host, port, app, threaded=True)
    url = f"http://{host}:{server.server_port}/"
    print(f"[toolbox] Ready: {url}", flush=True)
    open_browser_when_ready(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("[toolbox] Stopped.", flush=True)


if __name__ == "__main__":
    main()
