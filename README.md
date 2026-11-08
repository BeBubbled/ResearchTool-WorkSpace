# Research Toolkit · 科研工具箱

This repository is a local research toolbox for literature processing, paper reading, document translation, reference management, experiment media processing, and presentation-ready outputs. Its tools run from one local web panel and keep task files on the current machine.

## ResearchStudio

侧栏的 **ResearchStudio**（`/researchstudio`）接入 [Microsoft ResearchStudio](https://github.com/microsoft/ResearchStudio) 两组工作流：

- **Idea**：IdeaSpark 研究选题、跨来源文献搜索、Scoop Check 新颖性检查。填写研究问题，可附加 Research Gap Map 项目的当前快照或种子论文 PDF；未审核证据仍标记为暂定。
- **Reel**：共享论文素材、海报、双语 Word 博客、PowerPoint + 讲解视频，以及完整 Reel 展示包。可上传 PDF 或复用论文库中保留的原始 PDF；只有 OCR/Markdown 的缓存不能代替原始 PDF。

首次准备运行环境（在 ResearchTool 根目录，Python 3.11+、Git）：

```powershell
python scripts/setup_researchstudio.py --install-deps --video
```

脚本将三个上游仓库固定到指定 commit，保留原始许可证，安装在 `.runtime/researchstudio/`，Python 包使用该目录内的独立 `.venv`；Chromium 使用 Playwright 的浏览器缓存。省略 `--video` 可跳过额外视频技能，省略 `--install-deps` 则只下载源码。已有不同版本或被修改的 checkout 不会被覆盖。实际 Python 包版本记录在 `installed-requirements.txt`；上游源码固定不代表所有传递依赖都被锁定。没有调用上游安装器，也不改动全局 Codex 技能或权限配置。

Poppler（`pdftotext`、`pdftoppm`）、LibreOffice（`soffice`）、FFmpeg（`ffmpeg`、`ffprobe`）和 Node.js 需另行安装并加入启动面板时的 `PATH`。也可将便携 LibreOffice 放在 `.runtime/researchstudio/native/libreoffice/`（其中 `program/soffice.exe` 会被自动发现），或用 `.env` 中的 `RESEARCHSTUDIO_TOOL_PATH` 指定额外工具目录，以操作系统路径分隔符分隔。博客完整 QA 与视频需要 LibreOffice；上游视频渲染的 Windows 支持仍处于 preview。页面按工作流检查基础依赖，缺失时禁止提交并列出原因。`RESEARCHSTUDIO_PYTHON` 可在 `.env` 中指定已有 Python 环境；默认为私有 `.venv`，其次为面板 Python。检查依赖存在不等于所有字体、外部文献服务和真实生成链路均已验证。

任务复用全局 Codex 默认模型、推理强度和登录凭据，显式注入上游技能，通过独立线程运行，允许在任务工作目录内写文件及访问公开文献服务。暂不支持把技能直接作为普通 LLM API 请求执行。提交前需确认使用套餐额度；文献内容会交给模型，检索请求会发送到公开服务，默认 Edge TTS 也需要联网。原始文件、Gap Map 和全局设置不由生成任务修改。生成内容没有自动发布或自动写回 Gap Map。

每个任务串行排队，支持停止，保存模型、原始要求、输入副本、项目快照、状态和产物。控制记录位于任务工作区之外；服务重启将未完成任务标记为中断，不会自动重跑消耗额度。两小时未完成会请求中止。产物缺失、博客/视频 QA 未通过或 IdeaSpark 明确标记失败的结果显示为“产物不完整”，可下载已生成文件核查。成果仍需人工审核，研究方案不代表创新性或实验有效性已经得到证明。

下载可选单个文件或 ZIP，包含共享素材及上游质量报告。生成的 HTML 强制下载，不在面板的同源页面执行。Reel 解压后请按上游 `serve_reel.py` 说明通过支持 HTTP Range 的本地服务查看视频定位功能。

集成回归测试（模拟 Codex，不消耗额度）：

```powershell
python -m unittest discover -s tests -p test_research_studio.py
node --check static/researchstudio.js
```

## Research Gap Map

Open `/research-gaps` from the panel sidebar to create topic-based literature projects. The workspace can import papers from Semantic Scholar by title, DOI, arXiv ID, or supported link, can batch-resolve up to 50 newline-separated paper titles directly against arXiv, can recursively ingest a local folder containing 1,000+ PDFs, and can reuse documents already stored in the local paper reader. Batch arXiv import verifies close title agreement before adding a result. Open-access PDFs are downloaded and parsed locally when available; otherwise the paper is clearly marked as abstract-level evidence.

Local PDF-folder imports run as persistent background jobs with per-file progress, pagination, pause/resume, and explicit recovery after an application restart. Files are deduplicated within the project by their SHA-256 content fingerprint. The original PDFs are never copied or modified: the project stores the extracted text, original absolute/relative path, and fingerprint. PDFs with fewer than 2,000 extractable text characters are reported as skipped, while encrypted, damaged, missing, or unreadable files are reported independently without aborting the batch. Titles and authors come only from embedded PDF metadata, with the filename used as the title fallback; folder import never performs automatic online metadata lookup or AI analysis.

Run evidence extraction with the local Codex App Server and the signed-in ChatGPT/Codex allowance, or choose a saved OpenAI-compatible LLM preset billed through its API key. Research Gap Map always uses the Codex model and reasoning-effort defaults managed under “AI, Voice & GitHub”; it shows the effective defaults but does not provide per-run overrides. AI results enter a review queue and remain visibly provisional until accepted or rejected. Project records, extracted text, evidence, and analysis jobs are stored under `.runtime/research-gaps/` and can be exported as JSON.

Semantic Scholar works without a key under its shared public rate limit. Set `SEMANTIC_SCHOLAR_API_KEY` in `.env` for a dedicated API key when available.

This project is developed entirely through ViveCoding. The code is provided as-is, mainly for personal automation, experimentation, and learning purposes. No guarantee is made regarding correctness, stability, maintainability, or compatibility. Users should review, test, and modify the code before using it in their own workflows.

## Local toolbox

### 统一客户端导航与全局设置

各模块页面左侧共用一条导航栏（窄屏时改为底部导航）：**精读**（`/reader`）、**改稿**（`/paper-revision`）、**Prompt**（`/prompts`），以及“更多”中的工具箱（`/`）、研究空白地图（`/research-gaps`）和 ResearchStudio（`/researchstudio`），底部是**设置**（`/settings`）。

全局设置已独立为 `/settings` 页面（标签页地址如 `/settings#codex`）。在任一模块中点击“AI 设置”或“管理全局配置”之类的链接，会在当前页右侧以抽屉形式打开设置，不必离开正在阅读或改稿的页面。保存后关闭抽屉，AI 引擎相关的改动（登录、默认模型、API 预设、Google Key）会在精读和改稿页面直接生效，其他改动会提示刷新当前页面。旧的 `/#settings/<tab>` 与 `/#llm-settings` 链接会自动转到新地址。

### Prompt 库统一与送到 ChatGPT

- **精读划选动作进入 Prompt 库**：精读页选区菜单里的 10 个动作（翻译选区、解释公式、Three-Pass 解读等）现在是 Prompt 工作台里的内置模板，分类为“精读划选动作”。可以在 `/prompts` 修改名称和内容，精读页的按钮与 AI 请求会直接使用新内容；“恢复默认”可撤销修改。修改保存在本机的 `reader_actions.json`，内置模板不能删除。选区菜单的“编辑动作”链接会直接打开这个分类。
- **从精读、改稿送到 ChatGPT**：ChatGPT 桥接扩展只在 `/prompts` 页面运行，所以其他页面会把内容交给 Prompt 工作台（`static/prompt_handoff.js`），在新标签页中打开 `/prompts` 并自动带入：
  - 精读选区菜单的“送到 ChatGPT（经 Prompt 工作台）”：带入选区、相邻原文和补充问题；PDF 文献会同时附上原文 PDF。
  - 改稿批注面板的“送到 ChatGPT”：带入本轮全部批注（引用原文、批注内容和修改范围），并附上当前版本的英文 LaTeX 原稿 ZIP。
  - 带入后可以继续追加模板、增删附件，检查后再点“提交到 ChatGPT”。交接内容只保存在本机浏览器，10 分钟内有效，只能带入一次；附件只会从本机工具的 `/api/` 地址读取。
- 左侧导航栏已覆盖各模块，因此去掉了工具箱侧栏里重复的模块入口，以及精读、Prompt、改稿、ResearchStudio 页头里的“返回工具箱”和互相跳转的链接。

### 共享引擎层与 PDF 核心

- **统一引擎状态**：`GET /api/engines`（`?refresh=1` 强制重查）一次返回 Claude Code、Codex、Google 翻译和全部 API 预设的状态，格式统一：`ready`、`error`、可选模型与推理强度、对应的设置标签页；不包含任何 API Key。实现在 `engines.py`，任一引擎查询失败不会影响其他引擎。
- **共享引擎选择器**：`static/engine_picker.js`（`window.EnginePicker`）提供“引擎 + 模型 + 推理强度”选择控件、未就绪提示和计费说明。双语改稿的工作流编辑器（页面和 全局设置 → 双语改稿）都基于它；设置抽屉保存后，它通知各页面刷新引擎状态。
- **PDF 核心**：`pdf_core.py` 统一处理 PDF 上传与下载校验（开头 1 KB 内须有 `%PDF-` 标记，所有入口统一上限 100 MB，写入是原子的，失败时不留残余文件）、内容指纹、元数据标题与作者，以及文字层提取。精读、工具箱 PDF OCR、研究空白地图和 ResearchStudio 共用这些逻辑。研究空白地图下载开放获取 PDF 的上限从 50 MB 提高到 100 MB；精读上传 PDF 现在也会校验文件内容和大小。

### 双语论文改稿

侧栏的“**双语论文改稿**”（`/paper-revision`）提供 LaTeX 英文原文与中文译文的左右对照，支持反复批注、修订与重新翻译：

1. 选择或拖入 UTF-8 `.tex` / `.bib` 文件或项目目录，目录导入保留相对路径。也可粘贴本机目录/主 `.tex` 的绝对路径或直接粘贴 LaTeX。主文件路径会自动读取同一项目目录内显式引用的 `\input{...}`、`\include{...}`、`\bibliography{...}`、`\addbibresource{...}` 依赖；缺失或越出目录时明确报错。路径入口只读，浏览器拖拽文件本身不会提供绝对路径。保存原稿后生成中文对照，使用全局 Codex 默认模型和套餐额度，或已保存的 API 预设（独立计费），提交前需勾选使用确认。
2. 在中文侧圈选文字，填写 comment 并保存。支持跨句、跨段选区与重叠批注；点击句子会联动高亮英文。已保存批注持久化在本机，刷新后仍保留。
3. 标注完成后统一提交。模型使用全文上下文，默认从对应句子开始，最多扩到选中段落及同一小节的相邻段落；也可为每条批注限定“仅选中段落”，或授权“所在章节 / 小节”“全文”。修改范围由服务端检查，每处修改必须关联批注并说明原因。长文使用下述分层上下文，读取背景与允许写入的范围独立控制。
4. 英文修改与更新中文全部通过校验后才保存新版本，随后继续下一轮。批注绑定版本号、段落与句子单元 ID、Unicode 字符位置及选中文字；旧版本的定位不会自动套用到新版本。冲突批注、越界改写、译文缺失等均保留上一版本与本轮批注，便于调整后重新提交。
5. 查看历史中的英文修改前后对照、批注处理说明、上下文模式和模型一致性检查，或恢复旧稿为一个新版本。导出 ZIP 包含导入路径下的 `.tex` 与原样保留的 `.bib`、相对于原稿的 patch、双语对应关系、全文背景记录及历史记录。

首次翻译前，系统分批读取全部已导入正文（包括受保护的公式/表格源码），提取研究问题、方法、结果、局限、论证线索和中英术语。每条背景记录附带源片段 ID 与原文引用，服务端校验引用确实存在；这只校验出处，不证明模型的概括正确。页面“全文背景、术语与导入检查”可查看这些记录、术语冲突和缺失依赖。`.bib` 按引用 key 提供对应条目，明确标记为文献元数据，不代表读过被引用论文全文；BibTeX 宏不执行，无法解析的条目会提示，原文件仍保留供导出。

每批中文翻译共用全文背景和术语，并附带当前完整段落及相邻英文段落。已采用的术语译法在后续版本继续沿用，模型提出其他译法时会显示冲突，避免未改段落与新译段落之间漂移。修订请求较小时直接包含全部正文；较大时使用“全文背景 + 允许修改的原文 + 相邻段落”，将广泛修改拆成不重叠的写入批次，并汇总全部批注结果。模型随后检查修改与全文论证、科学措辞和相邻段落是否一致；任何未解决问题都会阻止整轮写入。该检查是模型审查，不是科学结论验证。修订后重新生成受影响的背景记录，复用未改变的记录，再翻译新英文，所有记录与版本一起保存。

这里显示的是从 LaTeX 源码提取的正文对照，并非编译后的 PDF。段落与保守切句结果建立稳定对应；不允许模型增删、移动或合并段落。公式、数字、引用、标签、命令和宏参数使用占位符逐项保护，未知宏、表格、图片、复杂环境整体保持原样，其内部文字暂不改写；常见正文环境和文字样式支持提取。原文件不覆盖，不执行 LaTeX 编译，也不自动回写 Overleaf。导出的 `.tex` 需放回原工程的副本，配合原图片、`.bib`、`.cls` 等文件编译核查；内容正确性仍需人工审阅。

项目保存于 `.runtime/paper-revision/`。每次最多 100 份源文件、2 MB，其中 `.tex` 最多 80 份、正文合计 50 万字符。本地目录读取会跳过隐藏目录、依赖缓存及目录链接，不读取项目边界外的文件，不支持网络共享路径。单次模型输入保留 18 万字符上限；分层后如仍存在超长单段或过大的背景/文献条目，会明确失败，不会静默截断。初次翻译按小批次进行，后续只重新翻译修改过的段落；全文背景提取与一致性检查会增加模型调用。停止会立即禁止本轮结果写入，已发出的模型请求可能继续消耗额度至返回；服务重启后不会自动重跑任务。该工作台按单服务进程使用。

验证（模拟模型，不消耗额度）：

```powershell
python -m unittest discover -s tests -p test_paper_revision.py
node --check static/paper_revision.js
node tests/test_paper_revision_import.js
python tests/paper_revision_browser_harness.py
```

浏览器验收夹具访问 `http://127.0.0.1:8793/paper-revision`，使用独立临时目录与模拟模型，启动时输出可测试的主文件路径。`/paper-revision-drag-fixture` 可投递带实际 File 对象的测试拖放事件；目录条目分页、嵌套路径、多目录及大小限制由 Node 夹具验证。

### 启动本地工具箱

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

### Prompt 工作台

从首页侧栏打开“Prompt 工作台”（`/prompts`），创建、编辑、分类和搜索常用 Prompt 模板。点击模板会将完整文字按点击顺序追加到可编辑文本框，同一模板在本次草稿中不会重复添加。手动删除某段模板文字后，对应按钮会恢复可点击，再次添加会追加到末尾；仅修改其中部分文字时仍保留已添加状态。清空文字后可以重新选择；已有草稿不会随模板编辑而被覆盖。模板库保存在 `.runtime/prompt-workbench/templates.json`，文字与已选顺序自动保存在当前浏览器。

模板按分类分组展示。“三问学习法”单独包含“核心思维模型”“专家根本分歧”“理解诊断问题”三个中文提示词，正文来自引用的“调研人物与提示词”对话。可通过分类筛选只显示这一组，再按需要依次添加并补充具体领域或学科。

支持选择或拖拽多个附件、按文件名/大小/修改时间去重以及单独移除；本工具每次最多 20 个、合计 100 MB，ChatGPT 的实际限制仍以网页和账户为准。刷新页面后需重新选择附件。

“提交到 ChatGPT”使用配套 Chrome / Edge 扩展 `browser_extension/prompt_bridge/`：首次在工作台下载 ZIP 并解压，在 `chrome://extensions` 或 `edge://extensions` 开启开发者模式，加载包含 `manifest.json` 的文件夹，再刷新工作台。工作台与 ChatGPT 必须在同一浏览器中打开；支持 `localhost` 和 `127.0.0.1` 的任意端口。

提交后扩展新开 ChatGPT 标签页，使用已有登录状态填入文字、添加附件，并把进度或失败原因显示在两侧。请检查文字、附件卡片及上传处理结果后在 ChatGPT 中点击发送。扩展不需要 API Key；提交凭证不出现在网址中。临时附件在交接结束时清理，未结束的传输在一小时后的下一次工作台访问时清理。ChatGPT 页面结构变化可能需要更新扩展；识别失败时可复制文字并手动补传附件。

验证：`python -m unittest discover -s tests -p test_prompt_workbench.py`、`node tests/test_prompts.js` 和 `node tests/test_prompt_bridge.js`。
浏览器交接夹具：运行 `python tests/prompt_browser_harness.py` 后访问 `http://127.0.0.1:8792/fixture?case=textarea`；`case` 还支持 `contenteditable`、`paste`、`existing`、`filename`。该夹具在真实浏览器中验证生产内容脚本的文字、附件字节和已有草稿保护，完全使用本地模拟页面，不向 ChatGPT 发送数据。

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
- 阅读器右侧的“AI 三遍阅读”可选择两种后端：默认的本机 `codex app-server` 使用 ChatGPT/Codex 登录额度，也可选择全局 LLM 预设直接调用 API（OpenAI 官方地址自动使用 Responses API，其他兼容服务默认使用 Chat Completions）。两种后端都只通读论文一次，在同一次回答里同时填写 Pass 1（鸟瞰与 Five Cs）、Pass 2（主张与证据）、Pass 3（按关注点的虚拟复现）和综合报告——三个 Pass 指的是每部分要提取哪类信息，而不是读三遍；每部分仍按各自的篇幅设置，结果仍分别保存为 `pass1.md`、`pass2.md`、`pass3.md` 和 `report.md`，并支持追问。Codex 后端整篇分析只用一个 turn。API 模式会明确要求确认独立计费；论文放得下时只调用一次，超出安全上下文时按页码和标题自适应分块，每块读一次并同时为所有部分记笔记，最后一次调用汇总填写，导入前会确认预计调用次数（约为块数的两倍）。Codex 可在阅读器内通过浏览器或设备码登录，也可在终端运行 `codex login`。原生 PDF 必须具有足够文字层，扫描件请在导入时开启 OCR。
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
