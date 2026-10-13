"""Install pinned ResearchStudio sources and optional project-local dependencies.

Usage: python scripts/setup_researchstudio.py --install-deps --video
No upstream installer, global skill installation, shell configuration or API keys.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import venv

ROOT = Path(__file__).resolve().parents[1] / ".runtime" / "researchstudio"
SOURCES = {
    "upstream": ("https://github.com/microsoft/ResearchStudio.git", "063f10088af49909e1a4a7ea8d29efe19633f34e"),
    "dependencies/ppt-master": ("https://github.com/hugohe3/ppt-master.git", "ca04346de145ed6462ddf34682848e71df5e20a2"),
    "dependencies/pptx2video": ("https://github.com/ai-nuts/pptx2video.git", "3c4343136cceb78268dfa629135c6ee3bb5be427"),
}
DEPS = ["pymupdf>=1.26", "Pillow>=10", "numpy>=1.26", "python-docx>=1.1.2", "python-pptx>=1.0",
        "qrcode", "playwright>=1.50", "imageio-ffmpeg>=0.6", "edge-tts>=7.2.8", "fonttools>=4.59",
        "pdf2image>=1.17", "lxml>=5", "pyphen>=0.14", "feedparser>=6.0.12", "openreview-py>=2.2.3",
        "beautifulsoup4>=4.13", "requests>=2.31", "scholarly>=1.7.11", "python-dotenv"]


def run(args, **kwargs):
    return subprocess.run([str(arg) for arg in args], check=True, **kwargs)


def install_source(name):
    url, commit = SOURCES[name]
    destination = ROOT / name
    if destination.exists():
        current = run(["git", "-C", destination, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        dirty = run(["git", "-C", destination, "status", "--porcelain"], capture_output=True, text=True).stdout.strip()
        if current != commit or dirty:
            raise RuntimeError(f"Existing checkout differs from pinned source; left untouched: {destination}")
        print(f"Verified {name} @ {commit[:12]}")
        return
    destination.mkdir(parents=True)
    run(["git", "init", destination])
    run(["git", "-C", destination, "remote", "add", "origin", url])
    run(["git", "-C", destination, "fetch", "--depth", "1", "origin", commit])
    run(["git", "-C", destination, "checkout", "--detach", "FETCH_HEAD"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install-deps", action="store_true", help="Create a private venv and install Python packages + Chromium")
    parser.add_argument("--video", action="store_true", help="Also fetch pinned ppt-master and pptx2video sources")
    args = parser.parse_args()
    if sys.version_info < ((3, 11) if args.video else (3, 10)):
        parser.error("Use Python 3.10+ (3.11+ with --video).")
    install_source("upstream")
    if args.video:
        install_source("dependencies/ppt-master")
        install_source("dependencies/pptx2video")
    if args.install_deps:
        environment = ROOT / ".venv"
        venv.EnvBuilder(with_pip=True).create(environment)
        python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        run([python, "-m", "pip", "install", *DEPS])
        if args.video:
            run([python, "-m", "pip", "install", "-r", ROOT / "dependencies/ppt-master/requirements.txt"])
            run([python, "-m", "pip", "install", ROOT / "dependencies/pptx2video"])
        run([python, "-m", "playwright", "install", "chromium"])
        freeze = run([python, "-m", "pip", "freeze"], capture_output=True, text=True).stdout
        (ROOT / "installed-requirements.txt").write_text(freeze, encoding="utf-8")
    (ROOT / "sources.json").write_text(json.dumps({name: {"url": values[0], "commit": values[1]}
        for name, values in SOURCES.items() if (ROOT / name).exists()}, indent=2), encoding="utf-8")
    print("Ready to check /researchstudio. Native tools are not installed by this script.")
    print("Ensure pdftotext / pdftoppm (Poppler), soffice (LibreOffice), ffmpeg and node are on PATH.")


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"ResearchStudio setup failed: {exc}", file=sys.stderr)
        sys.exit(1)
