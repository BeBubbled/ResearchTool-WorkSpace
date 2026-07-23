const readerState = { config: null, document: null, content: null, view: "original", selection: "", blockId: "", poller: null, selectedPreset: "", sourceFile: null };
const $ = selector => document.querySelector(selector);
const escapeHtml = value => String(value).replace(/[&<>'"]/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[char]));

function configHtml(id) {
  const presets = readerState.config?.llmPresets || [];
  if (!readerState.selectedPreset || !presets.some(item => item.id === readerState.selectedPreset)) readerState.selectedPreset = presets[0]?.id || "custom";
  const options = presets.map(item => `<option value="${escapeHtml(item.id)}" ${item.id === readerState.selectedPreset ? "selected" : ""}>${escapeHtml(item.name)} · ${escapeHtml(item.model)}</option>`).join("") + `<option value="custom" ${readerState.selectedPreset === "custom" ? "selected" : ""}>手动添加 OpenAI-compatible 配置</option>`;
  return `<label>LLM 配置<select class="llm-preset" data-config="${id}">${options}</select></label><div class="custom-llm hidden" data-custom="${id}"><label>名称<input data-llm="name" placeholder="例如：实验室代理"></label><label>Base URL<input data-llm="baseUrl" placeholder="https://api.example.com/v1"></label><label>API Key<input data-llm="apiKey" type="password" autocomplete="off"></label><label>模型 ID<input data-llm="model" placeholder="模型名称"></label><button type="button" class="save-preset">保存为本机预设</button><p class="preset-note">将写入项目的本地 .env；密钥不会发送回浏览器。</p></div>`;
}

function wireLlmConfig(target) {
  target.querySelector(".llm-preset")?.addEventListener("change", event => {
    target.querySelector(".custom-llm")?.classList.toggle("hidden", event.target.value !== "custom");
  });
  target.querySelector(".save-preset")?.addEventListener("click", () => persistLlmPreset(target));
}

function llmConfig(target) {
  const presetId = target.querySelector(".llm-preset")?.value;
  if (presetId !== "custom") return { mode: "preset", presetId };
  const custom = key => target.querySelector(`[data-llm="${key}"]`)?.value.trim();
  const result = { mode: "custom", name: custom("name"), baseUrl: custom("baseUrl"), apiKey: custom("apiKey"), model: custom("model") };
  if (Object.values(result).some(value => !value)) throw new Error("请完整填写手动 LLM 配置。");
  return result;
}

function refreshLlmConfigs() {
  $("#uploadLlm").innerHTML = configHtml("upload"); $("#questionLlm").innerHTML = configHtml("question");
  wireLlmConfig($("#uploadLlm")); wireLlmConfig($("#questionLlm"));
}

async function persistLlmPreset(target) {
  let config;
  try { config = llmConfig(target); } catch (error) { $("#uploadStatus").textContent = error.message; return; }
  const button = target.querySelector(".save-preset"); if (button) button.disabled = true;
  try {
    const response = await fetch("/api/llm-presets", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(config) });
    const data = await response.json(); if (!response.ok) throw new Error(data.error || "保存预设失败。");
    readerState.config.llmPresets.push(data.preset); readerState.selectedPreset = data.preset.id;
    refreshLlmConfigs();
    $("#uploadStatus").textContent = `已保存本机预设：${data.preset.name}`;
  } catch (error) { $("#uploadStatus").textContent = error.message; }
  finally { if (button) button.disabled = false; }
}

function localAsset(url) {
  if (/^(https?:|data:)/i.test(url)) return url;
  return `${readerState.content.assetBase}${url.split("/").map(encodeURIComponent).join("/")}`;
}

function safeLink(url) {
  return /^(https?:|mailto:|#)/i.test(url) ? url : "#";
}

function markdownInline(text) {
  const formulas = [];
  const protectFormula = value => { formulas.push(value); return `@@READER_FORMULA_${formulas.length - 1}@@`; };
  // Keep source LaTex on a wrapper. MathJax replaces the visual DOM, but the
  // wrapper lets selection handling recover exactly what the author wrote.
  const protectedText = String(text)
    .replace(/\\\(([\s\S]*?)\\\)/g, protectFormula)
    .replace(/(?<!\\)\$\$([\s\S]*?)\$\$/g, protectFormula)
    .replace(/(?<!\\)\$(?!\$)([^$\n]+?)\$/g, protectFormula);
  let html = escapeHtml(protectedText);
  html = html.replace(/!\[([^\]]*)\]\(([^\s)]+)(?:\s+[^)]*)?\)/g, (_all, alt, url) => `<img src="${localAsset(url)}" alt="${alt}">`);
  html = html.replace(/\\includegraphics(?:\[[^\]]*\])?\{([^}]+)\}/g, (_all, url) => `<img src="${localAsset(url)}" alt="论文插图">`);
  html = html.replace(/\[([^\]]+)\]\(([^\s)]+)(?:\s+[^)]*)?\)/g, (_all, label, url) => `<a href="${escapeHtml(safeLink(url))}" target="_blank" rel="noreferrer">${label}</a>`);
  html = html.replace(/`([^`]+)`/g, "<code>$1</code>");
  html = html.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  html = html.replace(/(?<!\*)\*([^*\n]+)\*/g, "<em>$1</em>");
  return html.replace(/@@READER_FORMULA_(\d+)@@/g, (_all, index) => {
    const source = formulas[Number(index)];
    return `<span class="math-source" data-latex="${escapeHtml(source)}">${escapeHtml(source)}</span>`;
  });
}

function tableCells(line) { return line.trim().replace(/^\||\|$/g, "").split("|").map(cell => cell.trim()); }
function tableHtml(content) {
  const rows = content.split("\n").filter(Boolean).map(tableCells);
  const header = rows.shift() || []; rows.shift();
  return `<div class="table-wrap"><table><thead><tr>${header.map(cell => `<th>${markdownInline(cell)}</th>`).join("")}</tr></thead><tbody>${rows.map(row => `<tr>${header.map((_, index) => `<td>${markdownInline(row[index] || "")}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
}
function listHtml(content) {
  const lines = content.split("\n").filter(line => line.trim());
  const ordered = /^\s*\d+[.)]\s+/.test(lines[0] || "");
  const pattern = ordered ? /^\s*\d+[.)]\s+/ : /^\s*[-+*]\s+/;
  const tag = ordered ? "ol" : "ul";
  return `<${tag}>${lines.map(line => {
    const indent = Math.floor((line.match(/^\s*/)?.[0].length || 0) / 2);
    return `<li style="margin-left:${indent * 16}px">${markdownInline(line.replace(pattern, ""))}</li>`;
  }).join("")}</${tag}>`;
}

