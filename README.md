# Research Toolkit · 科研工具箱

This repository is a local research toolbox for literature processing, paper reading, document translation, reference management, experiment media processing, and presentation-ready outputs. Its tools run from one local web panel and keep task files on the current machine.

## Research Gap Map

Open `/research-gaps` from the panel sidebar to create topic-based literature projects. The workspace can import papers from Semantic Scholar by title, DOI, arXiv ID, or supported link, can batch-resolve up to 50 newline-separated paper titles directly against arXiv, can recursively ingest a local folder containing 1,000+ PDFs, and can reuse documents already stored in the local paper reader. Batch arXiv import verifies close title agreement before adding a result. Open-access PDFs are downloaded and parsed locally when available; otherwise the paper is clearly marked as abstract-level evidence.

Local PDF-folder imports run as persistent background jobs with per-file progress, pagination, pause/resume, and explicit recovery after an application restart. Files are deduplicated within the project by their SHA-256 content fingerprint. The original PDFs are never copied or modified: the project stores the extracted text, original absolute/relative path, and fingerprint. PDFs with fewer than 2,000 extractable text characters are reported as skipped, while encrypted, damaged, missing, or unreadable files are reported independently without aborting the batch. Titles and authors come only from embedded PDF metadata, with the filename used as the title fallback; folder import never performs automatic online metadata lookup or AI analysis.

Run evidence extraction with the local Codex App Server and the signed-in ChatGPT/Codex allowance, or choose a saved OpenAI-compatible LLM preset billed through its API key. Research Gap Map always uses the Codex model and reasoning-effort defaults managed under “AI, Voice & GitHub”; it shows the effective defaults but does not provide per-run overrides. AI results enter a review queue and remain visibly provisional until accepted or rejected. Project records, extracted text, evidence, and analysis jobs are stored under `.runtime/research-gaps/` and can be exported as JSON.

Semantic Scholar works without a key under its shared public rate limit. Set `SEMANTIC_SCHOLAR_API_KEY` in `.env` for a dedicated API key when available.

This project is developed entirely through ViveCoding. The code is provided as-is, mainly for personal automation, experimentation, and learning purposes. No guarantee is made regarding correctness, stability, maintainability, or compatibility. Users should review, test, and modify the code before using it in their own workflows.

## Local toolbox

Start the local web panel on Windows:

```powershell
.\run_web_panel.ps1
```

If your PowerShell policy requires signed scripts, use the companion launcher
instead; it bypasses the policy only for this one child process and does not
change your system policy:

```powershell
.\run_web_panel.cmd
```

On macOS, double-click `run_web_panel.command` in Finder, or run the shared
macOS/Linux launcher from Terminal:

```bash
chmod +x run_web_panel.command
./run_web_panel.command
# Or: ./run_web_panel.sh
```

On Linux, run:

```bash
chmod +x run_web_panel.sh
./run_web_panel.sh
```

The macOS/Linux launcher creates and reuses a project-local `.venv`, installs
dependencies from `requirements.txt` when needed, and opens the panel in the
default browser. It requires Python 3.10+ and Go 1.23+; macOS installs missing
Python and Go through Homebrew, while Linux prints distribution-specific install
instructions. FFmpeg is optional: without it the non-video tools remain usable.
You can pass `--no-browser`, `--no-pause`, or `--port 8765` to either launcher.
The launcher opens the local web panel automatically when it is ready. If port
`8765` is unavailable, it chooses another local port and prints the actual URL
in the launcher window.

The panel includes Sheet-to-Anki plus the image, video, PowerPoint and BibTeX
tools in `Potential_Scripts`. Select a tool, drag in files or folders, optionally
reorder and rename their task-only working names, then submit a local background
job. Results are downloaded from the panel; original files are never renamed.

### 文档翻译与 PDF OCR

