"""Local multi-tool web panel for the scripts in this repository."""

from __future__ import annotations

import importlib.util
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
from pathlib import Path
from typing import Any

import pandas as pd
from flask import Flask, jsonify, render_template, request, send_file
from werkzeug.serving import make_server
from werkzeug.utils import secure_filename

from sheet_to_anki import SheetToAnkiError, clean_cell, read_table, require_columns


PROJECT_ROOT = Path(__file__).resolve().parent
RUNTIME_DIR = PROJECT_ROOT / ".runtime"
UPLOAD_DIR = RUNTIME_DIR / "uploads"
JOBS_DIR = RUNTIME_DIR / "jobs"
SCRIPTS_DIR = PROJECT_ROOT / "Potential_Scripts"
ALLOWED_TABLE_SUFFIXES = {".xlsx", ".xlsm", ".xls", ".csv", ".txt"}
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"}
VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}

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


def dependency_error(tool: ToolSpec) -> str | None:
    missing = [name for name in tool.dependencies if not has_module(name)]
    if missing:
        return f"Missing Python dependency: {', '.join(missing)}. Run .\\run_web_panel.ps1 to install project dependencies."
    if tool.needs_ffmpeg:
        missing_bins = [binary for binary in ("ffmpeg", "ffprobe") if shutil.which(binary) is None]
        if missing_bins:
            return f"Missing system dependency: {', '.join(missing_bins)}. Install FFmpeg and add it to PATH."
    return None


def tool_public(tool: ToolSpec) -> dict[str, Any]:
    error = dependency_error(tool)
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
    job.log(f"[INFO] Starting {job.tool.title}.")
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
        work_name = safe_work_name(str(item.get("workName") or relative.stem), suffix)
        target = input_dir / relative.parent / f"{index:04d}_{work_name}"
        target.parent.mkdir(parents=True, exist_ok=True)
        upload.save(target)
        if needs_ordered_copies:
            shutil.copyfile(target, ordered_dir / target.name)


@app.get("/")
def index():
    return render_template("index.html")


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
    try:
        options = json.loads(request.form.get("options", "{}"))
        manifest = json.loads(request.form.get("manifest", "[]"))
    except json.JSONDecodeError:
        return json_error("Invalid job options.")
    if not isinstance(options, dict) or not isinstance(manifest, list):
        return json_error("Invalid job payload.")
    uploaded_files = request.files.getlist("files")
    if len(uploaded_files) < tool.min_files:
        return json_error(f"{tool.title} requires at least {tool.min_files} file(s).")
    if tool.max_files is not None and len(uploaded_files) > tool.max_files:
        return json_error(f"{tool.title} accepts at most {tool.max_files} file(s).")
    job_id = uuid.uuid4().hex
    job = Job(job_id, tool, JOBS_DIR / job_id, options)
    try:
        store_job_uploads(job, uploaded_files, manifest)
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
