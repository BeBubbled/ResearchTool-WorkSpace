const state = { tools: [], active: null, view: "tool", files: [], anki: null, captions: { titles: [], captions: [] }, gridLabels: { rowTitles: [], colTitles: [], captions: [] }, poller: null, currentJob: null, translationSource: "", llmPreset: "" };
const nav = document.querySelector("#toolNav");
const form = document.querySelector("#toolForm");
const toolSearch = document.querySelector("#toolSearch");
const title = document.querySelector("#toolTitle");
const category = document.querySelector("#toolCategory");
const description = document.querySelector("#toolDescription");
const availability = document.querySelector("#availability");
const taskPanel = document.querySelector("#taskPanel");
const taskStatus = document.querySelector("#taskStatus");
const taskLog = document.querySelector("#taskLog");
const downloadLink = document.querySelector("#downloadLink");
const githubCommitLink = document.querySelector("#githubCommitLink");
const translationDebugLink = document.querySelector("#translationDebugLink");
const retryTranslation = document.querySelector("#retryTranslation");
const readerImportPanel = document.querySelector("#readerImportPanel");
const toolCount = document.querySelector("#toolCount");

const escapeHtml = (value) => String(value).replace(/[&<>'"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[c]));
const optionField = (key, label, value, type = "number", extra = "") => `<div class="field"><label>${label}</label><input data-option="${key}" type="${type}" value="${escapeHtml(value)}" ${extra}></div>`;
const selectField = (key, label, value, values) => `<div class="field"><label>${label}</label><select data-option="${key}">${values.map(([v, text]) => `<option value="${v}" ${v === value ? "selected" : ""}>${text}</option>`).join("")}</select></div>`;

async function init() {
  const legacyTab = legacySettingsTab();
  if (legacyTab) { location.replace(`/settings#${encodeURIComponent(legacyTab)}`); return; }
  const response = await fetch("/api/tools");
  const data = await response.json();
  state.tools = data.tools;
  toolCount.textContent = String(state.tools.length);
  state.active = state.tools.find(tool => tool.id === "pdf_ocr_translate") || state.tools[0];
  toolSearch.addEventListener("input", renderNav);
  toolSearch.addEventListener("keydown", event => {
    if (event.key === "Escape") { toolSearch.value = ""; renderNav(); }
    if (event.key === "Enter") nav.querySelector("[data-tool]")?.click();
  });
  document.addEventListener("keydown", event => {
    if (event.key !== "/" || event.metaKey || event.ctrlKey || event.altKey) return;
    if (event.target.closest?.("input, textarea, select, [contenteditable='true']")) return;
    event.preventDefault(); toolSearch.focus(); toolSearch.select();
  });
  window.addEventListener("hashchange", () => { const tab = legacySettingsTab(); if (tab) openLlmSettings(tab); });
  renderNav(); renderTool();
}

// Pages that live beside the tools but have their own route (shown as links in the list).
const TOOLBOX_LINKS = [{ href: "/health", title: "睡眠整理", category: "生活与健康", description: "整理 Apple 健康导出的睡眠数据，只在此浏览器处理。" }];

function highlightMatch(text, query) {
  const value = escapeHtml(text);
  if (!query) return value;
  const index = text.toLowerCase().indexOf(query);
  if (index < 0) return value;
  return `${escapeHtml(text.slice(0, index))}<mark>${escapeHtml(text.slice(index, index + query.length))}</mark>${escapeHtml(text.slice(index + query.length))}`;
}

// Tool list: search matches title, description and category (DevUtils-style source list).
function renderNav() {
  const query = toolSearch.value.trim().toLowerCase();
  const matches = item => !query || [item.title, item.description, item.category].some(text => String(text || "").toLowerCase().includes(query));
  const groups = new Map();
  const add = (category, html) => { if (!groups.has(category)) groups.set(category, []); groups.get(category).push(html); };
  state.tools.filter(matches).forEach(tool => {
    const active = state.view === "tool" && tool.id === state.active.id;
    add(tool.category, `<button type="button" class="tool-card ${active ? "active" : ""} ${tool.available ? "" : "unavailable"}" data-tool="${tool.id}" title="${escapeHtml(tool.available ? tool.description : tool.unavailableReason || tool.description)}" ${active ? 'aria-current="page"' : ""}>${highlightMatch(tool.title, query)}${tool.available ? "" : '<span class="tool-card-flag">需配置</span>'}</button>`);
  });
  TOOLBOX_LINKS.filter(matches).forEach(link => add(link.category, `<a class="tool-card" href="${link.href}" title="${escapeHtml(link.description)}">${highlightMatch(link.title, query)}<span class="tool-card-flag">↗</span></a>`));
  nav.innerHTML = groups.size
    ? [...groups].map(([group, items]) => `<div class="nav-category">${escapeHtml(group)}</div>${items.join("")}`).join("")
    : `<p class="tool-nav-empty">没有匹配“${escapeHtml(toolSearch.value.trim())}”的工具。</p>`;
  nav.querySelectorAll("[data-tool]").forEach(button => button.addEventListener("click", () => {
    state.view = "tool"; state.active = state.tools.find(tool => tool.id === button.dataset.tool); state.files = []; state.anki = null; state.captions = { titles: [], captions: [] }; state.gridLabels = { rowTitles: [], colTitles: [], captions: [] }; state.currentJob = null; state.translationSource = ""; state.llmPreset = ""; stopPolling();
    if (location.hash) history.replaceState(null, "", location.pathname + location.search);
    renderNav(); renderTool();
  }));
}

function renderTool() {
  const tool = state.active;
  state.view = "tool";
  taskPanel.classList.remove("hidden");
  downloadLink.classList.add("hidden");
  githubCommitLink.classList.add("hidden");
  translationDebugLink.classList.add("hidden");
  retryTranslation.classList.add("hidden");
  readerImportPanel.classList.add("hidden");
  title.textContent = tool.title; category.textContent = tool.category; description.textContent = tool.description;
  availability.textContent = tool.available ? "环境就绪" : "需配置或依赖"; availability.className = `badge ${tool.available ? "ok" : "bad"}`;
  const upload = document.querySelector("#uploadTemplate").content.cloneNode(true);
  form.innerHTML = ""; form.append(upload);
  if (tool.id === "markdown_github") {
    form.querySelector(".upload-section .section-heading h3").textContent = "Markdown 与图片目录";
    form.querySelector(".upload-section .section-heading .hint").textContent = "恰好一份 .md/.mmd；图片目录结构必须与文档引用一致";
    form.querySelector("[data-dropzone] > strong").textContent = "拖入 Markdown 与图片目录";
    form.querySelector(".upload-section > .hint").textContent = "请选择包含文档与图片的共同目录，或分别添加文档和与引用一致的图片目录；原文件不会被修改。";
  }
  if (!tool.available) form.insertAdjacentHTML("afterbegin", `<p class="message">${escapeHtml(tool.unavailableReason)}</p>`);
  if (tool.id === "bibtex") form.insertAdjacentHTML("beforeend", `<div class="info-box">此工具会通过 scholarly 查询 Google Scholar。查询可能受网络或来源限流影响。</div><div class="field wide"><label>或直接粘贴论文标题（每行一个；没有选择 TXT 时将自动生成任务文件）</label><textarea id="bibtexText" placeholder="Paper title one\nPaper title two"></textarea></div>`);
  form.insertAdjacentHTML("beforeend", optionsHtml(tool.id));
  form.insertAdjacentHTML("beforeend", `<div class="submit-row"><button type="button" class="primary" id="submitJob" ${tool.available ? "" : "disabled"}>加入处理队列</button></div>`);
  if (tool.id === "pdf_ocr_translate") form.insertAdjacentHTML("beforeend", `<section id="ocrArtifacts" class="ocr-artifacts hidden"></section>`);
  bindUpload(); bindDynamicOptions(); renderFileList();
  document.querySelector("#submitJob").addEventListener("click", submitJob);
}

// 全局设置 now lives at /settings (static/settings.js); old "#settings/<tab>" links are forwarded there.
function legacySettingsTab() {
  if (location.hash === "#llm-settings") return "llm";
  return location.hash.startsWith("#settings/") ? location.hash.slice("#settings/".length) || "llm" : null;
}

function openLlmSettings(tab = "llm") {
  const target = typeof tab === "string" ? tab : "llm";
  if (window.ClientShell) window.ClientShell.openSettings(target);
  else location.href = `/settings#${encodeURIComponent(target)}`;
}

function optionsHtml(id) {
  const gridStart = `<section><div class="section-heading"><h3>处理设置</h3><span class="hint">常用参数</span></div><div class="options">`;
  let fields = "";
  if (id === "anki") fields = `<div class="field"><label>正面 Sheet</label><select id="frontSheet"></select></div><div class="field"><label>正面列</label><select id="frontColumn"></select></div><div class="field"><label>背面 Sheet</label><select id="backSheet"></select></div><div class="field"><label>背面列</label><select id="backColumn"></select></div>`;
  if (id === "image_crop") fields = optionField("crop", "裁剪尺寸 (px)", 256) + optionField("out", "输出尺寸 (px)", 512);
  if (id === "video_crop") fields = optionField("crop", "裁剪尺寸 (px)", 256) + optionField("out", "输出尺寸 (px)", 512) + `<details class="advanced wide"><summary>高级设置</summary><div class="options">${optionField("offsetY", "垂直偏移 (px)", -80)}${optionField("crf", "视频质量 CRF (0–51)", 18)}</div></details>`;
  if (id === "image_ppt" || id === "video_ppt") fields = optionField("rows", "行数", 3) + optionField("cols", "列数", 5) + optionField("cellSize", "单元格尺寸 (cm)", 5) + optionField("gap", "间距 (px)", 4) + optionField("margin", "边距 (cm)", 1) + selectField("fit", "图片适配", "fit", [["fit","完整显示"],["fill","裁切填满"]]) + (id === "video_ppt" ? `<div class="field wide"><label>每个视频提取的帧序号（逗号或空格分隔）</label><input data-option="frameIndexes" value="0" placeholder="0, 3, 5, 7"><p class="hint">帧按视频顺序依次填入网格；帧数等于列数时，每行恰好对应一个视频。</p></div>` : "") + `<div class="field wide"><label>行标题 / 列标题 / 每格说明</label><p class="hint">全部留空则不预留标题区域。行标题位于每行左侧，列标题位于每列上方，说明位于每格下方。 <button type="button" class="inline-link" data-fill-grid-labels>${id === "video_ppt" ? "用视频工作名填充行标题" : "用图片工作名填充每格说明"}</button></p><div id="gridLabelFields"></div></div><details class="advanced wide"><summary>高级设置</summary><div class="options">${optionField("cellWidth", "单元格宽 (cm，留空为正方形)", "", "number", 'step="any" min="0"')}${optionField("cellHeight", "单元格高 (cm，留空为正方形)", "", "number", 'step="any" min="0"')}${optionField("rowTitleWidth", "行标题宽度 (cm)", 2.5, "number", 'step="any" min="0"')}${optionField("colTitleHeight", "列标题高度 (cm)", 1, "number", 'step="any" min="0"')}${optionField("captionHeight", "说明高度 (cm)", 0.8, "number", 'step="any" min="0"')}${optionField("titleFont", "标题字号 (pt)", 14, "number", 'step="any" min="1"')}${optionField("captionFont", "说明字号 (pt)", 11, "number", 'step="any" min="1"')}${optionField("fontName", "字体（留空为主题字体）", "", "text", 'placeholder="例如 Arial、微软雅黑"')}</div></details>`;
  if (id === "stack_images") fields = selectField("direction", "排列方向", "horizontal", [["horizontal","横向"],["vertical","纵向"]]) + optionField("gap", "间距 (px)", 5) + optionField("border", "边框 (pt)", 1);
  if (id === "stack_videos") fields = optionField("rows", "行数", 1) + optionField("cols", "列数", 1) + selectField("mode", "标题布局", "h", [["h","按行说明"],["v","按列说明"]]) + `<div class="field wide"><label>标题与说明</label><div id="captionFields" class="caption-grid"></div></div><details class="advanced wide"><summary>高级设置</summary><div class="options">${optionField("gap", "间距 (px)", 5)}${optionField("outerBorder", "外边距 (px)", 5)}${optionField("titleBand", "标题带高度 (px)", 40)}${optionField("captionBand", "说明带高度 (px)", 150)}${optionField("titleFont", "标题字号", 26)}${optionField("captionFont", "说明字号", 30)}${selectField("audio", "保留音频", "first", [["first","第一条视频"],["none","不保留"]])}</div></details>`;
  if (id === "document_translate") fields = llmConfigurationHtml();
  if (id === "markdown_repair") fields = `<div class="field wide"><p class="hint">会自动将含空格但未用尖括号包裹的本地图片路径（如 <code>![图](Conceptual Framework.png)</code>）修复为合法 Markdown；代码和带标题的图片引用保持不变。</p></div><div class="field wide"><label class="format-options"><span><input id="mathRepair" type="checkbox" checked> 修复并校验行间公式 <code>$$</code></span></label><p class="hint">无需 LLM；会跳过围栏代码和行内公式，只自动修复高置信度的公式边界问题。</p></div><div class="field wide"><label class="format-options"><span><input id="footnoteRepair" type="checkbox" checked> 修复 OCR 合并脚注</span></label><p class="hint">无需 LLM；仅在正文上标与合并脚注定义可确定对应时拆分，歧义内容不会猜测修改。</p></div><div class="field wide"><label class="format-options"><span><input id="normalizeChinesePunctuation" type="checkbox"> 中文标点转英文符号</span></label><p class="hint">将中文逗号、句号、引号、括号等转为英文符号；代码、公式、链接与 URL 保持不变。</p></div><div class="field wide"><label class="format-options"><span><input id="deepRepair" type="checkbox"> 深度结构修复（OCR Markdown）</span></label><p class="hint">需要 LLM；修复围栏、脚注、公式环境及含公式的伪代码。仅在需要处理 OCR 结构异常时开启。</p></div><div id="standaloneRepairLlmFields" class="wide" hidden>${llmConfigurationHtml()}</div>`;
  if (id === "markdown_github") fields = `<div class="field wide github-tool-note"><strong>仅发布文档实际引用的本地图片</strong><p class="hint">支持 Markdown、MMD、Wiki 图片与内嵌 HTML 图片。远程/data/root URL 保持原样；任何本地图片缺失都会在访问 GitHub 前终止。</p><p class="hint">远端路径使用 &lt;图片根目录&gt;/YYYY/MM/DD/&lt;完整 SHA-256&gt;.&lt;扩展名&gt;，成功后下载不修改原文件的 <code>_github</code> 副本。</p><button type="button" class="inline-link" data-open-github-settings>管理 GitHub 配置</button></div>`;
  if (id === "pdf_ocr_translate") fields = `<div class="field wide"><label>原始 PDF 本地路径</label><div class="path-picker"><input data-option="localSourcePath" type="text" required placeholder="拖入 PDF 后自动识别，或点击“浏览…”"><button type="button" data-browse-source-pdf>浏览…</button></div><p class="hint" data-source-path-status>拖入 PDF 时会在下载、桌面、文档和最近使用的文件夹中按文件名、大小与修改时间自动定位；也可点击“浏览…”直接选择（会同时加入上传列表）。系统会在该 PDF 同级新建同名文件夹，先复制 PDF，再自动保存所有 OCR 与翻译结果。</p></div><div class="field wide"><label>OCR 导出格式</label><div class="format-options"><label><input type="checkbox" data-ocr-format="docx" checked> DOCX</label><label><input type="checkbox" data-ocr-format="md" checked> Markdown</label><label><input type="checkbox" data-ocr-format="html" checked> HTML</label><label><input type="checkbox" data-ocr-format="tex.zip" checked> LaTeX ZIP</label></div><p class="hint">MMD 与 lines.json 会始终导出。OCR 后可将 Markdown 或 MMD 交给 AI-Markdown-Translator 后端。</p></div><div class="field wide"><label class="format-options"><span><input id="repairMarkdown" type="checkbox" checked> 自动修复 Markdown/MMD 结构</span></label><p class="hint">图片本地化后保留 <code>_legacy</code> 原文，再修复代码围栏、脚注、公式环境和含公式的伪代码。</p></div><div id="repairLlmFields" class="wide">${llmConfigurationHtml()}</div>`;
  return `${gridStart}${fields}</div></section>`;
}

function llmConfigurationHtml() {
  const presets = state.active.llmPresets || [];
  if (!state.llmPreset || !presets.some(item => item.id === state.llmPreset)) state.llmPreset = presets[0]?.id || "";
  const options = presets.length
    ? presets.map(item => `<option value="${escapeHtml(item.id)}" ${item.id === state.llmPreset ? "selected" : ""}>${escapeHtml(item.name)} · ${escapeHtml(item.model)} · 并发 ${escapeHtml(item.concurrency || 1)} · ${escapeHtml(item.baseUrl)}</option>`).join("")
    : `<option value="">请先添加 LLM 配置</option>`;
  return `<div class="field wide"><label>LLM 配置</label><select id="llmPreset" ${presets.length ? "" : "disabled"}>${options}</select><p class="hint">模型、密钥与并发统一由全局 LLM 管理面板提供。 <button type="button" class="inline-link" data-open-llm-settings>管理全局配置</button></p></div>`;
}

function customLlmFieldsHtml(extraClass = "") { return `<div id="customLlmFields" class="custom-llm-fields ${extraClass}" ${state.llmPreset === "custom" ? "" : "hidden"}><div class="field"><label>配置名称</label><input id="customLlmName" placeholder="例如：临时代理"></div><div class="field"><label>Base URL</label><input id="customLlmBaseUrl" placeholder="https://api.example.com/v1"></div><div class="field"><label>API Key</label><input id="customLlmApiKey" type="password" autocomplete="off" placeholder="仅用于本次请求"></div><div class="field"><label>Model ID</label><input id="customLlmModel" placeholder="模型名称"></div><div class="field"><label>并发请求数</label><input id="customLlmConcurrency" type="number" min="1" max="64" step="1" value="1"><p class="hint">仅用于本次任务。</p></div></div>`; }

function bindUpload() {
  const fileInput = form.querySelector("[data-file-input]"); const folderInput = form.querySelector("[data-folder-input]"); const dropzone = form.querySelector("[data-dropzone]");
  fileInput.accept = state.active.accepts.join(","); folderInput.accept = state.active.accepts.join(",");
  form.querySelector("[data-pick-files]").addEventListener("click", () => fileInput.click()); form.querySelector("[data-pick-folder]").addEventListener("click", () => folderInput.click());
  fileInput.addEventListener("change", () => addFiles([...fileInput.files].map(file => ({ file, relativePath: file.name }))));
  folderInput.addEventListener("change", () => addFiles([...folderInput.files].map(file => ({ file, relativePath: file.webkitRelativePath || file.name }))));
  ["dragenter","dragover"].forEach(type => dropzone.addEventListener(type, event => { event.preventDefault(); dropzone.classList.add("dragover"); }));
  ["dragleave","drop"].forEach(type => dropzone.addEventListener(type, event => { event.preventDefault(); dropzone.classList.remove("dragover"); }));
  dropzone.addEventListener("drop", async event => addFiles(await droppedFiles(event.dataTransfer)));
}

async function droppedFiles(dataTransfer) {
  const entries = [...dataTransfer.items].map(item => item.webkitGetAsEntry && item.webkitGetAsEntry()).filter(Boolean);
  if (!entries.length) return [...dataTransfer.files].map(file => ({ file, relativePath: file.name }));
  const result = [];
  async function read(entry, prefix = "") {
    if (entry.isFile) { const file = await new Promise(resolve => entry.file(resolve)); result.push({ file, relativePath: `${prefix}${file.name}` }); return; }
    const reader = entry.createReader(); let entries = [];
    do { const batch = await new Promise(resolve => reader.readEntries(resolve)); entries = entries.concat(batch); if (!batch.length) break; } while (true);
    await Promise.all(entries.map(child => read(child, `${prefix}${entry.name}/`)));
  }
  await Promise.all(entries.map(entry => read(entry))); return result;
}

function addFiles(items) {
  const allowed = new Set(state.active.accepts.map(value => value.toLowerCase()));
  const accepted = items.filter(item => allowed.has(`.${item.file.name.split(".").pop().toLowerCase()}`));
  state.files.push(...accepted.map(item => ({ ...item, workName: item.file.name.replace(/\.[^.]+$/, "") })));
  if (state.active.maxFiles === 1 && state.files.length > 1) state.files = state.files.slice(-1);
  renderFileList(); if (state.active.id === "anki") inspectAnki(); if (state.active.id === "stack_videos") renderCaptions();
  if (state.active.id === "pdf_ocr_translate" && accepted.length && !accepted[accepted.length - 1].localPath) locateSourcePdf(accepted[accepted.length - 1].file);
}

function sourcePathControls() {
  return { input: form.querySelector('[data-option="localSourcePath"]'), status: form.querySelector("[data-source-path-status]") };
}

async function locateSourcePdf(file) {
  const { input, status } = sourcePathControls(); if (!input) return;
  status.className = "hint"; status.textContent = `正在本机查找 ${file.name}…`;
  try {
    const result = await LocalPaths.locate(file);
    if (state.files[state.files.length - 1]?.file !== file) return;
    if (result.path) { input.value = result.path; status.className = "hint ok"; status.textContent = `已自动定位：${result.path}`; return; }
    status.className = "hint bad";
    status.textContent = result.candidates.length
      ? `找到 ${result.candidates.length} 个可能的同名文件，无法确定是哪一个，请点击“浏览…”选择：${result.candidates.join("；")}`
      : `未在常用文件夹${result.timedOut ? "（搜索已超时）" : ""}中找到 ${file.name}，请点击“浏览…”选择。`;
  } catch (error) { status.className = "hint bad"; status.textContent = `自动定位失败：${error.message}，请点击“浏览…”选择。`; }
}

async function browseSourcePdf() {
  const { input, status } = sourcePathControls();
  status.className = "hint"; status.textContent = "请在弹出的系统窗口中选择 PDF…";
  try {
    const result = await LocalPaths.pick({ kind: "file", title: "选择原始 PDF", suffixes: [".pdf"] });
    if (!result) { status.textContent = "已取消选择。"; return; }
    const current = state.files[state.files.length - 1];
    if (!current || current.file.name !== result.name || current.file.size !== result.size) {
      status.textContent = "正在读取所选 PDF…";
      const file = await LocalPaths.fetchPicked(result);
      addFiles([{ file, relativePath: file.name, localPath: result.path }]);
    }
    input.value = result.path; status.className = "hint ok"; status.textContent = `已选择：${result.path}`;
  } catch (error) { status.className = "hint bad"; status.textContent = error.message; }
}

function renderFileList() {
  const list = form.querySelector("[data-file-list]"); if (!list) return;
  const preserveNames = state.active.id === "markdown_github";
  list.innerHTML = state.files.map((item, index) => `<li class="file-row ${preserveNames ? "preserved-file-row" : ""}" draggable="true" data-index="${index}"><span class="handle">⠿</span><span class="file-path" title="${escapeHtml(item.relativePath)}">${escapeHtml(item.relativePath)}</span>${preserveNames ? `<span class="file-kind">${/\.(?:md|mmd)$/i.test(item.file.name) ? "Markdown" : "图片"}</span>` : `<input class="file-name" data-name-index="${index}" value="${escapeHtml(item.workName)}" aria-label="任务工作名">`}<button type="button" class="remove-file" data-remove="${index}">移除</button></li>`).join("") || `<li class="hint">尚未添加符合此工具要求的文件。</li>`;
  list.querySelectorAll("[data-remove]").forEach(button => button.addEventListener("click", () => { state.files.splice(Number(button.dataset.remove), 1); renderFileList(); if (state.active.id === "stack_videos") renderCaptions(); }));
  list.querySelectorAll("[data-name-index]").forEach(input => input.addEventListener("input", () => { state.files[Number(input.dataset.nameIndex)].workName = input.value; if (state.active.id === "stack_videos") renderCaptions(); }));
  let dragged = null; list.querySelectorAll(".file-row").forEach(row => { row.addEventListener("dragstart", () => { dragged = Number(row.dataset.index); row.classList.add("dragging"); }); row.addEventListener("dragend", () => row.classList.remove("dragging")); row.addEventListener("dragover", event => event.preventDefault()); row.addEventListener("drop", event => { event.preventDefault(); const target = Number(row.dataset.index); if (dragged !== null && dragged !== target) { const [item] = state.files.splice(dragged, 1); state.files.splice(target, 0, item); renderFileList(); if (state.active.id === "stack_videos") renderCaptions(); } }); });
}

function bindDynamicOptions() {
  if (state.active.id === "anki") return;
  if (["document_translate", "pdf_ocr_translate", "markdown_repair"].includes(state.active.id)) bindLlmControls(form);
  if (state.active.id === "pdf_ocr_translate") {
    const toggle = form.querySelector("#repairMarkdown");
    const fields = form.querySelector("#repairLlmFields");
    const update = () => fields?.toggleAttribute("hidden", !toggle?.checked);
    toggle?.addEventListener("change", update); update();
    form.querySelector("[data-browse-source-pdf]")?.addEventListener("click", browseSourcePdf);
  }
  if (state.active.id === "markdown_repair") {
    const toggle = form.querySelector("#deepRepair");
    const fields = form.querySelector("#standaloneRepairLlmFields");
    const update = () => fields?.toggleAttribute("hidden", !toggle?.checked);
    toggle?.addEventListener("change", update); update();
  }
  form.querySelector("[data-open-github-settings]")?.addEventListener("click", () => openLlmSettings("github"));
  form.querySelectorAll('[data-option="rows"],[data-option="cols"],[data-option="mode"]').forEach(input => input.addEventListener("change", renderCaptions));
  if (state.active.id === "stack_videos") renderCaptions();
  if (isGridPptTool()) {
    form.querySelectorAll('[data-option="rows"],[data-option="cols"]').forEach(input => input.addEventListener("change", renderGridLabels));
    form.querySelector("[data-fill-grid-labels]")?.addEventListener("click", fillGridLabelsFromFiles);
    renderGridLabels();
  }
}

function isGridPptTool() { return ["image_ppt", "video_ppt"].includes(state.active?.id); }

function gridDimensions() {
  const read = key => Math.min(100, Math.max(1, Number(form.querySelector(`[data-option="${key}"]`)?.value || 1)));
  return { rows: read("rows"), cols: read("cols") };
}

function renderGridLabels() {
  const target = form.querySelector("#gridLabelFields"); if (!target) return;
  const { rows, cols } = gridDimensions(); const cells = Math.min(rows * cols, 400);
  const labels = state.gridLabels;
  const resize = (values, count) => Array.from({ length: count }, (_, index) => values[index] ?? "");
  labels.rowTitles = resize(labels.rowTitles, rows); labels.colTitles = resize(labels.colTitles, cols); labels.captions = resize(labels.captions, cells);
  const group = (title, key, values, label) => `<p class="hint"><strong>${title}</strong></p><div class="caption-grid">${values.map((value, index) => `<div class="field"><label>${label(index)}</label><input data-grid-label="${key}" data-index="${index}" value="${escapeHtml(value)}"></div>`).join("")}</div>`;
  target.innerHTML = group("列标题", "colTitles", labels.colTitles, index => `列 ${index + 1}`) + group("行标题", "rowTitles", labels.rowTitles, index => `行 ${index + 1}`) + group("每格说明", "captions", labels.captions, index => `第 ${Math.floor(index / cols) + 1} 行 · 第 ${index % cols + 1} 列`);
  target.querySelectorAll("[data-grid-label]").forEach(input => input.addEventListener("input", () => { state.gridLabels[input.dataset.gridLabel][Number(input.dataset.index)] = input.value; }));
}

function fillGridLabelsFromFiles() {
  const names = state.files.map(item => item.workName || "");
  if (!names.length) return;
  const { rows, cols } = gridDimensions();
  if (state.active.id === "video_ppt") state.gridLabels.rowTitles = Array.from({ length: rows }, (_, index) => names[index] ?? "");
  else state.gridLabels.captions = Array.from({ length: Math.min(rows * cols, 400) }, (_, index) => names[index] ?? "");
  renderGridLabels();
}

function bindLlmControls(scope) {
  scope.querySelector("#llmPreset")?.addEventListener("change", event => { state.llmPreset = event.target.value; });
  scope.querySelector("[data-open-llm-settings]")?.addEventListener("click", () => openLlmSettings("llm"));
}

function renderCaptions() {
  const target = form.querySelector("#captionFields"); if (!target) return;
  const rows = Math.max(1, Number(form.querySelector('[data-option="rows"]')?.value || 1)); const cols = Math.max(1, Number(form.querySelector('[data-option="cols"]')?.value || 1)); const mode = form.querySelector('[data-option="mode"]')?.value || "h";
  const total = Math.min(rows * cols, 100); const capCount = Math.min(mode === "h" ? rows : cols, 50);
  const oldTitles = state.captions.titles; const oldCaps = state.captions.captions;
  state.captions.titles = Array.from({ length: total }, (_, index) => oldTitles[index] ?? state.files[index]?.workName ?? ""); state.captions.captions = Array.from({ length: capCount }, (_, index) => oldCaps[index] ?? "");
  target.innerHTML = state.captions.titles.map((value, index) => `<div class="field"><label>格 ${index + 1} 标题</label><input data-title="${index}" value="${escapeHtml(value)}"></div>`).join("") + state.captions.captions.map((value, index) => `<div class="field"><label>${mode === "h" ? "行" : "列"} ${index + 1} 说明</label><input data-caption="${index}" value="${escapeHtml(value)}"></div>`).join("");
  target.querySelectorAll("[data-title]").forEach(input => input.addEventListener("input", () => { state.captions.titles[Number(input.dataset.title)] = input.value; })); target.querySelectorAll("[data-caption]").forEach(input => input.addEventListener("input", () => { state.captions.captions[Number(input.dataset.caption)] = input.value; }));
}

async function inspectAnki() {
  if (state.files.length !== 1) return;
  const body = new FormData(); body.append("file", state.files[0].file, state.files[0].file.name);
  try { const response = await fetch("/api/inspect", { method:"POST", body }); const data = await response.json(); if (!response.ok) throw new Error(data.error); state.anki = data; fillAnkiSelects(); }
  catch (error) { state.anki = null; taskStatus.textContent = "表格读取失败"; taskStatus.className = "badge bad"; taskLog.textContent = error.message; }
}

function fillSelect(element, values, selected = "") { element.innerHTML = values.map((value, i) => `<option value="${escapeHtml(value)}" ${value === selected || (!selected && i === 0) ? "selected" : ""}>${escapeHtml(value)}</option>`).join(""); }
function fillAnkiSelects() {
  if (!state.anki) return; const hasSheets = state.anki.sheets.length > 0; const frontSheet = form.querySelector("#frontSheet"); const backSheet = form.querySelector("#backSheet");
  fillSelect(frontSheet, hasSheets ? state.anki.sheets : ["" ]); fillSelect(backSheet, hasSheets ? state.anki.sheets : ["" ]); fillSelect(form.querySelector("#frontColumn"), state.anki.columns); fillSelect(form.querySelector("#backColumn"), state.anki.columns.slice(1).concat(state.anki.columns.slice(0,1)));
  [frontSheet, backSheet].forEach(select => { select.disabled = !hasSheets; select.addEventListener("change", () => loadColumns(select === frontSheet ? "front" : "back")); });
}
async function loadColumns(side) { const select = form.querySelector(side === "front" ? "#frontSheet" : "#backSheet"); const target = form.querySelector(side === "front" ? "#frontColumn" : "#backColumn"); const response = await fetch("/api/columns", { method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({ token:state.anki.token, sheet:select.value }) }); const data = await response.json(); if (!response.ok) throw new Error(data.error); fillSelect(target, data.columns); }

function collectOptions() {
  const options = {}; form.querySelectorAll("[data-option]").forEach(element => { options[element.dataset.option] = element.value; });
  if (state.active.id === "anki") Object.assign(options, { frontSheet:form.querySelector("#frontSheet").value, backSheet:form.querySelector("#backSheet").value, front:form.querySelector("#frontColumn").value, back:form.querySelector("#backColumn").value });
  if (state.active.id === "stack_videos") Object.assign(options, { cellTitles:state.captions.titles, captions:state.captions.captions });
  if (isGridPptTool()) Object.assign(options, { rowTitles:state.gridLabels.rowTitles, colTitles:state.gridLabels.colTitles, cellCaptions:state.gridLabels.captions });
  if (state.active.id === "pdf_ocr_translate") {
    options.ocrFormats = [...form.querySelectorAll("[data-ocr-format]")].filter(input => input.checked).map(input => input.dataset.ocrFormat);
    options.repairMarkdown = Boolean(form.querySelector("#repairMarkdown")?.checked);
    if (options.repairMarkdown) options.repairLlm = selectedLlmConfiguration();
  }
  if (state.active.id === "markdown_repair") {
    options.deepRepair = Boolean(form.querySelector("#deepRepair")?.checked);
    options.mathRepair = Boolean(form.querySelector("#mathRepair")?.checked);
    options.footnoteRepair = Boolean(form.querySelector("#footnoteRepair")?.checked);
    options.normalizeChinesePunctuation = Boolean(form.querySelector("#normalizeChinesePunctuation")?.checked);
    if (options.deepRepair) options.repairLlm = selectedLlmConfiguration();
  }
  if (state.active.id === "document_translate") options.llm = selectedLlmConfiguration();
  return options;
}

function selectedLlmConfiguration() {
  const presetId = form.querySelector("#llmPreset")?.value;
  if (presetId === "custom") return { mode:"custom", name:form.querySelector("#customLlmName")?.value.trim(), baseUrl:form.querySelector("#customLlmBaseUrl")?.value.trim(), apiKey:form.querySelector("#customLlmApiKey")?.value.trim(), model:form.querySelector("#customLlmModel")?.value.trim(), concurrency:Number(form.querySelector("#customLlmConcurrency")?.value || 1) };
  return { mode:"preset", presetId };
}

async function submitJob() {
  let files = [...state.files];
  if (state.active.id === "bibtex" && !files.length) { const text = form.querySelector("#bibtexText")?.value.trim(); if (text) files = [{ file:new File([text], "paper_titles.txt", { type:"text/plain" }), relativePath:"paper_titles.txt", workName:"paper_titles" }]; }
  if (!files.length) return setTask("需要先添加文件。", "bad");
  if (state.active.id === "anki" && !state.anki) return setTask("请等待表格列读取完成。", "bad");
  if (state.active.id === "pdf_ocr_translate" && !form.querySelector('[data-option="localSourcePath"]')?.value.trim()) return setTask("请填写原始 PDF 的本地路径，或点击“浏览…”选择。", "bad");
  if (state.active.id === "markdown_github" && files.filter(item => /\.(?:md|mmd)$/i.test(item.file.name)).length !== 1) return setTask("请恰好添加一份 .md 或 .mmd 文档。", "bad");
  if (state.active.id === "document_translate" || (state.active.id === "pdf_ocr_translate" && form.querySelector("#repairMarkdown")?.checked) || (state.active.id === "markdown_repair" && form.querySelector("#deepRepair")?.checked)) {
    const llm = selectedLlmConfiguration();
    if (!llm.presetId) return setTask("请先在 LLM 管理面板中添加并选择一个配置。", "bad");
  }
  const body = new FormData(); body.append("tool", state.active.id); body.append("options", JSON.stringify(collectOptions())); body.append("manifest", JSON.stringify(files.map(item => ({ relativePath:item.relativePath, workName:item.workName })))); files.forEach(item => body.append("files", item.file, item.file.name));
  document.querySelector("#submitJob").disabled = true; setTask("正在提交任务…", ""); downloadLink.classList.add("hidden"); githubCommitLink.classList.add("hidden"); readerImportPanel.classList.add("hidden");
  try { const response = await fetch("/api/jobs", { method:"POST", body }); const data = await response.json(); if (!response.ok) throw new Error(data.error || "提交失败"); state.currentJob = data.id; state.translationSource = ""; setTask("任务已排队", ""); taskLog.textContent = "任务已提交，等待本地科研工作线程。"; translationDebugLink.classList.add("hidden"); startPolling(data.id); }
  catch (error) { setTask(error.message, "bad"); } finally { document.querySelector("#submitJob").disabled = false; }
}

function setTask(text, kind) { taskStatus.textContent = text; taskStatus.className = `badge ${kind}`; }
function stopPolling() { if (state.poller) { clearInterval(state.poller); state.poller = null; } }
function renderOcrArtifacts(data) {
  if (state.active?.id !== "pdf_ocr_translate") return;
  const target = form.querySelector("#ocrArtifacts"); if (!target) return;
  const artifacts = data.artifacts || [];
  if (!artifacts.length) { target.classList.add("hidden"); return; }
  target.classList.remove("hidden");
  const ready = (["completed", "completed_with_warnings"].includes(data.status) && ["ocr_complete", "translation_complete"].includes(data.phase)) || (data.status === "failed" && data.phase === "translation_postprocess_failed");
  const sourcePriority = item => (item.kind === "ocr_legacy" ? 10 : 0) + (item.name.toLowerCase().endsWith(".md") ? 0 : item.name.toLowerCase().endsWith(".mmd") ? 1 : 2);
  const sources = artifacts.filter(item => ["ocr", "ocr_legacy"].includes(item.kind) && item.translationSupported).sort((left, right) => sourcePriority(left) - sourcePriority(right));
  if (!state.translationSource || !sources.some(item => item.id === state.translationSource)) state.translationSource = sources[0]?.id || "";
  const presets = state.active.llmPresets || [];
  if (!state.llmPreset || !presets.some(item => item.id === state.llmPreset)) state.llmPreset = presets[0]?.id || "";
  const llmOptions = presets.length ? presets.map(item => `<option value="${escapeHtml(item.id)}" ${item.id === state.llmPreset ? "selected" : ""}>${escapeHtml(item.name)} · ${escapeHtml(item.model)} · 并发 ${escapeHtml(item.concurrency || 1)} · ${escapeHtml(item.baseUrl)}</option>`).join("") : `<option value="">请先添加 LLM 配置</option>`;
  target.innerHTML = `<div class="section-heading"><h3>OCR 输出</h3><span class="hint">下载或选择可翻译文件</span></div><div class="artifact-list">${artifacts.map(item => `<div class="artifact-row"><div><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml(item.format)}${item.translationSupported ? " · 可翻译" : " · 仅下载"}</small></div><a class="artifact-download" href="${escapeHtml(item.downloadUrl)}">下载</a></div>`).join("")}</div>${ready ? `<div class="translation-box"><label>翻译源文件<select id="translationSource">${sources.map(item => `<option value="${escapeHtml(item.id)}" ${item.id === state.translationSource ? "selected" : ""}>${escapeHtml(item.name)}</option>`).join("")}</select></label><label>LLM 配置<select id="llmPreset" ${presets.length ? "" : "disabled"}>${llmOptions}</select></label><p class="hint">模型、密钥与并发统一由全局 LLM 管理面板提供。 <button type="button" class="inline-link" data-open-llm-settings>管理全局配置</button></p><button type="button" class="primary" id="startTranslation" ${sources.length && presets.length ? "" : "disabled"}>翻译所选文件</button></div>` : `<p class="hint">OCR 完成后可在这里下载或选择 Markdown、MMD 或 HTML 进行翻译。</p>`}`;
  const select = target.querySelector("#translationSource"); if (select) select.addEventListener("change", () => { state.translationSource = select.value; });
  bindLlmControls(target);
  target.querySelector("#startTranslation")?.addEventListener("click", () => submitTranslation(data.id));
}

async function submitTranslation(jobId) {
  if (!state.translationSource) return setTask("没有可翻译的 OCR 文件。", "bad");
  const presetId = form.querySelector("#llmPreset")?.value;
  if (!presetId) return setTask("请先在 LLM 管理面板中添加并选择一个配置。", "bad");
  const llm = { mode:"preset", presetId };
  const button = form.querySelector("#startTranslation"); if (button) button.disabled = true;
  try { const response = await fetch(`/api/jobs/${jobId}/translations`, { method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({ artifactId:state.translationSource, llm }) }); const data = await response.json(); if (!response.ok) throw new Error(data.error || "翻译提交失败"); setTask("翻译已排队", ""); startPolling(data.id); }
  catch (error) { setTask(error.message, "bad"); if (button) button.disabled = false; }
}

async function retryDocumentTranslation(jobId) {
  const llm = selectedLlmConfiguration();
  if (!llm.presetId) return setTask("请先在 LLM 管理面板中添加并选择一个配置。", "bad");
  retryTranslation.disabled = true;
  try {
    const response = await fetch(`/api/jobs/${jobId}/translation-retry`, {
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({ llm }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "继续翻译提交失败");
    retryTranslation.classList.add("hidden");
    setTask("续传已排队", "");
    startPolling(data.id);
  } catch (error) {
    setTask(error.message, "bad");
    retryTranslation.disabled = false;
  }
}

function renderReaderImports(data) {
  const pairs = data.readerPairs || [];
  if (!pairs.length) {
    readerImportPanel.classList.add("hidden");
    readerImportPanel.innerHTML = "";
    return;
  }
  readerImportPanel.classList.remove("hidden");
  readerImportPanel.innerHTML = `
    <div class="section-heading">
      <h3>导入阅读器</h3>
      <span class="hint">保留原文 + 译文对照</span>
    </div>
    <div class="reader-pair-list">${pairs.map(pair => `
      <div class="reader-pair-row">
        <div>
          <strong>${escapeHtml(pair.title)}</strong>
          <small>${escapeHtml(pair.sourceName)} → ${escapeHtml(pair.translatedName)}</small>
        </div>
        <button type="button" data-import-reader="${escapeHtml(pair.id)}">导入并打开</button>
      </div>
    `).join("")}</div>`;
  readerImportPanel.querySelectorAll("[data-import-reader]").forEach(button => {
    button.addEventListener("click", () => importReaderPair(data.id, button.dataset.importReader, button));
  });
}

async function importReaderPair(jobId, pairId, button) {
  const originalText = button.textContent;
  button.disabled = true;
  button.textContent = "正在导入…";
  try {
    const response = await fetch(`/api/jobs/${jobId}/reader-imports`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pairId }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "无法导入阅读器。");
    button.textContent = "已导入";
    window.location.assign(data.readerUrl);
  } catch (error) {
    setTask(error.message, "bad");
    button.disabled = false;
    button.textContent = originalText;
  }
}

function startPolling(jobId) {
  stopPolling();
  retryTranslation.classList.add("hidden");
  retryTranslation.disabled = false;
  const poll = async () => {
    try {
      const response = await fetch(`/api/jobs/${jobId}`);
      const data = await response.json();
      if (!response.ok) throw new Error(data.error);
      const labels = {queued:"排队中",running:"处理中",completed:"已完成",completed_with_warnings:"完成（有警告）",failed:"失败"};
      const progress = data.translationProgress;
      const progressText = progress ? ` · 翻译 ${progress.completed}/${progress.total} · 并发 ${progress.active || 0}/${progress.concurrency || 1}` : "";
      const failedPartial = data.status === "failed" && data.phase === "translation_partial";
      const statusText = failedPartial ? `翻译失败，已保留 ${progress?.completed || 0}/${progress?.total || 0} · 并发 ${progress?.active || 0}/${progress?.concurrency || 1}` : `${labels[data.status] || data.status}${progressText}`;
      setTask(statusText, data.status === "completed" ? "ok" : data.status === "completed_with_warnings" ? "warn" : data.status === "failed" ? "bad" : "");
      taskLog.textContent = data.logs.join("\n");
      taskLog.scrollTop = taskLog.scrollHeight;
      if (data.downloadReady) {
        downloadLink.href = `/api/jobs/${jobId}/download`;
        downloadLink.textContent = `下载 ${data.downloadName}`;
        downloadLink.classList.remove("hidden");
      }
      if (data.publication?.commitUrl) {
        githubCommitLink.href = data.publication.commitUrl;
        githubCommitLink.textContent = data.publication.commitCreated === false ? "查看复用的 GitHub 版本" : "查看 GitHub 提交";
        githubCommitLink.classList.remove("hidden");
      } else {
        githubCommitLink.classList.add("hidden");
      }
      if (data.translationDebugUrl) {
        translationDebugLink.href = data.translationDebugUrl;
        translationDebugLink.textContent = `下载翻译诊断（${data.translationDebugCount} 条）`;
        translationDebugLink.classList.remove("hidden");
      } else {
        translationDebugLink.classList.add("hidden");
      }
      if (data.translationRetryAvailable) {
        retryTranslation.classList.remove("hidden");
        retryTranslation.onclick = () => retryDocumentTranslation(jobId);
      } else {
        retryTranslation.classList.add("hidden");
      }
      renderOcrArtifacts(data);
      renderReaderImports(data);
      if (["completed","completed_with_warnings","failed"].includes(data.status)) stopPolling();
    } catch (error) {
      setTask(error.message, "bad");
      stopPolling();
    }
  };
  poll();
  state.poller = setInterval(poll, 1000);
}

init().catch(error => { title.textContent = "无法加载工具"; description.textContent = error.message; });
