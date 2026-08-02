const state = { tools: [], active: null, view: "tool", files: [], anki: null, captions: { titles: [], captions: [] }, poller: null, currentJob: null, translationSource: "", llmPreset: "", llmPresets: [], editingLlmId: null, speechConfig: null, githubConfig: null, settingsTab: "llm" };
const nav = document.querySelector("#toolNav");
const form = document.querySelector("#toolForm");
const llmSettingsNav = document.querySelector("#llmSettingsNav");
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
  const response = await fetch("/api/tools");
  const data = await response.json();
  state.tools = data.tools;
  toolCount.textContent = String(state.tools.length);
  state.active = state.tools.find(tool => tool.id === "pdf_ocr_translate") || state.tools[0];
  llmSettingsNav.addEventListener("click", () => openLlmSettings(true, state.settingsTab));
  window.addEventListener("hashchange", () => {
    if (location.hash === "#llm-settings" || location.hash.startsWith("#settings/")) openLlmSettings(false);
  });
  if (location.hash === "#llm-settings" || location.hash.startsWith("#settings/")) openLlmSettings(false);
  else { renderNav(); renderTool(); }
}

function renderNav() {
  const groups = new Map();
  state.tools.forEach(tool => { if (!groups.has(tool.category)) groups.set(tool.category, []); groups.get(tool.category).push(tool); });
  nav.innerHTML = [...groups].map(([group, tools]) => `<div class="nav-category">${group}</div>${tools.map(tool => `<button class="tool-card ${state.view === "tool" && tool.id === state.active.id ? "active" : ""} ${tool.available ? "" : "unavailable"}" data-tool="${tool.id}" title="${escapeHtml(tool.unavailableReason || "")}">${escapeHtml(tool.title)}<small>${escapeHtml(tool.description)}</small></button>`).join("")}`).join("");
  llmSettingsNav.classList.toggle("active", state.view === "llm");
  nav.querySelectorAll("[data-tool]").forEach(button => button.addEventListener("click", () => {
    state.view = "tool"; state.active = state.tools.find(tool => tool.id === button.dataset.tool); state.files = []; state.anki = null; state.captions = { titles: [], captions: [] }; state.currentJob = null; state.translationSource = ""; state.llmPreset = ""; stopPolling();
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

function settingsTabFromHash() {
  if (location.hash === "#llm-settings") return "llm";
  const tab = location.hash.replace(/^#settings\//, "");
  return ["llm", "speech", "github"].includes(tab) ? tab : null;
}

async function openLlmSettings(updateHash = true, requestedTab = null) {
  state.view = "llm";
  state.settingsTab = requestedTab || settingsTabFromHash() || state.settingsTab || "llm";
  stopPolling();
  if (updateHash) history.replaceState(null, "", `#settings/${state.settingsTab}`);
  renderNav();
  taskPanel.classList.add("hidden");
  category.textContent = "全局设置";
  title.textContent = "AI、语音与 GitHub 配置";
  description.textContent = "统一管理 OpenAI-compatible 模型、Azure Speech 与 Markdown 图片发布仓库。";
  availability.textContent = "本机全局";
  availability.className = "badge ok";
  form.innerHTML = `<div class="llm-manager-loading">正在读取本机全局配置…</div>`;
  try {
    await Promise.all([loadGlobalLlmPresets(), loadGlobalSpeechConfig(), loadGlobalGithubConfig()]);
    renderLlmManager();
  } catch (error) {
    form.innerHTML = `<p class="message">${escapeHtml(error.message)}</p><button type="button" data-retry-llm>重新加载</button>`;
    form.querySelector("[data-retry-llm]")?.addEventListener("click", () => openLlmSettings(false));
  }
}

async function loadGlobalLlmPresets() {
  const response = await fetch("/api/llm-presets");
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "无法读取 LLM 配置。");
  state.llmPresets = data.presets || [];
  syncGlobalLlmPresets();
}

async function loadGlobalSpeechConfig() {
  const response = await fetch("/api/speech-config");
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "无法读取 Azure Speech 配置。");
  state.speechConfig = data.speech || {
    configured: false,
    region: "centralus",
    voice: "zh-CN-YunxiNeural",
  };
}

async function loadGlobalGithubConfig() {
  const response = await fetch("/api/github-config");
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "无法读取 GitHub 配置。");
  state.githubConfig = data.github || {
    configured: false,
    repository: "",
    branch: "main",
    imageRoot: "images",
  };
}

async function reloadTools() {
  const activeId = state.active?.id;
  const response = await fetch("/api/tools");
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "无法刷新工具状态。");
  state.tools = data.tools || [];
  state.active = state.tools.find(tool => tool.id === activeId) || state.tools[0];
  toolCount.textContent = String(state.tools.length);
  syncGlobalLlmPresets();
  renderNav();
}

function syncGlobalLlmPresets() {
  state.tools.forEach(tool => {
    if (["document_translate", "pdf_ocr_translate", "markdown_repair"].includes(tool.id)) tool.llmPresets = [...state.llmPresets];
  });
  if (!state.llmPresets.some(item => item.id === state.llmPreset)) state.llmPreset = state.llmPresets[0]?.id || "";
}

function renderLlmManager(message = "", kind = "", messageTarget = "llm") {
  const editing = state.editingLlmId ? state.llmPresets.find(item => item.id === state.editingLlmId) : null;
  const speech = state.speechConfig || {
    configured: false,
    region: "centralus",
    voice: "zh-CN-YunxiNeural",
  };
  const github = state.githubConfig || {
    configured: false,
    repository: "",
    branch: "main",
    imageRoot: "images",
  };
  const cards = state.llmPresets.map(preset => `
    <article class="llm-preset-card">
      <div class="llm-preset-copy">
        <div class="llm-preset-heading"><h3>${escapeHtml(preset.name)}</h3><span class="preset-id">${preset.id === "default" ? "默认配置" : escapeHtml(preset.id)}</span></div>
        <p>${escapeHtml(preset.model)} · 并发 ${escapeHtml(preset.concurrency || 1)}</p>
        <code>${escapeHtml(preset.baseUrl)}</code>
      </div>
      <div class="llm-preset-actions">
        <button type="button" class="test-button" data-test-llm="${escapeHtml(preset.id)}">测试</button>
        <button type="button" class="test-button" data-test-llm-concurrency="${escapeHtml(preset.id)}">测试并发</button>
        <button type="button" data-edit-llm="${escapeHtml(preset.id)}">编辑</button>
        <button type="button" class="danger-button" data-delete-llm="${escapeHtml(preset.id)}">删除</button>
      </div>
    </article>`).join("");
  form.innerHTML = `
    <section class="llm-manager">
      <div class="settings-tabs" role="tablist" aria-label="全局配置类别">
        <button type="button" role="tab" data-settings-tab="llm">LLM</button>
        <button type="button" role="tab" data-settings-tab="speech">Azure Speech</button>
        <button type="button" role="tab" data-settings-tab="github">GitHub</button>
      </div>
      <section class="settings-tab-panel speech-manager" data-settings-panel="speech" role="tabpanel">
        <div class="section-heading llm-manager-heading">
          <div>
            <div class="llm-preset-heading"><h3>Azure Speech</h3><span class="preset-id ${speech.configured ? "" : "speech-unconfigured"}">${speech.configured ? "已配置" : "未配置"}</span></div>
            <p class="hint">PDF 各阅读视图的段落朗读；API Key 只保存在项目 .env 和当前后端进程中。</p>
          </div>
        </div>
        <p id="speechManagerStatus" class="manager-message ${messageTarget === "speech" ? escapeHtml(kind) : ""} ${message && messageTarget === "speech" ? "" : "hidden"}" role="status">${messageTarget === "speech" ? escapeHtml(message) : ""}</p>
        <form id="speechConfigForm" class="llm-editor-form speech-editor-form">
          <label>Azure 区域<input name="region" value="${escapeHtml(speech.region)}" required maxlength="64" placeholder="centralus"></label>
          <label>语音名称<input name="voice" value="${escapeHtml(speech.voice)}" required maxlength="100" placeholder="zh-CN-YunxiNeural"></label>
          <label class="speech-key-field">API Key<input name="apiKey" type="password" autocomplete="new-password" ${speech.configured ? "" : "required"} maxlength="1000" placeholder="${speech.configured ? "留空表示保持当前密钥" : "仅保存到本机 .env"}"></label>
          <div class="speech-editor-actions">
            <button type="button" class="test-button" data-test-speech ${speech.configured ? "" : "disabled"}>测试语音</button>
            ${speech.configured ? `<button type="button" class="danger-button" data-delete-speech>移除配置</button>` : ""}
            <button type="submit" class="primary">保存 Azure Speech</button>
          </div>
        </form>
      </section>
      <section class="settings-tab-panel" data-settings-panel="llm" role="tabpanel">
      <div class="section-heading llm-manager-heading">
        <div><h3>OpenAI-compatible LLM</h3><p class="hint">统一用于翻译、OCR 后翻译和论文划词问答；API Key 始终只留在后端。</p></div>
        <button type="button" class="primary" data-add-llm>＋ 添加 LLM</button>
      </div>
      <p id="llmManagerStatus" class="manager-message ${messageTarget === "llm" ? escapeHtml(kind) : ""} ${message && messageTarget === "llm" ? "" : "hidden"}" role="status">${messageTarget === "llm" ? escapeHtml(message) : ""}</p>
      <div class="llm-preset-list">${cards || `<div class="llm-empty-state"><strong>还没有可用的 LLM 配置</strong><p>添加一个 OpenAI-compatible 服务后，所有需要 LLM 的功能都能直接选择它。</p></div>`}</div>
      <section id="llmEditor" class="llm-editor ${state.editingLlmId === null ? "hidden" : ""}">
        <div class="section-heading"><div><p class="eyebrow">${editing ? "EDIT PRESET" : "NEW PRESET"}</p><h3>${editing ? `编辑 ${escapeHtml(editing.name)}` : "添加 LLM 配置"}</h3></div><button type="button" data-cancel-llm>取消</button></div>
        <form id="llmEditorForm" class="llm-editor-form">
          <label>配置名称<input name="name" value="${escapeHtml(editing?.name || "")}" required maxlength="100" placeholder="例如：DeepSeek"></label>
          <label>Base URL<input name="baseUrl" value="${escapeHtml(editing?.baseUrl || "")}" required maxlength="500" placeholder="https://api.example.com/v1"></label>
          <label>API Key<input name="apiKey" type="password" autocomplete="new-password" ${editing ? "" : "required"} maxlength="1000" placeholder="${editing ? "留空表示保持当前密钥" : "仅保存到本机 .env"}"></label>
          <label>Model ID<input name="model" value="${escapeHtml(editing?.model || "")}" required maxlength="200" placeholder="例如：deepseek-chat"></label>
          <label>并发请求数<input name="concurrency" type="number" value="${escapeHtml(editing?.concurrency || 1)}" required min="1" max="64" step="1"><span class="hint">同一配置的所有 LLM 调用共享此上限（1–64）。</span></label>
          <div class="llm-editor-actions"><button type="submit" class="primary">${editing ? "保存修改" : "添加配置"}</button></div>
        </form>
      </section>
      </section>
      <section class="settings-tab-panel github-manager" data-settings-panel="github" role="tabpanel">
        <div class="section-heading llm-manager-heading">
          <div>
            <div class="llm-preset-heading"><h3>GitHub 图片仓库</h3><span class="preset-id ${github.configured ? "" : "speech-unconfigured"}">${github.configured ? "已配置" : "未配置"}</span></div>
            <p class="hint">供“Markdown 图片发布”使用。Token 仅保存在项目 .env；Raw 链接要求公开仓库。</p>
          </div>
        </div>
        <p id="githubManagerStatus" class="manager-message ${messageTarget === "github" ? escapeHtml(kind) : ""} ${message && messageTarget === "github" ? "" : "hidden"}" role="status">${messageTarget === "github" ? escapeHtml(message) : ""}</p>
        <form id="githubConfigForm" class="llm-editor-form github-editor-form">
          <label>仓库（owner/repo）<input name="repository" value="${escapeHtml(github.repository)}" required maxlength="140" placeholder="octocat/research-images"></label>
          <label>目标分支<input name="branch" value="${escapeHtml(github.branch || "main")}" required maxlength="255" placeholder="main"></label>
          <label>图片根目录<input name="imageRoot" value="${escapeHtml(github.imageRoot || "images")}" required maxlength="500" placeholder="images"></label>
          <label class="github-token-field">Personal Access Token<input name="token" type="password" autocomplete="new-password" ${github.configured ? "" : "required"} maxlength="1000" placeholder="${github.configured ? "留空表示保持当前 Token" : "需要 Contents: write 权限"}"></label>
          <p class="hint github-path-preview">目标路径：&lt;图片根目录&gt;/YYYY/MM/DD/&lt;完整 SHA-256&gt;.&lt;扩展名&gt;</p>
          <div class="speech-editor-actions">
            <button type="button" class="test-button" data-test-github ${github.configured ? "" : "disabled"}>测试 GitHub</button>
            ${github.configured ? `<button type="button" class="danger-button" data-delete-github>移除配置</button>` : ""}
            <button type="submit" class="primary">保存 GitHub</button>
          </div>
        </form>
      </section>
    </section>`;
  form.querySelectorAll("[data-settings-tab]").forEach(button => button.addEventListener("click", () => activateSettingsTab(button.dataset.settingsTab)));
  form.querySelector("[data-add-llm]")?.addEventListener("click", () => { state.editingLlmId = ""; renderLlmManager(); form.querySelector("#llmEditor")?.scrollIntoView({ behavior:"smooth", block:"start" }); });
  form.querySelector("[data-cancel-llm]")?.addEventListener("click", () => { state.editingLlmId = null; renderLlmManager(); });
  form.querySelectorAll("[data-test-llm]").forEach(button => button.addEventListener("click", () => testGlobalLlmPreset(button.dataset.testLlm, button)));
  form.querySelectorAll("[data-test-llm-concurrency]").forEach(button => button.addEventListener("click", () => testGlobalLlmConcurrency(button.dataset.testLlmConcurrency, button)));
  form.querySelectorAll("[data-edit-llm]").forEach(button => button.addEventListener("click", () => { state.editingLlmId = button.dataset.editLlm; renderLlmManager(); form.querySelector("#llmEditor")?.scrollIntoView({ behavior:"smooth", block:"start" }); }));
  form.querySelectorAll("[data-delete-llm]").forEach(button => button.addEventListener("click", () => removeGlobalLlmPreset(button.dataset.deleteLlm)));
  form.querySelector("#llmEditorForm")?.addEventListener("submit", saveGlobalLlmPreset);
  form.querySelector("#speechConfigForm")?.addEventListener("submit", saveGlobalSpeechConfig);
  form.querySelector("[data-test-speech]")?.addEventListener("click", event => testGlobalSpeechConfig(event.currentTarget));
  form.querySelector("[data-delete-speech]")?.addEventListener("click", removeGlobalSpeechConfig);
  form.querySelector("#githubConfigForm")?.addEventListener("submit", saveGlobalGithubConfig);
  form.querySelector("[data-test-github]")?.addEventListener("click", event => testGlobalGithubConfig(event.currentTarget));
  form.querySelector("[data-delete-github]")?.addEventListener("click", removeGlobalGithubConfig);
  activateSettingsTab(state.settingsTab, false);
}

function activateSettingsTab(tab, updateHash = true) {
  if (!["llm", "speech", "github"].includes(tab)) return;
  state.settingsTab = tab;
  form.querySelectorAll("[data-settings-tab]").forEach(button => {
    const active = button.dataset.settingsTab === tab;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", String(active));
    button.tabIndex = active ? 0 : -1;
  });
  form.querySelectorAll("[data-settings-panel]").forEach(panel => panel.classList.toggle("hidden", panel.dataset.settingsPanel !== tab));
  if (updateHash) history.replaceState(null, "", `#settings/${tab}`);
}

async function testGlobalLlmPreset(presetId, button) {
  const preset = state.llmPresets.find(item => item.id === presetId);
  if (!preset) return;
  const status = form.querySelector("#llmManagerStatus");
  button.disabled = true;
  button.textContent = "测试中…";
  status.textContent = `正在测试 ${preset.name}…`;
  status.className = "manager-message";
  try {
    const testMessage = "这是一次 LLM 连接测试。请只回复：OK";
    const response = await fetch(`/api/llm-presets/${encodeURIComponent(presetId)}/test`, {
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({ message:testMessage }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "LLM 测试失败。");
    const responseText = data.result.contentEmpty ? "服务已响应，但没有返回文本内容" : `回复：${data.result.response}`;
    status.textContent = `连接成功：${data.result.name} · ${data.result.model} · ${data.result.latencyMs} ms · ${responseText}`;
    status.className = `manager-message ${data.result.contentEmpty ? "warn" : "ok"}`;
  } catch (error) {
    status.textContent = `${preset.name} 测试失败：${error.message}`;
    status.className = "manager-message bad";
  } finally {
    if (button.isConnected) {
      button.disabled = false;
      button.textContent = "测试";
    }
  }
}

async function testGlobalLlmConcurrency(presetId, button) {
  const preset = state.llmPresets.find(item => item.id === presetId);
  if (!preset) return;
  const target = Number(preset.concurrency || 1);
  if (!window.confirm(`将对“${preset.name}”同时发送 ${target} 个极短请求，直接验证已保存的并发数。测试结果会受当前账户限流和服务负载影响。是否继续？`)) return;
  const status = form.querySelector("#llmManagerStatus");
  button.disabled = true;
  button.textContent = "测试中…";
  status.textContent = `正在测试 ${preset.name} 的并发上限（目标 ${target}）…`;
  status.className = "manager-message";
  try {
    const response = await fetch(`/api/llm-presets/${encodeURIComponent(presetId)}/concurrency-test`, { method:"POST" });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "并发测试失败。");
    const result = data.result;
    const stageText = result.stages.map(stage => {
      const errors = Object.entries(stage.errors || {}).map(([kind, count]) => `${kind} ${count}`).join("、");
      return `并发 ${stage.concurrency}: ${stage.successCount}/${stage.concurrency} 成功，耗时 ${stage.wallMs} ms，有效并行度 ${stage.effectiveParallelism}${errors ? `，${errors}` : ""}`;
    }).join("；");
    status.textContent = result.reachedConfiguredLimit
      ? `测试通过：已配置并发 ${result.configuredConcurrency} 全部成功。${stageText}。这是当前时点结果，不代表永久配额。`
      : `测试未通过：并发 ${result.configuredConcurrency} 本轮未全部成功。${stageText}。请降低保存的并发数后重新测试。`;
    status.className = `manager-message ${result.reachedConfiguredLimit ? "ok" : "bad"}`;
  } catch (error) {
    status.textContent = `${preset.name} 并发测试失败：${error.message}`;
    status.className = "manager-message bad";
  } finally {
    if (button.isConnected) {
      button.disabled = false;
      button.textContent = "测试并发";
    }
  }
}

async function testGlobalSpeechConfig(button) {
  const status = form.querySelector("#speechManagerStatus");
  button.disabled = true;
  button.textContent = "测试中…";
  status.textContent = "正在调用 Azure Speech 合成一小段测试语音…";
  status.className = "manager-message";
  try {
    const response = await fetch("/api/speech-config/test", { method:"POST" });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Azure Speech 测试失败。");
    status.textContent = `连接成功：${data.result.region} · ${data.result.voice} · ${data.result.latencyMs} ms · ${data.result.audioBytes} bytes`;
    status.className = "manager-message ok";
  } catch (error) {
    status.textContent = `Azure Speech 测试失败：${error.message}`;
    status.className = "manager-message bad";
  } finally {
    if (button.isConnected) {
      button.disabled = false;
      button.textContent = "测试语音";
    }
  }
}

async function saveGlobalSpeechConfig(event) {
  event.preventDefault();
  const editorForm = event.currentTarget;
  const submit = editorForm.querySelector('[type="submit"]');
  const payload = Object.fromEntries(new FormData(editorForm).entries());
  submit.disabled = true;
  try {
    const response = await fetch("/api/speech-config", {
      method:"PUT",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify(payload),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "保存 Azure Speech 配置失败。");
    state.speechConfig = data.speech;
    renderLlmManager(`已保存：${data.speech.region} · ${data.speech.voice}。阅读器页面重新加载后即可使用。`, "ok", "speech");
  } catch (error) {
    const status = form.querySelector("#speechManagerStatus");
    status.textContent = error.message;
    status.className = "manager-message bad";
    submit.disabled = false;
  }
}

async function removeGlobalSpeechConfig() {
  if (!window.confirm("确定移除 Azure Speech 配置吗？PDF 段落朗读将停止可用。")) return;
  try {
    const response = await fetch("/api/speech-config", { method:"DELETE" });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "移除 Azure Speech 配置失败。");
    await loadGlobalSpeechConfig();
    renderLlmManager("已移除 Azure Speech 配置。", "ok", "speech");
  } catch (error) {
    const status = form.querySelector("#speechManagerStatus");
    status.textContent = error.message;
    status.className = "manager-message bad";
  }
}

async function testGlobalGithubConfig(button) {
  const status = form.querySelector("#githubManagerStatus");
  button.disabled = true;
  button.textContent = "测试中…";
  status.textContent = "正在只读验证公开仓库与目标分支…";
  status.className = "manager-message";
  try {
    const response = await fetch("/api/github-config/test", { method:"POST" });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "GitHub 测试失败。");
    status.textContent = `连接成功：${data.result.repository} · 仓库 ID ${data.result.repositoryId} · ${data.result.branch} · ${data.result.visibility}`;
    status.className = "manager-message ok";
  } catch (error) {
    status.textContent = `GitHub 测试失败：${error.message}`;
    status.className = "manager-message bad";
  } finally {
    if (button.isConnected) {
      button.disabled = false;
      button.textContent = "测试 GitHub";
    }
  }
}

async function saveGlobalGithubConfig(event) {
  event.preventDefault();
  const editorForm = event.currentTarget;
  const submit = editorForm.querySelector('[type="submit"]');
  const payload = Object.fromEntries(new FormData(editorForm).entries());
  submit.disabled = true;
  try {
    const response = await fetch("/api/github-config", {
      method:"PUT",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify(payload),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "保存 GitHub 配置失败。");
    state.githubConfig = data.github;
    await reloadTools();
    renderLlmManager(`已保存：${data.github.repository} · ${data.github.branch} · ${data.github.imageRoot}`, "ok", "github");
  } catch (error) {
    const status = form.querySelector("#githubManagerStatus");
    status.textContent = error.message;
    status.className = "manager-message bad";
    submit.disabled = false;
  }
}

async function removeGlobalGithubConfig() {
  if (!window.confirm("确定移除 GitHub 图片仓库配置吗？Markdown 图片发布将停止可用。")) return;
  try {
    const response = await fetch("/api/github-config", { method:"DELETE" });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "移除 GitHub 配置失败。");
    await loadGlobalGithubConfig();
    await reloadTools();
    renderLlmManager("已移除 GitHub 图片仓库配置。", "ok", "github");
  } catch (error) {
    const status = form.querySelector("#githubManagerStatus");
    status.textContent = error.message;
    status.className = "manager-message bad";
  }
}

async function saveGlobalLlmPreset(event) {
  event.preventDefault();
  const editorForm = event.currentTarget;
  const submit = editorForm.querySelector('[type="submit"]');
  const payload = Object.fromEntries(new FormData(editorForm).entries());
  const editingId = state.editingLlmId || null;
  submit.disabled = true;
  try {
    const response = await fetch(editingId ? `/api/llm-presets/${encodeURIComponent(editingId)}` : "/api/llm-presets", {
      method: editingId ? "PUT" : "POST",
      headers: { "Content-Type":"application/json" },
      body: JSON.stringify(payload),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "保存 LLM 配置失败。");
    state.editingLlmId = null;
    await loadGlobalLlmPresets();
    renderLlmManager(editingId ? `已更新：${data.preset.name}` : `已添加：${data.preset.name}`, "ok");
  } catch (error) {
    const status = form.querySelector("#llmManagerStatus");
    status.textContent = error.message; status.className = "manager-message bad";
    submit.disabled = false;
  }
}

async function removeGlobalLlmPreset(presetId) {
  const preset = state.llmPresets.find(item => item.id === presetId);
  if (!preset || !window.confirm(`确定删除 LLM 配置“${preset.name}”吗？使用它的已保存浏览器偏好会自动回退到其他配置。`)) return;
  try {
    const response = await fetch(`/api/llm-presets/${encodeURIComponent(presetId)}`, { method:"DELETE" });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "删除 LLM 配置失败。");
    state.editingLlmId = null;
    await loadGlobalLlmPresets();
    renderLlmManager(`已删除：${data.preset.name}`, "ok");
  } catch (error) {
    const status = form.querySelector("#llmManagerStatus");
    status.textContent = error.message; status.className = "manager-message bad";
  }
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
  if (id === "markdown_repair") fields = `<div class="field wide"><label class="format-options"><span><input id="mathRepair" type="checkbox" checked> 修复并校验行间公式 <code>$$</code></span></label><p class="hint">无需 LLM；会跳过围栏代码和行内公式，只自动修复高置信度的公式边界问题。</p></div><div class="field wide"><label class="format-options"><span><input id="footnoteRepair" type="checkbox" checked> 修复 OCR 合并脚注</span></label><p class="hint">无需 LLM；仅在正文上标与合并脚注定义可确定对应时拆分，歧义内容不会猜测修改。</p></div><div class="field wide"><label class="format-options"><span><input id="normalizeChinesePunctuation" type="checkbox"> 中文标点转英文符号</span></label><p class="hint">将中文逗号、句号、引号、括号等转为英文符号；代码、公式、链接与 URL 保持不变。</p></div><div class="field wide"><label class="format-options"><span><input id="deepRepair" type="checkbox"> 深度结构修复（OCR Markdown）</span></label><p class="hint">需要 LLM；修复围栏、脚注、公式环境及含公式的伪代码。仅在需要处理 OCR 结构异常时开启。</p></div><div id="standaloneRepairLlmFields" class="wide" hidden>${llmConfigurationHtml()}</div>`;
  if (id === "markdown_github") fields = `<div class="field wide github-tool-note"><strong>仅发布文档实际引用的本地图片</strong><p class="hint">支持 Markdown、MMD、Wiki 图片与内嵌 HTML 图片。远程/data/root URL 保持原样；任何本地图片缺失都会在访问 GitHub 前终止。</p><p class="hint">远端路径使用 &lt;图片根目录&gt;/YYYY/MM/DD/&lt;完整 SHA-256&gt;.&lt;扩展名&gt;，成功后下载不修改原文件的 <code>_github</code> 副本。</p><button type="button" class="inline-link" data-open-github-settings>管理 GitHub 配置</button></div>`;
  if (id === "pdf_ocr_translate") fields = `<div class="field wide"><label>原始 PDF 本地绝对路径</label><input data-option="localSourcePath" type="text" required placeholder="/Users/name/Documents/paper.pdf"><p class="hint">浏览器不会提供上传文件的绝对路径。填入与所选 PDF 相同的本地路径后，系统会在该 PDF 同级新建同名文件夹，先复制 PDF，再自动保存所有 OCR 与翻译结果。</p></div><div class="field wide"><label>OCR 导出格式</label><div class="format-options"><label><input type="checkbox" data-ocr-format="docx" checked> DOCX</label><label><input type="checkbox" data-ocr-format="md" checked> Markdown</label><label><input type="checkbox" data-ocr-format="html" checked> HTML</label><label><input type="checkbox" data-ocr-format="tex.zip" checked> LaTeX ZIP</label></div><p class="hint">MMD 与 lines.json 会始终导出。OCR 后可将 Markdown 或 MMD 交给 AI-Markdown-Translator 后端。</p></div><div class="field wide"><label class="format-options"><span><input id="repairMarkdown" type="checkbox" checked> 自动修复 Markdown/MMD 结构</span></label><p class="hint">图片本地化后保留 <code>_legacy</code> 原文，再修复代码围栏、脚注、公式环境和含公式的伪代码。</p></div><div id="repairLlmFields" class="wide">${llmConfigurationHtml()}</div>`;
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
  }
  if (state.active.id === "markdown_repair") {
    const toggle = form.querySelector("#deepRepair");
    const fields = form.querySelector("#standaloneRepairLlmFields");
    const update = () => fields?.toggleAttribute("hidden", !toggle?.checked);
    toggle?.addEventListener("change", update); update();
  }
  form.querySelector("[data-open-github-settings]")?.addEventListener("click", () => openLlmSettings(true, "github"));
  form.querySelectorAll('[data-option="rows"],[data-option="cols"],[data-option="mode"]').forEach(input => input.addEventListener("change", renderCaptions));
  if (state.active.id === "stack_videos") renderCaptions();
}

function bindLlmControls(scope) {
  scope.querySelector("#llmPreset")?.addEventListener("change", event => { state.llmPreset = event.target.value; });
  scope.querySelector("[data-open-llm-settings]")?.addEventListener("click", openLlmSettings);
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
  if (state.active.id === "pdf_ocr_translate" && !form.querySelector('[data-option="localSourcePath"]')?.value.trim()) return setTask("请填写原始 PDF 的本地绝对路径。", "bad");
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
