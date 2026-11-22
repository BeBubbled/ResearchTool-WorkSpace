/* 演化树调研（/gap-survey）：项目、逐步检查、项目文件，以及在子进程里运行 gapr 命令。
   确认点不在这里运行：只显示命令，由用户本人在自己的终端里执行。 */
(() => {
"use strict";
const $ = selector => document.querySelector(selector);
const escapeHtml = value => String(value ?? "").replace(/[&<>'"]/g, c => ({ "&":"&amp;", "<":"&lt;", ">":"&gt;", "'":"&#39;", '"':"&quot;" }[c]));
const PROJECT_KEY = "research-toolkit.gap-survey.project";
const TAB_KEY = "research-toolkit.gap-survey.tab";
const COMMAND_KEY = "research-toolkit.gap-survey.command";
const store = {
  get(key) { try { return localStorage.getItem(key) || ""; } catch { return ""; } },
  set(key, value) { try { localStorage.setItem(key, value); } catch {} },
};

const refresh = { k:"refresh", label:"忽略缓存，重新请求", type:"check" };
const forGap = { k:"for", label:"为候选空白（第 6 步）", placeholder:"G01" };
const postCutoff = { k:"postCutoff", label:"查截止日之后（回测第 6 步，会锁定候选）", type:"check", backtestOnly:true };
const linkSource = { k:"source", label:"引用数据来源", type:"select", options:[["auto", "自动（S2 key → S2，否则 OpenAlex）"], ["openalex", "OpenAlex"], ["s2", "Semantic Scholar"]] };
const paperId = { k:"id", label:"论文 ID", placeholder:"arXiv:2205.13504 / ACL:… / DOI:…", wide:true, required:true };
const FIELDS = {
  search: [
    { k:"query", label:"检索词", wide:true, required:true },
    { k:"tool", label:"来源", type:"select", options:[["s2", "Semantic Scholar"], ["openalex", "OpenAlex"], ["arxiv", "arXiv"], ["openreview", "OpenReview"]] },
    { k:"limit", label:"条数", type:"number", value:20 },
    { k:"from", label:"起始日期", type:"date" },
    { k:"to", label:"结束日期", type:"date" },
    { k:"sort", label:"排序（仅 arXiv）", type:"select", options:[["relevance", "相关度"], ["date", "提交日期"]] },
    forGap,
    { k:"kind", label:"检索类型（第 6 步）", type:"select", options:[["", "—"], ["phrasing", "不同说法"], ["forward", "正向追踪"], ["venue", "最新会议"], ["cross", "跨领域"]] },
    postCutoff, refresh,
  ],
  paper: [{ ...paperId, placeholder:"arXiv:… / ACL:… / DOI:… / S2 paperId" }, refresh],
  fulltext: [{ k:"key", label:"论文 ID", placeholder:"arXiv:… / ACL:…", wide:true, required:true }, refresh],
  quote: [
    { k:"key", label:"在哪里找", placeholder:"arXiv:… / ACL:… / citations / reviews", required:true },
    { k:"text", label:"原句（… 可省略中间部分）", type:"textarea", wide:true, required:true },
  ],
  citations: [paperId, linkSource, { k:"max", label:"最多取多少篇", type:"number", value:2000 }, { k:"show", label:"显示前几篇", type:"number", value:15 }, forGap,
    { k:"critique", label:"只保留含批评词的引用句（仅 S2）", type:"check" }, postCutoff, refresh],
  references: [paperId, linkSource, { k:"max", label:"最多取多少篇", type:"number", value:2000 }, { k:"show", label:"显示前几篇", type:"number", value:15 }, refresh],
  contexts: [paperId, { k:"alias", label:"方法名（逗号分隔，如 PatchTST）", wide:true }, { k:"top", label:"取被引最多的几篇", type:"number", value:30 },
    { k:"pool", label:"候选引用者上限", type:"number", value:600 }, { k:"show", label:"每篇显示几句批评句", type:"number", value:3 }, forGap, linkSource, postCutoff, refresh],
  reviews: [{ k:"forum", label:"OpenReview 论坛 ID", placeholder:"如 Jbdc0vTOcol", wide:true, required:true }, refresh],
  "matrix-template": [],
  gaps: [],
};
const HINTS = {
  search: "自动写进 search_log.csv；回测项目自动套用日期防火墙。第 6 步验证时填“为候选空白”和“检索类型”。",
  paper: "单篇信息，日期优先用 arXiv v1；回测时晚于截止日会提醒。",
  fulltext: "下载并登记全文（arXiv HTML → ar5iv → PDF；ACL Anthology PDF），登记后才能核对原句。",
  quote: "核对“论文原文 / 检索核对”的原句是否真在登记过的证据里。只比较字母和数字。",
  citations: "正向追踪：谁引用了它。结果存进 evidence/，完整列表可在项目目录查看。",
  references: "向前补漏：它引用了谁。OpenAlex 的参考文献偶有错配，当线索用。",
  contexts: "他人指出（不需要 S2 key）：取被引最多的引用者全文，抽出引用这篇论文的句子。论文多时要几分钟。",
  reviews: "OpenReview 审稿意见。匿名访问常被人机验证拦下，可在设置环境变量 OPENREVIEW_TOKEN 后重试。",
  "matrix-template": "按属性和节点生成空的 matrix.csv。要求第 0–3 步通过且确认点 2 已确认。",
  gaps: "第 5 步：按 R1/R2/R2′/R3 机械列出候选空白，写进 gaps_mechanical.csv。要求第 0–4 步通过。",
};

const state = { status:null, projects:[], projectId:"", project:null, tab:"checks", file:"", job:null, pollTimer:null };

async function api(url, options = {}) {
  const init = { ...options, headers:{ ...(options.headers || {}) } };
  if (init.method && init.method !== "GET") {
    init.headers["X-Gap-Survey"] = "1";
    if (init.body) init.headers["Content-Type"] = "application/json";
  }
  const response = await fetch(url, init);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `请求失败（HTTP ${response.status}）。`);
  return data;
}
const projectUrl = (suffix = "") => `/api/gap-survey/projects/${encodeURIComponent(state.projectId)}${suffix}`;

function showBanner(text) {
  const banner = $("#gsUnavailable");
  banner.textContent = text || "";
  banner.classList.toggle("hidden", !text);
}

async function init() {
  bindStatic();
  try {
    state.status = await api("/api/gap-survey/status");
  } catch (error) {
    showBanner(error.message);
    return;
  }
  if (!state.status.available) {
    showBanner(state.status.reason);
    return;
  }
  renderCommandOptions();
  await loadProjects(store.get(PROJECT_KEY));
}

async function loadProjects(preferred = "") {
  try {
    state.projects = (await api("/api/gap-survey/projects")).projects || [];
  } catch (error) {
    showBanner(error.message);
    return;
  }
  const select = $("#gsProjectSelect");
  select.innerHTML = state.projects.map(p => `<option value="${escapeHtml(p.id)}">${escapeHtml(p.id)}${p.domain ? ` · ${escapeHtml(p.domain)}` : ""}</option>`).join("");
  select.classList.toggle("hidden", !state.projects.length);
  const id = state.projects.some(p => p.id === preferred) ? preferred : state.projects[0]?.id || "";
  $("#gsEmpty").classList.toggle("hidden", Boolean(id));
  $("#gsWorkspace").classList.toggle("hidden", !id);
  if (id) {
    select.value = id;
    await openProject(id);
  }
}

async function openProject(id) {
  if (state.projectId !== id) { state.file = ""; stopPolling(); state.job = null; renderJob(); }
  state.projectId = id;
  store.set(PROJECT_KEY, id);
  await reloadProject();
  activateTab(store.get(TAB_KEY) || state.tab);
  loadJobs();
  if (state.project?.running && (!state.job || state.job.id !== state.project.running.id)) watchJob(state.project.running.id);
}

async function reloadProject() {
  try {
    state.project = (await api(projectUrl())).project;
  } catch (error) {
    showBanner(error.message);
    return;
  }
  showBanner(state.project.error || "");
  renderSide();
  renderChecks();
  renderFiles();
  // 只在换项目时重建命令表单，命令跑完后的刷新不清掉已填的参数
  if ($("#gsCommandFields").dataset.project !== state.projectId) renderCommandFields();
  if (state.file) openFile(state.file, { quiet:true });
}

// ------------------------------------------------------------ 侧栏

function renderSide() {
  const p = state.project;
  const backtest = p.mode === "backtest";
  $("#gsMode").textContent = backtest ? "回测" : "正式调研";
  $("#gsDomain").textContent = p.domain || "（领域未填写）";
  const meta = [["项目", p.id], ["规模", p.scale], ["起始", p.start || "—"], [backtest ? "截止日" : "截至", p.cutoff || "—"], ["读者", p.audience || "—"]];
  if (p.candidatesLockedAt) meta.push(["候选锁定", p.candidatesLockedAt.replace("T", " ")]);
  $("#gsMeta").innerHTML = meta.map(([k, v]) => `<dt>${escapeHtml(k)}</dt><dd>${escapeHtml(v)}</dd>`).join("");
  $("#gsPath").textContent = p.path;
  const s = state.status;
  const key = (ok, name, note, link) => `<div class="gs-key ${ok ? "ok" : ""}"><i aria-hidden="true"></i><span>${name}</span><small>${ok ? "已配置" : link ? `<a href="${link}">${note}</a>` : note}</small></div>`;
  $("#gsKeys").innerHTML = key(s.openalexKey, "OpenAlex", "未配置 · 共用免费额度", "/settings#openalex")
    + key(s.s2Key, "Semantic Scholar", "未配置 · 常被限流（.env）")
    + key(s.openreviewToken, "OpenReview", "未配置（.env）");
  $("#gsSteps").innerHTML = (p.steps || []).map(step => {
    const errors = step.issues.filter(i => i.level === "error").length;
    const mark = !step.started ? `<span class="gs-mark">${step.step}</span>` : step.passed ? `<span class="gs-mark pass">✓</span>` : `<span class="gs-mark fail">${step.step}</span>`;
    const count = !step.started ? "未开始" : errors ? `${errors} 个错误` : "通过";
    return `<button type="button" data-jump-step="${step.step}" aria-label="第 ${step.step} 步 ${escapeHtml(step.title)}：${count}">${mark}<span>${escapeHtml(step.title)}</span><span class="gs-count">${count}</span></button>`;
  }).join("");
}

// ------------------------------------------------------------ 逐步检查

function renderChecks() {
  const p = state.project;
  $("#gsConfirmations").innerHTML = (p.confirmations || []).map(c => `
    <article class="gs-confirm ${c.confirmedAt ? "done" : ""}">
      <h3><span class="gs-mark ${c.confirmedAt ? "pass" : ""}">${c.confirmedAt ? "✓" : c.point}</span>确认点 ${c.point}</h3>
      <p>${escapeHtml(c.title)}</p>
      <p>${c.confirmedAt ? `已于 ${escapeHtml(c.confirmedAt.replace("T", " "))} 确认${c.note ? `：${escapeHtml(c.note)}` : ""}` : "还没确认。确认点只能由你本人在自己的终端（PowerShell）里运行，面板和模型都不能代替你确认。"}</p>
      <div class="gs-cmd"><code>${escapeHtml(c.command)}</code><button class="gs-btn small" type="button" data-copy="${escapeHtml(c.command)}">复制命令</button></div>
    </article>`).join("");
  $("#gsCheckList").innerHTML = (p.steps || []).map(step => {
    const errors = step.issues.filter(i => i.level === "error");
    const status = !step.started ? "未开始" : step.passed ? "通过" : `未通过（${errors.length} 个错误）`;
    const mark = !step.started ? `<span class="gs-mark">${step.step}</span>` : step.passed ? `<span class="gs-mark pass">✓</span>` : `<span class="gs-mark fail">${step.step}</span>`;
    const issues = step.issues.map(i => `<li class="${i.level}"><span class="lv">${i.level === "error" ? "错误" : "提醒"}</span><span>${i.where ? `<b>${escapeHtml(i.where)}</b>：` : ""}${escapeHtml(i.message)}</span></li>`).join("");
    const open = step.started && !step.passed ? "open" : "";
    return `<details class="gs-step ${step.started ? "" : "pending"}" id="gs-step-${step.step}" ${open}>
      <summary>${mark}<strong>第 ${step.step} 步 · ${escapeHtml(step.title)}</strong><span class="gs-count">${status}${step.issues.length - errors.length ? ` · ${step.issues.length - errors.length} 条提醒` : ""}</span></summary>
      ${issues ? `<ul class="gs-issues">${issues}</ul>` : ""}
    </details>`;
  }).join("");
}

// ------------------------------------------------------------ 文件

function renderFiles() {
  const files = state.project.files || [];
  $("#gsFileList").innerHTML = files.map(f => `<li><button type="button" data-file="${escapeHtml(f.name)}" class="${f.name === state.file ? "active" : ""}">
    <span>${escapeHtml(f.name)}</span><small>${f.rows === null ? "" : `${f.rows} 行`}</small><em>${escapeHtml(f.label)}</em></button></li>`).join("");
}

async function openFile(name, { quiet = false } = {}) {
  state.file = name;
  $("#gsFileList").querySelectorAll("[data-file]").forEach(b => b.classList.toggle("active", b.dataset.file === name));
  $("#gsFileTitle").textContent = name;
  if (!quiet) $("#gsFileBody").innerHTML = `<p class="gs-empty-note">正在读取…</p>`;
  try {
    const data = await api(`${projectUrl("/file")}?name=${encodeURIComponent(name)}`);
    if (data.kind === "text") {
      $("#gsFileInfo").textContent = "";
      $("#gsFileBody").innerHTML = `<pre>${escapeHtml(data.text)}</pre>`;
      return;
    }
    $("#gsFileInfo").textContent = `${data.total} 行${data.truncated ? `，只显示前 ${data.rows.length} 行` : ""}`;
    $("#gsFileBody").innerHTML = data.rows.length || data.columns.length
      ? `<table class="gs-table"><thead><tr>${data.columns.map(c => `<th>${escapeHtml(c)}</th>`).join("")}</tr></thead><tbody>${data.rows.map(r => `<tr>${data.columns.map((_, i) => `<td>${escapeHtml(r[i] ?? "")}</td>`).join("")}</tr>`).join("")}</tbody></table>${data.rows.length ? "" : `<p class="gs-empty-note">还没有内容。</p>`}`
      : `<p class="gs-empty-note">空文件。</p>`;
  } catch (error) {
    $("#gsFileBody").innerHTML = `<p class="gs-empty-note">${escapeHtml(error.message)}</p>`;
  }
}

// ------------------------------------------------------------ 命令

function renderCommandOptions() {
  const commands = state.status.commands || [];  // 列表：保持服务端定义的顺序
  const select = $("#gsCommand");
  select.innerHTML = commands.map(c => `<option value="${escapeHtml(c.name)}">${escapeHtml(c.label)} · ${escapeHtml(c.name)}</option>`).join("");
  const saved = store.get(COMMAND_KEY);
  if (saved && commands.some(c => c.name === saved)) select.value = saved;
}

function renderCommandFields() {
  const command = $("#gsCommand").value;
  const backtest = state.project?.mode === "backtest";
  const fields = (FIELDS[command] || []).filter(f => !f.backtestOnly || backtest);
  $("#gsCommandFields").dataset.project = state.projectId;
  $("#gsCommandFields").innerHTML = fields.map(f => {
    const id = `gs-f-${f.k}`;
    if (f.type === "check") return `<label class="gs-check ${f.wide ? "wide" : ""}"><input type="checkbox" name="${f.k}" id="${id}">${escapeHtml(f.label)}</label>`;
    let control;
    if (f.type === "select") control = `<select name="${f.k}" id="${id}">${f.options.map(([v, l]) => `<option value="${escapeHtml(v)}">${escapeHtml(l)}</option>`).join("")}</select>`;
    else if (f.type === "textarea") control = `<textarea name="${f.k}" id="${id}" ${f.required ? "required" : ""}></textarea>`;
    else control = `<input name="${f.k}" id="${id}" type="${f.type === "number" ? "number" : f.type === "date" ? "date" : "text"}" ${f.type === "number" ? `min="1" step="1"` : ""} ${f.value !== undefined ? `value="${escapeHtml(f.value)}"` : ""} ${f.placeholder ? `placeholder="${escapeHtml(f.placeholder)}"` : ""} ${f.required ? "required" : ""}>`;
    return `<label class="gs-field ${f.wide ? "wide" : ""}">${escapeHtml(f.label)}${control}</label>`;
  }).join("") || `<p class="gs-hint wide">这个命令不需要参数。</p>`;
  $("#gsCommandHint").textContent = HINTS[command] || "";
  updatePreview();
}

function readArgs() {
  const command = $("#gsCommand").value;
  const args = {};
  for (const f of FIELDS[command] || []) {
    const node = $("#gsCommandFields").querySelector(`[name="${f.k}"]`);
    if (!node) continue;
    if (f.type === "check") { if (node.checked) args[f.k] = true; continue; }
    let value = node.value.trim();
    if (f.type === "textarea") value = value.replace(/\s+/g, " ");
    if (value) args[f.k] = f.type === "number" ? Number(value) : value;
  }
  return { command, args };
}

function updatePreview() {
  const { command, args } = readArgs();
  const parts = ["gapr", command, "<项目>"];
  for (const f of FIELDS[command] || []) {
    const value = args[f.k];
    if (value === undefined) continue;
    const show = typeof value === "string" && /\s/.test(value) ? `"${value}"` : String(value);
    if (["query", "id", "key", "text", "forum"].includes(f.k)) parts.push(show);
    else parts.push(value === true ? `--${f.k.replace(/[A-Z]/g, m => `-${m.toLowerCase()}`)}` : `--${f.k}=${show}`);
  }
  $("#gsCommandPreview").textContent = parts.join(" ");
}

async function runCommand(event) {
  event.preventDefault();
  const { command, args } = readArgs();
  store.set(COMMAND_KEY, command);
  $("#gsRunButton").disabled = true;
  try {
    const data = await api(projectUrl("/run"), { method:"POST", body:JSON.stringify({ command, args }) });
    state.job = data.job;
    renderJob();
    watchJob(data.job.id);
  } catch (error) {
    $("#gsConsole").textContent = error.message;
    $("#gsJobState").textContent = "未运行";
    $("#gsRunButton").disabled = false;
  }
}

function stopPolling() {
  if (state.pollTimer) clearTimeout(state.pollTimer);
  state.pollTimer = null;
}

function watchJob(id) {
  stopPolling();
  const tick = async () => {
    try {
      state.job = (await api(`/api/gap-survey/jobs/${encodeURIComponent(id)}`)).job;
    } catch (error) {
      $("#gsJobState").textContent = error.message;
      $("#gsRunButton").disabled = false;
      return;
    }
    renderJob();
    if (state.job.running) {
      state.pollTimer = setTimeout(tick, 900);
    } else {
      loadJobs();
      reloadProject();
    }
  };
  tick();
}

function renderJob() {
  const job = state.job;
  const consoleNode = $("#gsConsole");
  if (!job) {
    $("#gsJobTitle").textContent = "输出";
    $("#gsJobState").textContent = "";
    consoleNode.textContent = "";
    $("#gsRunButton").disabled = false;
    $("#gsCancelButton").classList.add("hidden");
    return;
  }
  const stick = consoleNode.scrollTop + consoleNode.clientHeight >= consoleNode.scrollHeight - 24;
  $("#gsJobTitle").textContent = job.display;
  $("#gsJobState").textContent = job.running ? "运行中…" : job.cancelled ? "已停止" : job.returncode === 0 ? "完成" : `结束（退出码 ${job.returncode}）`;
  consoleNode.textContent = job.output || (job.running ? "（等待输出…）" : "（没有输出）");
  if (stick) consoleNode.scrollTop = consoleNode.scrollHeight;
  $("#gsRunButton").disabled = job.running;
  $("#gsCancelButton").classList.toggle("hidden", !job.running);
}

async function loadJobs() {
  if (!state.projectId) return;
  try {
    const jobs = (await api(projectUrl("/jobs"))).jobs || [];
    $("#gsJobList").innerHTML = jobs.map(j => `<li><button type="button" data-job="${escapeHtml(j.id)}"><span>${j.running ? "运行中" : j.cancelled ? "已停止" : j.returncode === 0 ? "完成" : `退出码 ${j.returncode}`}</span><code>${escapeHtml(j.display)}</code></button></li>`).join("") || `<li class="gs-hint">还没有。</li>`;
  } catch {}
}

// ------------------------------------------------------------ 新建项目 / 方法说明

function openProjectDialog() {
  const dialog = $("#gsProjectDialog");
  $("#gsProjectForm").reset();
  $("#gsProjectError").classList.add("hidden");
  dialog.showModal();
}

async function createProject(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const data = Object.fromEntries(new FormData(form).entries());
  const submit = form.querySelector('[type="submit"]');
  submit.disabled = true;
  try {
    const result = await api("/api/gap-survey/projects", { method:"POST", body:JSON.stringify(data) });
    $("#gsProjectDialog").close();
    await loadProjects(result.project.id);
  } catch (error) {
    $("#gsProjectError").textContent = error.message;
    $("#gsProjectError").classList.remove("hidden");
  } finally {
    submit.disabled = false;
  }
}

function inlineMarkdown(text) {
  return escapeHtml(text)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
}

function renderMarkdown(source) {
  const lines = source.replace(/\r\n/g, "\n").split("\n");
  const out = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (/^```/.test(line)) {
      const body = [];
      for (i += 1; i < lines.length && !/^```/.test(lines[i]); i += 1) body.push(lines[i]);
      out.push(`<pre><code>${escapeHtml(body.join("\n"))}</code></pre>`);
      i += 1;
      continue;
    }
    if (/^-{3,}\s*$/.test(line)) { out.push("<hr>"); i += 1; continue; }
    const heading = line.match(/^(#{1,4})\s+(.*)$/);
    if (heading) { const level = Math.min(heading[1].length, 3); out.push(`<h${level}>${inlineMarkdown(heading[2])}</h${level}>`); i += 1; continue; }
    if (/^\|/.test(line)) {
      const rows = [];
      for (; i < lines.length && /^\|/.test(lines[i]); i += 1) rows.push(lines[i]);
      const cells = row => row.replace(/^\||\|$/g, "").split("|").map(c => c.trim());
      const body = rows.filter((_, n) => n !== 1 || !/^\|[\s:|-]+\|?$/.test(rows[1]));
      out.push(`<table>${body.map((row, n) => `<tr>${cells(row).map(c => n === 0 ? `<th>${inlineMarkdown(c)}</th>` : `<td>${inlineMarkdown(c)}</td>`).join("")}</tr>`).join("")}</table>`);
      continue;
    }
    if (/^\s*([-*]|\d+\.)\s+/.test(line)) {
      const ordered = /^\s*\d+\./.test(line);
      const items = [];
      for (; i < lines.length && /^\s*([-*]|\d+\.)\s+/.test(lines[i]); i += 1) items.push(lines[i].replace(/^\s*([-*]|\d+\.)\s+/, ""));
      out.push(`<${ordered ? "ol" : "ul"}>${items.map(item => `<li>${inlineMarkdown(item)}</li>`).join("")}</${ordered ? "ol" : "ul"}>`);
      continue;
    }
    if (/^>\s?/.test(line)) {
      const quote = [];
      for (; i < lines.length && /^>\s?/.test(lines[i]); i += 1) quote.push(lines[i].replace(/^>\s?/, ""));
      out.push(`<blockquote>${inlineMarkdown(quote.join(" "))}</blockquote>`);
      continue;
    }
    if (!line.trim()) { i += 1; continue; }
    const para = [];
    for (; i < lines.length && lines[i].trim() && !/^(#{1,4}\s|```|\||>|\s*([-*]|\d+\.)\s)/.test(lines[i]); i += 1) para.push(lines[i]);
    out.push(`<p>${inlineMarkdown(para.join(" "))}</p>`);
  }
  return out.join("\n");
}

async function openGuide() {
  const dialog = $("#gsGuideDialog");
  const body = $("#gsGuideBody");
  if (!body.dataset.loaded) {
    body.innerHTML = `<p class="gs-hint">正在读取…</p>`;
    try {
      body.innerHTML = renderMarkdown((await api("/api/gap-survey/guide")).text);
      body.dataset.loaded = "1";
    } catch (error) {
      body.innerHTML = `<p class="gs-hint">${escapeHtml(error.message)}</p>`;
    }
  }
  dialog.showModal();
}

// ------------------------------------------------------------ 事件

function activateTab(tab) {
  if (!["checks", "files", "run"].includes(tab)) tab = "checks";
  state.tab = tab;
  store.set(TAB_KEY, tab);
  document.querySelectorAll(".gs-tabs [data-tab]").forEach(b => { const on = b.dataset.tab === tab; b.classList.toggle("active", on); b.setAttribute("aria-selected", String(on)); });
  document.querySelectorAll(".gs-view").forEach(v => v.classList.toggle("active", v.dataset.view === tab));
  if (tab === "files" && !state.file && state.project?.files?.length) openFile(state.project.files[0].name);
}

async function copyText(text, button) {
  try {
    await navigator.clipboard.writeText(text);
    const old = button.textContent;
    button.textContent = "已复制";
    setTimeout(() => { button.textContent = old; }, 1400);
  } catch {
    window.prompt("复制下面的内容：", text);
  }
}

function bindStatic() {
  $("#gsProjectSelect").addEventListener("change", event => openProject(event.target.value));
  $("#gsNewProject").addEventListener("click", openProjectDialog);
  $("#gsEmptyCreate").addEventListener("click", openProjectDialog);
  $("#gsProjectForm").addEventListener("submit", createProject);
  $("#gsGuideButton").addEventListener("click", openGuide);
  document.querySelectorAll(".gs-dialog [data-close]").forEach(b => b.addEventListener("click", () => b.closest("dialog").close()));
  document.querySelectorAll(".gs-tabs [data-tab]").forEach(b => b.addEventListener("click", () => activateTab(b.dataset.tab)));
  $("#gsRefresh").addEventListener("click", () => reloadProject());
  $("#gsCopyPath").addEventListener("click", event => copyText(state.project?.path || "", event.currentTarget));
  $("#gsCommand").addEventListener("change", renderCommandFields);
  $("#gsCommandFields").addEventListener("input", updatePreview);
  $("#gsCommandFields").addEventListener("change", updatePreview);
  $("#gsRunForm").addEventListener("submit", runCommand);
  $("#gsCancelButton").addEventListener("click", async () => {
    if (!state.job) return;
    try { state.job = (await api(`/api/gap-survey/jobs/${encodeURIComponent(state.job.id)}/cancel`, { method:"POST" })).job; renderJob(); } catch (error) { $("#gsJobState").textContent = error.message; }
  });
  document.addEventListener("click", event => {
    const copy = event.target.closest("[data-copy]");
    if (copy) copyText(copy.dataset.copy, copy);
    const file = event.target.closest("[data-file]");
    if (file) openFile(file.dataset.file);
    const step = event.target.closest("[data-jump-step]");
    if (step) {
      activateTab("checks");
      const node = document.getElementById(`gs-step-${step.dataset.jumpStep}`);
      if (node) { node.open = true; node.scrollIntoView({ behavior:"smooth", block:"start" }); }
    }
    const job = event.target.closest("[data-job]");
    if (job) { activateTab("run"); watchJob(job.dataset.job); }
  });
  const modeSelect = $("#gsProjectForm [name=mode]");
  modeSelect.addEventListener("change", () => {
    $("#gsProjectForm [data-cutoff-hint]").textContent = modeSelect.value === "backtest" ? "回测必填：只能看这一天及之前公开的论文。" : "正式调研留空 = 今天；回测必填。";
  });
}

init();
})();
