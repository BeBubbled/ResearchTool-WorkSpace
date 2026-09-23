const state = { tools: [], active: null, view: "tool", files: [], anki: null, captions: { titles: [], captions: [] }, poller: null, currentJob: null, translationSource: "", llmPreset: "", llmPresets: [], editingLlmId: null, mathpixConfig: null, codexConfig: null, threePassConfig: null, speechConfig: null, githubConfig: null, settingsTab: "llm" };
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
  return ["llm", "mathpix", "codex", "three-pass", "speech", "github"].includes(tab) ? tab : null;
}

async function openLlmSettings(updateHash = true, requestedTab = null) {
  state.view = "llm";
  state.settingsTab = requestedTab || settingsTabFromHash() || state.settingsTab || "llm";
  stopPolling();
  if (updateHash) history.replaceState(null, "", `#settings/${state.settingsTab}`);
  renderNav();
  taskPanel.classList.add("hidden");
  category.textContent = "全局设置";
  title.textContent = "AI、OCR、语音与 GitHub 配置";
  description.textContent = "统一管理 API 模型、Mathpix OCR、Codex 额度、Three-Pass 篇幅、Azure Speech 及 Markdown 图片发布仓库。";
  availability.textContent = "本机全局";
  availability.className = "badge ok";
  form.innerHTML = `<div class="llm-manager-loading">正在读取本机全局配置…</div>`;
  try {
    await Promise.all([loadGlobalLlmPresets(), loadGlobalMathpixConfig(), loadGlobalCodexConfig(), loadGlobalThreePassConfig(), loadGlobalSpeechConfig(), loadGlobalGithubConfig()]);
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

async function loadGlobalCodexConfig(forceRefresh = false) {
  const response = await fetch(forceRefresh ? "/api/codex/status/refresh" : "/api/codex/status", forceRefresh ? { method:"POST" } : undefined);
  const data = await response.json();
  state.codexConfig = data || { available:false, models:[], rateLimits:null };
  return state.codexConfig;
}

async function loadGlobalThreePassConfig() {
  const response = await fetch("/api/three-pass-config");
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "无法读取 Three-Pass 篇幅设置。");
  state.threePassConfig = data.threePass;
  return state.threePassConfig;
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

async function loadGlobalMathpixConfig() {
  const response = await fetch("/api/mathpix-config");
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "无法读取 Mathpix 配置。");
  state.mathpixConfig = data.mathpix || { configured: false };
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

function codexEffortsForModel(model, preferred = "") {
  const efforts = model?.supportedEfforts?.length
    ? [...model.supportedEfforts]
    : [model?.defaultEffort || preferred || "high"];
  return {
    efforts,
    selected: efforts.includes(preferred) ? preferred : (model?.defaultEffort || efforts[0]),
  };
}

function codexEffortLabel(model, effort) {
  return effort === "none" && ["gpt-5.6-sol", "gpt-5.6"].includes(model?.id)
    ? "Instant（none）"
    : effort;
}

function codexRateLimitSnapshots(rateLimits) {
  if (!rateLimits || typeof rateLimits !== "object") return [];
  if (Array.isArray(rateLimits.rateLimits)) return rateLimits.rateLimits;
  if (rateLimits.rateLimitsByLimitId && typeof rateLimits.rateLimitsByLimitId === "object") return Object.values(rateLimits.rateLimitsByLimitId);
  return rateLimits.rateLimit ? [rateLimits.rateLimit] : [];
}

function codexResetText(value) {
  const timestamp = Number(value);
  if (!Number.isFinite(timestamp) || timestamp <= 0) return "重置时间未知";
  return new Intl.DateTimeFormat("zh-CN", { month:"numeric", day:"numeric", hour:"2-digit", minute:"2-digit" }).format(new Date(timestamp * 1000));
}

function renderCodexQuotaCards(rateLimits) {
  const cards = [];
  codexRateLimitSnapshots(rateLimits).forEach((snapshot, snapshotIndex) => {
    if (!snapshot || typeof snapshot !== "object") return;
    const name = snapshot.limitName || snapshot.limitId || (snapshotIndex ? `额度 ${snapshotIndex + 1}` : "Codex 额度");
    [["主要窗口", snapshot.primary], ["次要窗口", snapshot.secondary]].forEach(([windowName, window]) => {
      if (!window || typeof window !== "object") return;
      const used = Math.min(100, Math.max(0, Number(window.usedPercent) || 0));
      const remaining = Math.max(0, 100 - used);
      const duration = Number(window.windowDurationMins);
      const durationText = Number.isFinite(duration) && duration > 0 ? ` · ${duration < 60 ? `${duration} 分钟` : `${Math.round(duration / 60)} 小时`}窗口` : "";
      cards.push(`<article class="codex-quota-card"><div><span>${escapeHtml(name)} · ${windowName}</span><strong>${remaining}%</strong><small>剩余${durationText} · ${escapeHtml(codexResetText(window.resetsAt))}重置</small></div><div class="codex-quota-track" role="progressbar" aria-label="${escapeHtml(name)}剩余额度" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${remaining}"><span style="width:${remaining}%"></span></div></article>`);
    });
  });
  return cards.length ? cards.join("") : `<div class="llm-empty-state codex-quota-empty"><strong>暂时无法读取额度</strong><p>账户或当前 Codex 版本可能未返回额度窗口；可稍后刷新。</p></div>`;
}

const THREE_PASS_PHASE_LABELS = {
  pass1: "Pass 1 · 快速定位",
  pass2: "Pass 2 · 结构与证据",
  pass3: "Pass 3 · 重点深读",
  synthesis: "综合报告",
};

function renderThreePassLengthCard(phase, config) {
  const item = config.phases?.[phase] || { level:"medium", target:config.presets?.[phase]?.medium || 0 };
  const preset = config.presets?.[phase] || {};
  const custom = item.level === "custom";
  return `<article class="three-pass-length-card" data-length-phase="${phase}">
    <div class="three-pass-length-heading"><strong>${escapeHtml(THREE_PASS_PHASE_LABELS[phase])}</strong><span data-length-summary>约 ${escapeHtml(item.target)} 字 / words</span></div>
    <label>篇幅
      <select name="${phase}Level" data-length-level>
        <option value="low" ${item.level === "low" ? "selected" : ""}>短 · ${escapeHtml(preset.low)}</option>
        <option value="medium" ${item.level === "medium" ? "selected" : ""}>中 · ${escapeHtml(preset.medium)}</option>
        <option value="long" ${item.level === "long" ? "selected" : ""}>长 · ${escapeHtml(preset.long)}</option>
        <option value="custom" ${custom ? "selected" : ""}>自定义</option>
      </select>
    </label>
    <label class="three-pass-custom-length ${custom ? "" : "hidden"}">自定义目标
      <input name="${phase}Target" data-length-target type="number" min="${escapeHtml(config.limits.min)}" max="${escapeHtml(config.limits.max)}" step="${escapeHtml(config.limits.step)}" value="${escapeHtml(item.target)}" ${custom ? "required" : ""}>
    </label>
  </article>`;
}

function renderLlmManager(message = "", kind = "", messageTarget = "llm") {
  const editing = state.editingLlmId ? state.llmPresets.find(item => item.id === state.editingLlmId) : null;
  const speech = state.speechConfig || {
    configured: false,
    region: "centralus",
    voice: "zh-CN-YunxiNeural",
  };
  const mathpix = state.mathpixConfig || { configured: false };
  const github = state.githubConfig || {
    configured: false,
    repository: "",
    branch: "main",
    imageRoot: "images",
  };
  const codex = state.codexConfig || { available:false, chatgptAuthenticated:false, models:[], rateLimits:null };
  const codexModels = codex.models || [];
  const codexModel = codexModels.find(item => item.id === codex.defaultModel) || codexModels[0] || null;
  const codexEffortView = codexEffortsForModel(codexModel, codex.defaultReasoningEffort);
  const codexReady = Boolean(codex.available && codex.chatgptAuthenticated && codexModel);
  const cards = state.llmPresets.map(preset => `
    <article class="llm-preset-card">
      <div class="llm-preset-copy">
        <div class="llm-preset-heading"><h3>${escapeHtml(preset.name)}</h3><span class="preset-id">${preset.id === "default" ? "默认配置" : escapeHtml(preset.id)}</span></div>
        <p>${escapeHtml(preset.model)} · ${escapeHtml(preset.protocol || "auto")} · 并发 ${escapeHtml(preset.concurrency || 1)}${preset.contextWindow ? ` · 上下文 ${escapeHtml(preset.contextWindow)}` : ""}</p>
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
        <button type="button" role="tab" data-settings-tab="mathpix">Mathpix</button>
        <button type="button" role="tab" data-settings-tab="codex">Codex</button>
        <button type="button" role="tab" data-settings-tab="three-pass">Three-Pass</button>
        <button type="button" role="tab" data-settings-tab="speech">Azure Speech</button>
        <button type="button" role="tab" data-settings-tab="github">GitHub</button>
      </div>
      <section class="settings-tab-panel mathpix-manager" data-settings-panel="mathpix" role="tabpanel">
        <div class="section-heading llm-manager-heading">
          <div>
            <div class="llm-preset-heading"><h3>Mathpix OCR</h3><span class="preset-id ${mathpix.configured ? "" : "speech-unconfigured"}">${mathpix.configured ? "已配置" : "未配置"}</span></div>
            <p class="hint">用于论文 PDF OCR。App ID 和 App Key 只保存在项目 .env 与当前后端进程；保存后立即启用，不会发起额外识别请求。</p>
          </div>
        </div>
        <p id="mathpixManagerStatus" class="manager-message ${messageTarget === "mathpix" ? escapeHtml(kind) : ""} ${message && messageTarget === "mathpix" ? "" : "hidden"}" role="status">${messageTarget === "mathpix" ? escapeHtml(message) : ""}</p>
        <form id="mathpixConfigForm" class="llm-editor-form mathpix-editor-form">
          <label>App ID<input name="appId" autocomplete="off" ${mathpix.configured ? "" : "required"} maxlength="1000" placeholder="${mathpix.configured ? "留空表示保持当前 App ID" : "仅保存到本机 .env"}"></label>
          <label>App Key<input name="appKey" type="password" autocomplete="new-password" ${mathpix.configured ? "" : "required"} maxlength="1000" placeholder="${mathpix.configured ? "留空表示保持当前 App Key" : "仅保存到本机 .env"}"></label>
          <div class="speech-editor-actions">
            ${mathpix.configured ? `<button type="button" class="danger-button" data-delete-mathpix>移除配置</button>` : ""}
            <button type="submit" class="primary">保存 Mathpix</button>
          </div>
        </form>
      </section>
      <section class="settings-tab-panel codex-manager" data-settings-panel="codex" role="tabpanel">
        <div class="section-heading llm-manager-heading">
          <div>
            <div class="llm-preset-heading"><h3>Codex 额度与默认设置</h3><span class="preset-id ${codexReady ? "" : "speech-unconfigured"}">${codexReady ? "已连接" : "不可用"}</span></div>
            <p class="hint">用于本工具箱内消耗 ChatGPT/Codex 额度的任务；不会修改全局 Codex CLI 配置。</p>
          </div>
          <div class="codex-heading-actions">
            ${codex.chatgptAuthenticated ? `<button type="button" class="danger-button" data-codex-logout>退出登录</button>` : `<button type="button" class="test-button" data-codex-login>浏览器登录</button>`}
            <button type="button" class="test-button" data-refresh-codex>刷新账户与额度</button>
          </div>
        </div>
        <p id="codexManagerStatus" class="manager-message ${messageTarget === "codex" ? escapeHtml(kind) : ""} ${message && messageTarget === "codex" ? "" : (codexReady ? "ok" : "warn")}" role="status">${messageTarget === "codex" && message ? escapeHtml(message) : escapeHtml(codexReady ? `ChatGPT ${codex.planType || "账户"} 已连接 · ${codexModels.length} 个可用模型` : (codex.error || "Codex 尚未通过 ChatGPT 登录。"))}</p>
        <div class="codex-quota-heading"><div><span class="eyebrow">USAGE</span><h4>剩余额度</h4></div><span class="hint">额度是即时快照，刷新后更新</span></div>
        <div class="codex-quota-grid">${renderCodexQuotaCards(codex.rateLimits)}</div>
        <form id="codexConfigForm" class="llm-editor-form codex-editor-form">
          <label>默认模型<select name="model" ${codexReady ? "" : "disabled"}>${codexModels.length ? codexModels.map(model => `<option value="${escapeHtml(model.id)}" ${model.id === codexModel?.id ? "selected" : ""}>${escapeHtml(model.displayName || model.id)}</option>`).join("") : `<option value="">当前无可用模型</option>`}</select><span class="hint">新建 Codex 论文分析时自动选中。</span></label>
          <label>默认推理强度<select name="reasoningEffort" ${codexReady ? "" : "disabled"}>${codexEffortView.efforts.map(effort => `<option value="${escapeHtml(effort)}" ${effort === codexEffortView.selected ? "selected" : ""}>${escapeHtml(codexEffortLabel(codexModel, effort))}</option>`).join("")}</select><span class="hint">Instant 响应最快；强度越高通常越慢，也会使用更多 token。</span></label>
          <div class="llm-editor-actions"><button type="submit" class="primary" ${codexReady ? "" : "disabled"}>保存 Codex 默认值</button></div>
        </form>
      </section>
      <section class="settings-tab-panel three-pass-settings" data-settings-panel="three-pass" role="tabpanel">
        <div class="section-heading llm-manager-heading">
          <div>
            <div class="llm-preset-heading"><h3>Three-Pass 分阶段篇幅</h3><span class="preset-id">全局默认</span></div>
            <p class="hint">同时用于 Codex 与直接 API。中文按目标字数、英文按目标 words 控制，实际输出允许约 ±${escapeHtml(state.threePassConfig?.tolerancePercent || 20)}% 浮动。</p>
          </div>
        </div>
        <p id="threePassConfigStatus" class="manager-message ${messageTarget === "three-pass" ? escapeHtml(kind) : ""} ${message && messageTarget === "three-pass" ? "" : "hidden"}" role="status">${messageTarget === "three-pass" ? escapeHtml(message) : ""}</p>
        <form id="threePassConfigForm">
          <div class="three-pass-length-grid">${Object.keys(THREE_PASS_PHASE_LABELS).map(phase => renderThreePassLengthCard(phase, state.threePassConfig)).join("")}</div>
          <p class="hint three-pass-length-note">这是可见报告的目标篇幅，不会机械截断。直接 API 的“最大输出 token”仍是独立安全上限。</p>
          <div class="llm-editor-actions three-pass-length-actions"><button type="button" data-reset-three-pass>恢复推荐值</button><button type="submit" class="primary">保存 Three-Pass 篇幅</button></div>
        </form>
      </section>
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
          <label>API 协议<select name="protocol"><option value="auto" ${(editing?.protocol || "auto") === "auto" ? "selected" : ""}>自动识别</option><option value="responses" ${editing?.protocol === "responses" ? "selected" : ""}>Responses API</option><option value="chat_completions" ${editing?.protocol === "chat_completions" ? "selected" : ""}>Chat Completions</option></select><span class="hint">OpenAI 官方地址在自动模式下使用 Responses API。</span></label>
          <label>上下文窗口（可选）<input name="contextWindow" type="number" value="${escapeHtml(editing?.contextWindow || "")}" min="8000" max="10000000" step="1000" placeholder="例如：128000"><span class="hint">用于长论文分块与调用次数预估。</span></label>
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
  form.querySelector("#codexConfigForm")?.addEventListener("submit", saveGlobalCodexConfig);
  form.querySelector("#codexConfigForm [name=model]")?.addEventListener("change", updateGlobalCodexEfforts);
  form.querySelector("[data-refresh-codex]")?.addEventListener("click", event => refreshGlobalCodexConfig(event.currentTarget));
  form.querySelector("[data-codex-login]")?.addEventListener("click", event => startGlobalCodexLogin(event.currentTarget));
  form.querySelector("[data-codex-logout]")?.addEventListener("click", event => logoutGlobalCodex(event.currentTarget));
  form.querySelector("#threePassConfigForm")?.addEventListener("submit", saveGlobalThreePassConfig);
  form.querySelectorAll("[data-length-level]").forEach(select => select.addEventListener("change", () => updateThreePassLengthCard(select.closest("[data-length-phase]"))));
  form.querySelectorAll("[data-length-target]").forEach(input => input.addEventListener("input", () => updateThreePassLengthCard(input.closest("[data-length-phase]"))));
  form.querySelector("[data-reset-three-pass]")?.addEventListener("click", resetGlobalThreePassConfig);
  form.querySelector("#speechConfigForm")?.addEventListener("submit", saveGlobalSpeechConfig);
  form.querySelector("[data-test-speech]")?.addEventListener("click", event => testGlobalSpeechConfig(event.currentTarget));
  form.querySelector("[data-delete-speech]")?.addEventListener("click", removeGlobalSpeechConfig);
  form.querySelector("#mathpixConfigForm")?.addEventListener("submit", saveGlobalMathpixConfig);
  form.querySelector("[data-delete-mathpix]")?.addEventListener("click", removeGlobalMathpixConfig);
  form.querySelector("#githubConfigForm")?.addEventListener("submit", saveGlobalGithubConfig);
  form.querySelector("[data-test-github]")?.addEventListener("click", event => testGlobalGithubConfig(event.currentTarget));
  form.querySelector("[data-delete-github]")?.addEventListener("click", removeGlobalGithubConfig);
  activateSettingsTab(state.settingsTab, false);
}

function activateSettingsTab(tab, updateHash = true) {
  if (!["llm", "mathpix", "codex", "three-pass", "speech", "github"].includes(tab)) return;
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

function updateThreePassLengthCard(card) {
  if (!card) return;
  const phase = card.dataset.lengthPhase;
  const level = card.querySelector("[data-length-level]").value;
  const customLabel = card.querySelector(".three-pass-custom-length");
  const targetInput = card.querySelector("[data-length-target]");
  const isCustom = level === "custom";
  customLabel.classList.toggle("hidden", !isCustom);
  targetInput.required = isCustom;
  const presetTarget = state.threePassConfig?.presets?.[phase]?.[level];
  if (!isCustom && presetTarget) targetInput.value = presetTarget;
  const target = isCustom ? targetInput.value : presetTarget;
  card.querySelector("[data-length-summary]").textContent = `约 ${target || "—"} 字 / words`;
}

function threePassConfigFormPayload(formNode) {
  const phases = {};
  formNode.querySelectorAll("[data-length-phase]").forEach(card => {
    const phase = card.dataset.lengthPhase;
    const level = card.querySelector("[data-length-level]").value;
    phases[phase] = { level };
    if (level === "custom") phases[phase].target = Number(card.querySelector("[data-length-target]").value);
  });
  return { phases };
}

async function saveGlobalThreePassConfig(event) {
  event.preventDefault();
  const editor = event.currentTarget;
  if (!editor.reportValidity()) return;
  const submit = editor.querySelector('[type="submit"]');
  submit.disabled = true;
  try {
    const response = await fetch("/api/three-pass-config", {
      method:"PUT",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify(threePassConfigFormPayload(editor)),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "保存 Three-Pass 篇幅失败。");
    state.threePassConfig = data.threePass;
    renderLlmManager("Three-Pass 四个阶段的目标篇幅已保存。", "ok", "three-pass");
  } catch (error) {
    const status = form.querySelector("#threePassConfigStatus");
    status.textContent = error.message;
    status.className = "manager-message bad";
    submit.disabled = false;
  }
}

function resetGlobalThreePassConfig() {
  form.querySelectorAll("[data-length-phase]").forEach(card => {
    const phase = card.dataset.lengthPhase;
    card.querySelector("[data-length-level]").value = "medium";
    card.querySelector("[data-length-target]").value = state.threePassConfig.presets[phase].medium;
    updateThreePassLengthCard(card);
  });
  const status = form.querySelector("#threePassConfigStatus");
  status.textContent = "已恢复推荐的中等篇幅；点击保存后生效。";
  status.className = "manager-message warn";
}

function updateGlobalCodexEfforts() {
  const editor = form.querySelector("#codexConfigForm");
  const modelId = editor?.querySelector("[name=model]")?.value;
  const effortSelect = editor?.querySelector("[name=reasoningEffort]");
  if (!effortSelect) return;
  const model = (state.codexConfig?.models || []).find(item => item.id === modelId);
  const view = codexEffortsForModel(model, model?.defaultEffort);
  effortSelect.innerHTML = view.efforts.map(effort => `<option value="${escapeHtml(effort)}">${escapeHtml(codexEffortLabel(model, effort))}</option>`).join("");
  effortSelect.value = view.selected;
}

async function saveGlobalCodexConfig(event) {
  event.preventDefault();
  const editor = event.currentTarget;
  const submit = editor.querySelector('[type="submit"]');
  const payload = Object.fromEntries(new FormData(editor).entries());
  submit.disabled = true;
  try {
    const response = await fetch("/api/codex-config", {
      method:"PUT",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify(payload),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "保存 Codex 默认设置失败。");
    state.codexConfig = data.codex;
    const model = data.codex.models?.find(item => item.id === data.codex.defaultModel);
    renderLlmManager(`已保存：${model?.displayName || data.codex.defaultModel} · ${data.codex.defaultReasoningEffort}`, "ok", "codex");
  } catch (error) {
    const status = form.querySelector("#codexManagerStatus");
    status.textContent = error.message;
    status.className = "manager-message bad";
    submit.disabled = false;
  }
}

async function refreshGlobalCodexConfig(button) {
  button.disabled = true;
  button.textContent = "刷新中…";
  try {
    await loadGlobalCodexConfig(true);
    renderLlmManager("账户、可用模型与额度已刷新。", state.codexConfig?.chatgptAuthenticated ? "ok" : "warn", "codex");
  } catch (error) {
    const status = form.querySelector("#codexManagerStatus");
    status.textContent = `刷新失败：${error.message}`;
    status.className = "manager-message bad";
    if (button.isConnected) {
      button.disabled = false;
      button.textContent = "刷新账户与额度";
    }
  }
}

async function startGlobalCodexLogin(button) {
  const status = form.querySelector("#codexManagerStatus");
  const loginWindow = window.open("about:blank", "research-toolkit-codex-login");
  if (loginWindow) loginWindow.opener = null;
  button.disabled = true;
  button.textContent = "正在启动…";
  try {
    const response = await fetch("/api/codex/login", {
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({ flow:"browser" }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "无法启动 Codex 登录。");
    if (data.authUrl && loginWindow) loginWindow.location.href = data.authUrl;
    else if (data.authUrl) window.open(data.authUrl, "_blank", "noopener,noreferrer");
    status.textContent = "已打开 ChatGPT 登录页；完成授权后，本页会自动刷新。";
    status.className = "manager-message";
    for (let index = 0; index < 90; index += 1) {
      await new Promise(resolve => setTimeout(resolve, 2000));
      const progressResponse = await fetch(`/api/codex/login/${encodeURIComponent(data.loginId)}`);
      const progress = await progressResponse.json();
      if (progress.status === "failed") throw new Error(progress.error || "Codex 登录失败。");
      if (progress.success) {
        await loadGlobalCodexConfig(true);
        renderLlmManager("Codex 登录成功，账户与额度已刷新。", "ok", "codex");
        return;
      }
    }
    throw new Error("等待登录超时；完成登录后请点击“刷新账户与额度”。");
  } catch (error) {
    const currentStatus = form.querySelector("#codexManagerStatus");
    if (currentStatus) {
      currentStatus.textContent = error.message;
      currentStatus.className = "manager-message bad";
    }
    if (button.isConnected) {
      button.disabled = false;
      button.textContent = "浏览器登录";
    }
  }
}

async function logoutGlobalCodex(button) {
  if (!window.confirm("确定退出当前 Codex ChatGPT 登录吗？使用 Codex 额度的任务将暂时不可用。")) return;
  button.disabled = true;
  try {
    const response = await fetch("/api/codex/logout", { method:"POST" });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "退出 Codex 登录失败。");
    await loadGlobalCodexConfig(true);
    renderLlmManager("已退出 Codex 登录。", "ok", "codex");
  } catch (error) {
    const status = form.querySelector("#codexManagerStatus");
    status.textContent = error.message;
    status.className = "manager-message bad";
    if (button.isConnected) button.disabled = false;
  }
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

async function saveGlobalMathpixConfig(event) {
  event.preventDefault();
  const editorForm = event.currentTarget;
  const submit = editorForm.querySelector('[type="submit"]');
  const payload = Object.fromEntries(new FormData(editorForm).entries());
  submit.disabled = true;
  try {
    const response = await fetch("/api/mathpix-config", {
      method:"PUT",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify(payload),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "保存 Mathpix 配置失败。");
    state.mathpixConfig = data.mathpix;
    await reloadTools();
    renderLlmManager("已保存 Mathpix 配置；论文 PDF OCR 已立即更新。", "ok", "mathpix");
  } catch (error) {
    const status = form.querySelector("#mathpixManagerStatus");
    status.textContent = error.message;
    status.className = "manager-message bad";
    submit.disabled = false;
  }
}

async function removeGlobalMathpixConfig() {
  if (!window.confirm("确定移除 Mathpix 配置吗？论文 PDF OCR 将停止可用。")) return;
  try {
    const response = await fetch("/api/mathpix-config", { method:"DELETE" });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "移除 Mathpix 配置失败。");
    state.mathpixConfig = data.mathpix;
    await reloadTools();
    renderLlmManager("已移除 Mathpix 配置；论文 PDF OCR 已停用。", "ok", "mathpix");
  } catch (error) {
    const status = form.querySelector("#mathpixManagerStatus");
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
  if (id === "markdown_repair") fields = `<div class="field wide"><p class="hint">会自动将含空格但未用尖括号包裹的本地图片路径（如 <code>![图](Conceptual Framework.png)</code>）修复为合法 Markdown；代码和带标题的图片引用保持不变。</p></div><div class="field wide"><label class="format-options"><span><input id="mathRepair" type="checkbox" checked> 修复并校验行间公式 <code>$$</code></span></label><p class="hint">无需 LLM；会跳过围栏代码和行内公式，只自动修复高置信度的公式边界问题。</p></div><div class="field wide"><label class="format-options"><span><input id="footnoteRepair" type="checkbox" checked> 修复 OCR 合并脚注</span></label><p class="hint">无需 LLM；仅在正文上标与合并脚注定义可确定对应时拆分，歧义内容不会猜测修改。</p></div><div class="field wide"><label class="format-options"><span><input id="normalizeChinesePunctuation" type="checkbox"> 中文标点转英文符号</span></label><p class="hint">将中文逗号、句号、引号、括号等转为英文符号；代码、公式、链接与 URL 保持不变。</p></div><div class="field wide"><label class="format-options"><span><input id="deepRepair" type="checkbox"> 深度结构修复（OCR Markdown）</span></label><p class="hint">需要 LLM；修复围栏、脚注、公式环境及含公式的伪代码。仅在需要处理 OCR 结构异常时开启。</p></div><div id="standaloneRepairLlmFields" class="wide" hidden>${llmConfigurationHtml()}</div>`;
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