function blockHtml(block) {
  const content = block.content || "";
  if (block.type === "heading") return `<h${Math.min(4, Math.max(2, block.level || 2))} id="${block.id}">${markdownInline(content)}</h${Math.min(4, Math.max(2, block.level || 2))}>`;
  if (block.type === "code") return `<pre><code>${escapeHtml(content.replace(/^```[^\n]*\n?|```$/g, ""))}</code></pre>`;
  if (block.type === "math") return `<div class="math-block math-source" data-latex="${escapeHtml(content)}">${escapeHtml(content)}</div>`;
  if (block.type === "table") return tableHtml(content);
  if (block.type === "quote") return `<blockquote>${content.split("\n").map(markdownInline).join("<br>")}</blockquote>`;
  if (block.type === "rule") return "<hr>";
  if (block.type === "list") return listHtml(content);
  const lines = content.split("\n");
  return `<p>${lines.map(markdownInline).join("<br>")}</p>`;
}

function activeBlocks() {
  if (readerState.view === "translated" && readerState.content.translatedBlocks) return readerState.content.translatedBlocks;
  return readerState.content.blocks;
}

function renderPaper() {
  const blocks = activeBlocks();
  $("#paperContent").innerHTML = blocks.map(block => `<section class="paper-block ${block.type}" data-block-id="${block.id}">${blockHtml(block)}</section>`).join("");
  const headings = readerState.content.blocks.filter(block => block.type === "heading");
  $("#outline").innerHTML = headings.map(block => `<button data-target="${block.id}" class="outline-level-${Math.min(block.level || 1, 3)}">${escapeHtml(block.content)}</button>`).join("") || "<p class=\"muted\">未检测到标题。</p>";
  $("#outline").querySelectorAll("[data-target]").forEach(button => button.addEventListener("click", () => document.getElementById(button.dataset.target)?.scrollIntoView({ behavior: "smooth", block: "start" })));
  typesetMath();
}

