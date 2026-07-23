"""Local multi-tool web panel for the scripts in this repository."""

from __future__ import annotations

import importlib.util
import hashlib
from html import unescape as html_unescape
import io
import json
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

import pandas as pd
import requests
from bs4 import BeautifulSoup, Comment
from dotenv import load_dotenv, set_key
from flask import Flask, jsonify, render_template, request, send_file
from werkzeug.serving import make_server
from werkzeug.utils import secure_filename

try:
    from openai import OpenAI
except ImportError:  # Dependency checks keep the rest of the panel usable.
    OpenAI = None  # type: ignore[assignment,misc]

from sheet_to_anki import SheetToAnkiError, clean_cell, read_table, require_columns


PROJECT_ROOT = Path(__file__).resolve().parent
ENV_FILE = PROJECT_ROOT / ".env"
load_dotenv(ENV_FILE, override=False)
RUNTIME_DIR = PROJECT_ROOT / ".runtime"
UPLOAD_DIR = RUNTIME_DIR / "uploads"
JOBS_DIR = RUNTIME_DIR / "jobs"
SCRIPTS_DIR = PROJECT_ROOT / "Potential_Scripts"
ALLOWED_TABLE_SUFFIXES = {".xlsx", ".xlsm", ".xls", ".csv", ".txt"}
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"}
VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}
PDF_SUFFIXES = {".pdf"}
READER_SOURCE_SUFFIXES = {".pdf", ".md", ".mmd"}
MATHPIX_BASE_URL = "https://api.mathpix.com/v3"
OCR_OPTIONAL_FORMATS = ("docx", "md", "html", "tex.zip")
MMD_BUNDLE_FORMAT = "mmd.zip"
TRANSLATABLE_SUFFIXES = {".mmd", ".md", ".html"}
MAX_OCR_WAIT_SECONDS = 20 * 60
POLL_INTERVAL_SECONDS = 5

