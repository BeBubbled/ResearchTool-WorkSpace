#!/usr/bin/env bash
# Finder-compatible macOS entry point. The shared launcher also supports Linux.

set -euo pipefail
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$PROJECT_ROOT/run_web_panel.sh" "$@"
