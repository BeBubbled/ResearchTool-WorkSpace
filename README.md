# Research Toolkit · 科研工具箱

本机运行的科研工具箱：文献阅读、翻译、改稿、文献管理和实验素材处理，统一在一个本地网页面板里，文件只留在本机。

## 启动

```powershell
.\run_web_panel.ps1        # Windows；脚本签名受限时用 .\run_web_panel.cmd
```

```bash
./run_web_panel.sh         # macOS / Linux；macOS 也可双击 run_web_panel.command
```

首次运行会建项目内 `.venv`、装 `requirements.txt`、编译 Go 翻译后端（需 Python 3.10+、Go 1.23+；FFmpeg 可选）。默认端口 8765，被占用时自动换端口并打印地址。可选参数：`--no-browser`、`--no-pause`、`--port <端口>`。

凭据：复制 `.env.example` 为 `.env` 后填写，改完重启面板。`.env` 不进 Git，密钥不会返回浏览器。

| 变量 | 用途 |
|---|---|
| `LLM_*` / `LLM_PRESETS` | OpenAI-compatible LLM 预设（翻译、Three-Pass 等） |
| `MATHPIX_APP_ID` / `MATHPIX_APP_KEY` | PDF OCR |
| `AZURE_SPEECH_KEY` / `_REGION` / `_VOICE` | 段落朗读 |
| `GITHUB_REPOSITORY` / `_BRANCH` / `_IMAGE_ROOT` / `_TOKEN` | Markdown 图片发布 |
| `SEMANTIC_SCHOLAR_API_KEY` | 可选，独享检索配额 |
| `CODEX_BIN` | 可选，改用外部 Codex CLI |

Codex 默认由工具箱自动安装到 `.runtime/codex/` 并复用已有 ChatGPT 登录；也可在面板「设置」里登录和选默认模型。

## 模块

| 模块 | 地址 | 功能 |
|---|---|---|
| 精读 | `/reader` | PDF / Markdown / HTML 阅读，划词翻译与解释、笔记、生词、朗读、整篇翻译、AI 三遍阅读 |
| 文献库 | `/library` | 所有读过的论文（按内容指纹去重），批量导出 BibTeX、加入研究项目 |
| 改稿 | `/paper-revision` | LaTeX 中英对照，圈选批注后由模型按范围修改并重译，带版本历史、编译检查 |
| Prompt | `/prompts` | Prompt 模板库，配合 `browser_extension/prompt_bridge/` 把文字和附件送到 ChatGPT |
| 研究空白地图 | `/research-gaps` | 按主题导入文献（Semantic Scholar、arXiv、本地 PDF 文件夹），AI 提取证据后人工审核 |
| ResearchStudio | `/researchstudio` | 接入 Microsoft ResearchStudio：选题与新颖性检查，海报、博客、PPT、讲解视频 |
| 对话树 | `/branchboard` | 把 Claude Code 会话做成可分叉的二维对话树 |
| 工具箱 | `/` | 文档翻译、PDF OCR、Markdown 修复、图片发布、BibTeX、图片 / 视频 / PPT 脚本、Sheet→Anki |
| 设置 | `/settings` | 引擎与账号、费用确认、环境检查、用量统计 |

全局快捷键：⌘K / Ctrl+K 命令面板与全文搜索，`?` 查看快捷键表。

ResearchStudio 首次使用需先准备环境（另需 Poppler、LibreOffice、FFmpeg、Node.js 在 `PATH` 中）：

```powershell
python scripts/setup_researchstudio.py --install-deps --video
```

## 计费与数据

- 用 Codex 或 Claude Code 的任务消耗套餐额度，用 API 预设、Mathpix、Google 翻译的任务单独计费。提交前都要勾选使用确认。
- 运行数据都在 `.runtime/`（阅读缓存、改稿项目、研究项目、任务记录、用量日志）。
- 原始文件不会被改写或覆盖，结果另存副本。AI 产出需人工审核。

## 命令行：Sheet → Anki

```powershell
.\run_sheet_to_anki.ps1 input.xlsx --front-sheet 正面Sheet --front 正面列名 --back-sheet 背面Sheet --back 背面列名 --output anki_cards.txt
```

macOS / Linux 用 `./run_sheet_to_anki.sh`，参数相同。输出制表符分隔的 `.txt`，可直接导入 Anki。

## 测试

```powershell
python -m unittest discover -s tests
```

默认测试使用模拟模型，不消耗额度。`tests/smoke_*.py` 会调用真实服务，需显式加 `--confirm-usage`。

## 免责声明

本项目完全通过 Vibe Coding 开发，按现状提供，不保证正确、稳定、安全或适用于特定用途。用于自己的数据或流程前请自行审阅和测试。
