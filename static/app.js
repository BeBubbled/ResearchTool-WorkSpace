const state = { tools: [], active: null, files: [], anki: null, captions: { titles: [], captions: [] }, poller: null, currentJob: null, translationSource: "", llmPreset: "" };
const nav = document.querySelector("#toolNav");
const form = document.querySelector("#toolForm");
const title = document.querySelector("#toolTitle");
const category = document.querySelector("#toolCategory");
const description = document.querySelector("#toolDescription");
const availability = document.querySelector("#availability");
const taskStatus = document.querySelector("#taskStatus");
const taskLog = document.querySelector("#taskLog");
const downloadLink = document.querySelector("#downloadLink");

const escapeHtml = (value) => String(value).replace(/[&<>'"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[c]));
const optionField = (key, label, value, type = "number", extra = "") => `<div class="field"><label>${label}</label><input data-option="${key}" type="${type}" value="${escapeHtml(value)}" ${extra}></div>`;
const selectField = (key, label, value, values) => `<div class="field"><label>${label}</label><select data-option="${key}">${values.map(([v, text]) => `<option value="${v}" ${v === value ? "selected" : ""}>${text}</option>`).join("")}</select></div>`;

async function init() {
  const response = await fetch("/api/tools");
  const data = await response.json();
  state.tools = data.tools;
  state.active = state.tools[0];
  renderNav(); renderTool();
}

function renderNav() {
  const groups = new Map();
  state.tools.forEach(tool => { if (!groups.has(tool.category)) groups.set(tool.category, []); groups.get(tool.category).push(tool); });
  nav.innerHTML = [...groups].map(([group, tools]) => `<div class="nav-category">${group}</div>${tools.map(tool => `<button class="tool-card ${tool.id === state.active.id ? "active" : ""} ${tool.available ? "" : "unavailable"}" data-tool="${tool.id}" title="${escapeHtml(tool.unavailableReason || "")}">${escapeHtml(tool.title)}<small>${escapeHtml(tool.description)}</small></button>`).join("")}`).join("");
  nav.querySelectorAll("[data-tool]").forEach(button => button.addEventListener("click", () => { state.active = state.tools.find(tool => tool.id === button.dataset.tool); state.files = []; state.anki = null; state.captions = { titles: [], captions: [] }; state.currentJob = null; state.translationSource = ""; state.llmPreset = ""; stopPolling(); renderNav(); renderTool(); }));
}

function renderTool() {
  const tool = state.active;
  downloadLink.classList.add("hidden");
  title.textContent = tool.title; category.textContent = tool.category; description.textContent = tool.description;
  availability.textContent = tool.available ? "环境就绪" : "需配置或依赖"; availability.className = `badge ${tool.available ? "ok" : "bad"}`;
  const upload = document.querySelector("#uploadTemplate").content.cloneNode(true);
  form.innerHTML = ""; form.append(upload);
  if (!tool.available) form.insertAdjacentHTML("afterbegin", `<p class="message">${escapeHtml(tool.unavailableReason)}</p>`);
  if (tool.id === "bibtex") form.insertAdjacentHTML("beforeend", `<div class="info-box">此工具会通过 scholarly 查询 Google Scholar。查询可能受网络或来源限流影响。</div><div class="field wide"><label>或直接粘贴论文标题（每行一个；没有选择 TXT 时将自动生成任务文件）</label><textarea id="bibtexText" placeholder="Paper title one\nPaper title two"></textarea></div>`);
  form.insertAdjacentHTML("beforeend", optionsHtml(tool.id));
  form.insertAdjacentHTML("beforeend", `<div class="submit-row"><button type="button" class="primary" id="submitJob" ${tool.available ? "" : "disabled"}>加入处理队列</button></div>`);
  if (tool.id === "pdf_ocr_translate") form.insertAdjacentHTML("beforeend", `<section id="ocrArtifacts" class="ocr-artifacts hidden"></section>`);
  bindUpload(); bindDynamicOptions(); renderFileList();
  document.querySelector("#submitJob").addEventListener("click", submitJob);
}

function optionsHtml(id) {
  const gridStart = `<section><div class="section-heading"><h3>处理设置</h3><span class="hint">常用参数</span></div><div class="options">`;
  let fields = "";
  if (id === "anki") fields = `<div class="field"><label>正面 Sheet</label><select id="frontSheet"></select></div><div class="field"><label>正面列</label><select id="frontColumn"></select></div><div class="field"><label>背面 Sheet</label><select id="backSheet"></select></div><div class="field"><label>背面列</label><select id="backColumn"></select></div>`;
  if (id === "image_crop") fields = optionField("crop", "裁剪尺寸 (px)", 256) + optionField("out", "输出尺寸 (px)", 512);
  if (id === "video_crop") fields = optionField("crop", "裁剪尺寸 (px)", 256) + optionField("out", "输出尺寸 (px)", 512) + `<details class="advanced wide"><summary>高级设置</summary><div class="options">${optionField("offsetY", "垂直偏移 (px)", -80)}${optionField("crf", "视频质量 CRF (0–51)", 18)}</div></details>`;
  if (id === "image_ppt" || id === "video_ppt") fields = optionField("rows", "行数", 3) + optionField("cols", "列数", 5) + optionField("cellSize", "单元格尺寸 (cm)", 5) + optionField("gap", "间距 (px)", 4) + optionField("margin", "边距 (cm)", 1) + selectField("fit", "图片适配", "fit", [["fit","完整显示"],["fill","裁切填满"]]) + (id === "video_ppt" ? `<div class="field wide"><label>每个视频提取的帧序号（逗号或空格分隔）</label><input data-option="frameIndexes" value="0" placeholder="0, 3, 5, 7"></div>` : "");
  if (id === "stack_images") fields = selectField("direction", "排列方向", "horizontal", [["horizontal","横向"],["vertical","纵向"]]) + optionField("gap", "间距 (px)", 5) + optionField("border", "边框 (pt)", 1);
  if (id === "stack_videos") fields = optionField("rows", "行数", 1) + optionField("cols", "列数", 1) + selectField("mode", "标题布局", "h", [["h","按行说明"],["v","按列说明"]]) + `<div class="field wide"><label>标题与说明</label><div id="captionFields" class="caption-grid"></div></div><details class="advanced wide"><summary>高级设置</summary><div class="options">${optionField("gap", "间距 (px)", 5)}${optionField("outerBorder", "外边距 (px)", 5)}${optionField("titleBand", "标题带高度 (px)", 40)}${optionField("captionBand", "说明带高度 (px)", 150)}${optionField("titleFont", "标题字号", 26)}${optionField("captionFont", "说明字号", 30)}${selectField("audio", "保留音频", "first", [["first","第一条视频"],["none","不保留"]])}</div></details>`;
  if (id === "document_translate") fields = llmConfigurationHtml();
  if (id === "pdf_ocr_translate") fields = `<div class="field wide"><label>原始 PDF 本地绝对路径</label><input data-option="localSourcePath" type="text" required placeholder="/Users/name/Documents/paper.pdf"><p class="hint">浏览器不会提供上传文件的绝对路径。填入与所选 PDF 相同的本地路径后，系统会在该 PDF 同级新建同名文件夹，先复制 PDF，再自动保存所有 OCR 与翻译结果。</p></div><div class="field wide"><label>OCR 导出格式</label><div class="format-options"><label><input type="checkbox" data-ocr-format="docx" checked> DOCX</label><label><input type="checkbox" data-ocr-format="md" checked> Markdown</label><label><input type="checkbox" data-ocr-format="html" checked> HTML</label><label><input type="checkbox" data-ocr-format="tex.zip" checked> LaTeX ZIP</label></div><p class="hint">MMD 与 lines.json 会始终导出。第一版可翻译 MMD、Markdown、HTML。</p></div>`;
  return `${gridStart}${fields}</div></section>`;
}

function llmConfigurationHtml() {
  const presets = state.active.llmPresets || [];
  if (!state.llmPreset || !presets.some(item => item.id === state.llmPreset)) state.llmPreset = presets[0]?.id || "custom";
  const options = `${presets.map(item => `<option value="${escapeHtml(item.id)}" ${item.id === state.llmPreset ? "selected" : ""}>${escapeHtml(item.name)} · ${escapeHtml(item.model)} · ${escapeHtml(item.baseUrl)}</option>`).join("")}<option value="custom" ${state.llmPreset === "custom" ? "selected" : ""}>手动添加 OpenAI-compatible 配置</option>`;
  return `<div class="field wide"><label>LLM 配置</label><select id="llmPreset">${options}</select><p class="hint">预设从本机 .env 读取。手动配置可仅用于本次任务，或保存为本机预设。</p></div>${customLlmFieldsHtml("wide")}`;
}

function customLlmFieldsHtml(extraClass = "") { return `<div id="customLlmFields" class="custom-llm-fields ${extraClass}" ${state.llmPreset === "custom" ? "" : "hidden"}><div class="field"><label>配置名称</label><input id="customLlmName" placeholder="例如：公司代理"></div><div class="field"><label>Base URL</label><input id="customLlmBaseUrl" placeholder="https://api.example.com/v1"></div><div class="field"><label>API Key</label><input id="customLlmApiKey" type="password" autocomplete="off" placeholder="仅保存于本机 .env"></div><div class="field"><label>Model ID</label><input id="customLlmModel" placeholder="模型名称"></div><button type="button" class="save-llm-preset" data-save-llm>保存为本机预设</button></div>`; }

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
}

function renderFileList() {
  const list = form.querySelector("[data-file-list]"); if (!list) return;
  list.innerHTML = state.files.map((item, index) => `<li class="file-row" draggable="true" data-index="${index}"><span class="handle">⠿</span><span class="file-path" title="${escapeHtml(item.relativePath)}">${escapeHtml(item.relativePath)}</span><input class="file-name" data-name-index="${index}" value="${escapeHtml(item.workName)}" aria-label="任务工作名"><button type="button" class="remove-file" data-remove="${index}">移除</button></li>`).join("") || `<li class="hint">尚未添加符合此工具要求的文件。</li>`;
  list.querySelectorAll("[data-remove]").forEach(button => button.addEventListener("click", () => { state.files.splice(Number(button.dataset.remove), 1); renderFileList(); if (state.active.id === "stack_videos") renderCaptions(); }));
  list.querySelectorAll("[data-name-index]").forEach(input => input.addEventListener("input", () => { state.files[Number(input.dataset.nameIndex)].workName = input.value; if (state.active.id === "stack_videos") renderCaptions(); }));
  let dragged = null; list.querySelectorAll(".file-row").forEach(row => { row.addEventListener("dragstart", () => { dragged = Number(row.dataset.index); row.classList.add("dragging"); }); row.addEventListener("dragend", () => row.classList.remove("dragging")); row.addEventListener("dragover", event => event.preventDefault()); row.addEventListener("drop", event => { event.preventDefault(); const target = Number(row.dataset.index); if (dragged !== null && dragged !== target) { const [item] = state.files.splice(dragged, 1); state.files.splice(target, 0, item); renderFileList(); if (state.active.id === "stack_videos") renderCaptions(); } }); });
}

function bindDynamicOptions() {
  if (state.active.id === "anki") return;
  if (state.active.id === "document_translate") bindLlmControls(form);
  form.querySelectorAll('[data-option="rows"],[data-option="cols"],[data-option="mode"]').forEach(input => input.addEventListener("change", renderCaptions));
  if (state.active.id === "stack_videos") renderCaptions();
}

function bindLlmControls(scope) {
  scope.querySelector("#llmPreset")?.addEventListener("change", event => { state.llmPreset = event.target.value; scope.querySelector("#customLlmFields")?.toggleAttribute("hidden", state.llmPreset !== "custom"); });
  scope.querySelector("[data-save-llm]")?.addEventListener("click", () => saveLlmPreset(scope));
}

function llmConfigurationFrom(scope = form) {
  const presetId = scope.querySelector("#llmPreset")?.value;
  if (presetId === "custom") return { mode:"custom", name:scope.querySelector("#customLlmName")?.value.trim(), baseUrl:scope.querySelector("#customLlmBaseUrl")?.value.trim(), apiKey:scope.querySelector("#customLlmApiKey")?.value.trim(), model:scope.querySelector("#customLlmModel")?.value.trim() };
  return { mode:"preset", presetId };
}

async function saveLlmPreset(scope) {
  const config = llmConfigurationFrom(scope);
  if (config.mode !== "custom" || !config.name || !config.baseUrl || !config.apiKey || !config.model) return setTask("请完整填写手动 LLM 配置后再保存。", "bad");
  const button = scope.querySelector("[data-save-llm]"); if (button) button.disabled = true;
  try { const response = await fetch("/api/llm-presets", { method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(config) }); const data = await response.json(); if (!response.ok) throw new Error(data.error || "保存预设失败"); state.active.llmPresets.push(data.preset); state.llmPreset = data.preset.id; const select = scope.querySelector("#llmPreset"); select.insertAdjacentHTML("beforeend", `<option value="${escapeHtml(data.preset.id)}" selected>${escapeHtml(data.preset.name)} · ${escapeHtml(data.preset.model)} · ${escapeHtml(data.preset.baseUrl)}</option>`); select.value = data.preset.id; scope.querySelector("#customLlmFields")?.setAttribute("hidden", ""); setTask(`已保存本机 LLM 预设：${data.preset.name}`, "ok"); }
  catch (error) { setTask(error.message, "bad"); } finally { if (button) button.disabled = false; }
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
  if (state.active.id === "pdf_ocr_translate") options.ocrFormats = [...form.querySelectorAll("[data-ocr-format]")].filter(input => input.checked).map(input => input.dataset.ocrFormat);
  if (state.active.id === "document_translate") options.llm = selectedLlmConfiguration();
  return options;
}

function selectedLlmConfiguration() {
  const presetId = form.querySelector("#llmPreset")?.value;
  if (presetId === "custom") return { mode:"custom", name:form.querySelector("#customLlmName")?.value.trim(), baseUrl:form.querySelector("#customLlmBaseUrl")?.value.trim(), apiKey:form.querySelector("#customLlmApiKey")?.value.trim(), model:form.querySelector("#customLlmModel")?.value.trim() };
  return { mode:"preset", presetId };
}

async function submitJob() {
  let files = [...state.files];
  if (state.active.id === "bibtex" && !files.length) { const text = form.querySelector("#bibtexText")?.value.trim(); if (text) files = [{ file:new File([text], "paper_titles.txt", { type:"text/plain" }), relativePath:"paper_titles.txt", workName:"paper_titles" }]; }
  if (!files.length) return setTask("需要先添加文件。", "bad");
  if (state.active.id === "anki" && !state.anki) return setTask("请等待表格列读取完成。", "bad");
  if (state.active.id === "pdf_ocr_translate" && !form.querySelector('[data-option="localSourcePath"]')?.value.trim()) return setTask("请填写原始 PDF 的本地绝对路径。", "bad");
  if (state.active.id === "document_translate") {
    const llm = selectedLlmConfiguration();
    if (llm.mode === "custom" && (!llm.name || !llm.baseUrl || !llm.apiKey || !llm.model)) return setTask("请完整填写手动 LLM 配置。", "bad");
  }
  const body = new FormData(); body.append("tool", state.active.id); body.append("options", JSON.stringify(collectOptions())); body.append("manifest", JSON.stringify(files.map(item => ({ relativePath:item.relativePath, workName:item.workName })))); files.forEach(item => body.append("files", item.file, item.file.name));
  document.querySelector("#submitJob").disabled = true; setTask("正在提交任务…", ""); downloadLink.classList.add("hidden");
  try { const response = await fetch("/api/jobs", { method:"POST", body }); const data = await response.json(); if (!response.ok) throw new Error(data.error || "提交失败"); state.currentJob = data.id; state.translationSource = ""; setTask("任务已排队", ""); taskLog.textContent = "任务已提交，等待本地工作线程。"; startPolling(data.id); }
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
  const ready = ["completed", "completed_with_warnings"].includes(data.status) && ["ocr_complete", "translation_complete"].includes(data.phase);
  const sourcePriority = item => item.name.toLowerCase().endsWith(".md") ? 0 : 1;
  const sources = artifacts.filter(item => item.kind === "ocr" && item.translationSupported).sort((left, right) => sourcePriority(left) - sourcePriority(right));
  if (!state.translationSource || !sources.some(item => item.id === state.translationSource)) state.translationSource = sources[0]?.id || "";
  const presets = state.active.llmPresets || [];
  if (!state.llmPreset || !presets.some(item => item.id === state.llmPreset)) state.llmPreset = presets[0]?.id || "custom";
  const llmOptions = `${presets.map(item => `<option value="${escapeHtml(item.id)}" ${item.id === state.llmPreset ? "selected" : ""}>${escapeHtml(item.name)} · ${escapeHtml(item.model)} · ${escapeHtml(item.baseUrl)}</option>`).join("")}<option value="custom" ${state.llmPreset === "custom" ? "selected" : ""}>手动添加 OpenAI-compatible 配置</option>`;
  const customHidden = state.llmPreset === "custom" ? "" : "hidden";
  target.innerHTML = `<div class="section-heading"><h3>OCR 输出</h3><span class="hint">下载或选择可翻译文件</span></div><div class="artifact-list">${artifacts.map(item => `<div class="artifact-row"><div><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml(item.format)}${item.translationSupported ? " · 可翻译" : " · 仅下载"}</small></div><a class="artifact-download" href="${escapeHtml(item.downloadUrl)}">下载</a></div>`).join("")}</div>${ready ? `<div class="translation-box"><label>翻译源文件<select id="translationSource">${sources.map(item => `<option value="${escapeHtml(item.id)}" ${item.id === state.translationSource ? "selected" : ""}>${escapeHtml(item.name)}</option>`).join("")}</select></label><label>LLM 配置<select id="llmPreset">${llmOptions}</select></label>${customLlmFieldsHtml("")}<p class="hint">预设从本机 .env 读取。手动配置可仅用于本次翻译，或保存为本机预设。</p><button type="button" class="primary" id="startTranslation" ${sources.length ? "" : "disabled"}>翻译所选文件</button></div>` : `<p class="hint">OCR 完成后可在这里下载或选择文本格式进行翻译。</p>`}`;
  const select = target.querySelector("#translationSource"); if (select) select.addEventListener("change", () => { state.translationSource = select.value; });
  bindLlmControls(target);
  target.querySelector("#startTranslation")?.addEventListener("click", () => submitTranslation(data.id));
}

