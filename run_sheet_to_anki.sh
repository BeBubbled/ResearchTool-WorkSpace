#!/usr/bin/env bash
# Run Sheet-to-Anki on macOS or Linux through the project-local virtual environment.

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$PROJECT_ROOT/.venv"
VENV_PYTHON="$VENV_DIR/bin/python"
REQUIREMENTS="$PROJECT_ROOT/requirements.txt"
REQUIREMENTS_STAMP="$VENV_DIR/.requirements.sha256"

get_system_python() {
    local candidate
    for candidate in python3.12 python3.11 python3.10 python3 python; do
        if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c \
            'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' \
            >/dev/null 2>&1; then
            command -v "$candidate"
            return 0
        fi
    done
    return 1
}

if [[ ! -x "$VENV_PYTHON" ]]; then
    system_python="$(get_system_python)" || {
        printf '%s\n' "[sheet-to-anki] Python 3.10+ is required. Install it, then rerun this launcher." >&2
        exit 1
    }
    printf '%s\n' "[sheet-to-anki] Creating project-local .venv."
    "$system_python" -m venv "$VENV_DIR"
fi

current_hash="$("$VENV_PYTHON" -c 'import hashlib, pathlib, sys; print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())' "$REQUIREMENTS")"
installed_hash=""
if [[ -f "$REQUIREMENTS_STAMP" ]]; then installed_hash="$(tr -d '[:space:]' < "$REQUIREMENTS_STAMP")"; fi
if [[ "$current_hash" != "$installed_hash" ]]; then
    printf '%s\n' "[sheet-to-anki] Installing dependencies into project-local .venv."
    "$VENV_PYTHON" -m ensurepip --upgrade
    "$VENV_PYTHON" -m pip install --upgrade pip
    "$VENV_PYTHON" -m pip install -r "$REQUIREMENTS"
    printf '%s\n' "$current_hash" > "$REQUIREMENTS_STAMP"
fi

exec "$VENV_PYTHON" "$PROJECT_ROOT/sheet_to_anki.py" "$@"
