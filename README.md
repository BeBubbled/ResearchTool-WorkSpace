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

On macOS, double-click `run_web_panel.command` in Finder, or run it from
Terminal:

```bash
chmod +x run_web_panel.command
./run_web_panel.command
```

The macOS launcher creates and reuses the same project-local `.venv`, installs
dependencies from `requirements.txt` when needed, and opens the panel in the
default browser. If Python 3.10+ is missing, it installs Python 3.12 through
Homebrew when Homebrew is available. It also installs FFmpeg through Homebrew
for the video tools. You can pass `--no-browser`, `--no-pause`, or `--port 8765`
to the macOS launcher.
The launcher opens the local web panel automatically when it is ready. If port
`8765` is unavailable, it chooses another local port and prints the actual URL
in the launcher window.

The panel includes Sheet-to-Anki plus the image, video, PowerPoint and BibTeX
tools in `Potential_Scripts`. Select a tool, drag in files or folders, optionally
reorder and rename their task-only working names, then submit a local background
job. Results are downloaded from the panel; original files are never renamed.

### 文档翻译与 PDF OCR

“研究”分类中的“文档翻译”是独立的翻译面板，不影响 PDF OCR 中原有的翻译入口。
它可接收 `.mmd`、`.md`、`.html` 文件，也可直接选择或拖入文件夹；文件夹会递归处理、
保留原目录结构，并优先处理 Markdown（`.md`），然后是 MMD 和 HTML。单个文件直接下载
译文；多个文件会下载包含相同目录结构的 ZIP。

翻译可选择 `.env` 中的 LLM 预设，或临时填写 OpenAI-compatible 配置。临时 API Key 仅用于
当前内存任务，不会写进 `.env`、任务选项、日志或输出文件。

PDF OCR 工具使用 Mathpix 生成 MMD、Markdown、HTML、DOCX、LaTeX ZIP 和
行级 JSON；完成后可在同一任务中选择 MMD、Markdown 或 HTML 翻译为简体中文。
复制 `.env.example` 为 `.env`，填入 Mathpix 和任意 OpenAI-compatible LLM 的
凭据后重启面板：

```bash
cp .env.example .env
```

`.env` 可保留一组兼容旧配置的 `LLM_*` 值，或通过 `LLM_PRESETS` 声明多组命名
预设；面板会显示预设名称、URL 和模型 ID，选择时在本地使用对应密钥。翻译页面也
可临时填写自定义名称、OpenAI-compatible URL、API Key 和模型 ID；临时配置不会
写入 `.env`、任务文件或日志。

`.env` 仅保存在本机且已被 Git 忽略。浏览器不会公开上传文件的绝对路径，因此提交
PDF 时还需填写原始 PDF 的本地绝对路径。面板会在该文件同级创建同名目录，例如
`paper.pdf` 会创建 `paper/`，先复制 `paper.pdf`，再自动保存 `paper.mmd`、
`paper.lines.json`、`paper.html`、`paper.md`、`paper.tex.zip`、`paper.docx` 及翻译
文件。DOCX、LaTeX ZIP 和 JSON 目前仅提供下载，不执行保版式翻译。

系统会从 Mathpix 的自包含 MMD ZIP 提取图片；例如 `paper.pdf` 的输出目录会包含
`paper.mmd`、可选的 `paper.md`、`paper.html`、`paper.lines.json` 和 `paper.assets/`。
MMD、Markdown、HTML 与行级 JSON 中的 Mathpix 图片链接都会改为 `paper.assets/...`；资源包
没有覆盖的图片会额外下载，因此这些输出不再依赖会过期的 Mathpix CDN URL。下载的 OCR 结果
ZIP 也会包含该资源目录。DOCX 和 LaTeX ZIP 已内嵌图片，保持原样。

翻译会按文本分块显示完成进度，并在每一块完成后立即保存。若 LLM 调用中断，已完成
的内容会保留为 `*_zh-CN.partial.*`，可直接从 OCR 输出列表下载，也会同步到原 PDF
同级目录；完整成功后会生成 `*_zh-CN.*`。

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