- 自动生成的 Markdown/MMD 整篇译文会在交付前进行本地数学结构后处理：仅修复高置信度的粘连或缺失行间 `$$` 分隔符，并跳过围栏代码块和普通行内公式。HTML/HTM 译文保持原样。
- 若译文仍存在未配对 `$$`、数学块吞入标题/图片/脚注/正文或未转义 `#` 等结构风险，任务会失败而不会发布正式 `*_zh-CN.md/.mmd`；原始模型输出会保留为 `*_zh-CN_unfixed.md/.mmd` 供下载、检查或重试。
- 面板中的“Markdown 修复”可独立处理 `.md` 与 `.mmd`，不会翻译正文或覆盖上传文件：默认输出 `*_fixed` 副本，并将含空格但未用尖括号包裹的本地图片路径（如 `![图](Conceptual Framework.png)`）修复为合法写法。它也执行确定性 `$$` 与 OCR 合并脚注修复。脚注仅在正文上标与合并定义能唯一对应时拆分；歧义内容不会猜测修改。可按需开启中文标点转英文符号（代码、公式、链接和 URL 保持不变）或需要 LLM 的深度 OCR 结构修复。校验失败时会保留 `*_fixed_unfixed` 原稿供下载。

### 科研论文阅读器

启动本地面板后，点击侧栏的“打开科研论文阅读器”，或访问 `/reader`。阅读器面向计算机科学、深度学习、机器学习、扩散模型及相关数学论文：

