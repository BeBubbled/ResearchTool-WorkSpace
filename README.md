# Everything_To_Memory

This repository contains automation scripts and utilities for converting learning materials into Anki flashcards. The core goal is to transform different types of input files, such as Excel spreadsheets or other structured documents, into Anki-compatible memory cards. The repository may also include Python-based workflows for directly generating Anki cards from code, templates, or parsed data.

This project is developed entirely through ViveCoding. The code is provided as-is, mainly for personal automation, experimentation, and learning purposes. No guarantee is made regarding correctness, stability, maintainability, or compatibility. Users should review, test, and modify the code before using it in their own workflows.

## Local toolbox

Start the local web panel:

```powershell
.\run_web_panel.ps1
```

If your PowerShell policy requires signed scripts, use the companion launcher
instead; it bypasses the policy only for this one child process and does not
change your system policy:

```powershell
.\run_web_panel.cmd
```

The launcher opens the local web panel automatically when it is ready. If port
`8765` is unavailable on Windows, it chooses another local port and prints the
actual URL in the PowerShell window.

The panel includes Sheet-to-Anki plus the image, video, PowerPoint and BibTeX
tools in `Potential_Scripts`. Select a tool, drag in files or folders, optionally
reorder and rename their task-only working names, then submit a local background
job. Results are downloaded from the panel; original files are never renamed.

The launcher installs all Python dependencies into the project `.venv` on its
first run. It also detects missing `ffmpeg`/`ffprobe` and installs the
user-scoped `Gyan.FFmpeg.Shared` package through `winget`; no system-wide Python
packages are changed. BibTeX lookup uses the network through Google Scholar and
can be rate limited.

## Sheet to Anki command line

Use the PowerShell launcher on Windows:

```powershell
.\run_sheet_to_anki.ps1 input.xlsx --front-sheet 正面Sheet --front 正面列名 --back-sheet 背面Sheet --back 背面列名 --output anki_cards.txt
```

On a new computer, the launcher checks for a project-local `.venv`. If it is
missing, it finds or installs Python 3 with `winget`, creates `.venv`, installs
`requirements.txt` into that isolated environment only, and then runs the
converter. Later runs reuse `.venv` and only reinstall dependencies when
`requirements.txt` changes. The launchers never install Python packages into the
user's system Python environment.

The generated `.txt` file is tab-separated and can be imported directly by Anki.



## Disclaimer

This repository is developed entirely through ViveCoding. The code is provided as-is and is not guaranteed to be correct, stable, secure, or suitable for any specific purpose. I do not take responsibility for issues caused by using this code. Please review and test everything carefully before applying it to your own data or workflow.
