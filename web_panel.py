"""Local drag-and-drop web panel for creating Anki import files."""

from __future__ import annotations

import io
import os
import socket
import threading
import uuid
import webbrowser
from pathlib import Path

import pandas as pd
from flask import Flask, jsonify, render_template, request, send_file
from werkzeug.serving import make_server
from werkzeug.utils import secure_filename

from sheet_to_anki import SheetToAnkiError, read_table, require_columns, clean_cell


PROJECT_ROOT = Path(__file__).resolve().parent
UPLOAD_DIR = PROJECT_ROOT / ".runtime" / "uploads"
ALLOWED_SUFFIXES = {".xlsx", ".xlsm", ".xls", ".csv", ".txt"}

app = Flask(__name__)


def json_error(message: str, status: int = 400):
    return jsonify({"error": message}), status


def upload_path(token: str, filename: str) -> Path:
    safe_name = secure_filename(filename) or "upload"
    return UPLOAD_DIR / f"{token}_{safe_name}"


def find_upload(token: str) -> Path:
    matches = list(UPLOAD_DIR.glob(f"{token}_*"))
    if not matches:
        raise SheetToAnkiError("Uploaded file was not found. Upload it again.")
    return matches[0]


def table_columns(path: Path, sheet: str | None = None) -> list[str]:
    df = read_table(path, sheet)
    return [str(column) for column in df.columns]


def is_excel_file(path: Path) -> bool:
    return path.suffix.lower() in {".xlsx", ".xlsm", ".xls"}


def excel_sheets(path: Path) -> list[str]:
    suffix = path.suffix.lower()
    engine = "xlrd" if suffix == ".xls" else "openpyxl"
    try:
        workbook = pd.ExcelFile(path, engine=engine)
    except ImportError as exc:
        raise SheetToAnkiError(
            f"Reading {suffix} files requires {engine}. Install dependencies with: "
            ".\\run_web_panel.ps1."
        ) from exc
    return workbook.sheet_names


@app.get("/")
def index():
    return render_template("index.html")


@app.post("/api/inspect")
def inspect_file():
    uploaded = request.files.get("file")
    if not uploaded or not uploaded.filename:
        return json_error("No file uploaded.")

    suffix = Path(uploaded.filename).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        return json_error("Unsupported file type. Upload .xlsx, .xlsm, .xls, .csv, or .txt.")

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    path = upload_path(token, uploaded.filename)
    uploaded.save(path)

    try:
        sheets = excel_sheets(path) if is_excel_file(path) else []
        columns = table_columns(path, sheets[0] if sheets else None)
    except SheetToAnkiError as exc:
        path.unlink(missing_ok=True)
        return json_error(str(exc))
    except Exception as exc:  # pragma: no cover - defensive UI boundary
        path.unlink(missing_ok=True)
        return json_error(f"Could not read file: {exc}")

    return jsonify(
        {
            "token": token,
            "filename": uploaded.filename,
            "fileType": suffix[1:],
            "sheets": sheets,
            "columns": columns,
        }
    )


@app.post("/api/columns")
def columns_for_sheet():
    data = request.get_json(silent=True) or {}
    token = str(data.get("token", ""))
    sheet = data.get("sheet")

    try:
        path = find_upload(token)
        columns = table_columns(path, str(sheet) if sheet else None)
    except SheetToAnkiError as exc:
        return json_error(str(exc))
    except Exception as exc:  # pragma: no cover - defensive UI boundary
        return json_error(f"Could not read columns: {exc}")

    return jsonify({"columns": columns})


@app.post("/api/generate")
def generate_cards():
    data = request.get_json(silent=True) or {}
    token = str(data.get("token", ""))
    sheet = data.get("sheet")
    front_sheet = data.get("frontSheet", sheet)
    back_sheet = data.get("backSheet", sheet)
    front = str(data.get("front", ""))
    back = str(data.get("back", ""))

    if not front or not back:
        return json_error("Choose both front and back columns.")

    try:
        path = find_upload(token)
        front_df = read_table(path, str(front_sheet) if front_sheet else None)
        back_df = read_table(path, str(back_sheet) if back_sheet else None)
        require_columns(front_df, front)
        require_columns(back_df, back)
    except SheetToAnkiError as exc:
        return json_error(str(exc))
    except Exception as exc:  # pragma: no cover - defensive UI boundary
        return json_error(f"Could not generate cards: {exc}")

    output = io.StringIO()
    count = 0
    row_count = min(len(front_df), len(back_df))
    for index in range(row_count):
        front_value = clean_cell(front_df.iloc[index][front])
        back_value = clean_cell(back_df.iloc[index][back])
        if not front_value or not back_value:
            continue
        output.write(f"{front_value}\t{back_value}\n")
        count += 1

    if count == 0:
        return json_error("No cards were generated. Check for empty selected columns.")

    data_bytes = io.BytesIO(output.getvalue().encode("utf-8"))
    data_bytes.seek(0)
    stem = Path(path.name).stem.split("_", 1)[-1] or "anki_cards"
    download_name = f"{stem}_anki_cards.txt"
    return send_file(
        data_bytes,
        mimetype="text/plain; charset=utf-8",
        as_attachment=True,
        download_name=download_name,
    )


def open_browser_when_ready(url: str) -> None:
    if os.environ.get("WEB_PANEL_OPEN_BROWSER", "1") == "0":
        return
    threading.Timer(0.35, webbrowser.open, args=(url,)).start()


def can_bind_port(host: str, port: int) -> tuple[bool, str | None]:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as test_socket:
            test_socket.bind((host, port))
        return True, None
    except OSError as exc:
        return False, str(exc)


def main() -> None:
    host = "127.0.0.1"
    requested_port = int(os.environ.get("WEB_PANEL_PORT", "8765"))
    port = requested_port

    if requested_port != 0:
        available, reason = can_bind_port(host, requested_port)
        if not available:
            port = 0
            print(
                f"[web-panel] Port {requested_port} is unavailable ({reason}). "
                "Using an available local port instead.",
                flush=True,
            )

    try:
        server = make_server(host, port, app, threaded=True)
    except SystemExit:
        if port == 0:
            raise
        print(
            f"[web-panel] Port {port} is unavailable. "
            "Using an available local port instead.",
            flush=True,
        )
        server = make_server(host, 0, app, threaded=True)

    url = f"http://{host}:{server.server_port}/"
    print(f"[web-panel] Ready: {url}", flush=True)
    open_browser_when_ready(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("[web-panel] Stopped.", flush=True)


if __name__ == "__main__":
    main()