- PDF 默认通过项目内置的 PDF.js 在本机直接打开，并抽样检测文字层；只有用户主动开启“使用 OCR”时，文件才会发送给 Mathpix 并生成包含本地资源的 HTML。OCR 检测结果只作提示，不会替用户切换。
- 阅读 PDF、Markdown/MMD 或 HTML 时，正文顶部都会显示“点击朗读”总开关、Azure 朗读语速（`0.5×`–`2.0×`）和“停止朗读”按钮；开关开启后单击段落即可调用 Azure Speech 朗读，所选语速通过 SSML 合成并保存在当前浏览器。原生 PDF 会按文字层位置推断段落，Markdown 与 HTML 使用语义段落。朗读覆盖原文、译文、段落对照、左右对照，以及“点段翻译”中的原文和动态生成译文；新增阅读视图也默认沿用段落朗读。“点段翻译”的原文点击会同时保持原有翻译行为。再次单击当前段落或点击顶部“停止朗读”即可停止。默认区域为 `centralus`，语音为 `zh-CN-YunxiNeural`；密钥仅由后端从 `.env` 读取，不会返回浏览器。
- 阅读器导入的 PDF、Markdown/MMD、HTML/HTM 和 OCR 结果都会按内容指纹持久缓存在 `.runtime/reader-cache`，并自动列在上传页的本地文档库中；每个已有项目都可直接重命名，自定义名称不会改变缓存键或笔记关联。导入已有 Markdown/MMD 或 HTML 原文-译文 pair 时，仍可显式选择 LLM 修复错误换行造成的段落错位。
- “生成整篇中文译文”对 Markdown/MMD、HTML/HTM 以及开启 OCR 后的 PDF 开放，并统一使用项目内的 AI-Markdown-Translator Go 后端；原始 HTML 按行分块交给 LLM，不调用旧 Python HTML 翻译、DOM 节点翻译或结构保护。使用相对图片路径时仍可同时选择对应的图片目录。
- 划选任意原文或译文后，可选择“翻译选区、解释选区、解释公式、公式的几何意义、推导这一步、总结、直觉解释、批判性阅读、实现视角”。系统只注入当前页或所在段落附近的上下文，而非整篇论文。
- 划词弹窗顶部提供类似 Word 的文字格式工具栏，可组合设置选区底色、文字颜色、粗体、斜体、下划线和删除线，也可单独或全部清除；格式与精确选区一起缓存在浏览器，并会在 Markdown、OCR/译文 HTML、PDF 文字层和切换视图后恢复。Prompt 缓存虚线使用独立文字装饰层，不会覆盖这些格式；多个相互重叠的 Prompt 会分配到多行虚线轨道，已有实线下划线时所有虚线自动排列在实线下方，并按最深轨道补足段落行距。弹窗底部继续显示 Prompt 动作。
- 对单个短词执行“翻译选区”后，阅读器会自动将其标记为生词；再次打开文档时仍会恢复紫色虚线标记，鼠标悬停或键盘聚焦即可查看此前缓存的 Markdown 翻译，点击则在右侧打开完整结果，不会重复调用 LLM。短句、公式和其他 Prompt 不会触发生词标记。
- 阅读器右侧提供“结构化阅读笔记”：每次执行划词 Prompt 后，选区、动作/补充问题和返回结果会自动组成一条配对笔记，按论文章节及文档内容指纹缓存在当前浏览器。执行过 Prompt 的正文选区会显示虚线下划线和右上角动作角标；角标会根据正文实际字号动态压缩到约 7–9px 字号、10–12px 高，并只在可用行间空隙不足时增大对应 Markdown/HTML 段落行距，避免遮挡正文。单击虚线、角标或重新划选缓存文字，会读取已有回答，并自动展开、滚动和高亮对应 Note。每条 Note 可单独删除，正文标记随之同步移除；笔记也可复制或导出为 Markdown。桌面端既可拖动右栏左侧手柄，也可使用面板内的宽度滑块及加减按钮调整整个“划词解读 + 回答 + Note”区域，宽度上限为 2500px，加宽右栏只向右扩展页面。正文左侧也提供独立拖拽手柄和宽度滑块，向左扩展时右侧解读栏位置保持不变，正文宽度上限同为 2500px；“阅读器中轴偏移”可将整组阅读布局向左或向右移动。段落对照与左右对照会随正文列实时铺满，不再受旧的 840/920/1280px 固定行宽限制。三项布局设置均保存在本地。窄屏保持自适应布局。右栏拥有独立滚动区域，长回答和笔记不会带动正文滚动。
- 文档专属阅读状态还会同步写入对应缓存目录的 `reader-state.json`（最多 200 条笔记、500 条文字格式记录、4 MB），浏览器本地存储作为即时副本；再次从本地文档库打开时会选择较新的状态，因此任务目录清理后笔记、格式、生词和回答仍可恢复。
- 论文顶部的“阅读设置”可分别定制中文、英文和数学文本字体，并调整字号、字距、行距、段距，以及页眉、正文、页脚各自的上/右/下/左边距；也可为每个划词动作指定 LLM、开启自动执行并选择默认动作。设置会即时作用于 OCR/译文、Markdown、划词回答和阅读笔记，并保存在当前浏览器。原生 PDF 页面仍使用 PDF 自带的嵌入字体，仅调整页面外围留白。
- 侧栏提供独立的“AI、语音与 GitHub 配置”菜单，以标签页统一管理 OpenAI-compatible LLM 预设、Codex、Three-Pass 篇幅、Azure Speech 和 Markdown 图片仓库。Three-Pass 可为 Pass 1、Pass 2、Pass 3 与综合报告分别选择短/中/长或 100–12,000 的自定义目标篇幅，新建分析会冻结当时设置。Codex 面板读取当前 ChatGPT 套餐、可用模型及剩余额度，并可保存本工具箱使用的默认模型和推理强度；支持 Instant 的 GPT-5.6/6 模型会把 `none` 显示为 `Instant（none）`。这些设置不会改写全局 Codex CLI 配置。整篇翻译只能选择这里保存的 LLM 预设；各类 Token/API Key 仅保存在本机 `.env` 与当前后端进程中，不会返回浏览器或写入任务日志。
- 阅读器右侧的“AI 三遍阅读”可选择两种后端：默认的本机 `codex app-server` 使用 ChatGPT/Codex 登录额度，也可选择全局 LLM 预设直接调用 API（OpenAI 官方地址自动使用 Responses API，其他兼容服务默认使用 Chat Completions）。两种后端都依次执行 Pass 1、Pass 2、Pass 3 和综合报告，保存 Markdown 报告并支持追问；API 模式会明确要求确认独立计费，超出安全上下文时按页码和标题自适应分块并再次确认预计调用次数。Codex 可在阅读器内通过浏览器或设备码登录，也可在终端运行 `codex login`。原生 PDF 必须具有足够文字层，扫描件请在导入时开启 OCR。
- 真实 Codex 烟雾测试不会被默认测试发现；如需显式验证登录、Skill、四阶段报告和追问，请运行 `python tests/smoke_codex_three_pass.py path/to/paper.md --confirm-usage`（会运行 5 个真实 turn 并消耗额度）。
- ResearchTool 默认把 npm `latest` 稳定版 Codex 安装到 `.runtime/codex/releases/<version>/`，每 24 小时检查一次；新版本先在临时目录安装并验证，空闲时切换，保留上一版并在 App Server 初始化失败时自动回滚。离线时继续使用最后一个可用版本，登录仍复用用户现有的 Codex/ChatGPT 凭据。设置 `CODEX_BIN` 可关闭托管模式并显式指定外部 CLI。
- 真实 API 烟雾测试同样需要显式确认：`python tests/smoke_api_three_pass.py path/to/paper.md --preset-id <预设ID> --confirm-usage`；长文还必须增加 `--confirm-chunked`，该测试会产生 API 费用。
- “Markdown 图片发布”接收恰好一份 `.md/.mmd` 和对应图片目录，只上传文档实际引用的本地图片。它支持 Markdown 行内/引用式图片、Wiki 图片、MMD `includegraphics` 和内嵌 HTML 图片，跳过代码、远程链接与 `data:` URL。图片以 `<根目录>/YYYY/MM/DD/<SHA-256>.<扩展名>` 写入公开 GitHub 仓库的单次原子提交，输出固定到 commit SHA 的 Raw URL，并下载不覆盖原文件的 `*_github.md/.mmd`。

