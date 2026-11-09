/* 双语改稿 add-ons, loaded by client_extras.js on /paper-revision (kept out of paper_revision.js):
     编译检查     POST /api/paper-revision-tools/projects/<id>/compile-check → errors with file:line, PDF preview
     写回 git     POST …/write-back (preview first, then confirm) → files written, nothing committed
     ?comment=<id> from 全文搜索: scroll to that comment card */
(() => {
  "use strict";
  if (window.PaperRevisionExtras) return;
  const $ = id => document.getElementById(id);
  const escapeHtml = value => String(value ?? "").replace(/[&<>'"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[c]));
  const base = "/api/paper-revision-tools/projects";
  const css = document.createElement("link");
  css.rel = "stylesheet";
  css.href = "/static/paper_revision_extras.css?v=1";
  document.head.append(css);

  const projectId = () => $("projectSelect")?.value || "";

  async function call(path, body) {
    const response = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
    const data = await response.json().catch(() => ({ error: `请求失败 (${response.status})` }));
    if (!response.ok) throw new Error(data.error || `请求失败 (${response.status})`);
    return data;
  }

  let dialog = null;
  function showDialog(title, html) {
    if (!dialog) {
      dialog = document.createElement("dialog");
      dialog.className = "revision-extras-dialog";
      dialog.addEventListener("click", event => { if (event.target === dialog || event.target.closest("[data-close]")) dialog.close(); });
      document.body.append(dialog);
    }
    dialog.innerHTML = `<header><h2>${escapeHtml(title)}</h2><button type="button" data-close aria-label="关闭">×</button></header><div class="revision-extras-body">${html}</div>`;
    if (!dialog.open) dialog.showModal();
    return dialog;
  }

  async function compileCheck(button) {
    const id = projectId();
    if (!id) return window.ClientToast?.show("先选择一篇论文。", { kind: "bad" });
    button.disabled = true;
    const label = button.textContent;
    button.textContent = "编译中…";
    showDialog("编译检查", `<p class="hint">正在复制论文目录并用本机 LaTeX 编译当前版本（不启用 shell-escape），通常需要几十秒…</p>`);
    try {
      const result = await call(`${base}/${encodeURIComponent(id)}/compile-check`);
      const list = (items, empty) => items.length ? `<ul>${items.map(item => `<li><code>${escapeHtml(item)}</code></li>`).join("")}</ul>` : `<p class="hint">${empty}</p>`;
      showDialog(result.ok ? "编译通过" : "编译失败", `
        <p class="revision-extras-status ${result.ok ? "ok" : "bad"}">${result.ok ? "✓ 已生成 PDF" : "✕ 没有生成 PDF"} · ${escapeHtml(result.engine)} · ${escapeHtml(result.main)} · v${escapeHtml(result.revision)} · ${escapeHtml(result.seconds)} 秒</p>
        ${result.copiedAssets ? "" : `<p class="hint">这篇论文不是从本机目录导入的，编译目录里只有 .tex / .bib，缺少图片、.cls 等文件时会报错。</p>`}
        <h3>错误 · ${result.errors.length}</h3>${list(result.errors, "没有错误。")}
        <h3>警告 · ${result.warnings.length}${result.undefinedReferences ? `（未定义引用 ${result.undefinedReferences}）` : ""}</h3>${list(result.warnings.slice(0, 15), "没有警告。")}
        <div class="revision-extras-actions">${result.pdfUrl ? `<a class="button" href="${escapeHtml(result.pdfUrl)}" target="_blank" rel="noopener">打开 PDF ↗</a>` : ""}${result.logUrl ? `<a class="button secondary" href="${escapeHtml(result.logUrl)}">下载 .log</a>` : ""}</div>
        <p class="hint">编译在临时副本里进行，原目录不变。编译通过只说明 LaTeX 语法和引用可用，内容仍需人工审阅。</p>`);
    } catch (error) {
      showDialog("无法编译检查", `<p class="revision-extras-status bad">${escapeHtml(error.message)}</p><p class="hint">可在 设置 → 环境检查 中确认 LaTeX 是否可用。</p>`);
    } finally {
      button.disabled = false;
      button.textContent = label;
    }
  }

  async function writeBack(button) {
    const id = projectId();
    if (!id) return window.ClientToast?.show("先选择一篇论文。", { kind: "bad" });
    button.disabled = true;
    try {
      const plan = await call(`${base}/${encodeURIComponent(id)}/write-back`, { confirm: false });
      const names = items => items.map(item => `<li><code>${escapeHtml(item.name)}</code>${item.new ? " <em>新文件</em>" : ""}${item.status ? ` <em>git: ${escapeHtml(item.status)}</em>` : ""}</li>`).join("");
      if (!plan.changes.length && !plan.blocked.length) {
        showDialog("写回 git 工作副本", `<p class="revision-extras-status ok">磁盘上的文件已经和 v${escapeHtml(plan.revision)} 一致，无需写回。</p>`);
        return;
      }
      const node = showDialog("写回 git 工作副本", `
        <p>把 v${escapeHtml(plan.revision)} 写入 <code>${escapeHtml(plan.root)}</code>（分支 <code>${escapeHtml(plan.branch || "?")}</code>）。不会提交，可以用 <code>git diff</code> 查看、用 <code>git checkout -- 文件</code> 撤销。</p>
        ${plan.changes.length ? `<h3>将要写入 · ${plan.changes.length}</h3><ul>${names(plan.changes)}</ul>` : ""}
        ${plan.blocked.length ? `<h3>有未提交改动，不能写回 · ${plan.blocked.length}</h3><ul>${names(plan.blocked)}</ul><p class="revision-extras-status bad">请先在 git 中提交或暂存这些文件，避免覆盖你的修改。</p>` : ""}
        <div class="revision-extras-actions"><button type="button" class="button" data-confirm-write ${plan.blocked.length ? "disabled" : ""}>写入 ${plan.changes.length} 个文件</button><button type="button" class="button secondary" data-close>取消</button></div>`);
      node.querySelector("[data-confirm-write]")?.addEventListener("click", async event => {
        event.currentTarget.disabled = true;
        try {
          const result = await call(`${base}/${encodeURIComponent(id)}/write-back`, { confirm: true, revision: plan.revision });
          showDialog("已写回", `<p class="revision-extras-status ok">已写入 ${result.written.length} 个文件，未提交。</p><ul>${result.written.map(name => `<li><code>${escapeHtml(name)}</code></li>`).join("")}</ul>${result.undo ? `<p class="hint">撤销：</p><pre>${escapeHtml(result.undo)}</pre>` : ""}<p class="hint">下次打开这篇论文时，改稿会把磁盘上的新内容作为原稿重新读取。</p>`);
        } catch (error) {
          showDialog("写回失败", `<p class="revision-extras-status bad">${escapeHtml(error.message)}</p>`);
        }
      });
    } catch (error) {
      showDialog("不能写回", `<p class="revision-extras-status bad">${escapeHtml(error.message)}</p>`);
    } finally {
      button.disabled = false;
    }
  }

  function mountButtons() {
    const anchor = $("export");
    if (!anchor || $("compileCheck")) return;
    const compile = Object.assign(document.createElement("button"), { id: "compileCheck", type: "button", className: "secondary", textContent: "编译检查" });
    compile.dataset.command = "编译检查（本机 LaTeX）";
    compile.title = "在临时副本里用本机 LaTeX 编译当前版本，列出错误";
    const write = Object.assign(document.createElement("button"), { id: "writeBackGit", type: "button", className: "secondary", textContent: "写回 git" });
    write.dataset.command = "写回 git 工作副本";
    write.title = "把当前版本写入原论文目录（须为 git 工作副本，不提交）";
    compile.addEventListener("click", () => compileCheck(compile));
    write.addEventListener("click", () => writeBack(write));
    anchor.after(compile, write);
    const sync = () => { const hidden = !projectId(); compile.hidden = hidden; write.hidden = hidden; };
    $("projectSelect")?.addEventListener("change", sync);
    new MutationObserver(sync).observe(anchor, { attributes: true, attributeFilter: ["hidden"] });
    sync();
  }

  // ?comment=<id>: wait for the project to open, then scroll to that comment.
  async function focusLinkedComment() {
    const params = new URLSearchParams(location.search);
    const commentId = params.get("comment");
    const pid = params.get("project");
    if (!commentId || !pid) return;
    for (let attempt = 0; attempt < 40 && projectId() !== pid; attempt += 1) await new Promise(resolve => setTimeout(resolve, 250));
    if (projectId() !== pid) return;
    await new Promise(resolve => setTimeout(resolve, 400));
    try {
      const response = await fetch(`/api/paper-revision/projects/${encodeURIComponent(pid)}`);
      const project = await response.json();
      const index = (project.comments || []).findIndex(comment => comment.id === commentId);
      const card = index >= 0 ? document.querySelectorAll("#comments .comment-card")[index] : null;
      if (!card) {
        window.ClientToast?.show("这条批注已在重新打开论文时移入备份；可在批注栏恢复上次的批注。", { timeout: 9000 });
        return;
      }
      card.scrollIntoView({ behavior: "smooth", block: "center" });
      card.classList.add("revision-extras-linked");
      setTimeout(() => card.classList.remove("revision-extras-linked"), 3200);
      card.querySelector("blockquote")?.click();
    } catch {}
  }

  window.PaperRevisionExtras = { compileCheck, writeBack };
  mountButtons();
  focusLinkedComment();
})();