app = Flask(__name__)


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
    ToolSpec("anki", "表格转 Anki", "Anki", "选择正反面列，导出可直接导入 Anki 的 TSV。", ALLOWED_TABLE_SUFFIXES, max_files=1),
    ToolSpec("image_crop", "图片居中裁剪缩放", "图片", "批量裁剪中心区域并缩放为方图。", IMAGE_SUFFIXES, ("PIL",)),
    ToolSpec("video_crop", "视频居中裁剪缩放", "视频", "批量裁剪、缩放视频并转为 MP4。", VIDEO_SUFFIXES, needs_ffmpeg=True),
    ToolSpec("frames", "MP4 全帧导出", "视频", "将每个 MP4 的全部帧导出为 PNG。", {".mp4"}, ("cv2",)),
    ToolSpec("fix_frame", "修复第 16 帧", "视频", "将第 16 帧替换为第 15 帧并输出 16 帧视频。", {".mp4"}, ("cv2",), True, 1, 1),
    ToolSpec("image_ppt", "图片网格 PPT", "PPT", "将图片按网格排版为 PowerPoint。", IMAGE_SUFFIXES, ("PIL", "pptx")),
    ToolSpec("video_ppt", "视频帧网格 PPT", "PPT", "从每个视频抽取指定帧后排版为 PowerPoint。", VIDEO_SUFFIXES, ("PIL", "pptx", "cv2")),
    ToolSpec("stack_images", "图片线性拼接 PPT", "PPT", "将图片横向或纵向拼接为 PowerPoint。", IMAGE_SUFFIXES, ("PIL", "pptx")),
    ToolSpec("stack_videos", "视频网格拼接", "视频", "按行列、标题和说明拼接视频为 MP4。", VIDEO_SUFFIXES, ("PIL", "pptx"), True),
    ToolSpec("bibtex", "论文标题转 BibTeX", "研究", "上传标题 TXT 或输入标题，联网查询 BibTeX。", {".txt"}, ("scholarly",), max_files=1),
    ToolSpec("document_translate", "文档翻译", "研究", "将 MMD、Markdown 或 HTML 翻译为简体中文；支持文件和文件夹，保留文件夹结构。", TRANSLATABLE_SUFFIXES, ("openai", "bs4")),
    ToolSpec("pdf_ocr_translate", "PDF OCR", "研究", "用 Mathpix 导出多种 OCR 格式；可在“文档翻译”中继续翻译 MMD、Markdown 或 HTML。", PDF_SUFFIXES, ("requests", "dotenv"), max_files=1),
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
    translation_config: dict[str, str] | None = None
    reader_document_id: str | None = None
    translation_progress: dict[str, int] | None = None
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
                "artifacts": [
                    {
                        **artifact,
                        "downloadUrl": f"/api/jobs/{self.id}/artifacts/{artifact['id']}",
                    }
                    for artifact in self.artifacts
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
    status: str = "queued"
    message: str = "Waiting for local worker."
    blocks: list[dict[str, Any]] = field(default_factory=list)
    translated_blocks: list[dict[str, Any]] | None = None
    job_id: str | None = None
    error: str | None = None
    created_at: float = field(default_factory=time.time)

    def snapshot(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "sourceType": self.source_type,
            "mode": self.mode,
            "status": self.status,
            "message": self.message,
            "hasTranslation": self.translated_blocks is not None,
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
READER_TOOL = ToolSpec(
    "paper_reader",
    "科研论文阅读器",
    "研究",
    "对 PDF 执行 Mathpix OCR，或直接阅读 Markdown。",
    READER_SOURCE_SUFFIXES,
    ("requests", "openai"),
    max_files=1,
)


def json_error(message: str, status: int = 400):
    return jsonify({"error": message}), status


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


def llm_config_error() -> str | None:
    missing = missing_environment("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL")
    if missing:
        return f"LLM translation is not configured. Add {', '.join(missing)} to the project .env file and restart the panel."
    return None


def llm_presets() -> list[dict[str, str]]:
    """Return safe public metadata plus in-memory credentials for .env presets."""
    presets: list[dict[str, str]] = []
    legacy_error = llm_config_error()
    if not legacy_error:
        presets.append(
            {
                "id": "default",
                "name": os.environ.get("LLM_NAME", "默认 LLM").strip() or "默认 LLM",
                "baseUrl": os.environ["LLM_BASE_URL"],
                "model": os.environ["LLM_MODEL"],
                "apiKey": os.environ["LLM_API_KEY"],
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
            }
        )
    return presets


def public_llm_presets() -> list[dict[str, str]]:
    return [{key: preset[key] for key in ("id", "name", "baseUrl", "model")} for preset in llm_presets()]


def validate_llm_fields(name: Any, base_url: Any, api_key: Any, model: Any) -> dict[str, str]:
    values = {
        "name": str(name or "").strip(),
        "baseUrl": str(base_url or "").strip(),
        "apiKey": str(api_key or "").strip(),
        "model": str(model or "").strip(),
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


def save_llm_preset(preset_name: Any, base_url: Any, api_key: Any, model: Any) -> dict[str, str]:
    """Persist one named OpenAI-compatible provider in the project-local .env."""
    config = validate_llm_fields(preset_name, base_url, api_key, model)
    preset_key = persistent_preset_id(config["name"])
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
    # Existing requests should see the provider immediately without a restart.
    os.environ["LLM_PRESETS"] = ",".join(updated_ids)
    os.environ[f"{prefix}NAME"] = config["name"]
    os.environ[f"{prefix}BASE_URL"] = config["baseUrl"]
    os.environ[f"{prefix}API_KEY"] = config["apiKey"]
    os.environ[f"{prefix}MODEL"] = config["model"]
    for preset in public_llm_presets():
        if preset["id"] == preset_key.lower():
            return preset
    raise RuntimeError("The saved LLM preset could not be reloaded.")


def translation_config_from_request(data: dict[str, Any]) -> dict[str, str]:
    config = data.get("llm")
    if not isinstance(config, dict):
        raise ValueError("Choose an LLM preset or enter a custom OpenAI-compatible configuration.")
    mode = str(config.get("mode", "")).strip()
    if mode == "preset":
        requested_id = str(config.get("presetId", "")).strip()
        for preset in llm_presets():
            if preset["id"] == requested_id:
                return validate_llm_fields(preset["name"], preset["baseUrl"], preset["apiKey"], preset["model"])
        raise ValueError("The selected LLM preset is unavailable. Check .env and restart the panel.")
    if mode == "custom":
        return validate_llm_fields(config.get("name"), config.get("baseUrl"), config.get("apiKey"), config.get("model"))
    raise ValueError("Invalid LLM configuration mode.")


def dependency_error(tool: ToolSpec) -> str | None:
    missing = [name for name in tool.dependencies if not has_module(name)]
    if missing:
        return (
            f"Missing Python dependency: {', '.join(missing)}. "
            "Run run_web_panel.ps1 on Windows or run_web_panel.command on macOS."
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
        "llmPresets": public_llm_presets() if tool.id in {"document_translate", "pdf_ocr_translate"} else [],
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
    translation_supported = kind == "ocr" and path.suffix.lower() in TRANSLATABLE_SUFFIXES
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
            path = (job.root / "output" / artifact["name"]).resolve()
            output_dir = (job.root / "output").resolve()
            if path.parent == output_dir and path.is_file():
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


def write_ocr_text_artifact(job: Job, output: Path, extension: str, text: str, assets: LocalAssetStore) -> None:
    output.write_text(rewrite_text_asset_references(job, text, assets), encoding="utf-8")
    save_local_artifact(job, output)
    labels = {"mmd": "Mathpix Markdown", "md": "Markdown"}
    labels.update({"html": "HTML", "lines.json": "Lines JSON"})
    add_artifact(job, output, "ocr", labels.get(extension, extension))
    job.log(f"[OCR] Downloaded {output.name}.")


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


def run_pdf_ocr(job: Job, files: list[Path]) -> None:
    if len(files) != 1:
        raise ValueError("PDF OCR requires exactly one PDF.")
    selected_formats = checked_ocr_formats(job.options)
    requested_formats = [*selected_formats, MMD_BUNDLE_FORMAT]
    source = files[0]
    output_dir = job.root / "output"
    output_dir.mkdir(exist_ok=True)
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


PLACEHOLDER_PATTERN = re.compile(r"\[\[\[KEEP_\d{4}\]\]\]")
PROTECTED_MARKDOWN_PATTERNS = (
    re.compile(r"```.*?```", re.DOTALL),
    re.compile(r"\\\[.*?\\\]", re.DOTALL),
    re.compile(r"\\\(.*?\\\)", re.DOTALL),
    re.compile(r"(?<!\\)`[^`\n]+`"),
    re.compile(r"\\(?:label|ref|eqref|cite\w*|begin|end|includegraphics)\{[^{}]*\}"),
    re.compile(r"(?<=\]\()[^)]*(?=\))"),
    re.compile(r"https?://[^\s)>]+"),
    re.compile(r"(?<!\\)\$[^$\n]+\$"),
)


def protect_literals(text: str) -> tuple[str, dict[str, str]]:
    protected: dict[str, str] = {}

    def replace(match: re.Match[str]) -> str:
        key = f"[[[KEEP_{len(protected):04d}]]]"
        protected[key] = match.group(0)
        return key

    result = text
    for pattern in PROTECTED_MARKDOWN_PATTERNS:
        result = pattern.sub(replace, result)
    return result, protected


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
    client = OpenAI(api_key=config["apiKey"], base_url=config["baseUrl"])
    response = client.chat.completions.create(
        model=config["model"],
        temperature=0.1,
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a professional academic translator. Translate into Simplified Chinese only. "
                    "Preserve Markdown or HTML structure, every [[[KEEP_0000]]] placeholder, numbers, and punctuation. "
                    "Return only the translated content with no explanation."
                ),
            },
            {"role": "user", "content": text},
        ],
    )
    result = response.choices[0].message.content if response.choices else None
    if not result or not result.strip():
        raise RuntimeError("Translation API returned empty content.")
    return result


def translation_unit_count(text: str) -> int:
    protected_text, _protected = protect_literals(text)
    return len(text_chunks(protected_text))


def translate_text_block(
    job: Job | None,
    text: str,
    on_chunk: Callable[[str], None] | None = None,
) -> str:
    protected_text, protected = protect_literals(text)

    def translate_without_markers(chunk: str) -> str:
        # The fallback keeps protected literals local. This is used only for a
        # chunk whose first translation changed a marker.
        pieces = re.split(f"({PLACEHOLDER_PATTERN.pattern})", chunk)
        return "".join(
            piece
            if PLACEHOLDER_PATTERN.fullmatch(piece)
            else "".join(
                llm_translate(job, prose) if re.search(r"[A-Za-z]", prose) else prose
                for prose in text_chunks(piece)
            )
            for piece in pieces
        )

    translated_chunks: list[str] = []
    for chunk in text_chunks(protected_text):
        translated_chunk = llm_translate(job, chunk) if re.search(r"[A-Za-z]", chunk) else chunk
        expected = sorted(PLACEHOLDER_PATTERN.findall(chunk))
        actual = sorted(PLACEHOLDER_PATTERN.findall(translated_chunk))
        if actual != expected:
            if job is not None:
                job.log("[TRANSLATION] Retrying a section without protected literals.")
            translated_chunk = translate_without_markers(chunk)
        chunk_literals = {key: protected[key] for key in expected}
        translated_chunk = restore_literals(translated_chunk, chunk_literals)
        translated_chunks.append(translated_chunk)
        if on_chunk:
            on_chunk(translated_chunk)
    return "".join(translated_chunks)


def set_translation_progress(job: Job | None, total: int, completed: int = 0) -> None:
    if job is None:
        return
    with job.lock:
        job.translation_progress = {"completed": completed, "total": total}


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


def translate_markdown(job: Job | None, source: Path, output: Path) -> None:
    text = source.read_text(encoding="utf-8")
    total = translation_unit_count(text)
    if not total:
        raise RuntimeError("The Markdown file contains no text to translate.")
    set_translation_progress(job, total)
    completed = 0
    with output.open("w", encoding="utf-8", newline="") as handle:
        def save_chunk(translated_chunk: str) -> None:
            nonlocal completed
            handle.write(translated_chunk)
            completed += 1
            write_checkpoint(handle, job, output, completed, total)

        translate_text_block(job, text, save_chunk)


def translate_html(job: Job | None, source: Path, output: Path) -> None:
    soup = BeautifulSoup(source.read_text(encoding="utf-8"), "html.parser")
    skipped_tags = {"script", "style", "code", "pre", "math", "svg", "kbd", "samp"}
    nodes = [
        node for node in soup.find_all(string=True)
        if not isinstance(node, Comment) and node.parent and node.parent.name not in skipped_tags
        and str(node).strip() and re.search(r"[A-Za-z]", str(node))
    ]
    total = sum(translation_unit_count(str(node).strip()) for node in nodes)
    if not total:
        raise RuntimeError("The HTML file contains no visible text to translate.")
    set_translation_progress(job, total)
    completed = 0
    for node in nodes:
        if isinstance(node, Comment) or not node.parent or node.parent.name in skipped_tags:
            continue
        value = str(node)
        if not value.strip() or not re.search(r"[A-Za-z]", value):
            continue
        leading = value[: len(value) - len(value.lstrip())]
        trailing = value[len(value.rstrip()):]
        partial_chunks: list[str] = []
        current_node = node

        def save_chunk(translated_chunk: str) -> None:
            nonlocal completed, current_node
            partial_chunks.append(translated_chunk)
            replacement = soup.new_string(f"{leading}{''.join(partial_chunks)}{trailing}")
            current_node.replace_with(replacement)
            current_node = replacement
            completed += 1
            output.write_text(str(soup), encoding="utf-8")
            with output.open("a", encoding="utf-8") as handle:
                write_checkpoint(handle, job, output, completed, total)

        translate_text_block(job, value.strip(), save_chunk)


def partial_translation_path(source: Path) -> Path:
    return source.with_name(f"{source.stem}_zh-CN.partial{source.suffix}")


def translation_source_sort_key(source: Path) -> tuple[int, str]:
    """Prefer Markdown wherever a mixed file/folder upload is translated."""
    priority = {".md": 0, ".mmd": 1, ".html": 2}
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


def run_document_translation(job: Job) -> None:
    input_dir = job.root / "input"
    sources = sorted(
        (path for path in input_dir.rglob("*") if path.is_file() and path.suffix.lower() in TRANSLATABLE_SUFFIXES),
        key=lambda path: translation_source_sort_key(path.relative_to(input_dir)),
    )
    if not sources:
        raise RuntimeError("No MMD, Markdown, or HTML files were uploaded.")
    output_dir = job.root / "output"
    output_dir.mkdir(exist_ok=True)
    outputs: list[Path] = []
    job.phase = "translation"
    job.log(f"[TRANSLATION] Translating {len(sources)} file(s) into Simplified Chinese.")
    for index, source in enumerate(sources, start=1):
        relative = source.relative_to(input_dir)
        output = output_dir / relative.parent / f"{source.stem}_zh-CN{source.suffix}"
        partial_output = partial_translation_path(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        if output.exists() or partial_output.exists():
            raise FileExistsError(f"A translation already exists: {output.relative_to(output_dir)}")
        job.log(f"[TRANSLATION] ({index}/{len(sources)}) {relative.as_posix()}")
        if source.suffix.lower() == ".html":
            translate_html(job, source, partial_output)
        else:
            translate_markdown(job, source, partial_output)
        os.replace(partial_output, output)
        outputs.append(output)
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
        raise ValueError("Only MMD, Markdown, and HTML OCR outputs can be translated.")
    output = source.with_name(f"{source.stem}_zh-CN{source.suffix}")
    partial_output = partial_translation_path(source)
    if output.exists():
        raise FileExistsError(f"A translation already exists: {output.name}")
    if partial_output.exists():
        raise FileExistsError(f"A partial translation already exists: {partial_output.name}")
    job.phase = "translation"
    job.log(f"[TRANSLATION] Translating {source.name} into Simplified Chinese.")
    try:
        if source.suffix.lower() == ".html":
            translate_html(job, source, partial_output)
        else:
            translate_markdown(job, source, partial_output)
        os.replace(partial_output, output)
        if job.local_save_dir and (job.local_save_dir / partial_output.name).exists():
            os.replace(job.local_save_dir / partial_output.name, job.local_save_dir / output.name)
            job.log(f"[LOCAL] Saved {output.name} beside the original PDF.")
        else:
            save_local_artifact(job, output)
        add_artifact(job, output, "translation", "Simplified Chinese translation")
        job.download_path, job.download_name = package_pdf_artifacts(job)
        job.phase = "translation_complete"
        job.pending_artifact_id = None
    except Exception:
        if partial_output.exists() and partial_output.stat().st_size:
            add_artifact(job, partial_output, "translation_partial", "Partial Simplified Chinese translation")
            save_local_artifact_with_log(job, partial_output, log=False)
            job.phase = "translation_partial"
            progress = job.translation_progress or {"completed": 0, "total": 0}
            job.log(
                "[TRANSLATION] Failed after saving "
                f"{progress['completed']}/{progress['total']} section(s) to {partial_output.name}."
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
        document.message = "正在由 Mathpix 识别 PDF 与公式。"
        run_pdf_ocr(job, [source])
        markdown_source = reader_source_artifact(job)
    else:
        markdown_source = source
    original_text = markdown_source.read_text(encoding="utf-8")
    document.blocks = markdown_to_reader_blocks(original_text)
    if document.mode == "ocr_translate":
        if not job.translation_config:
            raise RuntimeError("OCR + translation requires an LLM configuration.")
        document.message = "正在按论文段落生成对照译文。"
        translated = job.root / "output" / f"{markdown_source.stem}_zh-CN{markdown_source.suffix}"
        partial = partial_translation_path(translated)
        translated.parent.mkdir(exist_ok=True)
        translate_markdown(job, markdown_source, partial)
        os.replace(partial, translated)
        document.translated_blocks = markdown_to_reader_blocks(translated.read_text(encoding="utf-8"))
    document.status = "ready"
    document.message = "论文已准备好，可划词提问。"
    job.phase = "reader_ready"
    job.download_path = markdown_source
    job.download_name = markdown_source.name


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
            run_reader_document(job)
        except Exception as exc:
            document = reader_manager.get(job.reader_document_id or "")
            if document:
                document.status = "failed"
                document.error = str(exc)
                document.message = "文档处理失败。"
            with job.lock:
                job.finished_at = time.time()
            raise
        finally:
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
            job.translation_config = None
        with job.lock:
            job.status = "completed"
            job.finished_at = time.time()
        job.log("[INFO] Completed. Download is ready.")
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
    for index, (upload, item) in enumerate(zip(uploaded_files, manifest), start=1):
        if not isinstance(item, dict):
            raise ValueError("Invalid upload manifest.")
        relative = safe_relative_path(str(item.get("relativePath") or upload.filename or "file"))
        suffix = Path(upload.filename or relative.name).suffix.lower()
        if suffix not in job.tool.accepts:
            raise ValueError(f"{relative.name}: unsupported file type for {job.tool.title}.")
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


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/reader")
def reader():
    return render_template("reader.html")


READER_ACTIONS = {
    "explain": "解释选区：先给一句结论，再逐句解释术语、变量和逻辑；优先给机器学习论文中的具体含义。",
    "formula": "解释公式：列出所有符号及张量形状/取值域，说明每一项的作用、输入输出、假设和训练/推理时的意义。",
    "geometry": "解释公式的几何意义：明确向量/参数/概率分布所处的空间，说明方向、距离、角度、投影、流形或优化几何；没有严格几何解释时要直说并给直觉。",
    "derivation": "推导这一步：从可见的前提开始，逐行给出代数、概率或微积分变形；指出使用的恒等式、近似或条件，不能凭空补全未给出的前提。",
    "summary": "总结选区和相邻上下文：说明问题、方法、关键机制、结论以及它在整篇论文中的作用。",
    "intuition": "给出直觉解释：使用一个小型数值、二维几何或训练过程例子，但不要改变原公式或暗示未证实结论。",
    "assumptions": "批判性阅读：识别显式/隐式假设、潜在失效情形、计算代价、数据分布要求与需要实验验证的主张。",
    "implementation": "转换为实现视角：说明张量形状、伪代码步骤、数值稳定性、常用默认值和在 PyTorch/JAX 中容易出错的位置。",
}


def reader_llm_config(data: dict[str, Any]) -> dict[str, str]:
    return translation_config_from_request(data)


def reader_context(document: ReaderDocument, block_id: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    blocks = document.blocks
    index = next((i for i, block in enumerate(blocks) if block["id"] == block_id), None)
    if index is None:
        return None, []
    start, end = max(0, index - 2), min(len(blocks), index + 3)
    return blocks[index], blocks[start:end]


def ask_reader_llm(config: dict[str, str], action: str, selection: str, block: dict[str, Any], context: list[dict[str, Any]], custom_question: str) -> str:
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
    client = OpenAI(api_key=config["apiKey"], base_url=config["baseUrl"])
    response = client.chat.completions.create(
        model=config["model"],
        temperature=0.2,
        messages=[
            {
                "role": "system",
                "content": (
                    "你是严谨的计算机科学研究导师，专长为深度学习、机器学习、扩散模型、概率建模、"
                    "优化与相关数学理论。用简体中文作答。只依据给定文本和明确标注的通用数学知识；"
                    "区分论文原文、你的推导和合理推断。保留全部 LaTeX 符号与变量命名，不编造实验、"
                    "引用或作者意图。答案先给直接结论，随后按需使用小标题；若上下文不足，明确说明缺少什么。"
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
    return jsonify({"llmPresets": public_llm_presets(), "actions": [{"id": key, "label": label.split("：", 1)[0]} for key, label in READER_ACTIONS.items()]})


@app.post("/api/llm-presets")
@app.post("/api/reader/llm-presets")  # Backward-compatible reader alias.
def create_reader_llm_preset():
    data = request.get_json(silent=True) or {}
    try:
        preset = save_llm_preset(data.get("name"), data.get("baseUrl"), data.get("apiKey"), data.get("model"))
    except (ValueError, OSError) as exc:
        return json_error(str(exc))
    return jsonify({"preset": preset}), 201


@app.post("/api/reader/documents")
def create_reader_document():
    uploaded = request.files.get("file")
    if not uploaded or not uploaded.filename:
        return json_error("Choose a PDF, Markdown, or MMD file.")
    suffix = Path(uploaded.filename).suffix.lower()
    if suffix not in READER_SOURCE_SUFFIXES:
        return json_error("Reader accepts .pdf, .md, and .mmd files.")
    mode = str(request.form.get("mode", "ocr")).strip()
    if mode not in {"ocr", "ocr_translate"}:
        return json_error("Choose OCR only or OCR + translation.")
    if suffix != ".pdf" and mode == "ocr":
        # Markdown is already OCR-equivalent; keep a single mode name on the client.
        mode = "markdown"
    if suffix == ".pdf":
        error = mathpix_config_error()
        if error:
            return json_error(error, 503)
    translation_config = None
    if mode == "ocr_translate":
        try:
            llm_payload = json.loads(request.form.get("llm", "{}"))
            translation_config = reader_llm_config({"llm": llm_payload})
        except (ValueError, json.JSONDecodeError) as exc:
            return json_error(str(exc))
    document_id = uuid.uuid4().hex
    root = JOBS_DIR / f"reader-{document_id}"
    document = ReaderDocument(document_id, root, Path(uploaded.filename).stem, "pdf" if suffix == ".pdf" else "markdown", mode)
    job = Job(document_id, READER_TOOL, root, {"ocrFormats": ["md"]}, operation="reader", translation_config=translation_config, reader_document_id=document_id)
    try:
        input_dir = root / "input"
        input_dir.mkdir(parents=True, exist_ok=True)
        destination = input_dir / safe_work_name(uploaded.filename, suffix)
        uploaded.save(destination)
        job.source_stem = safe_output_stem(destination.name)
    except Exception as exc:
        shutil.rmtree(root, ignore_errors=True)
        return json_error(str(exc))
    document.job_id = job.id
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


@app.get("/api/reader/documents/<document_id>/content")
def reader_document_content(document_id: str):
    document = reader_manager.get(document_id)
    if not document:
        return json_error("Reader document not found.", 404)
    if document.status != "ready":
        return json_error("The reader document is not ready.", 409)
    return jsonify({"title": document.title, "blocks": document.blocks, "translatedBlocks": document.translated_blocks, "assetBase": f"/api/reader/documents/{document.id}/assets/"})


@app.get("/api/reader/documents/<document_id>/assets/<path:asset_path>")
def reader_asset(document_id: str, asset_path: str):
    document = reader_manager.get(document_id)
    if not document:
        return json_error("Reader document not found.", 404)
    candidate = (document.root / "output" / safe_relative_path(asset_path)).resolve()
    output_root = (document.root / "output").resolve()
    if output_root not in candidate.parents or not candidate.is_file():
        return json_error("Reader asset not found.", 404)
    return send_file(candidate)


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
    block, context = reader_context(document, block_id)
    if not block:
        return json_error("The selected paragraph is unavailable.")
    try:
        config = reader_llm_config(data)
        answer = ask_reader_llm(config, action, selection, block, context, str(data.get("question", "")))
    except (ValueError, RuntimeError) as exc:
        return json_error(str(exc), 503 if "SDK" in str(exc) else 400)
    return jsonify({"answer": answer, "action": action, "sourceBlockId": block_id, "contextBlockIds": [item["id"] for item in context]})


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
    try:
        options = json.loads(request.form.get("options", "{}"))
        manifest = json.loads(request.form.get("manifest", "[]"))
    except json.JSONDecodeError:
        return json_error("Invalid job options.")
    if not isinstance(options, dict) or not isinstance(manifest, list):
        return json_error("Invalid job payload.")
    translation_config = None
    if tool.id == "document_translate":
        try:
            # Do not retain a custom API key in job.options, which may be used by
            # generic tooling or future diagnostics. The worker receives it only
            # through the in-memory translation configuration.
            translation_config = translation_config_from_request({"llm": options.pop("llm", None)})
        except ValueError as exc:
            return json_error(str(exc))
    uploaded_files = request.files.getlist("files")
    if len(uploaded_files) < tool.min_files:
        return json_error(f"{tool.title} requires at least {tool.min_files} file(s).")
    if tool.max_files is not None and len(uploaded_files) > tool.max_files:
        return json_error(f"{tool.title} accepts at most {tool.max_files} file(s).")
    job_id = uuid.uuid4().hex
    job = Job(
        job_id,
        tool,
        JOBS_DIR / job_id,
        options,
        operation="ocr" if tool.id == "pdf_ocr_translate" else "default",
        translation_config=translation_config,
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
        selected_llm = translation_config_from_request(data)
    except ValueError as exc:
        return json_error(str(exc))
    artifact_id = str(data.get("artifactId", ""))
    selected = artifact_for_id(job, artifact_id)
    if not selected:
        return json_error("Choose an OCR output from this job.")
    artifact, source = selected
    if not artifact.get("translationSupported"):
        return json_error("Only MMD, Markdown, and HTML OCR outputs can be translated.")
    with job.lock:
        if job.status not in {"completed", "completed_with_warnings"}:
            return json_error("Wait for the current OCR or translation task to finish.", 409)
        output = source.with_name(f"{source.stem}_zh-CN{source.suffix}")
        if output.exists():
            return json_error(f"A translation already exists: {output.name}", 409)
        partial_output = partial_translation_path(source)
        if partial_output.exists():
            return json_error(
                f"A partial translation already exists: {partial_output.name}. Download it before starting a new translation.",
                409,
            )
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


def open_browser_when_ready(url: str) -> None:
    if os.environ.get("WEB_PANEL_OPEN_BROWSER", "1") != "0":
        threading.Timer(0.35, webbrowser.open, args=(url,)).start()


def main() -> None:
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