function typesetMath() {
  if (window.MathJax?.typesetPromise) window.MathJax.typesetPromise([$("#paperContent"), $("#answerPanel")]).then(() => {
    // MathJax v3 renders custom mjx-container/SVG nodes. Carry the original
    // source onto that generated node as well, because browser selections can
    // start inside the SVG rather than inside the surrounding source span.
    $("#paperContent").querySelectorAll(".math-source[data-latex]").forEach(source => {
      const rendered = source.querySelector("mjx-container");
      if (rendered) rendered.dataset.latex = source.dataset.latex;
    });
  }).catch(() => {});
}

function renderAnswer(markdown) {
  const lines = escapeHtml(markdown).split("\n");
  $("#answerPanel").innerHTML = lines.map(line => {
    if (/^###\s+/.test(line)) return `<h4>${line.slice(4)}</h4>`;
    if (/^##\s+/.test(line)) return `<h3>${line.slice(3)}</h3>`;
    if (/^-\s+/.test(line)) return `<li>${line.slice(2)}</li>`;
    return line.trim() ? `<p>${line}</p>` : "";
  }).join("");
  typesetMath();
}

function hideSelectionMenu() { $("#selectionMenu").classList.add("hidden"); }

function blockForRange(range, element) {
  const direct = element?.closest?.("[data-block-id]");
  if (direct) return direct;
  return [...$("#paperContent").querySelectorAll("[data-block-id]")].find(node => {
    try { return range.intersectsNode(node); } catch { return false; }
  });
}

function selectedReaderFormula(range, element, block) {
  const directlySelected = element?.closest?.(".math-source, [data-latex]");
  if (directlySelected?.dataset.latex) return directlySelected.dataset.latex;
  if (block?.type === "math") return block.content;
  const formulas = [...$("#paperContent").querySelectorAll(".math-source")]
    .filter(node => range.intersectsNode(node))
    .map(node => node.dataset.latex)
    .filter(Boolean);
  return formulas.length ? formulas.join("\n") : "";
}

function captureSelection() {
  const selection = window.getSelection();
  let text = selection?.toString().trim() || "";
  if (!selection.rangeCount) return hideSelectionMenu();
  const range = selection.getRangeAt(0);
  const node = range.commonAncestorContainer;
  const element = node.nodeType === Node.ELEMENT_NODE ? node : node.parentElement;
  const block = blockForRange(range, element);
  if (!block) return hideSelectionMenu();
  const sourceBlock = activeBlocks().find(item => item.id === block.dataset.blockId);
  const latex = selectedReaderFormula(range, element, sourceBlock);
  if (!text && !latex) return hideSelectionMenu();
  if (sourceBlock?.type === "math" || element?.closest(".math-source")) text = latex;
  else if (latex && !text.includes(latex)) text = `${text}\n\n[选区包含的原始 LaTeX]\n${latex}`;
  if (text.length > 12000) return hideSelectionMenu();
  readerState.selection = text;
  readerState.blockId = block.dataset.blockId;
  const rect = selection.getRangeAt(0).getBoundingClientRect();
  const menu = $("#selectionMenu");
  $("#selectionPreview").textContent = text.length > 100 ? `${text.slice(0, 100)}…` : text;
  menu.style.left = `${Math.min(window.innerWidth - 300, Math.max(12, rect.left))}px`;
  menu.style.top = `${Math.max(12, rect.bottom + 8)}px`;
  menu.classList.remove("hidden");
}

async function ask(action) {
  if (!readerState.selection) return;
  let llm;
  try { llm = llmConfig($("#questionLlm")); } catch (error) { renderAnswer(error.message); return; }
  hideSelectionMenu();
  renderAnswer("正在根据选区及相邻段落推理…");
  try {
    const response = await fetch(`/api/reader/documents/${readerState.document.id}/questions`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ selection: readerState.selection, blockId: readerState.blockId, action, question: $("#customQuestion").value, llm }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "解读请求失败。");
    renderAnswer(data.answer);
  } catch (error) { renderAnswer(`请求失败：${error.message}`); }
}

function renderActions() {
  $("#actionButtons").innerHTML = (readerState.config.actions || []).map(action => `<button data-action="${action.id}">${escapeHtml(action.label)}</button>`).join("");
  $("#actionButtons").querySelectorAll("[data-action]").forEach(button => button.addEventListener("click", () => ask(button.dataset.action)));
}

async function loadContent() {
  const response = await fetch(`/api/reader/documents/${readerState.document.id}/content`);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "无法读取论文。");
  readerState.content = data;
  $("#documentTitle").textContent = data.title;
  $("#viewSwitch").hidden = !data.translatedBlocks;
  renderPaper();
}