PDF.js 6.1.200 固定并保存在 `static/vendor/pdfjs/`，原生 PDF 阅读不依赖 CDN。OCR HTML 在禁止脚本和外部资源的 sandbox iframe 中显示。MathJax 仍通过浏览器加载以显示 Markdown 中的 LaTeX；若本机离线，原始公式文本仍会保留，但不会排版为数学公式。

启用 PDF 段落朗读时，在 `.env` 中配置：

```dotenv
AZURE_SPEECH_KEY=你的 Azure Speech 密钥
AZURE_SPEECH_REGION=centralus
AZURE_SPEECH_VOICE=zh-CN-YunxiNeural
```

Markdown 图片发布需要公开且已初始化的 GitHub 仓库，以及具有 `Contents: write`
权限的 Personal Access Token。也可直接在全局 GitHub 标签页保存并测试以下配置：

```dotenv
GITHUB_REPOSITORY=owner/repository
GITHUB_BRANCH=main
GITHUB_IMAGE_ROOT=images
GITHUB_TOKEN=你的 Personal Access Token
```

修改后需要重启本地面板。

“研究”分类中的“文档翻译”使用
[GMYXDS/AI-Markdown-Translator](https://github.com/GMYXDS/AI-Markdown-Translator) 的后端核心。
它接收 `.md`、`.mmd`、`.html` 和 `.htm` 文件或文件夹，保留目录结构；单个文件直接下载译文，多个文件打包为 ZIP。
翻译只使用“AI 与语音配置”中保存的 LLM 预设，不提供任务级临时模型配置。

PDF OCR 工具使用 Mathpix 生成 MMD、Markdown、HTML、DOCX、LaTeX ZIP 和行级 JSON；
完成后可在同一任务中选择 Markdown、MMD 或 HTML 交给 AI-Markdown-Translator 后端。
复制 `.env.example` 为 `.env`，填入 Mathpix 和任意 OpenAI-compatible LLM 的
凭据后重启面板：

```bash
cp .env.example .env
```

`.env` 可保留一组兼容旧配置的 `LLM_*` 值，或通过 `LLM_PRESETS` 声明多组命名
预设；每组配置可用 `LLM_CONCURRENCY` 或 `LLM_PRESET_<ID>_CONCURRENCY` 设置翻译并发数
（默认 1，范围 1–64）。面板会显示预设名称、URL、模型 ID 和并发数，选择时在本地使用对应密钥。
LLM 管理页的“测试并发”会一次性发送已保存并发数个短请求，并给出成功率、错误类型和有效并行度；结果只代表测试当时的账户配额与服务负载。

`.env` 仅保存在本机且已被 Git 忽略。浏览器不会公开上传文件的绝对路径，因此提交
PDF 时还需填写原始 PDF 的本地绝对路径。面板会在该文件同级创建同名目录，例如
`paper.pdf` 会创建 `paper/`，先复制 `paper.pdf`，再自动保存 `paper.mmd`、
`paper.lines.json`、`paper.html`、`paper.md`、`paper.tex.zip`、`paper.docx` 及翻译
文件。Markdown/MMD 译文只有通过数学结构校验后才会以正式名称保存；失败的原始译文会以
`*_zh-CN_unfixed.md/.mmd` 保存到同一 OCR 目录。DOCX、LaTeX ZIP 和 JSON 目前仅提供下载，
不执行保版式翻译。

系统会从 Mathpix 的自包含 MMD ZIP 提取图片；例如 `paper.pdf` 的输出目录会包含
`paper.mmd`、可选的 `paper.md`、`paper.html`、`paper.lines.json` 和 `paper.assets/`。
MMD、Markdown、HTML 与行级 JSON 中的 Mathpix 图片链接都会改为 `paper.assets/...`；资源包
没有覆盖的图片会额外下载，因此这些输出不再依赖会过期的 Mathpix CDN URL。下载的 OCR 结果
ZIP 也会包含该资源目录。DOCX 和 LaTeX ZIP 已内嵌图片，保持原样。

OCR 表单默认启用 Markdown/MMD 自动结构修复，并要求显式选择 OpenAI-compatible LLM。图片链接
本地化完成后，未经结构修复的版本保存为 `paper_legacy.md` 与 `paper_legacy.mmd`；系统再修复错误
代码围栏、脚注引用与定义、公式环境，以及含公式的算法伪代码。合并脚注会先以本地严格规则修复，
仅在能确认正文上标位置时拆分。只有图片引用、脚注集合、围栏和
LaTeX 环境等不变量全部通过校验时才生成 `paper.md` 与 `paper.mmd`。修复失败不会丢弃其他 OCR
结果，只保留相应 legacy 文件并将任务标记为“完成（有警告）”。关闭自动修复后沿用原有输出命名。

Go 后端按 AI-Markdown-Translator 的原生策略逐行估算并分块，以 Worker Pool 并发翻译，
将任务、文件、分块和 Token 用量写入任务目录内的 `translation_backend.sqlite3`。失败后点击
“从缓冲继续翻译”会复用 SQLite 中已完成的分块；全部分块成功后才按原顺序合并成
`*_zh-CN.md`、`*_zh-CN.mmd`、`*_zh-CN.html` 或 `*_zh-CN.htm`。HTML 与 Markdown 使用相同的
原始逐行分块，不解析 DOM，也不会调用旧 Python Markdown/HTML 翻译、公式占位符、结构保护、
HTML 节点翻译或翻译后结构修复逻辑。
为避免推理模型耗尽输出预算，面板将分块控制在约 7200 字符，并拒绝
`finish_reason=length` 或 Token 用量触及输出上限的响应；截断内容不会再被标记为完成或合并，
从而避免孤立的 `$$` 让后续整章进入错误的 MathJax 数学模式。

The launcher installs all Python dependencies into the project `.venv` on its
first run and builds the Go translation backend (installing Go when needed). It
also detects missing `ffmpeg`/`ffprobe` and installs the
user-scoped `Gyan.FFmpeg.Shared` package through `winget`; no system-wide Python
packages are changed. BibTeX lookup uses the network through Google Scholar and
can be rate limited.

## Sheet to Anki command line

Use the PowerShell launcher on Windows:

```powershell
.\run_sheet_to_anki.ps1 input.xlsx --front-sheet 正面Sheet --front 正面列名 --back-sheet 背面Sheet --back 背面列名 --output anki_cards.txt
```

On macOS or Linux, use:

```bash
./run_sheet_to_anki.sh input.xlsx --front-sheet 正面Sheet --front 正面列名 --back-sheet 背面Sheet --back 背面列名 --output anki_cards.txt
```

On a new computer, the launcher creates a project-local `.venv` and installs
`requirements.txt` into that isolated environment only. Later runs reuse `.venv`
and only reinstall dependencies when `requirements.txt` changes. The launchers
never install Python packages into the user's system Python environment.

The generated `.txt` file is tab-separated and can be imported directly by Anki.



## Disclaimer

This repository is developed entirely through ViveCoding. The code is provided as-is and is not guaranteed to be correct, stable, secure, or suitable for any specific purpose. I do not take responsibility for issues caused by using this code. Please review and test everything carefully before applying it to your own data or workflow.
