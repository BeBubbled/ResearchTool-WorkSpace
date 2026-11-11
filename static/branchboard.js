/* 对话树（Branchboard）页面：无限画布、卡片、划词提问、抽屉、权限批准与 SSE 实时更新。 */
(function () {
  "use strict";
  const core = window.BranchboardCore;
  const { LAYOUT, escapeHtml: esc } = core;
  const $ = id => document.getElementById(id);
  const API = "/api/branchboard";
  const LAST_TREE_KEY = "branchboard.lastTree";

  const S = {
    config: null, trees: [], tree: null, els: {}, layout: null,
    selected: null, target: null, drawerId: null, pendingSel: null,
    zoom: 1, panX: 40, panY: 40, es: null, layoutTimer: 0,
  };

  const els = {
    viewport: $("bbViewport"), stage: $("bbStage"), wires: $("bbWires"), treeSelect: $("bbTreeSelect"),
    composer: $("bbComposer"), question: $("bbQuestion"), send: $("bbSend"), targetLabel: $("bbTargetLabel"),
    targetQuote: $("bbTargetQuote"), targetClear: $("bbTargetClear"), ask: $("bbAskSelection"),
    drawer: $("bbDrawer"), drawerBody: $("bbDrawerBody"), empty: $("bbEmpty"), status: $("bbStatus"),
  };

  /* ---------- 工具 ---------- */
  async function api(method, url, body) {
    const response = await fetch(API + url, {
      method, headers: body ? { "Content-Type": "application/json" } : {}, body: body ? JSON.stringify(body) : undefined,
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || `请求失败（${response.status}）`);
    return data;
  }
  let toastTimer = 0;
  function toast(message) {
    const el = $("bbToast");
    el.textContent = message; el.classList.remove("hidden");
    clearTimeout(toastTimer); toastTimer = setTimeout(() => el.classList.add("hidden"), 3200);
  }
  const num = id => core.turnNumber(S.tree, id);
  const turn = id => S.tree && S.tree.turns[id];
  const ready = t => t && t.sessionId && (t.answer.status === "done" || t.answer.status === "interrupted");
  const running = t => t && (t.answer.status === "pending" || t.answer.status === "streaming");
  const hasChildren = id => S.tree.order.some(cid => S.tree.turns[cid].parentId === id);
  const fillSelect = (select, items, value) => {
    select.innerHTML = items.map(([v, label]) => `<option value="${esc(v)}">${esc(label)}</option>`).join("");
    select.value = value ?? (items[0] && items[0][0]);
  };

  /* ---------- 配置与树列表 ---------- */
  async function loadConfig() {
    S.config = await api("GET", "/config");
    const models = [["", "默认模型"], ...S.config.models.map(m => [m.id, m.label])];
    const efforts = [["", "默认强度"], ...S.config.efforts.map(e => [e, `强度 ${e}`])];
    const modes = S.config.permissionModes.map(m => [m.id, m.label]);
    fillSelect($("bbModel"), models); fillSelect($("bbTreeModel"), models);
    fillSelect($("bbEffort"), efforts); fillSelect($("bbTreeEffort"), efforts);
    fillSelect($("bbPermission"), modes); fillSelect($("bbTreePermission"), modes);
    const claude = S.config.claude;
    if (claude && claude.error) showStatus(claude.error);
  }
  function showStatus(text) { els.status.textContent = text; els.status.classList.toggle("hidden", !text); }

  async function loadTrees(selectId) {
    const { trees } = await api("GET", "/trees");
    S.trees = trees;
    els.treeSelect.innerHTML = trees.length
      ? trees.map(t => `<option value="${t.id}">${esc(t.title)}${t.missing ? "（文件缺失）" : ""}</option>`).join("")
      : `<option value="">（还没有对话树）</option>`;
    let id = selectId;
    if (!id) { try { id = localStorage.getItem(LAST_TREE_KEY); } catch (_e) { /* 无痕模式 */ } }
    if (!trees.some(t => t.id === id)) id = trees[0] && trees[0].id;
    if (id) { els.treeSelect.value = id; await openTree(id); }
    else showNoTree();
  }

  function showNoTree() {
    closeStream();
    S.tree = null; clearStage();
    $("bbCwd").textContent = "";
    $("bbEmptyTitle").textContent = "还没有对话树";
    $("bbEmptyText").textContent = "新建一棵树并选择工作目录。主线向右延伸；在任意回答里选中文字即可就此追问，分支有自己独立的上下文。";
    $("bbEmptyCreate").classList.remove("hidden");
    els.empty.classList.remove("hidden");
    els.composer.classList.add("hidden");
  }

  async function openTree(id) {
    let data;
    try { data = await api("GET", `/trees/${id}`); }
    catch (err) { toast(err.message); showNoTree(); return; }
    closeStream(); closeDrawer(); clearStage();
    S.tree = data.tree; S.selected = null; S.target = null;
    try { localStorage.setItem(LAST_TREE_KEY, id); } catch (_e) { /* 忽略 */ }
    $("bbCwd").textContent = "📁 " + (S.tree.cwd.split(/[\\/]/).filter(Boolean).pop() || S.tree.cwd);
    $("bbCwd").title = `工作目录：${S.tree.cwd}`;
    const st = S.tree.settings || {};
    $("bbModel").value = st.model || ""; $("bbEffort").value = st.effort || ""; $("bbPermission").value = st.permissionMode || "default";
    for (const tid of S.tree.order) renderTurn(S.tree.turns[tid]);
    updateEmpty(); setDefaultTarget();
    layoutNow(); fitAll();
    openStream(data.tree.seq || 0);
  }

  function updateEmpty() {
    const blank = S.tree && !S.tree.rootId;
    $("bbEmptyTitle").textContent = "从一个问题开始";
    $("bbEmptyText").textContent = "主线向右延伸；在任意回答里选中文字即可就此追问，分支有自己独立的上下文，不会冲散主线。";
    $("bbEmptyCreate").classList.add("hidden");
    els.empty.classList.toggle("hidden", !blank);
    els.composer.classList.toggle("empty-mode", !!blank);
    els.composer.classList.remove("hidden");
  }

  /* ---------- SSE ---------- */
  function openStream(after) {
    const tid = S.tree.id;
    const es = new EventSource(`${API}/trees/${tid}/events?after=${after}`);
    S.es = es;
    const onTurn = e => { if (S.tree && S.tree.id === tid) upsertTurn(JSON.parse(e.data)); };
    es.addEventListener("turn.created", onTurn);
    es.addEventListener("turn", onTurn);
    es.addEventListener("turn.summary", e => {
      const d = JSON.parse(e.data); const t = turn(d.turnId);
      if (t) { t.answer.summary = d.summary; renderTurn(t); }
    });
    es.addEventListener("tree", e => {
      const d = JSON.parse(e.data);
      Object.assign(S.tree, d);
      const opt = els.treeSelect.querySelector(`option[value="${tid}"]`);
      if (opt) opt.textContent = d.title;
    });
  }
  function closeStream() { if (S.es) { S.es.close(); S.es = null; } }

  function upsertTurn(t) {
    const isNew = !S.tree.turns[t.id];
    S.tree.turns[t.id] = t;
    if (!S.tree.order.includes(t.id)) S.tree.order.push(t.id);
    if (t.kind === "root") S.tree.rootId = t.id;
    renderTurn(t);
    if (isNew) { updateEmpty(); }
    if (S.target && S.target.parentId === t.id) renderTarget();
    if (S.drawerId === t.id) renderDrawer(false);
    else if (S.drawerId && t.parentId === S.drawerId && isNew) renderDrawer(false);
  }

  /* ---------- 卡片 ---------- */
  function clearStage() {
    S.els = {};
    els.stage.querySelectorAll(".bb-card").forEach(el => el.remove());
    els.wires.innerHTML = "";
    S.layout = null;
  }

  function renderTurn(t) {
    let pair = S.els[t.id];
    if (!pair) {
      const q = document.createElement("div");
      q.className = "bb-card bb-q"; q.dataset.id = t.id; q.style.width = LAYOUT.QW + "px";
      const a = document.createElement("div");
      a.className = "bb-card bb-a"; a.dataset.id = t.id; a.style.width = LAYOUT.AW + "px";
      els.stage.append(q, a);
      pair = S.els[t.id] = { q, a };
    }
    const n = num(t.id);
    const tag = t.kind === "highlight" ? '<span class="bb-tag hl">划词提问</span>'
      : t.kind === "follow" ? '<span class="bb-tag main">继续提问</span>' : '<span class="bb-tag main">起点</span>';
    const quote = t.quote ? `<div class="bb-quote"><b>划中的原文${t.quote.source === "summary" ? "（摘要）" : ""}</b>${esc(t.quote.text)}</div>` : "";
    pair.q.innerHTML = `${tag}<span class="bb-num">#${n}</span>${quote}<div class="bb-qtext">${esc(t.question)}</div>`;
    pair.a.innerHTML = answerCardHtml(t, n);
    const preview = pair.a.querySelector(".bb-preview");
    if (preview) preview.classList.toggle("clipped", preview.scrollHeight > preview.clientHeight + 2);
    applySelectionClasses();
    scheduleLayout();
  }

  function badgesHtml(a) {
    const b = [];
    if (a.filesChanged && a.filesChanged.length) b.push(`<span class="bb-badge files" title="${esc(a.filesChanged.join("\n"))}">改了 ${a.filesChanged.length} 个文件</span>`);
    if (a.ranCommands) b.push('<span class="bb-badge">运行了命令</span>');
    if (a.status === "error") b.push('<span class="bb-badge err">出错</span>');
    if (a.status === "interrupted") b.push('<span class="bb-badge int">已中断</span>');
    return b.length ? `<span class="bb-badges">${b.join("")}</span>` : "";
  }

  function permissionsHtml(t) {
    return (t.permissions || []).map(p => {
      const detail = p.input && (p.input.command || p.input.file_path || p.input.notebook_path || p.input.url || p.input.pattern);
      const body = detail ? String(detail) : JSON.stringify(p.input, null, 1);
      return `<div class="bb-perm" data-perm="${esc(p.requestId)}"><strong>Claude 想要使用 ${esc(p.tool)}</strong>${p.description && p.description !== detail ? ` · ${esc(p.description)}` : ""}
        <code>${esc(body.length > 1200 ? body.slice(0, 1200) + "…" : body)}</code>
        <div class="bb-perm-actions"><button class="bb-btn small primary" type="button" data-perm-allow="${esc(p.requestId)}">批准</button><button class="bb-btn small danger" type="button" data-perm-deny="${esc(p.requestId)}">拒绝</button></div></div>`;
    }).join("");
  }

  function answerCardHtml(t, n) {
    const a = t.answer;
    let body = "";
    if (a.summary && a.summary.length && a.status === "done") body = `<ul class="bb-summary">${a.summary.map(s => `<li>${esc(s)}</li>`).join("")}</ul>`;
    else if (a.markdown) body = `<div class="bb-preview bb-md">${core.markdownToHtml(core.previewMarkdown(a.markdown, 10))}</div>`;
    const status = running(t) ? `<div class="bb-statusline">${esc(a.statusLine || "等待 Claude Code…")}</div>` : "";
    const error = a.status === "error" ? `<div class="bb-error-text">${esc(a.error || "出错了")}</div>` : "";
    const actions = [];
    if (running(t)) actions.push(`<button class="bb-btn small" type="button" data-interrupt="${t.id}">中断</button>`);
    if ((a.status === "error" || a.status === "interrupted") && !hasChildren(t.id)) actions.push(`<button class="bb-btn small" type="button" data-retry="${t.id}">重试</button>`);
    if (ready(t)) actions.push(`<button class="bb-btn small" type="button" data-follow="${t.id}">继续提问</button>`);
    const more = a.markdown || a.toolEvents.length ? '<span class="bb-more">查看完整回答 →</span>' : "";
    return `<span class="bb-tag ans">回答 #${n}</span>${badgesHtml(a)}${status}${permissionsHtml(t)}${error}${body}
      <div class="bb-a-foot">${more}${actions.join("")}</div>`;
  }

  /* ---------- 布局 ---------- */
  function scheduleLayout() {
    if (S.layoutTimer) return;
    S.layoutTimer = setTimeout(() => requestAnimationFrame(() => { S.layoutTimer = 0; layoutNow(); }), 100);
  }

  function layoutNow() {
    if (!S.tree) return;
    const sizes = {};
    for (const id of S.tree.order) {
      const pair = S.els[id];
      if (pair) sizes[id] = { qh: pair.q.offsetHeight, ah: pair.a.offsetHeight };
    }
    const layout = S.layout = core.layoutTree(S.tree, sizes);
    for (const g of Object.values(layout.cards)) {
      const pair = S.els[g.id];
      if (!pair) continue;
      pair.q.style.left = g.qx + "px"; pair.q.style.top = g.y + "px";
      pair.a.style.left = g.ax + "px"; pair.a.style.top = g.y + "px";
    }
    els.stage.style.width = layout.width + "px"; els.stage.style.height = layout.height + "px";
    els.wires.setAttribute("width", layout.width); els.wires.setAttribute("height", layout.height);
    drawWires();
  }

  function drawWires() {
    const path = new Set(S.selected ? core.ancestors(S.tree, S.selected) : []);
    const dim = id => (S.selected && !path.has(id) ? " dim" : "");
    let svg = "";
    for (const w of core.layoutWires(S.tree, S.layout)) {
      const d = dim(w.id);
      if (w.type === "qa") {
        svg += `<path class="qa${d}" d="M${w.x1},${w.y1} H${w.x2}" stroke-width="2"/><path class="qa${d}" d="M${w.x2 - 7},${w.y2 - 4} L${w.x2},${w.y2} L${w.x2 - 7},${w.y2 + 4}" fill="none" stroke-width="2"/>`;
      } else if (w.type === "highlight") {
        svg += `<path class="highlight${d}" d="M${w.x1},${w.y1} V${w.y2}" stroke-width="2"/><circle class="highlight-dot${d}" cx="${w.x1}" cy="${w.y1}" r="4"/><circle class="highlight-dot${d}" cx="${w.x2}" cy="${w.y2}" r="4"/>`;
      } else {
        const mx = (w.x1 + w.x2) / 2;
        const p = w.type === "follow" ? `M${w.x1},${w.y1} H${w.x2}` : `M${w.x1},${w.y1} H${mx} V${w.y2} H${w.x2}`;
        svg += `<path class="${w.type}${d}" d="${p}" stroke-width="2"/><circle class="follow-dot${d}" cx="${w.x1}" cy="${w.y1}" r="4"/><circle class="follow-dot${d}" cx="${w.x2}" cy="${w.y2}" r="4"/>`;
      }
    }
    els.wires.innerHTML = svg;
  }

  /* ---------- 平移 / 缩放 ---------- */
  function applyTransform() {
    els.stage.style.transform = `translate(${S.panX}px, ${S.panY}px) scale(${S.zoom})`;
    els.viewport.style.backgroundPosition = `${S.panX}px ${S.panY}px`;
    els.viewport.style.backgroundSize = `${22 * S.zoom}px ${22 * S.zoom}px`;
    $("bbZoomReset").textContent = Math.round(S.zoom * 100) + "%";
  }
  function zoomAt(factor, cx, cy) {
    const rect = els.viewport.getBoundingClientRect();
    const x = cx ?? rect.width / 2, y = cy ?? rect.height / 2;
    const z = Math.min(2, Math.max(0.15, S.zoom * factor));
    S.panX = x - (x - S.panX) * (z / S.zoom); S.panY = y - (y - S.panY) * (z / S.zoom);
    S.zoom = z; applyTransform();
  }
  function fitAll() {
    if (!S.layout || !S.tree.order.length) { S.zoom = 1; S.panX = 40; S.panY = 40; applyTransform(); return; }
    const rect = els.viewport.getBoundingClientRect();
    const bottom = els.composer.classList.contains("hidden") ? 0 : 110;
    const z = Math.min(1, Math.max(0.15, Math.min(rect.width / S.layout.width, (rect.height - bottom) / S.layout.height)));
    S.zoom = z;
    S.panX = (rect.width - S.layout.width * z) / 2;
    S.panY = Math.max(0, (rect.height - bottom - S.layout.height * z) / 2);
    applyTransform();
  }
  function revealTurn(id) {
    layoutNow();
    const g = S.layout && S.layout.cards[id];
    if (!g) return;
    const rect = els.viewport.getBoundingClientRect();
    const x1 = S.panX + g.qx * S.zoom, x2 = S.panX + (g.ax + LAYOUT.AW) * S.zoom;
    const y1 = S.panY + g.y * S.zoom, y2 = y1 + Math.max(g.qh, g.ah, 120) * S.zoom;
    const margin = 40, drawerW = els.drawer.classList.contains("open") ? els.drawer.offsetWidth : 0;
    if (x2 > rect.width - drawerW - margin) S.panX -= x2 - (rect.width - drawerW - margin);
    if (x1 < margin) S.panX += margin - x1;
    if (y2 > rect.height - 130) S.panY -= Math.min(y2 - (rect.height - 130), y1 - margin);
    else if (y1 < margin) S.panY += margin - y1;
    applyTransform();
  }

  let pan = null;
  els.viewport.addEventListener("pointerdown", e => {
    if (e.button !== 0 || e.target.closest(".bb-card, .bb-composer, .bb-drawer, .bb-ask-selection")) return;
    pan = { x: e.clientX, y: e.clientY, px: S.panX, py: S.panY, moved: false };
    els.viewport.setPointerCapture(e.pointerId);
    els.viewport.classList.add("panning");
  });
  els.viewport.addEventListener("pointermove", e => {
    if (!pan) return;
    const dx = e.clientX - pan.x, dy = e.clientY - pan.y;
    if (Math.abs(dx) + Math.abs(dy) > 3) pan.moved = true;
    S.panX = pan.px + dx; S.panY = pan.py + dy; applyTransform();
  });
  els.viewport.addEventListener("pointerup", () => {
    if (!pan) return;
    if (!pan.moved) { select(null); setDefaultTarget(); }
    pan = null; els.viewport.classList.remove("panning");
  });
  els.viewport.addEventListener("wheel", e => {
    if (e.target.closest(".bb-drawer, .bb-composer")) return;
    e.preventDefault();
    if (e.ctrlKey || e.metaKey) {
      const rect = els.viewport.getBoundingClientRect();
      zoomAt(Math.exp(-e.deltaY * 0.0022), e.clientX - rect.left, e.clientY - rect.top);
    } else {
      S.panX -= e.shiftKey && !e.deltaX ? e.deltaY : e.deltaX; S.panY -= e.shiftKey && !e.deltaX ? 0 : e.deltaY; applyTransform();
    }
  }, { passive: false });

  /* ---------- 选中与路径高亮 ---------- */
  function select(id) {
    S.selected = id;
    applySelectionClasses();
    if (S.layout) drawWires();
  }
  function applySelectionClasses() {
    const path = new Set(S.selected && S.tree ? core.ancestors(S.tree, S.selected) : []);
    els.stage.classList.toggle("has-selection", !!S.selected);
    for (const [id, pair] of Object.entries(S.els)) {
      for (const el of [pair.q, pair.a]) {
        el.classList.toggle("on-path", path.has(id));
        el.classList.toggle("selected", id === S.selected);
        el.classList.toggle("target", !!S.target && S.target.parentId === id && el === pair.a);
      }
    }
  }

  /* ---------- 输入目标 ---------- */
  function setDefaultTarget() {
    if (!S.tree) return;
    if (!S.tree.rootId) S.target = { parentId: null, kind: "root" };
    else S.target = { parentId: core.mainLeaf(S.tree), kind: "follow" };
    renderTarget();
  }
  function setTarget(target, focus = true) {
    S.target = target; renderTarget();
    if (focus) els.question.focus();
  }
  function renderTarget() {
    const t = S.target;
    if (!t || !S.tree) return;
    const label = els.targetLabel;
    label.classList.toggle("hl", t.kind === "highlight");
    els.targetQuote.classList.toggle("hidden", t.kind !== "highlight");
    let blocked = "";
    if (t.kind === "root") {
      label.textContent = "起点"; els.question.placeholder = "输入第一个问题，Ctrl+Enter 发送";
    } else {
      const p = turn(t.parentId);
      const n = num(t.parentId);
      if (t.kind === "highlight") {
        label.textContent = `划词提问 · 回答 #${n}`;
        els.targetQuote.textContent = t.quote.text.replace(/\s+/g, " ");
        els.targetQuote.title = t.quote.text;
      } else {
        const forked = S.tree.order.some(id => S.tree.turns[id].parentId === t.parentId && S.tree.turns[id].kind === "follow");
        label.textContent = forked ? `从回答 #${n} 另开一行继续` : `继续提问 · 接回答 #${n}`;
      }
      if (!ready(p)) blocked = running(p) ? `等待回答 #${n} 完成…` : `回答 #${n} 没有可继续的会话`;
      els.question.placeholder = blocked || "输入问题，Ctrl+Enter 发送";
    }
    const isMain = t.kind === "follow" && t.parentId === core.mainLeaf(S.tree);
    els.targetClear.classList.toggle("hidden", t.kind === "root" || isMain);
    els.send.disabled = !!blocked;
    applySelectionClasses();
  }

  async function send() {
    const question = els.question.value.trim();
    const t = S.target;
    if (!question || !t || els.send.disabled) return;
    els.send.disabled = true;
    try {
      const payload = { kind: t.kind, parentId: t.parentId, question };
      if (t.kind === "highlight") payload.quote = t.quote;
      const { turn: created } = await api("POST", `/trees/${S.tree.id}/turns`, payload);
      els.question.value = ""; autosize();
      upsertTurn(created);
      select(created.id);
      setTarget({ parentId: created.id, kind: "follow" }, false);
      revealTurn(created.id);
    } catch (err) {
      toast(err.message);
    } finally {
      renderTarget();
    }
  }
  function autosize() { els.question.style.height = "auto"; els.question.style.height = Math.min(200, els.question.scrollHeight + 2) + "px"; }

  els.composer.addEventListener("submit", e => { e.preventDefault(); send(); });
  els.question.addEventListener("input", autosize);
  els.question.addEventListener("keydown", e => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); send(); } });
  els.targetClear.addEventListener("click", () => { setDefaultTarget(); els.question.focus(); });

  /* ---------- 卡片点击与按钮 ---------- */
  els.stage.addEventListener("click", async e => {
    const btn = e.target.closest("button");
    if (btn) { await handleAction(btn); return; }
    const card = e.target.closest(".bb-card");
    if (!card) return;
    const sel = window.getSelection();
    if (sel && !sel.isCollapsed && card.contains(sel.anchorNode)) return;  // 正在划词
    const id = card.dataset.id;
    select(id);
    if (ready(turn(id))) setTarget({ parentId: id, kind: "follow" }, false);
    openDrawer(id);
  });
  els.drawer.addEventListener("click", async e => {
    const btn = e.target.closest("button[data-perm-allow], button[data-perm-deny]");
    if (btn) { await handleAction(btn, S.drawerId); return; }
    const quote = e.target.closest(".bb-qbox .bb-quote");
    if (quote && S.drawerId) { const t = turn(S.drawerId); if (t && t.parentId) openDrawer(t.parentId, t.id); return; }
    const mark = e.target.closest("mark.bb-quote-mark");
    if (mark) { const id = mark.dataset.child; select(id); revealTurn(id); }
  });

  async function handleAction(btn, drawerTurn) {
    const card = btn.closest(".bb-card");
    const tid = (card && card.dataset.id) || drawerTurn;
    try {
      if (btn.dataset.follow) { select(btn.dataset.follow); setTarget({ parentId: btn.dataset.follow, kind: "follow" }); }
      else if (btn.dataset.interrupt) { await api("POST", `/trees/${S.tree.id}/turns/${btn.dataset.interrupt}/interrupt`); toast("已请求中断"); }
      else if (btn.dataset.retry) { upsertTurn((await api("POST", `/trees/${S.tree.id}/turns/${btn.dataset.retry}/retry`)).turn); }
      else if (btn.dataset.permAllow || btn.dataset.permDeny) {
        const allow = !!btn.dataset.permAllow;
        btn.closest(".bb-perm").querySelectorAll("button").forEach(b => { b.disabled = true; });
        await api("POST", `/trees/${S.tree.id}/turns/${tid}/permissions/${encodeURIComponent(btn.dataset.permAllow || btn.dataset.permDeny)}`, { allow });
      }
    } catch (err) { toast(err.message); }
  }

  /* ---------- 划词提问 ---------- */
  function selectionInfo() {
    const sel = window.getSelection();
    if (!sel || sel.isCollapsed || !sel.rangeCount) return null;
    const text = sel.toString().trim();
    if (!text) return null;
    const range = sel.getRangeAt(0);
    const node = range.commonAncestorContainer.nodeType === 1 ? range.commonAncestorContainer : range.commonAncestorContainer.parentElement;
    let turnId = null, source = "full";
    const card = node.closest(".bb-a");
    if (card && els.stage.contains(card)) {
      turnId = card.dataset.id;
      if (node.closest(".bb-summary")) source = "summary";
      else if (!node.closest(".bb-preview")) return null;
    } else if (node.closest(".bb-answer-full") && S.drawerId) {
      turnId = S.drawerId;
    }
    if (!turnId) return null;
    return { turnId, text: text.slice(0, 4000), source, rect: range.getBoundingClientRect() };
  }

  document.addEventListener("mouseup", () => setTimeout(() => {
    const info = selectionInfo();
    S.pendingSel = info;
    if (!info) { els.ask.classList.add("hidden"); return; }
    els.ask.style.left = Math.max(8, Math.min(window.innerWidth - 110, info.rect.left + info.rect.width / 2 - 45)) + "px";
    els.ask.style.top = Math.max(8, info.rect.top - 38) + "px";
    els.ask.classList.remove("hidden");
  }, 0));
  document.addEventListener("selectionchange", () => {
    const sel = window.getSelection();
    if (!sel || sel.isCollapsed) els.ask.classList.add("hidden");
  });
  els.ask.addEventListener("mousedown", e => e.preventDefault());  // 保留选区
  els.ask.addEventListener("click", () => {
    const info = S.pendingSel;
    els.ask.classList.add("hidden");
    if (!info) return;
    const parent = turn(info.turnId);
    if (!ready(parent)) { toast("这条回答还没完成，完成后再划词提问。"); return; }
    const quote = { text: info.text, source: info.source };
    if (info.source === "full") {
      const hit = core.locateQuote(parent.answer.markdown, info.text);
      if (hit) { quote.start = hit.start; quote.end = hit.end; }
    }
    window.getSelection().removeAllRanges();
    select(info.turnId);
    setTarget({ parentId: info.turnId, kind: "highlight", quote });
  });

  /* ---------- 抽屉 ---------- */
  function openDrawer(id, focusChild) {
    S.drawerId = id;
    els.drawer.classList.add("open"); els.drawer.setAttribute("aria-hidden", "false");
    renderDrawer(true, focusChild);
  }
  function closeDrawer() {
    S.drawerId = null;
    els.drawer.classList.remove("open"); els.drawer.setAttribute("aria-hidden", "true");
  }
  $("bbDrawerClose").addEventListener("click", closeDrawer);
  $("bbDrawerFollow").addEventListener("click", () => { if (S.drawerId) setTarget({ parentId: S.drawerId, kind: "follow" }); });
  $("bbDrawerInterrupt").addEventListener("click", () => S.drawerId && handleAction({ dataset: { interrupt: S.drawerId }, closest: () => null }));
  $("bbDrawerRetry").addEventListener("click", () => S.drawerId && handleAction({ dataset: { retry: S.drawerId }, closest: () => null }));

  function renderDrawer(fresh, focusChild) {
    const t = turn(S.drawerId);
    if (!t) return;
    const body = els.drawerBody;
    const sel = window.getSelection();
    if (!fresh && sel && !sel.isCollapsed && body.contains(sel.anchorNode)) return;  // 不打断正在进行的划词
    const atBottom = body.scrollTop + body.clientHeight >= body.scrollHeight - 30;
    const scroll = body.scrollTop;
    const a = t.answer, n = num(t.id);
    $("bbDrawerEyebrow").textContent = `第 ${n} 轮 · ${core.KIND_LABEL[t.kind]}${t.parentId ? ` · 接回答 #${num(t.parentId)}` : ""}`;
    $("bbDrawerTitle").textContent = t.question.split("\n")[0].slice(0, 80);
    $("bbDrawerFollow").disabled = !ready(t);
    $("bbDrawerInterrupt").classList.toggle("hidden", !running(t));
    $("bbDrawerRetry").classList.toggle("hidden", !((a.status === "error" || a.status === "interrupted") && !hasChildren(t.id)));
    const quote = t.quote ? `<div class="bb-quote" title="在原回答中定位"><b>划中的原文 · 点击在回答 #${num(t.parentId)} 中定位</b>${esc(t.quote.text)}</div>` : "";
    const files = a.filesChanged.length ? `<p class="bb-section-title">修改的文件（分支不隔离文件，主线也能看到这些改动）</p><ul class="bb-files">${a.filesChanged.map(f => `<li><code>${esc(f)}</code></li>`).join("")}</ul>` : "";
    const tools = a.toolEvents.length ? `<p class="bb-section-title">工具调用（${a.toolEvents.length}）</p><div class="bb-tools">${a.toolEvents.map(ev => {
      const input = typeof ev.input === "string" ? ev.input : JSON.stringify(ev.input, null, 2);
      const flag = ev.approved === true ? ' <span class="ok">已批准</span>' : ev.approved === false ? ' <span class="no">已拒绝</span>' : "";
      const err = ev.isError ? ' <span class="no">失败</span>' : "";
      const hint = (ev.input && (ev.input.file_path || ev.input.command || ev.input.pattern || ev.input.url || ev.input.description)) || "";
      return `<details><summary><b>${esc(ev.name)}</b> ${esc(String(hint).slice(0, 120))}${flag}${err}</summary><pre>${esc(input.slice(0, 4000))}${ev.outputPreview ? "\n\n── 结果 ──\n" + esc(ev.outputPreview) : ""}</pre></details>`;
    }).join("")}</div>` : "";
    const status = running(t) ? `<div class="bb-statusline">${esc(a.statusLine || "等待 Claude Code…")}</div>` : "";
    const error = a.status === "error" ? `<p class="bb-error-text">${esc(a.error)}</p>` : "";
    body.innerHTML = `<div class="bb-qbox">${quote}<b>提问：</b>${esc(t.question)}</div>${files}${tools}${status}${permissionsHtml(t)}${error}
      <div class="bb-answer-full bb-md">${core.markdownToHtml(a.markdown) || (running(t) ? "" : "<p><i>（没有文字回答）</i></p>")}</div>`;
    // 回显：本回答里被划词追问过的原文
    const container = body.querySelector(".bb-answer-full");
    let focusMark = null;
    for (const cid of S.tree.order) {
      const c = S.tree.turns[cid];
      if (c.parentId !== t.id || c.kind !== "highlight" || !c.quote || c.quote.source === "summary") continue;
      const mark = markText(container, c.quote.text, cid);
      if (cid === focusChild) focusMark = mark;
    }
    if (focusMark) focusMark.scrollIntoView({ block: "center" });
    else if (fresh) body.scrollTop = 0;
    else body.scrollTop = atBottom && running(t) ? body.scrollHeight : scroll;
  }

  /** 在渲染后的 DOM 中找到一段文字（可跨元素），用 <mark> 包起来。返回第一个 mark。 */
  function markText(container, text, childId) {
    const norm = s => s.replace(/\s+/g, "");
    const target = norm(text);
    if (!target) return null;
    const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT);
    const nodes = []; let flat = ""; const map = [];
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      nodes.push(node);
      for (let i = 0; i < node.data.length; i++) {
        if (/\s/.test(node.data[i])) continue;
        flat += node.data[i]; map.push([nodes.length - 1, i]);
      }
    }
    const at = flat.indexOf(target);
    if (at < 0) return null;
    const [sn, so] = map[at], [en, eo] = map[at + target.length - 1];
    let first = null;
    for (let k = en; k >= sn; k--) {
      const node = nodes[k];
      const start = k === sn ? so : 0, end = k === en ? eo + 1 : node.data.length;
      if (start >= end) continue;
      const range = document.createRange();
      range.setStart(node, start); range.setEnd(node, end);
      const mark = document.createElement("mark");
      mark.className = "bb-quote-mark"; mark.dataset.child = childId; mark.title = `由此划词提问：#${num(childId)}（点击定位）`;
      try { range.surroundContents(mark); first = mark; } catch (_e) { /* 跨越元素边界的片段跳过 */ }
    }
    return first;
  }

  /* ---------- 新建 / 设置 / 菜单 / 导出 ---------- */
  function openTreeDialog() {
    $("bbTreeTitle").value = ""; $("bbTreeError").classList.add("hidden");
    const last = S.tree ? S.tree.cwd : "";
    $("bbTreeCwd").value = last;
    $("bbTreeModel").value = ""; $("bbTreeEffort").value = ""; $("bbTreePermission").value = "default";
    $("bbTreeDialog").showModal();
  }
  $("bbNewTree").addEventListener("click", openTreeDialog);
  $("bbEmptyCreate").addEventListener("click", openTreeDialog);
  $("bbBrowseCwd").addEventListener("click", async () => {
    try {
      const result = await window.LocalPaths.pick({ kind: "folder", title: "选择对话树的工作目录" });
      if (result && result.path) $("bbTreeCwd").value = result.path;
    } catch (err) { toast(err.message); }
  });
  $("bbTreeForm").addEventListener("submit", async e => {
    if (e.submitter && e.submitter.value === "cancel") return;
    e.preventDefault();
    try {
      const { tree } = await api("POST", "/trees", {
        title: $("bbTreeTitle").value, cwd: $("bbTreeCwd").value,
        settings: { model: $("bbTreeModel").value, effort: $("bbTreeEffort").value, permissionMode: $("bbTreePermission").value },
      });
      $("bbTreeDialog").close();
      await loadTrees(tree.id);
      els.question.focus();
    } catch (err) {
      $("bbTreeError").textContent = err.message; $("bbTreeError").classList.remove("hidden");
    }
  });
  els.treeSelect.addEventListener("change", () => els.treeSelect.value && openTree(els.treeSelect.value));
  for (const [id, key] of [["bbModel", "model"], ["bbEffort", "effort"], ["bbPermission", "permissionMode"]]) {
    $(id).addEventListener("change", async () => {
      if (!S.tree) return;
      try { S.tree.settings = (await api("PATCH", `/trees/${S.tree.id}`, { settings: { [key]: $(id).value } })).tree.settings; toast("设置已保存，对之后的提问生效"); }
      catch (err) { toast(err.message); }
    });
  }
  $("bbMore").addEventListener("click", e => { e.stopPropagation(); $("bbMoreMenu").classList.toggle("hidden"); });
  document.addEventListener("click", e => {
    $("bbMoreMenu").classList.add("hidden");
    if (!e.target.closest("#bbSettings")) $("bbSettings").classList.remove("pop");
  });
  $("bbMoreMenu").addEventListener("click", async e => {
    const act = e.target.closest("button") && e.target.closest("button").dataset.act;
    if (!act || !S.tree) return;
    try {
      if (act === "export-md") $("bbExportMd").click();
      else if (act === "export-html") $("bbExportHtml").click();
      else if (act === "settings") { e.stopPropagation(); $("bbMoreMenu").classList.add("hidden"); $("bbSettings").classList.add("pop"); }
      else if (act === "rename") {
        const title = prompt("对话树标题", S.tree.title);
        if (title && title.trim()) { await api("PATCH", `/trees/${S.tree.id}`, { title }); await loadTrees(S.tree.id); }
      } else if (act === "forget" || act === "delete") {
        const msg = act === "delete" ? `删除「${S.tree.title}」？对话树文件会被删除，无法恢复（Claude Code 自己的会话记录不受影响）。` : `从列表中移除「${S.tree.title}」？JSON 文件保留在工作目录的 .branchboard/ 里。`;
        if (!confirm(msg)) return;
        await api("DELETE", `/trees/${S.tree.id}${act === "forget" ? "?keepFile=1" : ""}`);
        try { localStorage.removeItem(LAST_TREE_KEY); } catch (_e) { /* 忽略 */ }
        await loadTrees();
      }
    } catch (err) { toast(err.message); }
  });
  function download(name, text, type) {
    const url = URL.createObjectURL(new Blob([text], { type }));
    const a = document.createElement("a");
    a.href = url; a.download = name; document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 2000);
  }
  const fileStem = () => (S.tree.title || "对话树").replace(/[\\/:*?"<>|\s]+/g, "_").slice(0, 60);
  $("bbExportMd").addEventListener("click", () => S.tree && download(`${fileStem()}.md`, core.treeToMarkdown(S.tree), "text/markdown;charset=utf-8"));
  $("bbExportHtml").addEventListener("click", () => S.tree && download(`${fileStem()}.html`, core.treeToStaticHtml(S.tree), "text/html;charset=utf-8"));
  $("bbFit").addEventListener("click", fitAll);
  $("bbZoomIn").addEventListener("click", () => zoomAt(1.15));
  $("bbZoomOut").addEventListener("click", () => zoomAt(1 / 1.15));
  $("bbZoomReset").addEventListener("click", () => { S.zoom = 1; applyTransform(); });

  document.addEventListener("keydown", e => {
    const typing = e.target.closest && e.target.closest("input, textarea, select, [contenteditable]");
    if (e.key === "Escape") {
      if (els.drawer.classList.contains("open")) closeDrawer();
      else if (S.target && S.target.kind === "highlight") setDefaultTarget();
      else select(null);
    } else if (!typing && !e.ctrlKey && !e.metaKey && !e.altKey) {
      if (e.key === "f") fitAll();
      else if (e.key === "/" || e.key === "Enter") { e.preventDefault(); els.question.focus(); }
    }
  });
  window.addEventListener("resize", scheduleLayout);

  /* ---------- 启动 ---------- */
  applyTransform();
  (async () => {
    try { await loadConfig(); } catch (err) { showStatus(err.message); }
    try { await loadTrees(new URLSearchParams(location.search).get("tree")); } catch (err) { toast(err.message); showNoTree(); }
  })();
})();