function stopPolling() { if (readerState.poller) { clearInterval(readerState.poller); readerState.poller = null; } }
function pollDocument() {
  stopPolling();
  const poll = async () => {
    try {
      const response = await fetch(`/api/reader/documents/${readerState.document.id}`);
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "无法查询文档状态。");
      readerState.document = data;
      $("#uploadStatus").textContent = data.message;
      if (data.status === "ready") {
        stopPolling(); await loadContent();
        $("#uploadCard").classList.add("hidden"); $("#readerShell").classList.remove("hidden");
      } else if (data.status === "failed") { stopPolling(); $("#uploadStatus").textContent = data.error || "文档处理失败。"; }
    } catch (error) { stopPolling(); $("#uploadStatus").textContent = error.message; }
  };
  poll(); readerState.poller = setInterval(poll, 1000);
}

async function submitUpload(event) {
  event.preventDefault();
  const source = readerState.sourceFile || $("#sourceFile").files[0];
  if (!source) return;
  const mode = $("#processingMode").value;
  const form = new FormData(); form.append("file", source); form.append("mode", mode);
  if (mode === "ocr_translate") {
    try { form.append("llm", JSON.stringify(llmConfig($("#uploadLlm")))); }
    catch (error) { $("#uploadStatus").textContent = error.message; return; }
  }
  $("#uploadStatus").textContent = "正在提交本地任务…";
  try {
    const response = await fetch("/api/reader/documents", { method: "POST", body: form });
    const data = await response.json(); if (!response.ok) throw new Error(data.error || "上传失败。");
    readerState.document = data; pollDocument();
  } catch (error) { $("#uploadStatus").textContent = error.message; }
}

function setReaderSource(file) {
  if (!file) return;
  const suffix = `.${file.name.split(".").pop().toLowerCase()}`;
  if (!new Set([".pdf", ".md", ".mmd"]).has(suffix)) { $("#uploadStatus").textContent = "仅支持 PDF、Markdown 或 MMD 文件。"; return; }
  readerState.sourceFile = file; $("#fileName").textContent = file.name; $("#uploadStatus").textContent = "";
}

async function initReader() {
  const response = await fetch("/api/reader/config"); readerState.config = await response.json();
  refreshLlmConfigs(); renderActions();
  $("#sourceFile").addEventListener("change", event => setReaderSource(event.target.files[0]));
  const dropzone = $("#readerDropzone");
  ["dragenter", "dragover"].forEach(type => dropzone.addEventListener(type, event => { event.preventDefault(); dropzone.classList.add("dragover"); }));
  ["dragleave", "drop"].forEach(type => dropzone.addEventListener(type, event => { event.preventDefault(); dropzone.classList.remove("dragover"); }));
  dropzone.addEventListener("drop", event => setReaderSource(event.dataTransfer.files[0]));
  $("#processingMode").addEventListener("change", event => $("#uploadLlm").classList.toggle("hidden", event.target.value !== "ocr_translate"));
  $("#uploadForm").addEventListener("submit", submitUpload);
  $("#paperContent").addEventListener("mouseup", () => setTimeout(captureSelection, 0));
  $("#paperContent").addEventListener("touchend", () => setTimeout(captureSelection, 0));
  document.addEventListener("mousedown", event => { if (!event.target.closest("#selectionMenu")) hideSelectionMenu(); });
  $("#viewSwitch").querySelectorAll("button").forEach(button => button.addEventListener("click", () => { readerState.view = button.dataset.view; $("#viewSwitch").querySelectorAll("button").forEach(item => item.classList.toggle("active", item === button)); renderPaper(); }));
  $("#newPaper").addEventListener("click", () => { stopPolling(); $("#readerShell").classList.add("hidden"); $("#uploadCard").classList.remove("hidden"); $("#uploadForm").reset(); readerState.sourceFile = null; $("#fileName").textContent = "尚未选择文件"; $("#uploadStatus").textContent = ""; });
}

initReader().catch(error => { $("#uploadStatus").textContent = `初始化失败：${error.message}`; });
