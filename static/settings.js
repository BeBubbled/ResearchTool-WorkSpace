/* 全局设置：LLM、Mathpix、Codex、Claude Code、Google 翻译、OpenAlex、精读默认行为、双语改稿工作流、Three-Pass、Azure Speech、GitHub。
   Standalone at /settings, and embedded by the client shell's settings drawer (/settings?embed=1). */
(() => {
"use strict";
const SETTINGS_TABS = ["general", "storage", "appearance", "doctor", "usage", "llm", "mathpix", "codex", "claude", "billing", "google", "openalex", "reader", "workflow", "three-pass", "studio", "speech", "github", "advanced"];
// Sidebar groups (macOS System Settings style): accounts & engines, services, then per-module defaults.
const SETTINGS_GROUPS = [
  { label: "通用", items: [["general", "通用"], ["storage", "数据存储"], ["appearance", "外观与语言"], ["doctor", "环境检查"], ["usage", "用量"]] },
  { label: "账号与引擎", items: [["codex", "Codex"], ["claude", "Claude Code"], ["llm", "API 预设"], ["billing", "费用确认"]] },
  { label: "服务", items: [["mathpix", "Mathpix OCR"], ["google", "Google 翻译"], ["openalex", "OpenAlex 文献"], ["speech", "Azure 语音"], ["github", "GitHub 图片"]] },
  { label: "功能", items: [["reader", "精读"], ["workflow", "双语改稿"], ["three-pass", "Three-Pass"], ["studio", "ResearchStudio"]] },
  { label: "高级", items: [["advanced", "高级"]] },
];
const LAST_PANE_KEY = "research-toolkit.settings.last-pane";
function storedPane() {
  try { const pane = localStorage.getItem(LAST_PANE_KEY); return SETTINGS_TABS.includes(pane) ? pane : ""; } catch { return ""; }
}
const state = { llmPresets: [], editingLlmId: null, mathpixConfig: null, codexConfig: null, threePassConfig: null, speechConfig: null, githubConfig: null, googleConfig: null, openalexConfig: null, storageConfig: null, claudeConfig: null, workflowOptions: null, readerActions: [], studioConfig: null, settingsTab: "llm", settingsQuery: "" };
const escapeHtml = (value) => String(value).replace(/[&<>'"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[c]));
let form = null;
let hooks = { onTabChange() {}, onChanged() {}, onSaved() {} };

// Tools availability (Mathpix / GitHub) changed: let the host page refresh what it shows.
async function reloadTools() { await hooks.onChanged(); }

// Fast endpoints (plain config reads) gate the first paint; slow ones (Codex / Claude CLI status, the
// revision workflow and the ResearchStudio environment probe each spawn or query a local program) fill in
// their own panes when they arrive, so the window never waits on the slowest check.
const SLOW_LOADERS = {
  codex: () => loadGlobalCodexConfig(),
  claude: () => loadGlobalClaudeConfig().catch(error => { state.claudeConfig = { available: false, error: error.message }; }),
  workflow: () => loadGlobalWorkflowConfig().catch(error => { state.workflowError = error.message; }),
  studio: () => loadStudioConfig(),
};
const pending = new Set();
// Fields the user typed into since the last full render; a background refresh puts these values back.
const dirtyFields = new Set();
const fieldKey = node => node?.matches?.("input, select, textarea") ? `${node.form?.id || node.closest("[data-settings-panel]")?.dataset.settingsPanel || ""}|${node.name || node.id || ""}` : "";
const fieldByKey = key => [...form.querySelectorAll("input, select, textarea")].find(node => fieldKey(node) === key);

async function mount(root, options = {}) {
  form = root;
  hooks = { ...hooks, ...options };
  // HIG: reopen on the pane the user last looked at unless a link names one.
  state.settingsTab = SETTINGS_TABS.includes(options.tab) ? options.tab : storedPane() || state.settingsTab;
  form.innerHTML = `<div class="llm-manager-loading">正在读取本机全局配置…</div>`;
  if (!form.dataset.dirtyTracking) {
    form.dataset.dirtyTracking = "1";
    const track = event => { const key = fieldKey(event.target); if (key && key !== "|settingsSearch") dirtyFields.add(key); };
    form.addEventListener("input", track);
    form.addEventListener("change", track);
  }
  let rendered = false;
  const slow = Object.entries(SLOW_LOADERS).map(([key, load]) => {
    pending.add(key);
    return Promise.resolve().then(load).catch(() => {}).finally(() => { pending.delete(key); if (rendered) refreshKeepingEdits(); });
  });
  try {
    await Promise.all([loadGlobalLlmPresets(), loadGlobalMathpixConfig(), loadGlobalThreePassConfig(), loadGlobalSpeechConfig(), loadGlobalGithubConfig(), loadGlobalGoogleConfig(), loadGlobalOpenalexConfig(), loadStorageConfig(), loadReaderActions()]);
    renderLlmManager();
    rendered = true;
  } catch (error) {
    form.innerHTML = `<p class="message">${escapeHtml(error.message)}</p><button type="button" data-retry-llm>重新加载</button>`;
    form.querySelector("[data-retry-llm]")?.addEventListener("click", () => mount(root, options));
  }
  return Promise.all(slow);
}

// Re-render after background data arrived without losing what the user is typing, the focus or the scroll.
function refreshKeepingEdits() {
  const values = [...dirtyFields].map(key => { const node = fieldByKey(key); return node ? [key, node.type === "checkbox" ? node.checked : node.value] : null; }).filter(Boolean);
  const active = document.activeElement;
  const activeKey = fieldKey(active) || (active?.id ? `#${active.id}` : "");
  const caret = typeof active?.selectionStart === "number" ? [active.selectionStart, active.selectionEnd] : null;
  const scroller = document.scrollingElement;
  const top = scroller?.scrollTop || 0;
  renderLlmManager("", "", "llm", { keepEdits: true });
  values.forEach(([key, value]) => { const node = fieldByKey(key); if (!node) return; if (node.type === "checkbox") node.checked = value; else node.value = value; });
  const target = activeKey.startsWith("#") ? form.querySelector(activeKey) : activeKey ? fieldByKey(activeKey) : null;
  if (target && form.contains(active) === false) {
    target.focus({ preventScroll: true });
    if (caret) { try { target.setSelectionRange(...caret); } catch {} }
  }
  if (scroller) scroller.scrollTop = top;
}

// 精读 defaults are per-browser: they share static/reader.js's localStorage entry, and the reader picks up
// changes through the storage event, so nothing here goes to the server.
const READER_SETTINGS_KEY = "paper-lens.selection-settings.v1";
// Keep in sync with DEFAULT_READING_LAYOUT.margins in static/reader.js.
const READER_MARGIN_DEFAULTS = {
  header: { top: 18, right: 48, bottom: 12, left: 48 },
  body: { top: 38, right: 64, bottom: 56, left: 64 },
  footer: { top: 12, right: 48, bottom: 24, left: 48 },
};
const READER_MARGIN_ZONES = [["header", "页眉"], ["body", "正文"], ["footer", "页脚"]];
const READER_MARGIN_SIDES = [["top", "上"], ["right", "右"], ["bottom", "下"], ["left", "左"]];

// ---- 通用 / 费用确认 / 高级: browser-side preferences (nothing here reaches the server or .env).
const BILLING_ENGINES = [
  ["codex", "Codex", "使用 ChatGPT / Codex 套餐额度：精读 Three-Pass、研究空白地图、ResearchStudio、双语改稿。"],
  ["claude", "Claude Code", "使用 Claude 套餐额度：双语改稿的翻译、修订与检查。"],
  ["api", "API 预设", "按所选预设的 API Key 单独计费：划词解读、Three-Pass 直接 API、双语改稿。"],
  ["google", "Google 翻译", "Cloud Translation 基础版按字符计费，每月前 50 万字符免费：双语改稿的快速翻译。"],
];
// Remembered layout state (pane widths, open tabs, sort orders…) — cleared by 重置界面布局.
const LAYOUT_PREFIXES = ["research-toolkit.inspector.", "research-toolkit.library.", "research-toolkit.settings.", "research-toolkit.prompts.", "paper-lens.selection-more-open", "health.trend-scale", "paperRevision.commentDialogPos"];
// Exportable browser preferences: layout state plus reader defaults and cost acknowledgements.
const EXPORT_PREFIXES = [...LAYOUT_PREFIXES, "research-toolkit.billing-ack", "paper-lens.selection-settings.v1", "research-toolkit.toolbox.", "research-toolkit.activity.desktop", "research-theme", "research-lang", "research-font", "research-ink", "research-content-ink"];
const storedKeys = prefixes => {
  const keys = [];
  try { for (let i = 0; i < localStorage.length; i += 1) { const key = localStorage.key(i); if (prefixes.some(prefix => key.startsWith(prefix))) keys.push(key); } } catch {}
  return keys.sort();
};

function renderGeneralSettingsPanel() {
  const shortcut = (keys, text) => `<tr><td><kbd>${keys}</kbd></td><td>${text}</td></tr>`;
  const mod = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent) ? "⌘" : "Ctrl";
  return `
      <section class="settings-tab-panel general-settings" data-settings-panel="general" role="tabpanel">
        <div class="section-heading llm-manager-heading">
          <div><div class="llm-preset-heading"><h3>通用</h3><span class="preset-id">保存在此浏览器</span></div>
          <p class="hint">命令面板、快捷键与界面布局。各模块的默认值在“功能”分组里。</p></div>
        </div>
        <p id="generalSettingsStatus" class="manager-message hidden" role="status"></p>
        <div class="reader-settings-group">
          <h4>命令面板与快捷键</h4>
          <table class="shortcut-table">${shortcut(`${mod} K`, "命令面板：执行当前页面的命令、前往模块或设置、打开文献库里的论文")}${shortcut("?", "显示当前页面的快捷键")}${shortcut("/", "聚焦工具箱或文献库的搜索框")}${shortcut("Alt 1…4", "切换右侧检查器的标签（精读、改稿）或视图（研究空白）")}${shortcut(`${mod} \\`, "开关左侧目录或章节")}${shortcut(`${mod} Enter`, "改稿：提交本轮；Prompt：提交到 ChatGPT")}</table>
          <p class="hint">字母和数字快捷键在输入框里不生效；带 ${mod} 的组合键随时可用。</p>
        </div>
        <div class="reader-settings-group">
          <h4>任务完成提醒</h4>
          <p class="hint">OCR、翻译、Three-Pass、改稿、研究空白和 ResearchStudio 的任务结束时，页面右下角会出现提示，左侧导航上显示进行中的任务数。开启桌面通知后，切到其他窗口时也会收到系统通知。</p>
          <label class="reader-switch-row"><span><strong>桌面通知</strong><small data-desktop-notify-state>${desktopNotifyText()}</small></span><input type="checkbox" role="switch" data-desktop-notify ${activityApi()?.desktopEnabled?.() ? "checked" : ""} ${"Notification" in window ? "" : "disabled"}></label>
        </div>
        <div class="reader-settings-group">
          <div class="reader-group-heading"><h4>界面布局</h4><button type="button" data-reset-layout>重置界面布局</button></div>
          <p class="hint">检查器的宽度和打开的标签、面板开关、文献库的来源与排序、上次打开的设置面板等会记在浏览器里。当前记住了 <strong>${storedKeys(LAYOUT_PREFIXES).length}</strong> 项。重置不影响精读默认值、费用确认和任何账号配置。</p>
        </div>
      </section>`;
}

// ---- 外观与语言: language, colour theme, interface font and text colour. Saved per browser; every open page
// (and the page behind the settings window) follows at once — see static/theme.js and static/i18n.js.
const FONT_CHOICES = [
  ["default", "默认", "各模块原有字体"],
  ["system", "系统", "跟随操作系统界面字体"],
  ["misans", "MiSans", "小米开源字体，未安装时从 jsDelivr 加载"],
  ["anthropic", "Anthropic Sans", "需要本机已安装该字体"],
  ["custom", "自定义", "输入本机已安装的字体名称"],
];
const INK_CHOICES = [["default", "主题默认", ""], ["black", "纯黑", "#000000"], ["graphite", "石墨", "#1f2328"], ["custom", "自定义", ""]];
const CONTENT_INK_CHOICES = [["default", "跟随文字颜色", ""], ["black", "纯黑", "#000000"], ["graphite", "石墨", "#1f2328"], ["custom", "自定义", ""]];

// One row of colour choices (文字颜色 / 正文文字颜色); `key` is the appearance field it writes.
function inkChoices(key, label, choices, value) {
  const mode = /^#/.test(value) ? "custom" : value;
  const custom = /^#/.test(value) ? value : "#2f5fb3";
  return `
          <div class="appearance-inks" role="radiogroup" aria-label="${label}" data-ink-key="${key}">${choices.map(([id, name, color]) => `
            <label class="appearance-ink ${id === mode ? "active" : ""}" data-ink="${id}"><input type="radio" name="appearance-${key}" value="${id}" ${id === mode ? "checked" : ""}><i aria-hidden="true" style="${color ? `background:${color}` : id === "custom" ? `background:${custom}` : ""}"></i><span>${name}</span>${id === "custom" ? `<code data-ink-hex>${mode === "custom" ? custom : ""}</code>` : ""}</label>`).join("")}
            <input type="color" data-ink-color value="${escapeHtml(custom)}" aria-label="选择${label}" title="选择任意颜色">
          </div>`;
}
const themeApi = () => window.ClientTheme;
const i18nApi = () => window.ClientI18n;

function fontNote(font, custom) {
  const api = themeApi();
  if (!api?.fontAvailable) return "";
  if (font === "anthropic") return api.fontAvailable("Anthropic Sans") || api.fontAvailable("Anthropic Sans Text") ? "已在本机找到 Anthropic Sans。" : "本机未安装 Anthropic Sans，暂时显示为系统字体。安装后刷新页面即可生效。";
  if (font === "misans") {
    if (api.fontAvailable("MiSans VF")) return "已使用本机安装的 MiSans。";
    const loaded = [...(document.fonts || [])].some(face => face.family.replace(/"/g, "") === "MiSans" && face.status === "loaded");
    return loaded ? "已从 jsDelivr 加载 MiSans；离线时显示为系统字体，可安装到本机离线使用。" : "正在从 jsDelivr 加载 MiSans（需要联网）；也可以安装到本机离线使用。";
  }
  if (font === "custom") {
    const first = String(custom || "").split(",")[0].trim();
    if (!first) return "输入字体名称，多个用逗号分隔，按顺序回退。";
    return api.fontAvailable(first) ? `已在本机找到 ${first}。` : `本机未找到 ${first}，请检查名称是否与系统里的字体名一致。`;
  }
  return "";
}

function renderAppearanceSettingsPanel() {
  const look = themeApi()?.appearance?.() || { font: "default", fontCustom: "", ink: "default", contentInk: "default" };
  const lang = i18nApi()?.current?.() || "zh";
  const theme = themeApi()?.current?.() || "paper";
  const languages = i18nApi()?.languages || [{ id: "zh", name: "简体中文" }, { id: "en", name: "English" }];
  return `
      <section class="settings-tab-panel appearance-settings" data-settings-panel="appearance" role="tabpanel">
        <div class="section-heading llm-manager-heading">
          <div><div class="llm-preset-heading"><h3>外观与语言</h3><span class="preset-id">保存在此浏览器</span></div>
          <p class="hint">界面语言、颜色主题、界面字体与文字颜色。修改后所有打开的页面立即生效。</p></div>
        </div>
        <div class="reader-settings-group">
          <h4>语言 · Language</h4>
          <div class="appearance-segment" role="radiogroup" aria-label="界面语言">${languages.map(item => `
            <label><input type="radio" name="appearanceLang" value="${item.id}" ${item.id === lang ? "checked" : ""}><span data-i18n-skip>${item.name}</span></label>`).join("")}
          </div>
          <p class="hint">英文界面覆盖导航、设置、改稿与精读的菜单和按钮；论文正文、译文、批注与笔记保持原样。少数提示可能仍显示中文。</p>
        </div>
        <div class="reader-settings-group">
          <h4>颜色主题</h4>
          <div class="appearance-themes" role="radiogroup" aria-label="颜色主题">${(themeApi()?.themes || []).map(item => `
            <button type="button" role="radio" class="appearance-theme ${item.id === theme ? "active" : ""}" aria-checked="${item.id === theme}" data-appearance-theme="${item.id}"><i aria-hidden="true" style="background:linear-gradient(90deg,${item.bg} 0 58%,${item.accent} 58%)"></i><span>${escapeHtml(item.name)}</span></button>`).join("")}
          </div>
          <p class="hint">也可以从左侧导航底部的“主题”用滚轮快速预览。</p>
        </div>
        <div class="reader-settings-group">
          <h4>界面字体</h4>
          <div class="appearance-fonts" role="radiogroup" aria-label="界面字体">${FONT_CHOICES.map(([id, label, note]) => `
            <label class="appearance-font ${id === look.font ? "active" : ""}"><input type="radio" name="appearanceFont" value="${id}" ${id === look.font ? "checked" : ""}>
              <strong class="appearance-font-${id}">${label}</strong><small>${note}</small></label>`).join("")}
          </div>
          <label class="appearance-custom-font ${look.font === "custom" ? "" : "hidden"}"><span>字体名称</span><input data-appearance-font-custom value="${escapeHtml(look.fontCustom)}" placeholder="例如 HarmonyOS Sans SC, Source Han Sans SC" autocomplete="off" spellcheck="false"></label>
          <p class="hint" data-appearance-font-note>${escapeHtml(fontNote(look.font, look.fontCustom))}</p>
          <p class="hint">影响菜单、按钮、设置与面板文字。精读的阅读字体在精读的“排版”选项卡里设置，改稿正文字体在改稿的“视图”选项卡里切换。</p>
        </div>
        <div class="reader-settings-group">
          <h4>文字颜色</h4>${inkChoices("ink", "文字颜色", INK_CHOICES, look.ink)}
          <p class="hint">所有界面文字：标题、菜单、按钮、标签、说明与占位提示。次要文字按同一颜色自动调浅，层次保持不变。主题默认是略带主题色调的深色。</p>
          <p class="appearance-preview" data-i18n-skip>界面文字 Interface <span style="color:var(--t-ink-2)">次要文字</span> <span style="color:var(--t-muted)">说明文字</span> <span style="color:var(--t-faint)">占位提示</span></p>
        </div>
        <div class="reader-settings-group">
          <h4>正文文字颜色</h4>${inkChoices("contentInk", "正文文字颜色", CONTENT_INK_CHOICES, look.contentInk || "default")}
          <p class="hint">具体文本内容：精读的论文正文与 AI 回答、笔记，改稿的原文与译文，提示词编辑区和各处输入的长文本。PDF 原版页面是图片，颜色不受影响。</p>
          <p class="appearance-preview appearance-preview-content" data-i18n-skip>示例正文 Sample text — The quick brown fox jumps over the lazy dog. 永和九年，岁在癸丑。</p>
        </div>
      </section>`;
}

function wireAppearanceSettings() {
  const panel = form.querySelector('[data-settings-panel="appearance"]');
  if (!panel) return;
  const api = themeApi();
  panel.querySelectorAll('input[name="appearanceLang"]').forEach(input => input.addEventListener("change", () => i18nApi()?.set(input.value)));
  panel.querySelectorAll("[data-appearance-theme]").forEach(button => button.addEventListener("click", () => {
    api?.apply(button.dataset.appearanceTheme);
    panel.querySelectorAll("[data-appearance-theme]").forEach(item => { const on = item === button; item.classList.toggle("active", on); item.setAttribute("aria-checked", String(on)); });
  }));
  const note = panel.querySelector("[data-appearance-font-note]");
  const customField = panel.querySelector(".appearance-custom-font");
  const customInput = panel.querySelector("[data-appearance-font-custom]");
  const refreshFont = () => {
    const look = api?.appearance?.() || {};
    panel.querySelectorAll(".appearance-font").forEach(label => label.classList.toggle("active", label.querySelector("input").checked));
    customField.classList.toggle("hidden", look.font !== "custom");
    note.textContent = fontNote(look.font, look.fontCustom);
    // Web fonts arrive a moment later; check again once they have loaded.
    document.fonts?.ready.then(() => { note.textContent = fontNote(look.font, look.fontCustom); });
  };
  panel.querySelectorAll('input[name="appearanceFont"]').forEach(input => input.addEventListener("change", () => { api?.setAppearance({ font: input.value }); refreshFont(); if (input.value === "custom") customInput.focus(); }));
  customInput?.addEventListener("input", () => { api?.setAppearance({ fontCustom: customInput.value }); refreshFont(); });
  panel.querySelectorAll("[data-ink-key]").forEach(group => {
    const key = group.dataset.inkKey;
    const color = group.querySelector("[data-ink-color]");
    const hex = group.querySelector("[data-ink-hex]");
    const customRadio = group.querySelector('input[type=radio][value="custom"]');
    const setInk = mode => {
      api?.setAppearance({ [key]: mode === "custom" ? color.value : mode });
      group.querySelectorAll(".appearance-ink").forEach(label => label.classList.toggle("active", label.querySelector("input").checked));
      if (hex) hex.textContent = mode === "custom" ? color.value : "";
    };
    group.querySelectorAll("input[type=radio]").forEach(input => input.addEventListener("change", () => setInk(input.value)));
    // 自定义 opens the colour picker straight away; picking a colour (from the chip or the swatch) applies it.
    group.querySelector('.appearance-ink[data-ink="custom"]')?.addEventListener("click", event => { if (event.target.matches("input")) return; try { color.showPicker(); } catch { color.click(); } });
    const pick = () => {
      customRadio.checked = true;
      group.querySelector('.appearance-ink[data-ink="custom"] i')?.style.setProperty("background", color.value);
      setInk("custom");
    };
    color.addEventListener("input", pick);
    color.addEventListener("change", pick);
  });
}

// The drawer embeds this page without the client shell; use the host page's activity API then.
const activityApi = () => window.ClientActivity || (window.parent !== window ? window.parent.ClientActivity : null);

function desktopNotifyText() {
  if (!("Notification" in window)) return "这个浏览器不支持桌面通知。";
  if (Notification.permission === "denied") return "浏览器已拒绝通知权限，请在地址栏的网站设置里允许后再开启。";
  return activityApi()?.desktopEnabled?.() ? "已开启：页面在后台时，任务结束会弹出系统通知。" : "未开启。";
}

// ---- 环境检查 (GET /api/doctor) and 用量 (GET /api/usage): filled in after the pane renders.
function renderDoctorSettingsPanel() {
  return `
      <section class="settings-tab-panel doctor-settings" data-settings-panel="doctor" role="tabpanel">
        <div class="section-heading llm-manager-heading">
          <div><div class="llm-preset-heading"><h3>环境检查</h3><span class="preset-id">本机</span></div>
          <p class="hint">首次使用或换电脑后，从这里确认每个功能需要的账号、服务和本机程序。标“可选”的项目只影响对应功能。</p></div>
          <button type="button" data-doctor-refresh>重新检查</button>
        </div>
        <div data-doctor-body class="doctor-body"><p class="hint">正在检查…</p></div>
      </section>`;
}

function renderUsageSettingsPanel() {
  return `
      <section class="settings-tab-panel usage-settings" data-settings-panel="usage" role="tabpanel">
        <div class="section-heading llm-manager-heading">
          <div><div class="llm-preset-heading"><h3>用量</h3><span class="preset-id">本机记录</span></div>
          <p class="hint">按引擎和功能统计这台电脑发出的请求：API 预设记录提供方返回的 Token 数，Mathpix 记录页数（重复 OCR 复用的不计费），Google 翻译记录字符数；Codex 与 Claude Code 按套餐额度计费，这里只记任务次数，剩余额度见 Codex 面板。不记录任何 Prompt 内容或密钥。</p></div>
          <label class="usage-range"><span class="sr-only">时间范围</span><select data-usage-days><option value="7">最近 7 天</option><option value="30" selected>最近 30 天</option><option value="90">最近 90 天</option><option value="365">最近一年</option></select></label>
        </div>
        <div data-usage-body class="usage-body"><p class="hint">正在读取…</p></div>
      </section>`;
}

const formatCount = value => Number(value || 0).toLocaleString("zh-CN");

async function loadDoctor(refresh = false) {
  const body = form.querySelector("[data-doctor-body]");
  if (!body) return;
  body.innerHTML = `<p class="hint">正在检查…</p>`;
  try {
    const response = await fetch(`/api/doctor${refresh ? "?refresh=1" : ""}`);
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "无法完成环境检查。");
    const groups = new Map();
    data.checks.forEach(check => { if (!groups.has(check.group)) groups.set(check.group, []); groups.get(check.group).push(check); });
    const summary = data.requiredMissing
      ? `<p class="manager-message bad">有 ${data.requiredMissing} 项必需条件未满足。</p>`
      : `<p class="manager-message ok">必需条件都已满足${data.optionalMissing ? `；${data.optionalMissing} 项可选功能尚未配置` : ""}。</p>`;
    body.innerHTML = summary + [...groups].map(([group, checks]) => `
      <div class="reader-settings-group doctor-group"><h4>${escapeHtml(group)}</h4><ul class="doctor-list">${checks.map(check => `
        <li class="doctor-item ${check.ok ? "ok" : check.optional ? "optional" : "bad"}">
          <i aria-hidden="true">${check.ok ? "✓" : check.optional ? "○" : "✕"}</i>
          <span><strong>${escapeHtml(check.label)}</strong>${check.optional && !check.ok ? `<em>可选</em>` : ""}<small>${escapeHtml(check.detail || "")}</small>${check.fix ? `<small class="doctor-fix">${escapeHtml(check.fix)}</small>` : ""}</span>
          ${!check.ok && check.settingsTab ? `<button type="button" data-doctor-open="${escapeHtml(check.settingsTab)}">去设置</button>` : ""}
        </li>`).join("")}</ul></div>`).join("");
  } catch (error) {
    body.innerHTML = `<p class="manager-message bad">${escapeHtml(error.message)}</p>`;
  }
}

async function loadUsage() {
  const body = form.querySelector("[data-usage-body]");
  if (!body) return;
  const days = form.querySelector("[data-usage-days]")?.value || "30";
  try {
    const response = await fetch(`/api/usage?days=${encodeURIComponent(days)}`);
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "无法读取用量。");
    if (!data.engines.length) { body.innerHTML = `<p class="hint">这段时间还没有记录。用量从这个版本开始记录，之前的任务不在其中。</p>`; return; }
    const metric = (bucket) => [
      bucket.calls ? `${formatCount(bucket.calls)} 次` : "",
      bucket.inputTokens || bucket.outputTokens ? `输入 ${formatCount(bucket.inputTokens)} / 输出 ${formatCount(bucket.outputTokens)} Token` : "",
      bucket.pages ? `${formatCount(bucket.pages)} 页` : "",
      bucket.characters ? `${formatCount(bucket.characters)} 字符` : "",
      bucket.reused ? `复用 ${formatCount(bucket.reused)} 次（未计费）` : "",
    ].filter(Boolean).join(" · ") || "—";
    const maxDay = Math.max(1, ...data.daily.map(day => day.api + day.codex + day.claude + day.mathpix + day.google));
    const bars = data.daily.slice(-60).map(day => {
      const total = day.api + day.codex + day.claude + day.mathpix + day.google;
      return `<span class="usage-bar" style="height:${Math.max(4, Math.round(total / maxDay * 100))}%" title="${escapeHtml(day.day)}：${total} 次"></span>`;
    }).join("");
    body.innerHTML = `<div class="usage-chart" role="img" aria-label="每日请求次数">${bars}</div>` + data.engines.map(engine => `
      <div class="reader-settings-group usage-engine"><div class="reader-group-heading"><h4>${escapeHtml(engine.label)}</h4><span class="hint">${escapeHtml(metric(engine))}</span></div>
        <table class="usage-table"><tbody>${engine.features.map(feature => `<tr><td>${escapeHtml(feature.label)}</td><td>${escapeHtml(metric(feature))}</td></tr>`).join("")}</tbody></table>
      </div>`).join("");
  } catch (error) {
    body.innerHTML = `<p class="manager-message bad">${escapeHtml(error.message)}</p>`;
  }
}

function wireDoctorAndUsage() {
  form.querySelector("[data-doctor-refresh]")?.addEventListener("click", () => loadDoctor(true));
  form.querySelector("[data-doctor-body]")?.addEventListener("click", event => {
    const button = event.target.closest("[data-doctor-open]");
    if (button) form.querySelector(`[data-settings-tab="${CSS.escape(button.dataset.doctorOpen)}"]`)?.click();
  });
  form.querySelector("[data-usage-days]")?.addEventListener("change", loadUsage);
  form.querySelector("[data-desktop-notify]")?.addEventListener("change", async event => {
    const input = event.target;
    if (input.checked) {
      const result = await (activityApi()?.requestDesktop?.() || Promise.resolve("unsupported"));
      if (result !== "granted") input.checked = false;
    } else activityApi()?.setDesktop?.(false);
    const text = form.querySelector("[data-desktop-notify-state]");
    if (text) text.textContent = desktopNotifyText();
  });
}

function renderBillingSettingsPanel() {
  const ack = window.EnginePicker?.billingAck?.get() || {};
  return `
      <section class="settings-tab-panel billing-settings" data-settings-panel="billing" role="tabpanel">
        <div class="section-heading llm-manager-heading">
          <div><div class="llm-preset-heading"><h3>费用确认</h3><span class="preset-id">保存在此浏览器</span></div>
          <p class="hint">为常用的引擎确认一次费用后，提交任务时会自动勾选使用确认（勾选框仍然显示，可以针对单次取消）。未确认的引擎仍需每次手动勾选。</p></div>
        </div>
        <p id="billingSettingsStatus" class="manager-message hidden" role="status"></p>
        <div class="reader-settings-group">${BILLING_ENGINES.map(([engine, label, text]) => `
          <label class="reader-switch-row"><span><strong>${label}</strong><small>${text}</small></span><input type="checkbox" role="switch" data-billing-ack="${engine}" ${ack[engine] === true ? "checked" : ""}></label>`).join("")}
        </div>
      </section>`;
}

function storageMigrationText(migration) {
  if (!migration || !migration.from) return "";
  const parts = [];
  if (migration.moved?.length) parts.push(`已迁移：${migration.moved.join("、")}`);
  if (migration.skipped?.length) parts.push(`目标已存在而跳过：${migration.skipped.join("、")}（旧数据仍在 ${migration.from}，需要时手动合并）`);
  if (migration.failed?.length) parts.push(`失败：${migration.failed.join("；")}`);
  return parts.length ? `上次启动时从 ${migration.from} 迁移数据。${parts.join("；")}。` : "";
}

function renderStorageSettingsPanel(message, kind, messageTarget) {
  const storage = state.storageConfig;
  if (!storage) return `<section class="settings-tab-panel storage-settings" data-settings-panel="storage" role="tabpanel"><p class="manager-message bad">无法读取数据存储设置。</p></section>`;
  const custom = !storage.usingDefault || Boolean(storage.configured);
  const notices = [];
  if (storage.warning) notices.push(["bad", storage.warning]);
  if (storage.restartRequired) notices.push(["warn", `设置已保存，重启面板后改用 ${storage.configured || storage.default}${storage.pendingMove ? "，并在启动时把现有数据从当前目录移过去" : ""}。`]);
  const migration = storageMigrationText(storage.migration);
  if (migration) notices.push([storage.migration.failed?.length ? "bad" : "ok", migration]);
  const show = messageTarget === "storage" && message;
  return `
      <section class="settings-tab-panel storage-settings" data-settings-panel="storage" role="tabpanel">
        <div class="section-heading llm-manager-heading">
          <div><div class="llm-preset-heading"><h3>数据存储</h3><span class="preset-id ${custom ? "" : "speech-unconfigured"}">${custom ? "自定义目录" : "默认目录"}</span></div>
          <p class="hint">OCR 与翻译结果、精读缓存、研究空白与演化树调研、双语改稿、对话树、ResearchStudio 等运行成果保存在这里。体积大时可以换到别的磁盘，或放进 Google Drive 等同步盘管理的文件夹，由同步客户端负责多端同步。Codex、翻译后端、上传临时文件和用量记录始终留在项目的 <code>.runtime</code>。</p></div>
        </div>
        <p id="storageManagerStatus" class="manager-message ${show ? escapeHtml(kind) : ""} ${show ? "" : "hidden"}" role="status">${show ? escapeHtml(message) : ""}</p>
        ${notices.map(([level, text]) => `<p class="manager-message ${level}" role="status">${escapeHtml(text)}</p>`).join("")}
        <div class="reader-settings-group">
          <h4>当前使用</h4>
          <p class="hint"><code class="storage-path">${escapeHtml(storage.active)}</code></p>
          <p class="hint">包含子目录：${storage.subdirs.map(name => `<code>${escapeHtml(name)}</code>`).join(" ")}</p>
        </div>
        <form id="storageConfigForm" class="llm-editor-form storage-editor-form">
          <label>数据存储目录<span class="storage-path-row"><input name="path" type="text" required maxlength="1000" spellcheck="false" value="${escapeHtml(storage.configured || storage.active)}" placeholder="/path/to/Google Drive/ResearchTool"><button type="button" data-pick-storage>选择文件夹…</button></span><span class="hint">填已存在的文件夹的绝对路径。保存后需重启面板才生效（各模块在启动时绑定目录）。</span></label>
          <label class="reader-switch-row"><span><strong>迁移现有数据</strong><small>下次启动时，先把当前目录里的上述子目录移动到新目录；新目录里已有同名子目录的会跳过，不覆盖。</small></span><input type="checkbox" role="switch" name="moveExisting" checked></label>
          <p class="hint">同步盘提示：不要在两台电脑上同时运行面板写同一个目录，否则同步客户端可能产生冲突副本。云盘未挂载时，面板会暂时退回默认目录并在这里提示。</p>
          <div class="speech-editor-actions">
            ${custom ? `<button type="button" class="danger-button" data-reset-storage>恢复默认目录</button>` : ""}
            <button type="submit" class="primary">检查并保存</button>
          </div>
        </form>
      </section>`;
}

function renderAdvancedSettingsPanel() {
  return `
      <section class="settings-tab-panel advanced-settings" data-settings-panel="advanced" role="tabpanel">
        <div class="section-heading llm-manager-heading">
          <div><div class="llm-preset-heading"><h3>高级</h3><span class="preset-id">浏览器设置</span></div>
          <p class="hint">导出或导入保存在此浏览器里的设置：精读默认行为、费用确认和界面布局，便于换浏览器或换电脑。API Key、账号和其他全局配置保存在项目 .env 与后端，不在导出文件里。</p></div>
        </div>
        <p id="advancedSettingsStatus" class="manager-message hidden" role="status"></p>
        <div class="reader-settings-group">
          <h4>浏览器设置</h4>
          <p class="hint">当前共 <strong>${storedKeys(EXPORT_PREFIXES).length}</strong> 项。</p>
          <div class="speech-editor-actions"><button type="button" data-export-browser-settings>导出为 JSON</button><button type="button" data-import-browser-settings>从 JSON 导入…</button><input type="file" accept=".json,application/json" data-import-browser-file hidden></div>
        </div>
      </section>`;
}

function wireBrowserSettings() {
  const status = (id, text, ok = true) => { const node = form.querySelector(id); if (!node) return; node.textContent = text; node.className = `manager-message ${ok ? "ok" : "bad"}`; };
  form.querySelectorAll("[data-billing-ack]").forEach(input => input.addEventListener("change", () => {
    window.EnginePicker?.billingAck?.set(input.dataset.billingAck, input.checked);
    status("#billingSettingsStatus", input.checked ? "已确认，提交时会自动勾选使用确认。" : "已取消确认，提交时需要手动勾选。");
  }));
  form.querySelector("[data-reset-layout]")?.addEventListener("click", () => {
    const keys = storedKeys(LAYOUT_PREFIXES);
    keys.forEach(key => { try { localStorage.removeItem(key); } catch {} });
    renderLlmManager();
    status("#generalSettingsStatus", `已重置 ${keys.length} 项界面布局，其他页面刷新后生效。`);
  });
  form.querySelector("[data-export-browser-settings]")?.addEventListener("click", () => {
    const values = {};
    storedKeys(EXPORT_PREFIXES).forEach(key => { try { values[key] = localStorage.getItem(key); } catch {} });
    const blob = new Blob([JSON.stringify({ format: "research-toolkit-browser-settings", version: 1, exportedAt: new Date().toISOString(), values }, null, 2)], { type: "application/json" });
    const link = Object.assign(document.createElement("a"), { href: URL.createObjectURL(blob), download: "research-toolkit-browser-settings.json" });
    document.body.append(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(link.href), 1000);
    status("#advancedSettingsStatus", `已导出 ${Object.keys(values).length} 项。`);
  });
  const file = form.querySelector("[data-import-browser-file]");
  form.querySelector("[data-import-browser-settings]")?.addEventListener("click", () => file?.click());
  file?.addEventListener("change", async () => {
    try {
      const data = JSON.parse(await file.files[0].text());
      if (data?.format !== "research-toolkit-browser-settings" || typeof data.values !== "object") throw new Error("这不是本工具导出的浏览器设置文件。");
      const entries = Object.entries(data.values).filter(([key, value]) => EXPORT_PREFIXES.some(prefix => key.startsWith(prefix)) && typeof value === "string");
      entries.forEach(([key, value]) => localStorage.setItem(key, value));
      renderLlmManager();
      status("#advancedSettingsStatus", `已导入 ${entries.length} 项，其他页面刷新后生效。`);
    } catch (error) {
      status("#advancedSettingsStatus", error.message || "导入失败。", false);
    }
  });
}

// ResearchStudio's environment check (moved here from the Studio page): per-workflow readiness and setup.
async function loadStudioConfig(refresh = false) {
  try {
    const response = await fetch(`/api/researchstudio/config${refresh ? "?refresh=1" : ""}`, { headers: { "X-ResearchStudio": "1" } });
    const data = await response.json();
    state.studioConfig = response.ok ? data : { error: data.error || "无法读取 ResearchStudio 环境。", modules: [] };
  } catch (error) {
    state.studioConfig = { error: error.message, modules: [] };
  }
}

function renderStudioSettingsPanel() {
  const config = state.studioConfig || { modules: [] };
  const modules = config.modules || [];
  const ready = modules.filter(item => item.ready).length;
  const rows = modules.map(item => `<tr><td>${escapeHtml(item.label)}</td><td>${escapeHtml(item.bundle === "Idea" ? "Idea · 研究发现" : "Reel · 论文成果")}</td><td>${item.ready ? '<span class="hint ok">可运行</span>' : `<span class="hint bad">缺少：${escapeHtml((item.missing || []).join("、"))}</span>`}</td></tr>`).join("");
  return `
      <section class="settings-tab-panel studio-settings" data-settings-panel="studio" role="tabpanel">
        <div class="section-heading llm-manager-heading">
          <div>
            <div class="llm-preset-heading"><h3>ResearchStudio · 运行环境</h3><span class="preset-id ${modules.length && ready === modules.length ? "" : "speech-unconfigured"}">${pending.has("studio") ? "检查中…" : modules.length ? `${ready}/${modules.length} 可运行` : "未检测"}</span></div>
            <p class="hint">任务使用全局 Codex 默认模型与推理强度（见 Codex）。环境检查只验证基础依赖；上游服务、字体和具体论文仍可能影响生成结果。</p>
          </div>
          <button type="button" data-refresh-studio>重新检查</button>
        </div>
        ${config.error ? `<p class="manager-message bad">${escapeHtml(config.error)}</p>` : ""}
        ${rows ? `<table class="studio-env-table"><thead><tr><th>工作流</th><th>分组</th><th>状态</th></tr></thead><tbody>${rows}</tbody></table>` : ""}
        <div class="studio-setup">
          <h4>安装</h4>
          <p>在 ResearchTool 目录运行以下命令，安装到项目独立环境（视频还需加上 <code>--video</code>）：</p>
          <code>python scripts/setup_researchstudio.py --install-deps</code>
          <p>Poppler、LibreOffice、FFmpeg 与 Node.js 需在启动面板时的 PATH 中可用；也可用 <code>.env</code> 的 <code>RESEARCHSTUDIO_TOOL_PATH</code> 指定额外工具目录。</p>
          <p><a href="https://github.com/microsoft/ResearchStudio" target="_blank" rel="noreferrer">上游代码与 MIT 许可证</a></p>
        </div>
      </section>`;
}

async function loadReaderActions() {
  try {
    const response = await fetch("/api/reader/config");
    const data = await response.json();
    state.readerActions = response.ok ? data.actions || [] : [];
  } catch {
    state.readerActions = [];
  }
}

function readReaderSettings() {
  try {
    const saved = JSON.parse(localStorage.getItem(READER_SETTINGS_KEY) || "{}");
    return saved && typeof saved === "object" ? saved : {};
  } catch {
    return {};
  }
}

// Read-modify-write so fields the reader owns (widths, typography, …) are never dropped.
function writeReaderSettings(patch) {
  try {
    localStorage.setItem(READER_SETTINGS_KEY, JSON.stringify({ ...readReaderSettings(), ...patch }));
    return true;
  } catch {
    return false;
  }
}

function clampMargin(value, fallback) {
  const number = Number(value);
  return value !== "" && value !== null && Number.isFinite(number) ? Math.min(240, Math.max(0, Math.round(number))) : fallback;
}

function readerMargins(saved = readReaderSettings()) {
  const margins = {};
  READER_MARGIN_ZONES.forEach(([zone]) => {
    margins[zone] = {};
    READER_MARGIN_SIDES.forEach(([side]) => {
      margins[zone][side] = clampMargin(saved.readingLayout?.margins?.[zone]?.[side], READER_MARGIN_DEFAULTS[zone][side]);
    });
  });
  return margins;
}

function renderReaderSettingsPanel() {
  const saved = readReaderSettings();
  const actions = state.readerActions;
  const presets = state.llmPresets;
  const autoAction = saved.autoSelectionAction === true;
  const defaultAction = actions.some(action => action.id === saved.defaultSelectionAction) ? saved.defaultSelectionAction : actions[0]?.id || "";
  const actionModels = saved.actionModels && typeof saved.actionModels === "object" ? saved.actionModels : {};
  const presetOptions = selected => presets.map(preset => `<option value="${escapeHtml(preset.id)}" ${preset.id === selected ? "selected" : ""}>${escapeHtml(preset.name)} · ${escapeHtml(preset.model)}</option>`).join("");
  const fallback = typeof saved.fallbackPreset === "string" ? saved.fallbackPreset : "";
  const fallbackSelected = presets.some(preset => preset.id === fallback) ? fallback : presets[0]?.id || "";
  const fallbackOptions = (presets.length ? "" : `<option value="">请先在 LLM 标签添加预设</option>`)
    + (fallback === "custom" ? `<option value="custom" selected>精读中的临时配置（仅当前请求）</option>` : "")
    + presetOptions(fallback === "custom" ? "" : fallbackSelected);
  const margins = readerMargins(saved);
  const actionRows = actions.length
    ? actions.map(action => {
      const selected = presets.some(preset => preset.id === actionModels[action.id]) ? actionModels[action.id] : "";
      return `<label class="reader-model-row"><span>${escapeHtml(action.label)}</span><select data-reader-action-model="${escapeHtml(action.id)}" aria-label="${escapeHtml(action.label)}使用的 LLM"><option value="">使用兜底预设</option>${presetOptions(selected)}</select></label>`;
    }).join("")
    : `<p class="hint">暂时读不到精读的划选动作列表。</p>`;
  const marginHead = `<span aria-hidden="true">px</span>${READER_MARGIN_SIDES.map(([, label]) => `<span aria-hidden="true">${label}</span>`).join("")}`;
  const marginRows = READER_MARGIN_ZONES.map(([zone, zoneLabel]) => `<strong>${zoneLabel}</strong>${READER_MARGIN_SIDES.map(([side, sideLabel]) => `<label><span class="sr-only">${zoneLabel}${sideLabel}边距</span><input type="number" min="0" max="240" step="1" value="${margins[zone][side]}" data-reader-margin-zone="${zone}" data-reader-margin-side="${side}"></label>`).join("")}`).join("");
  return `
      <section class="settings-tab-panel reader-settings" data-settings-panel="reader" role="tabpanel">
        <div class="section-heading llm-manager-heading">
          <div>
            <div class="llm-preset-heading"><h3>精读 · 默认行为</h3><span class="preset-id">保存在此浏览器</span></div>
            <p class="hint">不常改的默认值，改动立即生效，已打开的精读页会自动同步。字体、字号与行距请在精读功能区的“排版”选项卡里随时调整。</p>
          </div>
        </div>
        <p id="readerSettingsStatus" class="manager-message hidden" role="status"></p>
        <div class="reader-settings-group">
          <h4>划词</h4>
          <label class="reader-switch-row"><span><strong>划词后自动执行</strong><small>开启时自动运行默认动作，同时保留操作菜单以便切换。</small></span><input id="readerAutoAction" type="checkbox" role="switch" ${autoAction ? "checked" : ""}></label>
          <label class="reader-field">默认动作<select id="readerDefaultAction" ${autoAction ? "" : "disabled"}>${actions.map(action => `<option value="${escapeHtml(action.id)}" ${action.id === defaultAction ? "selected" : ""}>${escapeHtml(action.label)}</option>`).join("")}</select></label>
          <p class="hint">动作的名称和 Prompt 在 <a href="/prompts?category=%E7%B2%BE%E8%AF%BB%E5%88%92%E9%80%89%E5%8A%A8%E4%BD%9C" target="_blank" rel="noopener">Prompt 工作台</a> 修改。</p>
        </div>
        <div class="reader-settings-group">
          <h4>每个动作使用的 LLM</h4>
          <label class="reader-field">兜底 API 预设<select id="readerFallbackPreset" ${presets.length ? "" : "disabled"}>${fallbackOptions}</select></label>
          <div class="reader-model-list">${actionRows}</div>
          <p class="hint">未单独指定的动作使用兜底预设。临时的手动配置在精读右栏“AI、模型与高级工具 › 兜底 / 临时 LLM 配置”中填写，不会保存。</p>
        </div>
        <div class="reader-settings-group">
          <div class="reader-group-heading"><h4>页面区域边距</h4><button type="button" data-reset-reader-margins>恢复默认</button></div>
          <div class="reader-margin-grid" role="group" aria-label="页眉、正文与页脚边距（px）">${marginHead}${marginRows}</div>
          <p class="hint">Markdown、HTML、OCR 与译文应用全部边距；原生 PDF 保留文件内嵌排版，仅调整页面外围留白。</p>
        </div>
      </section>`;
}

function wireReaderSettings() {
  const panel = form.querySelector('[data-settings-panel="reader"]');
  if (!panel) return;
  const save = (patch, text) => {
    const ok = writeReaderSettings(patch);
    const node = panel.querySelector("#readerSettingsStatus");
    node.textContent = ok ? text : "浏览器不允许保存设置（可能处于隐私模式）。";
    node.className = `manager-message ${ok ? "ok" : "bad"}`;
  };
  const actionLabel = id => state.readerActions.find(action => action.id === id)?.label || id;
  const defaultSelect = panel.querySelector("#readerDefaultAction");
  panel.querySelector("#readerAutoAction")?.addEventListener("change", event => {
    defaultSelect.disabled = !event.target.checked;
    save({ autoSelectionAction: event.target.checked, defaultSelectionAction: defaultSelect.value }, event.target.checked ? "已开启划词后自动执行。" : "已关闭划词后自动执行。");
  });
  defaultSelect?.addEventListener("change", event => save({ defaultSelectionAction: event.target.value }, `默认动作已设为“${actionLabel(event.target.value)}”。`));
  panel.querySelector("#readerFallbackPreset")?.addEventListener("change", event => save({ fallbackPreset: event.target.value }, "兜底预设已更新。"));
  panel.querySelectorAll("[data-reader-action-model]").forEach(select => select.addEventListener("change", () => {
    const action = select.dataset.readerActionModel;
    const actionModels = { ...(readReaderSettings().actionModels || {}) };
    if (select.value) actionModels[action] = select.value;
    else delete actionModels[action];
    save({ actionModels }, `“${actionLabel(action)}”使用的 LLM 已更新。`);
  }));
  const saveMargins = (margins, text) => save({ readingLayout: { ...(readReaderSettings().readingLayout || {}), margins } }, text);
  panel.querySelectorAll("[data-reader-margin-zone]").forEach(input => input.addEventListener("change", () => {
    const { readerMarginZone: zone, readerMarginSide: side } = input.dataset;
    const margins = readerMargins();
    margins[zone][side] = clampMargin(input.value, READER_MARGIN_DEFAULTS[zone][side]);
    input.value = String(margins[zone][side]);
    saveMargins(margins, "页面区域边距已更新。");
  }));
  panel.querySelector("[data-reset-reader-margins]")?.addEventListener("click", () => {
    const margins = JSON.parse(JSON.stringify(READER_MARGIN_DEFAULTS));
    panel.querySelectorAll("[data-reader-margin-zone]").forEach(input => { input.value = String(margins[input.dataset.readerMarginZone][input.dataset.readerMarginSide]); });
    saveMargins(margins, "页面区域边距已恢复默认。");
  });
}

async function loadGlobalLlmPresets() {
  const response = await fetch("/api/llm-presets");
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "无法读取 LLM 配置。");
  state.llmPresets = data.presets || [];
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

async function loadGlobalGoogleConfig() {
  const response = await fetch("/api/google-translate-config");
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "无法读取 Google 翻译配置。");
  state.googleConfig = data.google || { configured: false };
}

async function loadGlobalOpenalexConfig() {
  const response = await fetch("/api/openalex-config");
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "无法读取 OpenAlex 配置。");
  state.openalexConfig = data.openalex || { configured: false };
}

async function loadStorageConfig() {
  const response = await fetch("/api/storage-config");
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "无法读取数据存储设置。");
  state.storageConfig = data.storage || null;
}

