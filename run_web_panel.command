#!/usr/bin/env bash
# Double-click this file in Finder, or run it from Terminal with:
#   ./run_web_panel.command [--no-browser] [--no-pause] [--port 8765]

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$PROJECT_ROOT/.venv"
VENV_PYTHON="$VENV_DIR/bin/python"
REQUIREMENTS="$PROJECT_ROOT/requirements.txt"
REQUIREMENTS_STAMP="$VENV_DIR/.requirements.sha256"
MAIN_SCRIPT="$PROJECT_ROOT/web_panel.py"
PREFIX="toolbox"
NO_PAUSE=0
NO_BROWSER=0
PORT=8765
PANEL_STARTED=0

write_step() {
    printf '[%s] %s\n' "$PREFIX" "$1"
}

pause_before_exit() {
    if [[ "$NO_PAUSE" -eq 0 && -t 0 ]]; then
        read -r -p "Press Enter to close this window." || true
    fi
}

cleanup() {
    local status=$?
    unset WEB_PANEL_PORT WEB_PANEL_OPEN_BROWSER

    if [[ "$status" -ne 0 ]]; then
        printf '\n[%s] Failed to start.\n' "$PREFIX" >&2
        pause_before_exit
    elif [[ "$PANEL_STARTED" -eq 1 ]]; then
        printf '\n[%s] Web panel stopped.\n' "$PREFIX"
        pause_before_exit
    fi
}

trap cleanup EXIT

usage() {
    cat <<'EOF'
Usage: ./run_web_panel.command [options]

Options:
  --port PORT     Preferred local port (default: 8765; use 0 for any available port)
  --no-browser    Do not open the browser automatically
  --no-pause      Do not wait for Enter when the panel stops or fails
  -h, --help      Show this help message
EOF
}

validate_port() {
    [[ "$1" =~ ^[0-9]+$ ]] && (( 10#$1 <= 65535 ))
}

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

install_python() {
    if ! command -v brew >/dev/null 2>&1; then
        cat >&2 <<'EOF'
[web-panel] Python 3.10+ was not found and Homebrew is unavailable.
Install Python 3.10+ from https://www.python.org/downloads/macos/ or install
Homebrew from https://brew.sh/, then run this file again.
EOF
        return 1
    fi

    write_step "Python 3.10+ not found. Installing Python 3.12 with Homebrew." >&2
    brew install python@3.12 >&2

    local brew_python
    brew_python="$(brew --prefix python@3.12)/bin/python3.12"
    if [[ -x "$brew_python" ]]; then
        printf '%s\n' "$brew_python"
        return 0
    fi

    get_system_python
}

ensure_ffmpeg() {
    if command -v ffmpeg >/dev/null 2>&1 && command -v ffprobe >/dev/null 2>&1; then
        write_step "FFmpeg and FFprobe are available."
        return
    fi

    cat >&2 <<'EOF'
[toolbox] FFmpeg/FFprobe are not installed. The panel will still start, but video
tools will be shown as unavailable. Install FFmpeg and restart the panel to enable
them: https://ffmpeg.org/download.html
EOF
}

ensure_pip() {
    if "$VENV_PYTHON" -c 'import pip' >/dev/null 2>&1; then
        return
    fi

    write_step "Project .venv is missing pip. Repairing it with ensurepip."
    "$VENV_PYTHON" -m ensurepip --upgrade
    "$VENV_PYTHON" -c 'import pip' >/dev/null 2>&1 || {
        printf '[%s] Could not repair pip. Delete .venv and run this file again.\n' "$PREFIX" >&2
        return 1
    }
}

sync_dependencies() {
    [[ -f "$REQUIREMENTS" ]] || {
        printf '[%s] Missing requirements file: %s\n' "$PREFIX" "$REQUIREMENTS" >&2
        return 1
    }

    local current_hash installed_hash=""
    current_hash="$("$VENV_PYTHON" -c \
        'import hashlib, pathlib, sys; print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())' \
        "$REQUIREMENTS")"
    if [[ -f "$REQUIREMENTS_STAMP" ]]; then
        installed_hash="$(tr -d '[:space:]' < "$REQUIREMENTS_STAMP")"
    fi

    if [[ "$current_hash" == "$installed_hash" ]]; then
        write_step "Dependencies already installed in project .venv."
        return
    fi

    ensure_pip
    write_step "Installing dependencies into project .venv only; system Python packages will not be changed."
    "$VENV_PYTHON" -m pip install --upgrade pip
    "$VENV_PYTHON" -m pip install -r "$REQUIREMENTS"
    printf '%s\n' "$current_hash" > "$REQUIREMENTS_STAMP"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --port)
            [[ $# -ge 2 ]] || { printf '[%s] --port requires a value.\n' "$PREFIX" >&2; exit 2; }
            PORT="$2"
            shift 2
            ;;
        --no-browser)
            NO_BROWSER=1
            shift
            ;;
        --no-pause)
            NO_PAUSE=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            printf '[%s] Unknown option: %s\n' "$PREFIX" "$1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

validate_port "$PORT" || { printf '[%s] Port must be an integer from 0 to 65535.\n' "$PREFIX" >&2; exit 2; }

write_step "Python dependencies are isolated in project .venv."
if [[ ! -x "$VENV_PYTHON" ]]; then
    SYSTEM_PYTHON="$(get_system_python || install_python)"
    write_step "Using system Python only to create an isolated project environment."
    write_step "Creating project Python environment at $VENV_DIR"
    "$SYSTEM_PYTHON" -m venv "$VENV_DIR"
else
    write_step "Using existing project Python environment."
fi

sync_dependencies
ensure_ffmpeg

export WEB_PANEL_PORT="$PORT"
export WEB_PANEL_OPEN_BROWSER=$([[ "$NO_BROWSER" -eq 1 ]] && printf '0' || printf '1')

write_step "Starting local multi-tool panel on 127.0.0.1."
if [[ "$NO_BROWSER" -eq 0 ]]; then
    write_step "Your browser will open automatically when the panel is ready."
fi
PANEL_STARTED=1
"$VENV_PYTHON" "$MAIN_SCRIPT"