async function submitTranslation(jobId) {
  if (!state.translationSource) return setTask("没有可翻译的 OCR 文件。", "bad");
  const presetId = form.querySelector("#llmPreset")?.value;
  let llm;
  if (presetId === "custom") {
    llm = { mode:"custom", name:form.querySelector("#customLlmName")?.value.trim(), baseUrl:form.querySelector("#customLlmBaseUrl")?.value.trim(), apiKey:form.querySelector("#customLlmApiKey")?.value.trim(), model:form.querySelector("#customLlmModel")?.value.trim() };
    if (!llm.name || !llm.baseUrl || !llm.apiKey || !llm.model) return setTask("请完整填写手动 LLM 配置。", "bad");
  } else {
    llm = { mode:"preset", presetId };
  }
  const button = form.querySelector("#startTranslation"); if (button) button.disabled = true;
  try { const response = await fetch(`/api/jobs/${jobId}/translations`, { method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({ artifactId:state.translationSource, llm }) }); const data = await response.json(); if (!response.ok) throw new Error(data.error || "翻译提交失败"); setTask("翻译已排队", ""); startPolling(data.id); }
  catch (error) { setTask(error.message, "bad"); if (button) button.disabled = false; }
}

function startPolling(jobId) { stopPolling(); const poll = async () => { try { const response = await fetch(`/api/jobs/${jobId}`); const data = await response.json(); if (!response.ok) throw new Error(data.error); const labels = {queued:"排队中",running:"处理中",completed:"已完成",completed_with_warnings:"完成（有警告）",failed:"失败"}; const progress = data.translationProgress; const progressText = progress ? ` · 翻译 ${progress.completed}/${progress.total}` : ""; const failedPartial = data.status === "failed" && data.phase === "translation_partial"; const statusText = failedPartial ? `翻译失败，已保留 ${progress?.completed || 0}/${progress?.total || 0}` : `${labels[data.status] || data.status}${progressText}`; setTask(statusText, data.status === "completed" ? "ok" : data.status === "completed_with_warnings" ? "warn" : data.status === "failed" ? "bad" : ""); taskLog.textContent = data.logs.join("\n"); taskLog.scrollTop = taskLog.scrollHeight; if (data.downloadReady) { downloadLink.href = `/api/jobs/${jobId}/download`; downloadLink.textContent = `下载 ${data.downloadName}`; downloadLink.classList.remove("hidden"); } renderOcrArtifacts(data); if (["completed","completed_with_warnings","failed"].includes(data.status)) stopPolling(); } catch (error) { setTask(error.message, "bad"); stopPolling(); } }; poll(); state.poller = setInterval(poll, 1000); }

init().catch(error => { title.textContent = "无法加载工具"; description.textContent = error.message; });
