/* The draft model is also exercised in Node without a browser. */
class PromptDraft {
  constructor(saved = {}) {
    this.text = typeof saved.text === "string" ? saved.text : "";
    this.selected = Array.isArray(saved.selected) ? saved.selected.filter(row => row && typeof row.id === "string" && typeof row.title === "string").map(row => ({ ...row })) : [];
    if (!this.text.trim()) this.selected = [];
    this.files = [];
  }
  restoreSelected(templates) {
    // Older saved drafts contain IDs only. Recover their positions once the library loads.
    this.selected = this.selected.flatMap(row => {
      if (Number.isInteger(row.start) && Number.isInteger(row.end) && row.start >= 0 && row.end > row.start && row.end <= this.text.length) {
        return this.text.slice(row.start, row.end).trim() ? [row] : [];
      }
      const content = templates.find(template => template.id === row.id)?.content.replace(/\r\n?/g, "\n");
      const start = content ? this.text.indexOf(content) : -1;
      return start < 0 ? [] : [{ id:row.id, title:row.title, start, end:start + content.length }];
    });
  }
  updateText(text, replacement = null) {
    const previous = this.text;
    if (text === previous) return false;
    // A textarea edit replaces one range. Track the surviving part of each inserted
    // template so small edits keep its selection, while deleting it releases its button.
    let start = 0;
    while (start < previous.length && start < text.length && previous[start] === text[start]) start++;
    let oldEnd = previous.length, newEnd = text.length;
    while (oldEnd > start && newEnd > start && previous[oldEnd - 1] === text[newEnd - 1]) { oldEnd--; newEnd--; }
    // Prefer the actual selection: adjacent templates can have identical prefixes or
    // suffixes, making a text-only diff attribute a deletion to the wrong template.
    if (replacement && Number.isInteger(replacement.start) && Number.isInteger(replacement.end)) {
      const nextEnd = replacement.end + text.length - previous.length;
      if (replacement.start >= 0 && replacement.end > replacement.start && replacement.end <= previous.length && nextEnd >= replacement.start &&
          previous.slice(0, replacement.start) === text.slice(0, replacement.start) && previous.slice(replacement.end) === text.slice(nextEnd)) {
        start = replacement.start; oldEnd = replacement.end; newEnd = nextEnd;
      }
    }
    const delta = newEnd - oldEnd, count = this.selected.length;
    this.selected = this.selected.flatMap(row => {
      if (!Number.isInteger(row.start) || !Number.isInteger(row.end)) return [row];
      if (row.end <= start) return [row];
      if (row.start >= oldEnd) return [{ ...row, start:row.start + delta, end:row.end + delta }];
      if (start <= row.start && oldEnd >= row.end) return [];
      const shifted = { ...row, start:row.start < start ? row.start : newEnd, end:row.end > oldEnd ? row.end + delta : start };
      return shifted.end > shifted.start && text.slice(shifted.start, shifted.end).trim() ? [shifted] : [];
    });
    this.text = text;
    if (!text.trim()) this.selected = [];
    return count !== this.selected.length;
  }
  add(template) {
    if (this.selected.some(row => row.id === template.id)) return false;
    const content = template.content.replace(/\r\n?/g, "\n");
    const existing = this.text.indexOf(content);
    if (existing >= 0) {
      // Undo or a manual paste may have restored a template whose selection was removed.
      this.selected.push({ id:template.id, title:template.title, start:existing, end:existing + content.length });
      this.selected.sort((a, b) => a.start - b.start);
      return false;
    }
    const separator = this.text ? (this.text.endsWith("\n\n") ? "" : this.text.endsWith("\n") ? "\n" : "\n\n") : "";
    if (this.text.length + separator.length + content.length > 100000) throw new Error("组合后的 Prompt 超过 100,000 字符，请先缩短文字。");
    const start = this.text.length + separator.length;
    this.text += separator + content;
    this.selected.push({ id:template.id, title:template.title, start, end:this.text.length });
    return true;
  }
  addFiles(files) {
    const key = file => JSON.stringify([file.name, file.size, file.lastModified]);
    const seen = new Set(this.files.map(key));
    const merged = [...this.files];
    for (const file of files) {
      if (!seen.has(key(file))) { merged.push(file); seen.add(key(file)); }
    }
    if (merged.length > 20) throw new Error("最多添加 20 个附件，请减少文件数量。");
    if (merged.reduce((sum, file) => sum + file.size, 0) > 100 * 1024 * 1024) throw new Error("附件合计不能超过 100 MB。");
    this.files = merged;
  }
  clear() { this.text = ""; this.selected = []; }
  serialize() { return { text: this.text, selected: this.selected }; }
}
class PromptBridge {
  constructor(target, onReady = () => {}) {
    this.target = target;
    this.pending = new Map();
    target.addEventListener("message", event => {
      if (event.source !== target || event.origin !== target.location.origin || event.data?.source !== "research-prompt-bridge") return;
      if (event.data.type === "READY") { onReady(); return; }
      const item = this.pending.get(event.data.id);
      if (!item) return;
      target.clearTimeout(item.timer); target.clearInterval(item.retry); this.pending.delete(event.data.id);
      if (event.data.ok) item.resolve(event.data);
      else item.reject(Object.assign(new Error(event.data.error || "扩展连接失败。"), { code:event.data.code }));
    });
  }
  request(type, payload = {}, timeout = 8000) {
    const target = this.target, id = target.crypto.randomUUID();
    return new Promise((resolve, reject) => {
      const timer = target.setTimeout(() => {
        target.clearInterval(this.pending.get(id)?.retry); this.pending.delete(id);
        reject(Object.assign(new Error("当前页面未收到扩展响应。已安装扩展时，请在同一 Chrome 或 Edge 中刷新工作台，并确认扩展允许访问此站点；刷新后需重新选择附件。"), { code:"BRIDGE_TIMEOUT" }));
      }, timeout);
      const send = () => target.postMessage({ source:"research-prompt-workbench", id, type, ...payload }, target.location.origin);
      // Only the harmless probe can repeat. Never replay a submission or open extra tabs.
      const retry = type === "PING" ? target.setInterval(send, 250) : null;
      this.pending.set(id, { resolve, reject, timer, retry });
      send();
    });
  }
}
if (typeof module !== "undefined") module.exports = { PromptDraft, PromptBridge };