async function loadGlobalClaudeConfig(refresh = false) {
  const response = await fetch(refresh ? "/api/claude-code-config/refresh" : "/api/claude-code-config", refresh ? { method:"POST" } : undefined);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "无法读取 Claude Code 状态。");
  state.claudeConfig = data.claude || { available: false };
}

async function loadGlobalWorkflowConfig() {
  const response = await fetch("/api/paper-revision/workflow", { headers:{ "X-Paper-Revision":"1" } });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "无法读取双语改稿工作流。");
  state.workflowOptions = data.options;
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
  const modelId = model?.id || "";
  const supportsInstant = modelId === "gpt-5.6" || modelId.startsWith("gpt-5.6-") || ["gpt-6-sol", "gpt-6-luna"].includes(modelId);
  return effort === "none" && supportsInstant
    ? "Instant（none）"
    : effort;
}

function codexRuntimeText(runtime) {
  if (!runtime?.version) return "";
  const label = runtime.managed ? "项目内 Codex " + runtime.version : "Codex " + runtime.version;
  return runtime.restartRequired && runtime.availableUpdateVersion
    ? label + " · " + runtime.availableUpdateVersion + " 将在空闲时刷新后启用"
    : label;
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

function renderLlmManager(message = "", kind = "", messageTarget = "llm", { keepEdits = false } = {}) {
  if (!keepEdits) dirtyFields.clear();
  if (message && kind === "ok") hooks.onSaved(messageTarget);
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
  const google = state.googleConfig || { configured: false };
  const openalex = state.openalexConfig || { configured: false };
  const claude = state.claudeConfig || { available: false, plan: false, models: [], efforts: [] };
  const codex = state.codexConfig || { available:false, chatgptAuthenticated:false, models:[], rateLimits:null };
  const codexModels = codex.models || [];
  const codexModel = codexModels.find(item => item.id === codex.defaultModel) || codexModels[0] || null;
  const codexEffortView = codexEffortsForModel(codexModel, codex.defaultReasoningEffort);
  const codexReady = Boolean(codex.available && codex.chatgptAuthenticated && codexModel);
  const codexRuntime = codexRuntimeText(codex.runtime);
  const paneStatus = {
    codex: pending.has("codex") ? undefined : codexReady, claude: pending.has("claude") ? undefined : Boolean(claude.plan), llm: state.llmPresets.length > 0,
    mathpix: Boolean(mathpix.configured), google: Boolean(google.configured), openalex: Boolean(openalex.configured), speech: Boolean(speech.configured), github: Boolean(github.configured),
  };
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
      <div class="settings-layout">
      <nav class="settings-nav" aria-label="设置分类">
        <label class="sr-only" for="settingsSearch">搜索设置</label>
        <input id="settingsSearch" class="settings-search" type="search" placeholder="搜索设置" autocomplete="off" value="${escapeHtml(state.settingsQuery)}">
        <div class="settings-nav-list" role="tablist" aria-orientation="vertical" aria-label="全局配置类别">${SETTINGS_GROUPS.map(group => `<p class="settings-nav-group" data-settings-group>${group.label}</p>${group.items.map(([tab, label]) => {
          const status = paneStatus[tab];
          const dot = status === undefined ? "" : `<i class="settings-dot ${status ? "ok" : ""}" title="${status ? "已就绪" : "未配置"}" aria-hidden="true"></i>`;
          return `<button type="button" role="tab" data-settings-tab="${tab}"><span>${label}</span>${dot}</button>`;
        }).join("")}`).join("")}</div>
        <p class="settings-search-empty hidden" role="status">没有匹配的设置。</p>
      </nav>
      <div class="settings-panes">
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
            <div class="llm-preset-heading"><h3>Codex 额度与默认设置</h3><span class="preset-id ${codexReady ? "" : "speech-unconfigured"}">${pending.has("codex") ? "读取中…" : codexReady ? "已连接" : "不可用"}</span></div>
            <p class="hint">默认自动安装并更新项目内 Codex 稳定版；不会修改全局 Codex CLI 配置。</p>
          </div>
          <div class="codex-heading-actions">
            ${codex.chatgptAuthenticated ? `<button type="button" class="danger-button" data-codex-logout>退出登录</button>` : `<button type="button" class="test-button" data-codex-login>浏览器登录</button>`}
            <button type="button" class="test-button" data-refresh-codex>刷新账户与额度</button>
          </div>
        </div>
        <p id="codexManagerStatus" class="manager-message ${messageTarget === "codex" ? escapeHtml(kind) : ""} ${message && messageTarget === "codex" ? "" : (codexReady ? "ok" : "warn")}" role="status">${messageTarget === "codex" && message ? escapeHtml(message) : escapeHtml(codexReady ? `ChatGPT ${codex.planType || "账户"} 已连接 · ${codexModels.length} 个可用模型${codexRuntime ? ` · ${codexRuntime}` : ""}` : (pending.has("codex") ? "正在读取 Codex 账户与额度…" : codex.error || "Codex 尚未通过 ChatGPT 登录。"))}</p>
        <div class="codex-quota-heading"><div><span class="eyebrow">USAGE</span><h4>剩余额度</h4></div><span class="hint">额度是即时快照，刷新后更新</span></div>
        <div class="codex-quota-grid">${renderCodexQuotaCards(codex.rateLimits)}</div>
        <form id="codexConfigForm" class="llm-editor-form codex-editor-form">
          <label>默认模型<select name="model" ${codexReady ? "" : "disabled"}>${codexModels.length ? codexModels.map(model => `<option value="${escapeHtml(model.id)}" ${model.id === codexModel?.id ? "selected" : ""}>${escapeHtml(model.displayName || model.id)}</option>`).join("") : `<option value="">当前无可用模型</option>`}</select><span class="hint">新建 Codex 论文分析时自动选中。</span></label>
          <label>默认推理强度<select name="reasoningEffort" ${codexReady ? "" : "disabled"}>${codexEffortView.efforts.map(effort => `<option value="${escapeHtml(effort)}" ${effort === codexEffortView.selected ? "selected" : ""}>${escapeHtml(codexEffortLabel(codexModel, effort))}</option>`).join("")}</select><span class="hint">Instant 响应最快；强度越高通常越慢，也会使用更多 token。</span></label>
          <div class="llm-editor-actions"><button type="submit" class="primary" ${codexReady ? "" : "disabled"}>保存 Codex 默认值</button></div>
        </form>
      </section>
      <section class="settings-tab-panel claude-manager" data-settings-panel="claude" role="tabpanel">
        <div class="section-heading llm-manager-heading">
          <div>
            <div class="llm-preset-heading"><h3>Claude Code CLI</h3><span class="preset-id ${claude.plan ? "" : "speech-unconfigured"}">${pending.has("claude") ? "读取中…" : claude.plan ? "已连接" : "不可用"}</span></div>
            <p class="hint">双语改稿通过本机已登录的 claude CLI 调用，消耗 Claude 套餐额度（与 claude.ai / Claude Code 共用）。调用时移除 API Key 环境变量、禁用全部工具，并在空目录中运行。</p>
          </div>
          <div class="codex-heading-actions">
            <button type="button" class="test-button" data-test-claude ${claude.plan ? "" : "disabled"}>测试调用</button>
            <button type="button" class="test-button" data-refresh-claude>刷新状态</button>
          </div>
        </div>
        <p id="claudeManagerStatus" class="manager-message ${messageTarget === "claude" && message ? escapeHtml(kind) : (claude.plan ? "ok" : "warn")}" role="status">${messageTarget === "claude" && message ? escapeHtml(message) : escapeHtml(claude.plan ? `已登录 · ${claude.subscriptionType || "Claude"} 套餐${claude.version ? ` · CLI ${claude.version}` : ""}` : (pending.has("claude") ? "正在读取 Claude Code 状态…" : claude.error || "未找到 Claude Code CLI。"))}</p>
        ${claude.plan || pending.has("claude") ? "" : `<p class="hint">登录方式：在终端运行 <code>claude</code>，按提示用 Claude 账号（Pro / Max）登录，然后点“刷新状态”。若 claude 不在 PATH 中，可在 .env 中设置 <code>CLAUDE_CODE_CLI</code> 为其绝对路径。</p>`}
        ${claude.cliPath ? `<p class="hint">CLI 路径：<code>${escapeHtml(claude.cliPath)}</code></p>` : ""}
        <p class="hint">双语改稿各步骤使用哪个 Claude 模型与推理强度，在 <a href="#settings/workflow" data-goto-workflow>双语改稿</a> 分区中设置。</p>
      </section>
${renderReaderSettingsPanel()}
${renderStudioSettingsPanel()}
${renderGeneralSettingsPanel()}
${renderStorageSettingsPanel(message, kind, messageTarget)}
${renderAppearanceSettingsPanel()}
${renderDoctorSettingsPanel()}
${renderUsageSettingsPanel()}
${renderBillingSettingsPanel()}
${renderAdvancedSettingsPanel()}
      <section class="settings-tab-panel workflow-manager" data-settings-panel="workflow" role="tabpanel">
        <div class="section-heading llm-manager-heading">
          <div>
            <div class="llm-preset-heading"><h3>双语改稿 · 默认工作流</h3><span class="preset-id">全局默认</span></div>
            <p class="hint">为每个步骤选择默认引擎：Claude Code（套餐额度）、Codex（套餐额度）、已保存的 API 预设（独立计费），翻译还可选 Google 翻译 Basic（v2）。改稿页面打开时使用这里的设置，页面上的改动只对当次会话有效。</p>
          </div>
        </div>
        <p id="workflowManagerStatus" class="manager-message ${messageTarget === "workflow" ? escapeHtml(kind) : ""} ${message && messageTarget === "workflow" ? "" : "hidden"}" role="status">${messageTarget === "workflow" ? escapeHtml(message) : ""}</p>
        <div id="workflowEditor" class="workflow-editor">${pending.has("workflow") ? `<p class="hint">正在读取各引擎状态…</p>` : state.workflowError ? `<p class="manager-message bad">${escapeHtml(state.workflowError)}</p>` : ""}</div>
        <p class="hint">默认：翻译用 Google 翻译，对不齐的句子由 Sonnet · 中补译；通读、修订与核对用 Opus · 中。未登录或未配置的引擎可以先选上，提交时才会检查。</p>
        <div class="llm-editor-actions"><button type="button" data-reset-workflow>恢复推荐值</button><button type="button" class="primary" data-save-workflow>保存默认工作流</button></div>
      </section>
      <section class="settings-tab-panel google-manager" data-settings-panel="google" role="tabpanel">
        <div class="section-heading llm-manager-heading">
          <div>
            <div class="llm-preset-heading"><h3>Google 翻译 · Basic（v2）</h3><span class="preset-id ${google.configured ? "" : "speech-unconfigured"}">${google.configured ? "已配置" : "未配置"}</span></div>
            <p class="hint">仅使用 Cloud Translation 基础版：<code>${escapeHtml(google.endpoint || "https://translation.googleapis.com/language/translate/v2")}</code>。用于双语改稿的快速翻译；按字符计费，每月前 50 万字符免费。Key 只保存在项目 .env，通过请求头发送。</p>
          </div>
        </div>
        <p id="googleManagerStatus" class="manager-message ${messageTarget === "google" ? escapeHtml(kind) : ""} ${message && messageTarget === "google" ? "" : "hidden"}" role="status">${messageTarget === "google" ? escapeHtml(message) : ""}</p>
        <p class="hint">获取 Key：Google Cloud 控制台 → 启用 “Cloud Translation API” 并开通结算 → API 和服务 → 凭据 → 创建 API 密钥（建议在“API 限制”中只允许 Cloud Translation API）。大陆网络需代理，后端请求遵循系统代理。</p>
        <form id="googleConfigForm" class="llm-editor-form mathpix-editor-form">
          <label>API Key<input name="apiKey" type="password" autocomplete="new-password" required maxlength="300" placeholder="${google.configured ? "粘贴新 Key 以替换当前 Key" : "仅保存到本机 .env"}"><span class="hint">保存前会发送一次极小的翻译请求验证 Key 与 API 是否可用。</span></label>
          <div class="speech-editor-actions">
            <button type="button" class="test-button" data-test-google ${google.configured ? "" : "disabled"}>测试翻译</button>
            ${google.configured ? `<button type="button" class="danger-button" data-delete-google>移除配置</button>` : ""}
            <button type="submit" class="primary">验证并保存</button>
          </div>
        </form>
      </section>
      <section class="settings-tab-panel openalex-manager" data-settings-panel="openalex" role="tabpanel">
        <div class="section-heading llm-manager-heading">
          <div>
            <div class="llm-preset-heading"><h3>OpenAlex 文献检索</h3><span class="preset-id ${openalex.configured ? "" : "speech-unconfigured"}">${openalex.configured ? `已配置${openalex.keyTail ? ` · …${escapeHtml(openalex.keyTail)}` : ""}` : "未配置"}</span></div>
            <p class="hint">演化树调研的检索、被引列表（正向追踪）和参考文献（向前补漏）使用 <code>${escapeHtml(openalex.endpoint || "https://api.openalex.org/works")}</code>。没有 Key 时与同一 IP 的所有人共用每天 0.10 美元的免费额度，且不能用 cursor 翻页；有 Key 时每天 1 美元。Key 只保存在项目 .env，只加在实际请求上，不写进检索缓存。</p>
          </div>
        </div>
        <p id="openalexManagerStatus" class="manager-message ${messageTarget === "openalex" ? escapeHtml(kind) : ""} ${message && messageTarget === "openalex" ? "" : "hidden"}" role="status">${messageTarget === "openalex" ? escapeHtml(message) : ""}</p>
        <p class="hint">获取 Key：登录 <a href="https://openalex.org/settings/api" target="_blank" rel="noopener noreferrer">openalex.org/settings/api</a>，免费申请后复制到下面。</p>
        <form id="openalexConfigForm" class="llm-editor-form mathpix-editor-form">
          <label>API Key<input name="apiKey" type="password" autocomplete="new-password" required maxlength="200" placeholder="${openalex.configured ? "粘贴新 Key 以替换当前 Key" : "仅保存到本机 .env"}"><span class="hint">保存前会发一次最小的检索请求验证 Key（约 0.0001 美元额度）。</span></label>
          <div class="speech-editor-actions">
            <button type="button" class="test-button" data-test-openalex ${openalex.configured ? "" : "disabled"}>测试并查看额度</button>
            ${openalex.configured ? `<button type="button" class="danger-button" data-delete-openalex>移除配置</button>` : ""}
            <button type="submit" class="primary">验证并保存</button>
          </div>
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
      </div>
      </div>
    </section>`;
  form.querySelectorAll("[data-settings-tab]").forEach(button => button.addEventListener("click", () => activateSettingsTab(button.dataset.settingsTab)));
  wireSettingsNavigation();
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
  form.querySelector("#googleConfigForm")?.addEventListener("submit", saveGlobalGoogleConfig);
  form.querySelector("[data-test-google]")?.addEventListener("click", event => testGlobalGoogleConfig(event.currentTarget));
  form.querySelector("[data-delete-google]")?.addEventListener("click", removeGlobalGoogleConfig);
  form.querySelector("#openalexConfigForm")?.addEventListener("submit", saveGlobalOpenalexConfig);
  form.querySelector("#storageConfigForm")?.addEventListener("submit", saveStorageConfig);
  form.querySelector("[data-pick-storage]")?.addEventListener("click", event => pickStorageFolder(event.currentTarget));
  form.querySelector("[data-reset-storage]")?.addEventListener("click", resetStorageConfig);
  form.querySelector("[data-test-openalex]")?.addEventListener("click", event => testGlobalOpenalexConfig(event.currentTarget));
  form.querySelector("[data-delete-openalex]")?.addEventListener("click", removeGlobalOpenalexConfig);
  const workflowEditor = form.querySelector("#workflowEditor");
  if (workflowEditor && state.workflowOptions && window.PaperRevisionWorkflow) window.PaperRevisionWorkflow.render(workflowEditor, state.workflowOptions, state.workflowOptions.defaults);
  form.querySelector("[data-save-workflow]")?.addEventListener("click", event => saveGlobalWorkflowConfig(event.currentTarget));
  form.querySelector("[data-reset-workflow]")?.addEventListener("click", () => {
    window.PaperRevisionWorkflow.render(workflowEditor, state.workflowOptions, state.workflowOptions.recommended);
    showSettingsStatus("#workflowManagerStatus", "已填入推荐值，保存后生效。", "");
  });
  form.querySelector("[data-goto-workflow]")?.addEventListener("click", event => { event.preventDefault(); activateSettingsTab("workflow"); });
  form.querySelector("[data-refresh-claude]")?.addEventListener("click", event => refreshGlobalClaudeConfig(event.currentTarget));
  form.querySelector("[data-test-claude]")?.addEventListener("click", event => testGlobalClaudeConfig(event.currentTarget));
  form.querySelector("[data-test-github]")?.addEventListener("click", event => testGlobalGithubConfig(event.currentTarget));
  form.querySelector("[data-delete-github]")?.addEventListener("click", removeGlobalGithubConfig);
  wireReaderSettings();
  wireBrowserSettings();
  wireAppearanceSettings();
  wireDoctorAndUsage();
  form.querySelector("[data-refresh-studio]")?.addEventListener("click", async event => {
    event.currentTarget.disabled = true;
    await loadStudioConfig(true);
    renderLlmManager();
  });
  activateSettingsTab(state.settingsTab, false);
}

// Sidebar search matches pane names and every word inside the pane; ↑/↓ move between panes.
function filterSettingsNav(query) {
  state.settingsQuery = query;
  const needle = query.trim().toLowerCase();
  let firstMatch = null;
  form.querySelectorAll("[data-settings-tab]").forEach(button => {
    const panel = form.querySelector(`[data-settings-panel="${button.dataset.settingsTab}"]`);
    const haystack = `${button.textContent} ${panel?.textContent || ""}`.toLowerCase();
    const match = !needle || haystack.includes(needle);
    button.hidden = !match;
    if (match && !firstMatch) firstMatch = button;
  });
  form.querySelectorAll("[data-settings-group]").forEach(label => {
    let node = label.nextElementSibling;
    let visible = false;
    while (node && !node.matches("[data-settings-group]")) { if (!node.hidden) visible = true; node = node.nextElementSibling; }
    label.hidden = !visible;
  });
  form.querySelector(".settings-search-empty")?.classList.toggle("hidden", Boolean(firstMatch));
  return firstMatch;
}

function wireSettingsNavigation() {
  const search = form.querySelector("#settingsSearch");
  search?.addEventListener("input", () => filterSettingsNav(search.value));
  search?.addEventListener("keydown", event => {
    if (event.key === "Enter") { event.preventDefault(); const first = filterSettingsNav(search.value); if (first) activateSettingsTab(first.dataset.settingsTab); }
    if (event.key === "Escape" && search.value) { event.stopPropagation(); search.value = ""; filterSettingsNav(""); }
  });
  form.querySelector(".settings-nav-list")?.addEventListener("keydown", event => {
    if (!["ArrowDown", "ArrowUp"].includes(event.key)) return;
    const buttons = [...form.querySelectorAll("[data-settings-tab]")].filter(button => !button.hidden);
    const index = buttons.indexOf(document.activeElement);
    if (index < 0) return;
    event.preventDefault();
    const next = buttons[(index + (event.key === "ArrowDown" ? 1 : buttons.length - 1)) % buttons.length];
    activateSettingsTab(next.dataset.settingsTab);
    next.focus();
  });
  if (state.settingsQuery) filterSettingsNav(state.settingsQuery);
}

function activateSettingsTab(tab, updateHash = true) {
  if (!SETTINGS_TABS.includes(tab)) return;
  state.settingsTab = tab;
  try { localStorage.setItem(LAST_PANE_KEY, tab); } catch {}
  form.querySelectorAll("[data-settings-tab]").forEach(button => {
    const active = button.dataset.settingsTab === tab;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", String(active));
    button.tabIndex = active ? 0 : -1;
  });
  form.querySelectorAll("[data-settings-panel]").forEach(panel => panel.classList.toggle("hidden", panel.dataset.settingsPanel !== tab));
  if (tab === "doctor") loadDoctor();
  if (tab === "usage") loadUsage();
  if (updateHash) hooks.onTabChange(tab);
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

function showSettingsStatus(id, text, kind) {
  const status = form.querySelector(id);
  if (!status) return;
  status.textContent = text;
  status.className = `manager-message ${kind}`;
}

async function settingsRequest(url, options = {}) {
  const response = await fetch(url, options.body ? { ...options, headers:{ "Content-Type":"application/json", ...(options.headers || {}) } } : options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "请求失败。");
  return data;
}

async function saveGlobalGoogleConfig(event) {
  event.preventDefault();
  const editorForm = event.currentTarget;
  const submit = editorForm.querySelector('[type="submit"]');
  submit.disabled = true;
  showSettingsStatus("#googleManagerStatus", "正在用一次极小的翻译请求验证 Key…", "");
  try {
    const data = await settingsRequest("/api/google-translate-config", { method:"PUT", body:JSON.stringify(Object.fromEntries(new FormData(editorForm).entries())) });
    state.googleConfig = data.google;
    renderLlmManager("Key 验证通过并已保存；双语改稿可选择 Google 翻译。", "ok", "google");
  } catch (error) {
    showSettingsStatus("#googleManagerStatus", error.message, "bad");
    submit.disabled = false;
  }
}

async function testGlobalGoogleConfig(button) {
  button.disabled = true;
  try {
    const data = await settingsRequest("/api/google-translate-config/test", { method:"POST" });
    showSettingsStatus("#googleManagerStatus", `测试成功：“${data.test.source}” → “${data.test.translated}”`, "ok");
  } catch (error) {
    showSettingsStatus("#googleManagerStatus", error.message, "bad");
  } finally {
    button.disabled = false;
  }
}

async function removeGlobalGoogleConfig() {
  if (!window.confirm("确定移除 Google 翻译 API Key 吗？双语改稿将无法使用 Google 翻译。")) return;
  try {
    state.googleConfig = (await settingsRequest("/api/google-translate-config", { method:"DELETE" })).google;
    renderLlmManager("已移除 Google 翻译 API Key。", "ok", "google");
  } catch (error) {
    showSettingsStatus("#googleManagerStatus", error.message, "bad");
  }
}

const openalexQuota = test => test?.remainingUsd ? `今天剩余 ${test.remainingUsd} / ${test.limitUsd || "?"} 美元额度。` : "";

async function saveGlobalOpenalexConfig(event) {
  event.preventDefault();
  const editorForm = event.currentTarget;
  const submit = editorForm.querySelector('[type="submit"]');
  submit.disabled = true;
  showSettingsStatus("#openalexManagerStatus", "正在用一次最小的检索请求验证 Key…", "");
  try {
    const data = await settingsRequest("/api/openalex-config", { method:"PUT", body:JSON.stringify(Object.fromEntries(new FormData(editorForm).entries())) });
    state.openalexConfig = data.openalex;
    renderLlmManager(`Key 验证通过并已保存。${openalexQuota(data.test)}`, "ok", "openalex");
  } catch (error) {
    showSettingsStatus("#openalexManagerStatus", error.message, "bad");
    submit.disabled = false;
  }
}

async function testGlobalOpenalexConfig(button) {
  button.disabled = true;
  try {
    const data = await settingsRequest("/api/openalex-config/test", { method:"POST" });
    showSettingsStatus("#openalexManagerStatus", `测试成功。${openalexQuota(data.test)}`, "ok");
  } catch (error) {
    showSettingsStatus("#openalexManagerStatus", error.message, "bad");
  } finally {
    button.disabled = false;
  }
}

async function removeGlobalOpenalexConfig() {
  if (!window.confirm("确定移除 OpenAlex API Key 吗？之后检索按 IP 共用免费额度。")) return;
  try {
    state.openalexConfig = (await settingsRequest("/api/openalex-config", { method:"DELETE" })).openalex;
    renderLlmManager("已移除 OpenAlex API Key。", "ok", "openalex");
  } catch (error) {
    showSettingsStatus("#openalexManagerStatus", error.message, "bad");
  }
}

const storageSavedText = storage => storage.restartRequired ? "已保存。重启面板后生效。" : "已保存，与当前使用的目录相同，无需重启。";

async function saveStorageConfig(event) {
  event.preventDefault();
  const editorForm = event.currentTarget;
  const submit = editorForm.querySelector('[type="submit"]');
  submit.disabled = true;
  try {
    const body = { path: editorForm.elements.path.value, moveExisting: editorForm.elements.moveExisting.checked };
    const data = await settingsRequest("/api/storage-config", { method:"PUT", body:JSON.stringify(body) });
    state.storageConfig = data.storage;
    renderLlmManager(storageSavedText(data.storage), "ok", "storage");
  } catch (error) {
    showSettingsStatus("#storageManagerStatus", error.message, "bad");
    submit.disabled = false;
  }
}

async function pickStorageFolder(button) {
  button.disabled = true;
  try {
    const data = await settingsRequest("/api/local-paths/pick", { method:"POST", body:JSON.stringify({ kind:"folder", title:"选择数据存储目录" }) });
    if (data.path) form.querySelector("#storageConfigForm").elements.path.value = data.path;
  } catch (error) {
    showSettingsStatus("#storageManagerStatus", error.message, "bad");
  } finally {
    button.disabled = false;
  }
}

async function resetStorageConfig() {
  const move = form.querySelector("#storageConfigForm")?.elements.moveExisting.checked;
  if (!window.confirm(`恢复为默认目录（项目 .runtime）吗？${move ? "下次启动时会把当前目录里的数据移回去。" : "当前目录里的数据不会移动。"}`)) return;
  try {
    const data = await settingsRequest(`/api/storage-config${move ? "?moveExisting=1" : ""}`, { method:"DELETE" });
    state.storageConfig = data.storage;
    renderLlmManager(storageSavedText(data.storage), "ok", "storage");
  } catch (error) {
    showSettingsStatus("#storageManagerStatus", error.message, "bad");
  }
}

async function saveGlobalWorkflowConfig(button) {
  button.disabled = true;
  try {
    const workflow = window.PaperRevisionWorkflow.read(form.querySelector("#workflowEditor"));
    const data = await settingsRequest("/api/paper-revision/workflow", { method:"PUT", body:JSON.stringify({ workflow }), headers:{ "X-Paper-Revision":"1" } });
    state.workflowOptions = data.options;
    renderLlmManager("已保存双语改稿默认工作流；改稿页面刷新后使用新的默认值。", "ok", "workflow");
  } catch (error) {
    showSettingsStatus("#workflowManagerStatus", error.message, "bad");
    button.disabled = false;
  }
}

async function refreshGlobalClaudeConfig(button) {
  button.disabled = true;
  try {
    await loadGlobalClaudeConfig(true);
    renderLlmManager("", "", "claude");
  } catch (error) {
    showSettingsStatus("#claudeManagerStatus", error.message, "bad");
    button.disabled = false;
  }
}

async function testGlobalClaudeConfig(button) {
  button.disabled = true;
  showSettingsStatus("#claudeManagerStatus", "正在用 Sonnet（推理 低）发送一次极小的请求…", "");
  try {
    const data = await settingsRequest("/api/claude-code-config/test", { method:"POST" });
    showSettingsStatus("#claudeManagerStatus", `测试成功：模型回复 “${data.test.reply}”，用时 ${data.test.seconds} 秒（消耗少量套餐额度）。`, "ok");
  } catch (error) {
    showSettingsStatus("#claudeManagerStatus", error.message, "bad");
  } finally {
    button.disabled = false;
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

window.GlobalSettings = {
  tabs: SETTINGS_TABS,
  mount,
  activate: tab => activateSettingsTab(tab, false),
};
})();