if (typeof document !== "undefined") (() => {
  const $ = id => document.getElementById(id);
  const escapeHtml = value => String(value).replace(/[&<>"']/g, char => ({ "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;" }[char]));
  const draftKey = "research-prompt-draft-v1";
  const transferKey = "research-prompt-transfer-v1";
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem(draftKey) || "{}"); } catch { /* Start with a usable empty draft. */ }
  const draft = new PromptDraft(saved || {});
  let templates = [], bridgeReady = false, busy = false, editingId = null, transfer = null, toastTimer, pollTimer;
  let bridgeCheck = null;
  const connection = new PromptBridge(window, () => { checkBridge(); });

  function notify(message) {
    $("pageMessage").textContent = message; $("pageMessage").hidden = false;
    clearTimeout(toastTimer); toastTimer = setTimeout(() => { $("pageMessage").hidden = true; }, 6000);
  }
  async function api(path, options = {}) {
    const response = await fetch(path, { ...options, headers: { "X-Prompt-Workbench":"1", ...options.headers } });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "操作失败，请重试。");
    return data;
  }
  function bridge(type, payload = {}, timeout = 8000) {
    return connection.request(type, payload, timeout);
  }
  function updateSubmit() { $("submitPrompt").disabled = busy || !draft.text.trim(); }
  function saveDraft() {
    try { localStorage.setItem(draftKey, JSON.stringify(draft.serialize())); $("draftSaveStatus").textContent = "文字草稿已保存 · 刷新后需重新选择附件"; }
    catch { $("draftSaveStatus").textContent = "浏览器存储不可用，请及时复制文字"; }
    $("charCount").textContent = `${draft.text.length.toLocaleString()} / 100,000`; updateSubmit();
  }
  function renderSelected() {
    $("selectedTemplates").innerHTML = draft.selected.length ? draft.selected.map((row, index) => `<span class="selected-tag"><b>${index + 1}</b>${escapeHtml(row.title)}</span>`).join("") : '<span class="hint">从左侧选择模板，或直接开始输入。</span>';
  }
  function renderTemplates() {
    const query = $("templateSearch").value.toLocaleLowerCase().trim(), category = $("categoryFilter").value;
    const visible = templates.filter(row => (!category || row.category === category) && (!query || `${row.title}\n${row.content}`.toLocaleLowerCase().includes(query)));
    const groups = new Map();
    for (const row of visible) {
      if (!groups.has(row.category)) groups.set(row.category, []);
      groups.get(row.category).push(row);
    }
    $("templateList").innerHTML = [...groups].map(([name, rows]) => `<div class="template-group-heading" role="heading" aria-level="3"><span>${escapeHtml(name)}</span><small>${rows.length} 个模板</small></div>${rows.map(row => {
      const index = draft.selected.findIndex(item => item.id === row.id);
      return `<div class="template-card ${index >= 0 ? "added" : ""}"><button type="button" class="template-pick" data-add="${escapeHtml(row.id)}" ${index >= 0 ? "disabled" : ""}><strong>${escapeHtml(row.title)}${index >= 0 ? `<span class="order-badge">已添加 ${index + 1}</span>` : ""}</strong><small>${escapeHtml(row.category)}</small><span class="template-preview">${escapeHtml(row.content)}</span></button><button type="button" class="template-edit" data-edit="${escapeHtml(row.id)}" aria-label="编辑 ${escapeHtml(row.title)}" title="编辑模板">⋯</button></div>`;
    }).join("")}`).join("") || '<p class="hint">没有匹配的模板，可以新建一个。</p>';
  }
  async function loadTemplates() {
    const data = await api("/api/prompts/templates"); templates = data.templates;
    draft.restoreSelected(templates); renderSelected(); saveDraft();
    const categories = [...new Set(templates.map(row => row.category))];
    const selected = $("categoryFilter").value;
    $("categoryFilter").innerHTML = '<option value="">全部分类</option>' + categories.map(name => `<option>${escapeHtml(name)}</option>`).join("");
    if (categories.includes(selected)) $("categoryFilter").value = selected;
    $("templateCategories").innerHTML = categories.map(name => `<option value="${escapeHtml(name)}"></option>`).join("");
    renderTemplates();
  }
  function editTemplate(id = null) {
    editingId = id; const item = templates.find(row => row.id === id);
    $("templateDialogTitle").textContent = item ? "编辑模板" : "新建模板";
    $("templateTitle").value = item?.title || ""; $("templateCategory").value = item?.category || "通用";
    $("templateContent").value = item?.content || ""; $("deleteTemplate").hidden = !item;
    $("templateError").textContent = ""; $("templateDialog").showModal(); $("templateTitle").focus();
  }
  function renderFiles() {
    $("fileCount").textContent = draft.files.length;
    $("attachmentList").innerHTML = draft.files.map((file, index) => `<li><span class="attachment-name" title="${escapeHtml(file.name)}">${escapeHtml(file.name)}</span><small>${file.size >= 1048576 ? `${(file.size / 1048576).toFixed(1)} MB` : `${Math.ceil(file.size / 1024)} KB`}</small><button type="button" data-remove-file="${index}" aria-label="移除 ${escapeHtml(file.name)}">×</button></li>`).join("");
  }
  function addFiles(files) {
    try { draft.addFiles(files); renderFiles(); } catch (error) { notify(error.message); }
  }
  function checkBridge() {
    if (bridgeCheck) return bridgeCheck;
    $("bridgeStatus").textContent = "检测扩展中…";
    $("bridgeStatus").className = "badge warn";
    $("bridgeDiagnostic").hidden = true;
    $("checkBridge").disabled = true;
    bridgeCheck = bridge("PING", {}, 6000).then(result => {
      bridgeReady = true;
      $("bridgeStatus").textContent = "扩展已连接"; $("bridgeStatus").className = "badge ok";
      $("bridgeDiagnostic").textContent = result.version ? `Prompt Bridge ${result.version} · 可以提交文字和附件。` : "扩展连接正常，可以提交文字和附件。";
    }).catch(error => {
      bridgeReady = false;
      $("bridgeStatus").textContent = error.code === "BRIDGE_RELOAD" ? "请刷新工作台" : error.code === "BRIDGE_TIMEOUT" ? "未连接扩展" : "扩展响应异常";
      $("bridgeStatus").className = "badge warn"; $("bridgeSetup").open = true;
      $("bridgeDiagnostic").textContent = error.message;
    }).finally(() => {
      bridgeCheck = null; $("bridgeDiagnostic").hidden = false; $("checkBridge").disabled = false; updateSubmit();
    });
    return bridgeCheck;
  }
  function showTransfer(status, message) {
    $("transferStatus").hidden = false; $("transferStatus").className = `transfer-status ${status}`;
    $("transferStatus").textContent = message;
  }
  function forgetTransfer() {
    transfer = null;
    try { sessionStorage.removeItem(transferKey); } catch { /* Storage is optional. */ }
  }
  async function pollTransfer() {
    if (!transfer) return;
    try {
      const result = await api(`/api/prompts/transfers/${transfer.id}`, { headers:{ Authorization:`Bearer ${transfer.token}` } });
      showTransfer(result.status, result.message);
      if (["completed", "error", "cancelled"].includes(result.status)) {
        busy = false; updateSubmit(); forgetTransfer(); return;
      }
      pollTimer = setTimeout(pollTransfer, 1600);
    } catch (error) { busy = false; updateSubmit(); showTransfer("error", error.message); forgetTransfer(); }
  }
  async function submit() {
    if (busy || !draft.text.trim()) return;
    busy = true; updateSubmit();
    try {
      await checkBridge();
      if (!bridgeReady) throw new Error("请先按下方步骤连接浏览器扩展，再提交。文字和附件仍保留在此页。");
      // Snapshot the editable draft and the currently selected files for this submission.
      const body = new FormData(); body.append("prompt", draft.text);
      for (const file of draft.files) body.append("files", file, file.name);
      showTransfer("opening", "正在准备本次文字和附件…");
      transfer = await api("/api/prompts/transfers", { method:"POST", body });
      try { sessionStorage.setItem(transferKey, JSON.stringify(transfer)); } catch { /* Can still submit without storage. */ }
      await bridge("OPEN_CHATGPT", { transfer }, 30000);
      showTransfer("opening", "已打开 ChatGPT，正在等待输入框；如果尚未登录，请在新标签页登录。");
      clearTimeout(pollTimer); pollTransfer();
    } catch (error) {
      if (transfer) {
        try { await api(`/api/prompts/transfers/${transfer.id}/status`, { method:"POST", headers:{ "Content-Type":"application/json", Authorization:`Bearer ${transfer.token}` }, body:JSON.stringify({status:"cancelled", message:"未能连接扩展，请重新提交。"}) }); } catch { /* It expires automatically. */ }
      }
      forgetTransfer(); busy = false; updateSubmit(); showTransfer("error", error.message);
    }
  }

  $("promptText").value = draft.text; renderSelected(); saveDraft();
  let textReplacement = null;
  $("promptText").addEventListener("beforeinput", event => {
    const input = event.target;
    textReplacement = !(event.inputType || "").startsWith("history") && input.selectionEnd > input.selectionStart
      ? { start:input.selectionStart, end:input.selectionEnd } : null;
  });
  $("promptText").addEventListener("input", () => {
    if (draft.updateText($("promptText").value, textReplacement)) { renderSelected(); renderTemplates(); }
    textReplacement = null;
    saveDraft();
  });
  $("templateSearch").addEventListener("input", renderTemplates); $("categoryFilter").addEventListener("change", renderTemplates);
  $("templateList").addEventListener("click", event => {
    const add = event.target.closest("[data-add]"), edit = event.target.closest("[data-edit]");
    if (edit) editTemplate(edit.dataset.edit);
    if (add) {
      try {
        draft.add(templates.find(row => row.id === add.dataset.add));
        $("promptText").value = draft.text; $("promptText").scrollTop = $("promptText").scrollHeight;
        saveDraft(); renderSelected(); renderTemplates();
      } catch (error) { notify(error.message); }
    }
  });
  $("newTemplate").addEventListener("click", () => editTemplate());
  $("closeTemplate").addEventListener("click", () => $("templateDialog").close());
  $("templateForm").addEventListener("submit", async event => {
    event.preventDefault(); $("saveTemplate").disabled = true;
    try {
      await api(`/api/prompts/templates${editingId ? `/${editingId}` : ""}`, { method: editingId ? "PUT" : "POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({ title:$("templateTitle").value, category:$("templateCategory").value, content:$("templateContent").value }) });
      await loadTemplates(); $("templateDialog").close(); notify("模板已保存；已加入草稿的文字保持当前编辑内容。");
    } catch (error) { $("templateError").textContent = error.message; }
    finally { $("saveTemplate").disabled = false; }
  });
  $("deleteTemplate").addEventListener("click", async () => {
    if (!editingId || !confirm("删除此模板？已加入草稿的文字会保留。")) return;
    try { await api(`/api/prompts/templates/${editingId}`, {method:"DELETE"}); await loadTemplates(); $("templateDialog").close(); }
    catch (error) { $("templateError").textContent = error.message; }
  });
  $("clearDraft").addEventListener("click", () => {
    if (draft.text && !confirm("清空本次文字和模板选择？附件会保留。")) return;
    draft.clear(); $("promptText").value = ""; saveDraft(); renderSelected(); renderTemplates();
  });
  $("attachmentDrop").addEventListener("click", () => $("attachmentInput").click());
  $("attachmentDrop").addEventListener("keydown", event => { if (["Enter"," "].includes(event.key)) { event.preventDefault(); $("attachmentInput").click(); } });
  $("attachmentInput").addEventListener("change", event => { addFiles([...event.target.files]); event.target.value = ""; });
  for (const type of ["dragenter","dragover"]) $("attachmentDrop").addEventListener(type, event => { event.preventDefault(); $("attachmentDrop").classList.add("dragover"); });
  $("attachmentDrop").addEventListener("dragleave", () => $("attachmentDrop").classList.remove("dragover"));
  $("attachmentDrop").addEventListener("drop", event => { event.preventDefault(); $("attachmentDrop").classList.remove("dragover"); addFiles([...event.dataTransfer.files]); });
  // A missed drop must not navigate away from an unsent draft.
  window.addEventListener("dragover", event => event.preventDefault()); window.addEventListener("drop", event => event.preventDefault());
  $("attachmentList").addEventListener("click", event => { const button = event.target.closest("[data-remove-file]"); if (button) { draft.files.splice(Number(button.dataset.removeFile),1); renderFiles(); } });
  $("copyPrompt").addEventListener("click", async () => { try { await navigator.clipboard.writeText(draft.text); notify("Prompt 已复制。"); } catch { $("promptText").focus(); $("promptText").select(); notify("无法访问剪贴板，请按 Ctrl+C 复制选中文字。"); } });
  $("checkBridge").addEventListener("click", checkBridge); $("submitPrompt").addEventListener("click", submit);
  window.addEventListener("focus", () => { if (!bridgeReady) checkBridge(); });
  loadTemplates().catch(error => { $("templateList").textContent = error.message; notify("读取模板失败，刷新页面可重试。"); });
  checkBridge();
  try { transfer = JSON.parse(sessionStorage.getItem(transferKey) || "null"); } catch { transfer = null; }
  if (transfer?.id && transfer?.token) { busy = true; updateSubmit(); pollTransfer(); }
})();
