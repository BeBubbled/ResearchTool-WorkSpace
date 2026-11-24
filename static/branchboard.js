/* 对话树（Branchboard）页面：无限画布、卡片、问题小框草稿、划词提问、抽屉、权限批准与 SSE 实时更新。

   外壳像聊天应用：左栏是聊天列表，右边是当前聊天的对话树。「新聊天」不用先建树——
   直接问第一个问题就建好了；标题由后端自动起（首问命名，第三问更新），手动改名后不再变。

   交互模型：
   - 大框 = 一轮的回答卡片，顶部是一个或多个问题小框。
   - 点「继续提问」→ 在那里出现一个可输入的问题小框（普通追问）；双击大框 → 视图定位到这张卡片（问题在顶部）。
   - 在大框的回答或问题里划词 →「就此提问」→ 出现一个带引用的问题小框。
   - 小框可以在多张卡片里攒多个，任意一个小框里按 Enter 一次问出：
     生成一张新卡片，里面每个问题小框各自被来源卡片的有向箭头指着。 */
(function () {
  "use strict";
  const core = window.BranchboardCore;
  const { LAYOUT, escapeHtml: esc } = core;
  const $ = id => document.getElementById(id);
  const API = "/api/branchboard";
  const LAST_TREE_KEY = "branchboard.lastTree";
  const CWD_KEY = "branchboard.cwd";
  const SIDE_KEY = "branchboard.sideCollapsed";
  const NEW_TITLE = "新聊天";
  const stored = key => { try { return localStorage.getItem(key); } catch (_e) { return null; } };
  const store = (key, value) => { try { if (value == null) localStorage.removeItem(key); else localStorage.setItem(key, value); } catch (_e) { /* 无痕模式 */ } };
  const MODE_KEY = "branchboard.mode";
  const DRAFT_W = 270;
  const storedMode = (() => { try { return localStorage.getItem(MODE_KEY); } catch (_e) { return null; } })();

  const S = {
    config: null, trees: [], tree: null, els: {}, layout: null,
    selected: null, drawerId: null, pendingSel: null,
    zoom: 1, panX: 40, panY: 40, es: null, layoutTimer: 0,
    // 显示模式：question 仅问题 / partial 问题 + 部分回答 / full 问题 + 完全展开；cardMode 是单张卡片的临时覆盖
    mode: core.MODES.includes(storedMode) ? storedMode : "partial", cardMode: {},
    // 待发送的问题小框：{key, sourceId, quote|null, text, dx, dy}；dx/dy 相对来源卡片左上角
    drafts: [], draftSeq: 0, sending: false,
    // 子树合并：expandedMerge[id] 展开 id 收起的卡片；showOriginal[id] 让合并过的卡片暂时显示原版
    expandedMerge: {}, showOriginal: {}, view: null,
    // 思考块 / 过程汇总的展开状态（用户点过才有）；详情抽屉里展开的工具调用
    thinkOpen: {}, drawerOpen: new Set(),
    // 新聊天要用的工作目录；空串 = 后端的默认目录
    newCwd: stored(CWD_KEY) || "", filter: "", menuTree: null,
  };
  const modeOf = id => S.cardMode[id] || S.mode;
  /** 被合并收起的卡片 → 收起它的那张卡片。 */
  function ownerMap() {
    const m = new Map();
    if (!S.tree) return m;
    for (const id of S.tree.order) for (const x of ((S.tree.turns[id] || {}).merged || {}).absorbed || []) if (!m.has(x)) m.set(x, id);
    return m;
  }
  /** 当前隐藏的卡片：合并它的卡片没有展开时。 */
  function hiddenSet() {
    const out = new Set();
    for (const [x, owner] of ownerMap()) if (!S.expandedMerge[owner]) out.add(x);
    return out;
  }
  const showingMerged = t => !!(t && t.merged && t.merged.markdown && !S.showOriginal[t.id]);
  /** 脚注渲染成上标（合并版、完整版预览共用）。 */
  const footnoteHtml = html => html.replace(/\[\^(\d+)\]:\s?/g, '<sup class="bb-fn-def">$1</sup> ').replace(/\[\^(\d+)\]/g, '<sup class="bb-fn">[$1]</sup>');
  function rerenderAll() { if (!S.tree) return; for (const id of S.tree.order) renderTurn(S.tree.turns[id]); renderDrafts(); layoutNow(); }

  const els = {
    viewport: $("bbViewport"), stage: $("bbStage"), wires: $("bbWires"), edge: $("bbEdge"), drafts: $("bbDrafts"), list: $("bbTreeList"),
    composer: $("bbComposer"), question: $("bbQuestion"), send: $("bbSend"), ask: $("bbAskSelection"),
    draftBar: $("bbDraftBar"), hint: $("bbHint"),
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
    clearTimeout(toastTimer); toastTimer = setTimeout(() => el.classList.add("hidden"), 3600);
  }
  const num = id => core.turnNumber(S.tree, id);
  const turn = id => S.tree && S.tree.turns[id];
  const finished = t => t && (t.answer.status === "done" || t.answer.status === "interrupted");
  const running = t => t && (t.answer.status === "pending" || t.answer.status === "streaming");
  const hasChildren = id => S.tree.order.some(cid => core.sourcesOf(S.tree.turns[cid]).includes(id));
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
    fillSelect($("bbModel"), models);
    fillSelect($("bbEffort"), efforts);
    fillSelect($("bbPermission"), modes);
    const claude = S.config.claude;
    if (claude && claude.error) showStatus(claude.error);
  }
  function showStatus(text) { els.status.textContent = text; els.status.classList.toggle("hidden", !text); }

  /* ---------- 左栏：聊天列表 ---------- */
  const baseName = path => String(path || "").split(/[\\/]/).filter(Boolean).pop() || String(path || "");
  const DAY = 86400000;
  function dateGroup(iso) {
    const t = Date.parse(iso || "");
    if (!t) return "更早";
    const today = new Date(); today.setHours(0, 0, 0, 0);
    const days = Math.floor((today.getTime() - t) / DAY) + 1;  // 0 = 今天
    return days <= 0 ? "今天" : days === 1 ? "昨天" : days <= 7 ? "过去 7 天" : days <= 30 ? "过去 30 天" : "更早";
  }
  function renderList() {
    const needle = S.filter.trim().toLowerCase();
    const items = needle ? S.trees.filter(t => (t.title || "").toLowerCase().includes(needle)) : S.trees;
    let last = null;
    els.list.innerHTML = items.map(t => {
      const group = dateGroup(t.updatedAt);
      const head = group !== last ? `<p class="bb-side-group">${group}</p>` : "";
      last = group;
      const active = S.tree && S.tree.id === t.id;
      return `${head}<div class="bb-side-item${active ? " active" : ""}${t.missing ? " missing" : ""}" data-id="${t.id}">
        <button class="bb-side-open" type="button"${active ? ' aria-current="page"' : ""} title="${esc(t.title)}${t.missing ? "（文件缺失）" : t.cwdMissing ? "（原目录在另一台电脑上，可以浏览）" : ""}\n📁 ${esc(t.cwd || "")}">${esc(t.title || NEW_TITLE)}</button>
        <button class="bb-side-more" type="button" aria-haspopup="menu" aria-label="更多" title="更多">⋯</button></div>`;
    }).join("") || `<p class="bb-side-empty">${needle ? "没有匹配的聊天。" : "还没有聊天。直接在右边问第一个问题就开始了。"}</p>`;
  }
  async function refreshList() {
    try { S.trees = (await api("GET", "/trees")).trees; } catch (_e) { return; }
    renderList();
  }
  /** 标题变了（自动命名 / 改名）：顶栏和左栏一起更新。 */
  function showTitle(animate) {
    const el = $("bbTitle");
    const title = S.tree ? S.tree.title : NEW_TITLE;
    const changed = el.textContent !== title;
    el.textContent = title;
    el.title = S.tree ? `${title}\n点击重命名` : "";
    if (animate && changed) { el.classList.remove("fresh"); void el.offsetWidth; el.classList.add("fresh"); }
    if (S.tree) { const meta = S.trees.find(t => t.id === S.tree.id); if (meta) meta.title = title; }
    renderList();
  }

  async function loadTrees(selectId) {
    const { trees } = await api("GET", "/trees");
    S.trees = trees;
    let id = selectId || stored(LAST_TREE_KEY);
    if (!trees.some(t => t.id === id)) id = null;
    if (id) await openTree(id);
    else newChat();
  }

  /** 新聊天：还没有树，问出第一个问题时才创建。 */
  function newChat() {
    closeStream(); closeDrawer();
    S.tree = null; S.selected = null; clearStage(); clearDrafts();
    store(LAST_TREE_KEY, null);
    $("bbCwd").textContent = "";
    showTitle(false);
    updateEmpty();
    S.zoom = 1; S.panX = 40; S.panY = 40; applyTransform();
    els.question.focus();
  }

  function showCwdPick() {
    // 已建好的树，工作目录不能再换；除非原目录不在这台电脑上（对话树从另一台电脑同步过来）
    const relocate = !!(S.tree && S.tree.cwdMissing);
    const fixed = !!S.tree && !relocate;
    const path = S.tree ? S.tree.cwd : S.newCwd;
    const isDefault = !path || (S.config && path === S.config.defaultCwd);
    $("bbCwdPickName").textContent = relocate ? "原目录不在本机" : isDefault ? "默认目录" : baseName(path);
    $("bbCwdPick").disabled = fixed;
    $("bbCwdPick").classList.toggle("missing", relocate);
    $("bbCwdPick").title = relocate
      ? `原工作目录在另一台电脑上：${path}\n点击选一个本机目录后才能继续提问（此前的对话会作为记录带上）`
      : `工作目录：${path || (S.config && S.config.defaultCwd) || "默认目录"}${fixed ? "" : "\n点击换一个目录"}`;
    $("bbCwdReset").classList.toggle("hidden", fixed || relocate || isDefault);
  }
  $("bbCwdPick").addEventListener("click", async () => {
    try {
      const relocate = !!(S.tree && S.tree.cwdMissing);
      const result = await window.LocalPaths.pick({ kind: "folder", title: relocate ? "为这个对话树选一个本机工作目录" : "选择这个聊天的工作目录" });
      if (!result || !result.path) return;
      if (relocate) {
        await api("PATCH", `/trees/${S.tree.id}`, { cwd: result.path });
        await openTree(S.tree.id);
        refreshList();
        toast("已换到本机目录，可以继续提问。");
        return;
      }
      S.newCwd = result.path; store(CWD_KEY, S.newCwd); showCwdPick();
    } catch (err) { toast(err.message); }
  });
  $("bbCwdReset").addEventListener("click", () => { S.newCwd = ""; store(CWD_KEY, null); showCwdPick(); });

  async function openTree(id) {
    let data;
    try { data = await api("GET", `/trees/${id}`); }
    catch (err) { toast(err.message); newChat(); return; }
    closeStream(); closeDrawer(); clearStage(); clearDrafts();
    S.tree = data.tree; S.selected = null;
    store(LAST_TREE_KEY, id);
    $("bbCwd").textContent = "📁 " + baseName(S.tree.cwd);
    $("bbCwd").title = `工作目录：${S.tree.cwd}`;
    showTitle(false);
    const st = S.tree.settings || {};
    $("bbModel").value = st.model || ""; $("bbEffort").value = st.effort || ""; $("bbPermission").value = st.permissionMode || "default";
    for (const tid of S.tree.order) renderTurn(S.tree.turns[tid]);
    updateEmpty();
    layoutNow(); fitAll();
    openStream(data.tree.seq || 0);
  }

  /** 空树：底部输入框问起点；有起点之后：「继续提问」/ 划词出小框。 */
  function updateEmpty() {
    const blank = !S.tree || !S.tree.rootId;
    $("bbEmptyTitle").textContent = "从一个问题开始";
    $("bbEmptyText").textContent = "问出第一个问题，就开始了一棵对话树。之后点「继续提问」追问，选中文字划词提问，双击卡片把视图定位到它；多个小框可以攒起来一次问出。";
    showCwdPick();
    els.empty.classList.toggle("hidden", !blank);
    els.composer.classList.toggle("hidden", !blank);
    els.composer.classList.toggle("empty-mode", !!blank);
    updateDraftBar();
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
      if (!S.tree || S.tree.id !== tid) return;
      Object.assign(S.tree, d);
      showTitle(true);
    });
  }
  function closeStream() { if (S.es) { S.es.close(); S.es = null; } }

  function upsertTurn(t) {
    const isNew = !S.tree.turns[t.id];
    S.tree.turns[t.id] = t;
    if (!S.tree.order.includes(t.id)) S.tree.order.push(t.id);
    if (t.kind === "root") S.tree.rootId = t.id;
    renderTurn(t);
    if (isNew) {
      updateEmpty();
      const meta = S.trees.find(x => x.id === S.tree.id);  // 刚聊过的排到最前
      if (meta) { meta.updatedAt = new Date().toISOString(); S.trees.sort((a, b) => (b.updatedAt || "").localeCompare(a.updatedAt || "")); renderList(); }
      // 来源卡片要刷新：回显新的划词标记，「重试」按钮也随之消失
      for (const sid of core.sourcesOf(t)) if (turn(sid)) renderTurn(turn(sid));
    }
    renderDrafts();
    // 新分支出现 / 回答更新时，打开着的完整版跟着刷新
    if (FV.turnId && $("bbFvDialog").open && (t.id === FV.turnId || core.sourcesOf(t).includes(FV.turnId))) renderFullVersion();
    if (S.drawerId === t.id) renderDrawer(false);
    else if (S.drawerId && isNew && core.sourcesOf(t).includes(S.drawerId)) renderDrawer(false);
  }

  /* ---------- 卡片 ---------- */
  function clearStage() {
    S.els = {};
    els.stage.querySelectorAll(".bb-card").forEach(el => el.remove());
    els.wires.innerHTML = "";
    els.edge.innerHTML = "";
    S.layout = null; S.wireList = null; S.lastJump = null; S.navHistory = [];
  }

  const KIND_TAG = {
    root: '<span class="bb-tag main">起点</span>', follow: '<span class="bb-tag main">继续提问</span>',
    highlight: '<span class="bb-tag hl">划词提问</span>', multi: '<span class="bb-tag hl">多处提问</span>',
  };

  /** 一轮 = 一张卡片：顶部是问题小框（可以有多个，每个对应一条指过来的箭头），下面是回答。 */
  function renderTurn(t) {
    let el = S.els[t.id];
    if (!el) {
      el = document.createElement("div");
      el.className = "bb-card"; el.dataset.id = t.id;
      els.stage.insertBefore(el, els.drafts);
      S.els[t.id] = el;
    }
    const mode = modeOf(t.id);
    el.dataset.mode = mode;
    el.style.width = LAYOUT.WIDTH[mode] + "px";
    const n = num(t.id);
    const qs = core.questionsOf(t);
    const qboxes = qs.map((q, i) => {
      const owner = i === 0 ? ownerMap().get(t.id) : null;
      const head = i === 0 ? `${KIND_TAG[t.kind] || ""}${owner ? `<span class="bb-absorbed-tag">已并入 #${num(owner)}</span>` : ""}<span class="bb-num">#${n}</span>` : "";
      const from = q.sourceId && turn(q.sourceId) ? num(q.sourceId) : 0;
      const quote = q.quote && q.quote.text
        ? `<div class="bb-quote" data-quote-of="${esc(q.sourceId || "")}"${q.sourceId && turn(q.sourceId) ? ` style="--src:${core.sourceColor(S.tree, q.sourceId)}"` : ""}><b>摘自 #${from}${q.quote.source === "summary" ? " 的摘要" : q.quote.source === "question" ? " 的提问" : ""}</b>${esc(q.quote.text)}</div>`
        : (qs.length > 1 && from ? `<div class="bb-qfrom">接 #${from}</div>` : "");
      return `<div class="bb-qin" data-qi="${i}">${head}${quote}<div class="bb-qtext">${esc(q.text || "（就这段内容展开）")}</div></div>`;
    }).join("");
    el.classList.toggle("absorbed", ownerMap().has(t.id));
    el.classList.toggle("merged", showingMerged(t));
    el.innerHTML = `<div class="bb-qins">${qboxes}</div><div class="bb-ans">${answerHtml(t, n, mode)}</div>`;
    const preview = el.querySelector(".bb-preview");
    if (preview) preview.classList.toggle("clipped", preview.scrollHeight > preview.clientHeight + 2);
    markChildQuotes(el.querySelector(".bb-qins"), t.id, "question");
    if (mode === "full") markChildQuotes(el.querySelector(".bb-answer-full"), t.id, "full");
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

  /** 正在输出的回答取最后 n 行做预览；截断处如果落在代码块里，补上开头的 ``` 。 */
  function tailMarkdown(md, n) {
    const lines = String(md).replace(/\s+$/, "").split("\n");
    if (lines.length <= n) return md;
    const head = lines.slice(0, lines.length - n), tail = lines.slice(-n);
    const fences = head.filter(l => /^\s*(```|~~~)/.test(l)).length;
    return (fences % 2 ? "```\n" : "…\n\n") + tail.join("\n");
  }

  /* ---------- 过程（像 Claude 客户端那样）：思考、工具调用、计时 ---------- */
  const stepsOf = t => (t.answer && Array.isArray(t.answer.steps)) ? t.answer.steps : [];
  function fmtSecs(ms) {
    const s = Math.max(0, Math.round(ms / 1000));
    return s < 60 ? `${s} 秒` : `${Math.floor(s / 60)} 分 ${s % 60} 秒`;
  }
  /** 计时器：data-since 起点，data-until 终点（缺省 = 现在，每秒刷新）。 */
  function elapsedHtml(since, until) {
    if (!since) return "";
    const ms = (until ? Date.parse(until) : Date.now()) - Date.parse(since);
    return `<span class="bb-elapsed" data-since="${esc(since)}"${until ? ` data-until="${esc(until)}"` : ""}>${fmtSecs(ms)}</span>`;
  }
  setInterval(() => {
    document.querySelectorAll(".bb-elapsed[data-since]:not([data-until])").forEach(el => {
      el.textContent = fmtSecs(Date.now() - Date.parse(el.dataset.since));
    });
  }, 1000);
  /** 思考块默认：正在思考时展开（显示最新的几行），想完了收起；用户点过就按用户的。 */
  function thinkOpen(key, live) { return key in S.thinkOpen ? S.thinkOpen[key] : live; }
  function stepHtml(t, s, i, where) {
    const live = !s.end && running(t);
    if (s.kind === "thinking") {
      const key = `${where}:${t.id}:${i}`;
      const text = String(s.text || "").trim();
      const open = text && thinkOpen(key, live);
      const label = live ? "正在思考" : "思考了";
      return `<div class="bb-step think${live ? " live" : ""}${open ? " open" : ""}">`
        + `<button class="bb-step-head" type="button"${text ? ` data-think="${esc(key)}" aria-expanded="${open}"` : ""}>`
        + `<span class="bb-step-icon">${live ? '<span class="bb-spark"></span>' : "✻"}</span>${label} ${elapsedHtml(s.start, s.end)}${text ? '<span class="bb-chev">›</span>' : ""}</button>`
        + (open ? `<div class="bb-think-body"><div>${esc(text)}</div></div>` : "") + `</div>`;
    }
    const icon = live ? '<span class="bb-spinner"></span>' : s.isError ? "✕" : "✓";
    return `<div class="bb-step tool${live ? " live" : ""}${s.isError ? " err" : ""}"><span class="bb-step-icon">${icon}</span>`
      + `<b>${esc(s.name || "工具")}</b>${s.hint ? ` <span class="bb-step-hint">${esc(s.hint)}</span>` : ""}`
      + (s.end ? "" : ` ${elapsedHtml(s.start, null)}`) + `</div>`;
  }
  /** 卡片里的过程：运行中实时列出最近几步；完成后只留一行汇总（完全展开时列出全部步骤，思考默认收起）。 */
  function activityHtml(t, mode) {
    const a = t.answer, steps = stepsOf(t);
    if (running(t)) {
      const limit = mode === "full" ? steps.length : mode === "partial" ? 4 : 2;
      const shown = steps.slice(-limit), skipped = steps.length - shown.length;
      const live = `<div class="bb-statusline live"><span class="bb-spinner"></span><span class="bb-status-text">${esc(a.statusLine || "等待 Claude Code…")}</span>${elapsedHtml(a.startedAt, null)}</div>`;
      return `<div class="bb-activity">${live}${skipped ? `<div class="bb-step more">… 前面还有 ${skipped} 步</div>` : ""}${shown.map((s, k) => stepHtml(t, s, skipped + k, "card")).join("")}</div>`;
    }
    if (!steps.length) return "";
    const thinkMs = steps.filter(s => s.kind === "thinking" && s.end).reduce((sum, s) => sum + (Date.parse(s.end) - Date.parse(s.start)), 0);
    const tools = steps.filter(s => s.kind === "tool").length;
    const parts = [];
    if (thinkMs > 0 || steps.some(s => s.kind === "thinking")) parts.push(`思考 ${fmtSecs(thinkMs)}`);
    if (tools) parts.push(`${tools} 次工具调用`);
    if (a.startedAt && a.endedAt) parts.push(`共 ${fmtSecs(Date.parse(a.endedAt) - Date.parse(a.startedAt))}`);
    const key = `card:${t.id}:all`;
    const open = mode === "full" && thinkOpen(key, false);
    const toggle = mode === "full" ? `data-think="${esc(key)}" aria-expanded="${open}"` : `data-details="${t.id}" title="在详情里查看思考和工具调用"`;
    return `<div class="bb-activity done"><button class="bb-step-head summary" type="button" ${toggle}>`
      + `<span class="bb-step-icon">✻</span>${parts.join(" · ")}<span class="bb-chev">›</span></button>`
      + (open ? steps.map((s, i) => stepHtml(t, s, i, "card")).join("") : "") + `</div>`;
  }

  function answerHtml(t, n, mode) {
    const a = t.answer;
    const merged = showingMerged(t);
    const md = merged ? t.merged.markdown : a.markdown;
    let body = "";
    const streaming = running(t) && !merged ? " streaming" : "";  // 正在输出：末尾显示闪烁的光标
    if (mode === "full") {
      body = md ? `<div class="bb-answer-full bb-md${streaming}">${footnoteHtml(core.markdownToHtml(md))}</div>` : "";
    } else if (mode === "partial") {
      if (!merged && a.summary && a.summary.length && a.status === "done") body = `<ul class="bb-summary">${a.summary.map(s => `<li>${esc(s)}</li>`).join("")}</ul>`;
      // 正在输出时预览取最新的几行（跟着往下走），完成后取开头
      else if (md) body = `<div class="bb-preview bb-md${streaming}">${footnoteHtml(core.markdownToHtml(streaming ? tailMarkdown(md, 10) : core.previewMarkdown(md, 10)))}</div>`;
    }
    let mergeBar = "";
    if (t.merged) {
      const k = (t.merged.absorbed || []).length;
      const expanded = !!S.expandedMerge[t.id];
      mergeBar = `<div class="bb-merge-bar"><span class="bb-badge merged">${merged ? "合并版" : "原版"} · 已合并 ${k} 张卡片</span>
        <button class="bb-link" type="button" data-merge-view="${t.id}">${merged ? "看原版" : "看合并版"}</button>
        <button class="bb-link" type="button" data-merge-expand="${t.id}">${expanded ? "收起" : `展开 ${k} 张`}</button></div>`;
    }
    const status = merged ? "" : activityHtml(t, mode);
    const error = a.status === "error" ? `<div class="bb-error-text">${esc(a.error || "出错了")}</div>` : "";
    const actions = [];
    if (running(t)) actions.push(`<button class="bb-btn small" type="button" data-interrupt="${t.id}">中断</button>`);
    if ((a.status === "error" || a.status === "interrupted") && !hasChildren(t.id)) actions.push(`<button class="bb-btn small" type="button" data-retry="${t.id}">重试</button>`);
    if (finished(t)) actions.push(`<button class="bb-btn small" type="button" data-follow="${t.id}">继续提问</button>`);
    const hasAnswer = a.markdown || a.toolEvents.length || stepsOf(t).length;
    const toggle = !hasAnswer ? "" : mode === "full"
      ? `<button class="bb-link" type="button" data-expand="${t.id}" data-to="${S.mode === "full" ? "partial" : S.mode}">收起</button>`
      : `<button class="bb-link" type="button" data-expand="${t.id}" data-to="full">展开全文</button>`;
    const details = hasAnswer ? `<button class="bb-link" type="button" data-details="${t.id}">详情</button>` : "";
    const fullVersion = a.status === "done" && hasBranches(t.id) && !ownerMap().has(t.id)
      ? `<button class="bb-link" type="button" data-full-version="${t.id}" title="把这张卡片下面的划词子树整合进来：导出完整版，或合并到这张卡片">${t.merged ? "合并…" : "完整版 / 合并"}</button>` : "";
    const meta = mode === "question" && a.status === "done"
      ? `<span class="bb-meta">${a.summary && a.summary.length ? `${a.summary.length} 条要点` : `${(a.markdown || "").length} 字`}</span>` : "";
    return `<div class="bb-ans-head"><span class="bb-tag ans">回答 #${n}</span>${badgesHtml(a)}${meta}</div>${mergeBar}${status}${permissionsHtml(t)}${error}${body}
      <div class="bb-a-foot">${toggle}${details}${fullVersion}${actions.join("")}</div>`;
  }

  /* ---------- 布局 ---------- */
  function scheduleLayout() {
    if (S.layoutTimer) return;
    S.layoutTimer = setTimeout(() => requestAnimationFrame(() => { S.layoutTimer = 0; layoutNow(); }), 100);
  }

  function layoutNow() {
    if (!S.tree) return;
    const sizes = {};
    const hidden = hiddenSet();
    const view = S.view = hidden.size ? { ...S.tree, order: S.tree.order.filter(id => !hidden.has(id)) } : S.tree;
    for (const id of S.tree.order) {
      const el = S.els[id];
      if (!el) continue;
      el.style.display = hidden.has(id) ? "none" : "";
      if (hidden.has(id)) continue;
      // 每个问题小框一个连线挂点：小框较高时取它顶部往下 26px，避免挂到很长的问题中间
      const qms = [...el.querySelectorAll(".bb-qin")].map(q => q.offsetTop + Math.min(q.offsetHeight / 2, 26));
      sizes[id] = { w: el.offsetWidth, h: el.offsetHeight, qms };
    }
    const layout = S.layout = core.layoutTree(view, sizes);
    for (const g of Object.values(layout.cards)) {
      const el = S.els[g.id];
      if (!el) continue;
      el.style.left = g.x + "px"; el.style.top = g.y + "px";
    }
    els.stage.style.width = layout.width + "px"; els.stage.style.height = layout.height + "px";
    els.wires.setAttribute("width", layout.width); els.wires.setAttribute("height", layout.height);
    positionDrafts();
    drawWires();
  }

  function drawWires() {
    if (!S.layout) return;
    const ctx = new Set(S.selected ? core.contextIds(S.tree, S.selected) : []);
    const dim = w => (S.selected && !(ctx.has(w.id) && ctx.has(w.sourceId)) ? " dim" : "");
    // 箭头：主线一种颜色，划词线按来源卡片各用一种颜色（与目标卡片里引用框的颜色一致）
    const marker = (id, fill) => `<marker id="${id}" viewBox="0 0 10 10" refX="9" refY="5" markerUnits="userSpaceOnUse" markerWidth="11" markerHeight="11" orient="auto"><path d="M0,0.5 L10,5 L0,9.5 L2.2,5 z" ${fill}/></marker>`;
    let svg = `<defs>${marker("bbArrowMain", 'class="arrow-main"')}${core.SOURCE_COLORS.map((c, i) => marker(`bbArrow${i}`, `fill="${c}"`)).join("")}</defs>`;
    S.wireList = core.layoutWires(S.view || S.tree, S.layout);
    S.wireList.forEach((w, i) => {
      const hl = w.type === "highlight";
      const color = hl ? w.color : "";
      const arrow = hl ? `bbArrow${core.SOURCE_COLORS.indexOf(w.color)}` : "bbArrowMain";
      const q = core.questionsOf(turn(w.id))[w.qi] || {};
      const tip = `#${num(w.sourceId)} → #${num(w.id)}${core.questionsOf(turn(w.id)).length > 1 ? ` · 问题 ${w.qi + 1}` : ""}`
        + (q.quote && q.quote.text ? ` · 划词「${q.quote.text.replace(/\s+/g, " ").slice(0, 40)}」` : " · 继续提问");
      // 每条线一组：透明的粗线负责悬停命中；底色描边把线和背景网格、交叉的线隔开；上面一层负责显示
      svg += `<g class="wire ${w.type}${dim(w)}" data-wire="${i}" data-from="${w.sourceId}" data-to="${w.id}"${color ? ` style="--wire:${color}"` : ""}>`
        + `<path class="wire-hit" d="${w.d}"><title>${esc(tip)}</title></path>`
        + `<path class="wire-halo" d="${w.d}"/>`
        + `<path class="wire-line" d="${w.d}" marker-end="url(#${arrow})"/>`
        + `<circle class="wire-dot" cx="${w.x1}" cy="${w.y1}" r="4"/></g>`;
    });
    // 草稿小框：从来源卡片拉一条淡淡的虚线过去
    for (const dr of S.drafts) {
      const g = S.layout.cards[dr.sourceId];
      if (!g) continue;
      const x2 = g.x + dr.dx, y2 = g.y + dr.dy + 18;
      const x1 = Math.min(Math.max(x2, g.x + 12), g.x + g.w - 12);
      const y1 = y2 > g.y + g.h ? g.y + g.h : Math.max(g.y, Math.min(y2 - 18, g.y + g.h));
      if (y2 - 18 > g.y + g.h || x2 > g.x + g.w) svg += `<path class="draft-wire" d="M${x1},${y1} L${x2},${y2}" stroke-width="1.5" fill="none"/>`;
    }
    els.wires.innerHTML = svg;
    applyFocus();
    updateEdges();
  }

  /* ---------- 边缘箭头：放大后连线的起点 / 终点滚出视口时，把它吸附在卡片可见部分的边缘 ----------
     出口（来源卡片右边缘）：点一下跳到对应的提问；紧接着再点同一个箭头，回到来源卡片的末尾。
     入口（目标卡片左边缘）：点一下回到来源卡片的末尾。 */
  /* 导航规则（每张卡片的左右两侧）：
     - 右侧 = 往后：平行的问答框（普通追问 / 分叉，水平排在右边）；没有平行的，就是对这张卡片划词提问出来的卡片。
     - 左侧 = 往回：如果是从别处跳过来的，回到跳过来的那张（跳转历史，多跳也能一步步退回）；
       否则回到上级（本卡片的来源）。多处提问的卡片有多个来源，左侧按来源等分，各回各的。
     一侧有几个目标，就把这一侧上下等分成几段，每段一个超粗箭头。 */
  function navTargets() {
    const view = S.view || S.tree, cards = S.layout ? S.layout.cards : {};
    const plain = {}, hl = {}, srcs = {};
    for (const id of view.order) {
      const t = view.turns[id];
      if (!t || !cards[id]) continue;
      for (const q of core.questionsOf(t)) {
        const src = q.sourceId;
        if (!src || !cards[src]) continue;
        const quoted = !!(q.quote && q.quote.text) || t.kind === "highlight";
        const bucket = quoted ? hl : plain;
        (bucket[src] = bucket[src] || []).includes(id) || bucket[src].push(id);
        (srcs[id] = srcs[id] || []).includes(src) || srcs[id].push(src);
      }
    }
    const right = id => (plain[id] && plain[id].length ? plain[id] : hl[id] || []);
    const left = id => {
      const h = lastArrival(id);
      return h ? [h.from] : (srcs[id] || []);
    };
    return { right, left, plain, hl };
  }
  /** 跳转历史里最近一次「跳到 id」的记录。 */
  function lastArrival(id) {
    const hist = S.navHistory || [];
    for (let i = hist.length - 1; i >= 0; i--) if (hist[i].to === id) return { ...hist[i], index: i };
    return null;
  }
  /** 往后：从 from 跳到 to 的提问（to 里接 from 的那个问题小框贴近顶部）。 */
  function navForward(from, to) {
    S.navHistory = (S.navHistory || []).concat([{ from, to }]).slice(-50);
    S.lastJump = `${from}>${to}`;
    const g = S.layout && S.layout.cards[to];
    const qi = Math.max(0, core.questionsOf(turn(to)).findIndex(q => q.sourceId === from));
    const dy = g && g.qms ? Math.max(0, g.qms[Math.min(qi, g.qms.length - 1)] - 34) : 0;
    select(to); focusTurn(to, dy);
  }
  /** 往回：从 at 回到 to 的末尾；如果正是从 to 跳过来的，顺带把这一步从历史里退掉。 */
  function navBack(at, to) {
    const h = lastArrival(at);
    if (h && h.from === to) S.navHistory = S.navHistory.slice(0, h.index);
    S.lastJump = null;
    select(to); focusTurnEnd(to);
  }
  function updateEdges() {
    const box = els.edge;
    if (!box) return;
    const want = new Map();  // 复用按钮：频繁重绘（流式输出时每 100ms 一次）也不会吞掉正在进行的点击
    if (S.layout && S.tree) {
      const nav = navTargets();
      const rect = els.viewport.getBoundingClientRect();
      const drawerW = els.drawer.classList.contains("open") ? els.drawer.offsetWidth : 0;
      const W = rect.width - drawerW, H = rect.height - 80;  // 底部留给输入框
      const z = S.zoom, sx = x => S.panX + x * z, sy = y => S.panY + y * z;
      const wd = Math.max(56, Math.min(160, LAYOUT.GAP * z - 6));  // 宽度占满卡片之间的那道缝
      const visible = c => c && sx(c.x + c.w) > 0 && sx(c.x) < W && sy(c.y) < H && sy(c.y + c.h) > 8;
      for (const card of Object.values(S.layout.cards)) {
        if (!visible(card)) continue;
        const top = Math.max(sy(card.y), 8), bot = Math.min(sy(card.y + card.h), H);
        for (const side of ["out", "in"]) {
          let targets = side === "out" ? nav.right(card.id) : nav.left(card.id);
          // 左侧箭头和左边那张卡片的右侧箭头挤在同一道缝里：那张卡片看得见、右侧又正好指向这里时，就不重复放
          if (side === "in") targets = targets.filter(t => {
            const c = S.layout.cards[t];
            const sameGap = Math.abs(c.x + c.w + LAYOUT.GAP - card.x) < 1 && c.y < card.y + card.h && card.y < c.y + c.h;
            return !(sameGap && visible(c) && nav.right(t).includes(card.id));
          });
          const x = side === "out" ? sx(card.x + card.w) : sx(card.x);
          if (!targets.length || x < 0 || x > W) continue;
          const h = (bot - top) / targets.length;
          if (h < 28) continue;
          targets.forEach((to, k) => {
            const quoted = side === "out" ? !(nav.plain[card.id] || []).includes(to) : core.questionsOf(turn(card.id)).some(q => q.sourceId === to && q.quote && q.quote.text);
            const back = side === "in" || S.lastJump === `${card.id}>${to}`;
            const colorOf = side === "out" ? card.id : to;  // 划词：用来源卡片的颜色
            want.set(`${card.id}|${side}|${to}`, {
              side, back, from: card.id, to, x: Math.round(x), y: Math.round(top + h * (k + 0.5)), h: Math.round(h), wd: Math.round(wd),
              color: quoted ? core.sourceColor(S.tree, colorOf) : "",
              tip: side === "in" ? `回到 #${num(to)} 的末尾` : back ? `回到 #${num(card.id)} 的末尾` : `跳到 #${num(to)} 的提问`,
            });
          });
        }
      }
    }
    for (const el of [...box.children]) if (!want.has(el.dataset.key)) el.remove();
    for (const [key, it] of want) {
      let el = box.querySelector(`[data-key="${CSS.escape(key)}"]`);
      if (!el) {
        el = document.createElement("button");
        el.type = "button"; el.dataset.key = key;
        el.innerHTML = '<svg viewBox="0 0 26 22" aria-hidden="true"><path d="M1,6.5 H13 V1 L25,11 L13,21 V15.5 H1 z"/></svg>';
        box.appendChild(el);
      }
      el.className = `bb-edge-arrow ${it.side}${it.back ? " back" : ""}`;
      el.style.left = it.x + "px"; el.style.top = it.y + "px";
      el.style.height = it.h + "px"; el.style.width = it.wd + "px";
      if (it.color) el.style.setProperty("--wire", it.color); else el.style.removeProperty("--wire");
      el.title = it.tip; el.setAttribute("aria-label", it.tip);
    }
  }
  els.edge.addEventListener("click", e => {
    const btn = e.target.closest(".bb-edge-arrow");
    if (!btn) return;
    e.stopPropagation();
    const [from, side, to] = btn.dataset.key.split("|");
    if (side === "in") navBack(from, to);
    else if (S.lastJump === `${from}>${to}`) navBack(to, from);  // 紧接着再点同一个箭头：回到原来那张的末尾
    else navForward(from, to);
  });

  /* ---------- 悬停聚焦：线多了以后，只突出和当前卡片 / 当前连线相关的那几条 ---------- */
  const focus = { card: null, wire: null };
  function applyFocus() {
    const active = focus.card || focus.wire;
    els.wires.classList.toggle("has-focus", !!active);
    els.stage.classList.toggle("has-focus", !!active);
    const linked = new Set();
    els.wires.querySelectorAll("g.wire").forEach(g => {
      const from = g.dataset.from, to = g.dataset.to;
      const hot = focus.wire ? g === focus.wire : !!focus.card && (from === focus.card || to === focus.card);
      g.classList.toggle("hot", hot);
      if (hot) { linked.add(from); linked.add(to); }
    });
    if (focus.card) linked.add(focus.card);
    for (const [id, el] of Object.entries(S.els)) el.classList.toggle("linked", active ? linked.has(id) : false);
  }
  els.stage.addEventListener("mouseover", e => {
    if (pan) return;
    const wire = e.target.closest && e.target.closest("g.wire");
    const card = e.target.closest && e.target.closest(".bb-card");
    const next = { card: wire ? null : (card ? card.dataset.id : null), wire: wire || null };
    if (next.card === focus.card && next.wire === focus.wire) return;
    focus.card = next.card; focus.wire = next.wire;
    applyFocus();
  });
  els.stage.addEventListener("mouseleave", () => { focus.card = null; focus.wire = null; applyFocus(); });
  els.wires.addEventListener("click", e => {
    // 点一条线：跳到它指向的提问并高亮上下文；紧接着再点同一条线，回到来源卡片的末尾
    const wire = e.target.closest("g.wire");
    const w = wire && S.wireList && S.wireList[+wire.dataset.wire];
    if (w) { e.stopPropagation(); if (S.lastJump === `${w.sourceId}>${w.id}`) navBack(w.id, w.sourceId); else navForward(w.sourceId, w.id); }
  });

  function setMode(mode) {
    if (!core.MODES.includes(mode)) return;
    S.mode = mode; S.cardMode = {};
    try { localStorage.setItem(MODE_KEY, mode); } catch (_e) { /* 忽略 */ }
    document.querySelectorAll(".bb-modes [data-mode]").forEach(b => {
      b.classList.toggle("on", b.dataset.mode === mode); b.setAttribute("aria-checked", String(b.dataset.mode === mode));
    });
    if (!S.tree) return;
    const anchor = viewAnchor();
    for (const id of S.tree.order) renderTurn(S.tree.turns[id]);
    layoutNow();
    restoreAnchor(anchor);
  }
  /**
   * 切换显示模式前记下视口左上角的参考卡片，切换后照此重新定位，视野不乱跳。
   * 1. 左上角有被视口上边或左边切掉一部分的卡片（取可见部分离左上角最近的一张）：
   *    被切的方向记显示比例，切换后保持这个比例；没被切的方向记卡片边缘离视口边缘的屏幕像素。
   * 2. 没有这样的卡片：取左上角离视口左上角最近的完整卡片，切换后保持它左上角在屏幕上的位置不变。
   */
  function viewAnchor() {
    if (!S.layout) return null;
    const rect = els.viewport.getBoundingClientRect();
    const drawerW = els.drawer.classList.contains("open") ? els.drawer.offsetWidth : 0;
    const vx = -S.panX / S.zoom, vy = -S.panY / S.zoom;  // 视口左上角的画布坐标
    const vw = (rect.width - drawerW) / S.zoom, vh = rect.height / S.zoom;
    let partial = null, whole = null;
    for (const g of Object.values(S.layout.cards)) {
      const x1 = Math.max(g.x, vx), y1 = Math.max(g.y, vy);
      const x2 = Math.min(g.x + g.w, vx + vw), y2 = Math.min(g.y + g.h, vy + vh);
      if (x2 <= x1 || y2 <= y1) continue;  // 不在视口里
      const d = Math.hypot(x1 - vx, y1 - vy);  // 可见部分的左上角离视口左上角多远
      if (g.x < vx || g.y < vy) { if (!partial || d < partial.d) partial = { d, g }; }  // 被上边或左边切掉一部分
      else if (!whole || d < whole.d) whole = { d, g };  // 左上角完整可见，d 就是卡片左上角的距离
    }
    const g = (partial || whole || {}).g;
    if (!g) return null;
    const axis = (v, start, size) => v > start
      ? { cut: (v - start) / size }          // 被切掉的比例（显示比例 = 1 - cut）
      : { gap: (start - v) * S.zoom };       // 卡片边缘离视口边缘的屏幕像素
    return { id: g.id, x: axis(vx, g.x, g.w), y: axis(vy, g.y, g.h) };
  }
  function restoreAnchor(anchor) {
    const g = anchor && S.layout && S.layout.cards[anchor.id];
    if (!g) return;  // 那张卡片在新模式下被隐藏了：视图不动
    const vx = anchor.x.cut != null ? g.x + anchor.x.cut * g.w : g.x - anchor.x.gap / S.zoom;
    const vy = anchor.y.cut != null ? g.y + anchor.y.cut * g.h : g.y - anchor.y.gap / S.zoom;
    S.panX = -vx * S.zoom; S.panY = -vy * S.zoom;
    applyTransform();
  }
  document.querySelectorAll(".bb-modes [data-mode]").forEach(b => b.addEventListener("click", () => setMode(b.dataset.mode)));

  /* ---------- 平移 / 缩放 ---------- */
  function applyTransform() {
    els.stage.style.transform = `translate(${S.panX}px, ${S.panY}px) scale(${S.zoom})`;
    els.viewport.style.backgroundPosition = `${S.panX}px ${S.panY}px`;
    els.viewport.style.backgroundSize = `${22 * S.zoom}px ${22 * S.zoom}px`;
    $("bbZoomReset").textContent = Math.round(S.zoom * 100) + "%";
    updateEdges();
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
    const bottom = 70;
    const z = Math.min(1, Math.max(0.15, Math.min(rect.width / S.layout.width, (rect.height - bottom) / S.layout.height)));
    S.zoom = z;
    S.panX = (rect.width - S.layout.width * z) / 2;
    S.panY = Math.max(0, (rect.height - bottom - S.layout.height * z) / 2);
    applyTransform();
  }
  /** 把画布坐标的矩形移进视口。 */
  function revealRect(x, y, w, h) {
    const rect = els.viewport.getBoundingClientRect();
    const x1 = S.panX + x * S.zoom, x2 = S.panX + (x + w) * S.zoom;
    const y1 = S.panY + y * S.zoom, y2 = S.panY + (y + h) * S.zoom;
    const margin = 40, drawerW = els.drawer.classList.contains("open") ? els.drawer.offsetWidth : 0;
    if (x2 > rect.width - drawerW - margin) S.panX -= Math.min(x2 - (rect.width - drawerW - margin), x1 - margin);
    else if (x1 < margin) S.panX += margin - x1;
    if (y2 > rect.height - 90) S.panY -= Math.min(y2 - (rect.height - 90), y1 - margin);
    else if (y1 < margin) S.panY += margin - y1;
    applyTransform();
  }
  function revealTurn(id) {
    layoutNow();
    const g = S.layout && S.layout.cards[id];
    if (g) revealRect(g.x, g.y, g.w, Math.max(g.h, 120));
  }
  /** 把一张卡片定到视口中心：水平居中（避开抽屉），问题贴近顶部，其余往下显示。缩放不变。 */
  function focusTurn(id, dy = 0) {  // dy：卡片内要贴到顶部的位置（画布坐标，相对卡片顶边）
    layoutNow();
    const g = S.layout && S.layout.cards[id];
    if (!g) return;
    const rect = els.viewport.getBoundingClientRect();
    const drawerW = els.drawer.classList.contains("open") ? els.drawer.offsetWidth : 0;
    const top = 24;
    S.panX = (rect.width - drawerW) / 2 - (g.x + g.w / 2) * S.zoom;
    S.panY = top - (g.y + dy) * S.zoom;
    applyTransform();
  }
  /** 回到一张卡片的末尾：水平居中；卡片放得下就整张显示，放不下就让底边贴近视口底部（留出输入框）。 */
  function focusTurnEnd(id) {
    layoutNow();
    const g = S.layout && S.layout.cards[id];
    if (!g) return;
    const rect = els.viewport.getBoundingClientRect();
    const drawerW = els.drawer.classList.contains("open") ? els.drawer.offsetWidth : 0;
    const top = 24, bottom = rect.height - 96;
    S.panX = (rect.width - drawerW) / 2 - (g.x + g.w / 2) * S.zoom;
    S.panY = g.h * S.zoom <= bottom - top ? top - g.y * S.zoom : bottom - (g.y + g.h) * S.zoom;
    applyTransform();
  }
  /** 屏幕坐标 → 画布坐标。 */
  function toStage(clientX, clientY) {
    const rect = els.viewport.getBoundingClientRect();
    return { x: (clientX - rect.left - S.panX) / S.zoom, y: (clientY - rect.top - S.panY) / S.zoom };
  }

  /* 拖拽手势：
     - 在空白处、卡片的非文字区域（边距、标签、按钮栏之外的空隙）按下 → 直接平移画布；
     - 在可划词的文字上按下 → 先当作划词；只要指针拖出了这张卡片，就判定为「想去看别处」，
       清掉选区、改为平移（位移从按下的那一刻算起，画面不会跳）；
     - 按住空格拖动，或用鼠标中键拖动 → 无论按在哪里都只平移，不会选中文字。 */
  const TEXT_AREAS = ".bb-qtext, .bb-md, .bb-summary, .bb-quote, .bb-error-text, .bb-perm code";
  const NO_PAN = ".bb-draft, .bb-composer, .bb-drawer, .bb-ask-selection, .bb-draftbar, button, textarea, input, select, a, summary";
  let pan = null, maybeSelect = null, spaceDown = false, suppressClick = false;

  function startPan(origin, pointerId) {
    pan = { x: origin.x, y: origin.y, px: origin.px, py: origin.py, moved: false, pointerId };
    els.stage.classList.add("no-select");  // 平移期间不再产生新的选区
  }
  function movePan(clientX, clientY) {
    const dx = clientX - pan.x, dy = clientY - pan.y;
    if (!pan.moved && Math.abs(dx) + Math.abs(dy) > 3) {
      // 真的拖起来了才捕获指针：按下就捕获的话，原地单击 / 双击会被浏览器派发给视口而不是卡片
      pan.moved = true;
      els.viewport.classList.add("panning");
      try { els.viewport.setPointerCapture(pan.pointerId); } catch (_e) { /* 指针已释放 */ }
    }
    if (pan.moved) { S.panX = pan.px + dx; S.panY = pan.py + dy; applyTransform(); }
  }
  function endPan() {
    if (!pan) return;
    if (pan.moved) { suppressClick = true; setTimeout(() => { suppressClick = false; }, 0); }
    else if (!pan.fromCard) select(null);
    pan = null;
    els.viewport.classList.remove("panning");
    els.stage.classList.remove("no-select");
  }

  els.viewport.addEventListener("pointerdown", e => {
    const forcePan = e.button === 1 || (e.button === 0 && spaceDown);
    if (!forcePan && (e.button !== 0 || e.target.closest(NO_PAN))) return;
    const origin = { x: e.clientX, y: e.clientY, px: S.panX, py: S.panY };
    const card = e.target.closest(".bb-card");
    if (forcePan) {
      e.preventDefault();
      window.getSelection().removeAllRanges();
      startPan(origin, e.pointerId);
    } else if (card && e.target.closest(TEXT_AREAS)) {
      maybeSelect = { ...origin, card, pointerId: e.pointerId };  // 先让浏览器正常选字
    } else {
      startPan(origin, e.pointerId);
      if (card) pan.fromCard = true;  // 在卡片空隙处按下又原地松开：仍算点击卡片
    }
  });
  els.viewport.addEventListener("mousedown", e => {
    // 中键会触发浏览器自动滚动；在卡片空隙处按下左键会开始选字。两种情况都交给平移处理。
    if (e.button === 1 || (e.button === 0 && (spaceDown || (pan && !maybeSelect)))) e.preventDefault();
  });
  document.addEventListener("pointermove", e => {
    if (maybeSelect && e.pointerId === maybeSelect.pointerId) {
      const r = maybeSelect.card.getBoundingClientRect();
      const outside = e.clientX < r.left - 4 || e.clientX > r.right + 4 || e.clientY < r.top - 4 || e.clientY > r.bottom + 4;
      if (outside) {
        window.getSelection().removeAllRanges();
        els.ask.classList.add("hidden");
        const origin = maybeSelect;
        maybeSelect = null;
        startPan(origin, e.pointerId);
        movePan(e.clientX, e.clientY);
      }
      return;
    }
    if (pan) movePan(e.clientX, e.clientY);
  });
  const finishPointer = () => { maybeSelect = null; endPan(); };
  document.addEventListener("pointerup", finishPointer);
  document.addEventListener("pointercancel", finishPointer);

  document.addEventListener("keydown", e => {
    if (e.code !== "Space" || e.repeat) return;
    if (e.target.closest && e.target.closest("input, textarea, select, [contenteditable], button")) return;
    e.preventDefault();
    spaceDown = true; els.viewport.classList.add("space-pan");
  });
  document.addEventListener("keyup", e => { if (e.code === "Space") { spaceDown = false; els.viewport.classList.remove("space-pan"); } });
  window.addEventListener("blur", () => { spaceDown = false; els.viewport.classList.remove("space-pan"); });
  els.viewport.addEventListener("wheel", e => {
    if (e.target.closest(".bb-drawer, .bb-composer, .bb-draft textarea")) return;
    e.preventDefault();
    if (e.ctrlKey || e.metaKey) {
      const rect = els.viewport.getBoundingClientRect();
      zoomAt(Math.exp(-e.deltaY * 0.0022), e.clientX - rect.left, e.clientY - rect.top);
    } else {
      S.panX -= e.shiftKey && !e.deltaX ? e.deltaY : e.deltaX; S.panY -= e.shiftKey && !e.deltaX ? 0 : e.deltaY; applyTransform();
    }
  }, { passive: false });

  /* ---------- 选中与上下文高亮 ---------- */
  function select(id) {
    S.selected = id;
    applySelectionClasses();
    drawWires();
  }
  function applySelectionClasses() {
    const ctx = new Set(S.selected && S.tree ? core.contextIds(S.tree, S.selected) : []);
    els.stage.classList.toggle("has-selection", !!S.selected);
    const targets = new Set(S.drafts.map(d => d.sourceId));
    for (const [id, el] of Object.entries(S.els)) {
      el.classList.toggle("on-path", ctx.has(id));
      el.classList.toggle("selected", id === S.selected);
      el.classList.toggle("target", targets.has(id));
    }
  }

  /* ---------- 问题小框草稿 ---------- */
  function clearDrafts() { S.drafts = []; renderDrafts(); }

  /** 在来源卡片附近加一个问题小框。at：画布坐标（双击点 / 选区下方），缺省放在卡片右下。 */
  function addDraft(sourceId, quote, at) {
    const src = turn(sourceId);
    const g = S.layout && S.layout.cards[sourceId];
    if (!src || !g) return;
    const owner = ownerMap().get(sourceId);
    if (owner) { toast(`回答 #${num(sourceId)} 已并入 #${num(owner)}，请在合并后的卡片上提问，或先取消合并。`); return; }
    if (running(src)) { toast(`回答 #${num(sourceId)} 还没完成，完成后再提问。`); return; }
    if (!finished(src)) { toast(`回答 #${num(sourceId)} 出错了，不能从它继续。`); return; }
    let dx = at ? at.x - g.x - 20 : g.w - DRAFT_W + 24;
    let dy = at ? at.y - g.y + 10 : g.h + 14;
    dx = Math.max(-40, Math.min(dx, g.w - 40));
    // 同一张卡片上已有的小框：往下错开，避免叠在一起
    for (const other of S.drafts) {
      if (other.sourceId === sourceId && Math.abs(other.dx - dx) < DRAFT_W && Math.abs(other.dy - dy) < 90) dy = other.dy + 96;
    }
    const draft = { key: `d${++S.draftSeq}`, sourceId, quote: quote || null, text: "", dx, dy };
    S.drafts.push(draft);
    renderDrafts();
    const box = els.drafts.querySelector(`[data-key="${draft.key}"]`);
    if (box) {
      revealRect(g.x + dx, g.y + dy, DRAFT_W, box.offsetHeight + 20);
      box.querySelector("textarea").focus();
    }
  }

  function removeDraft(key) {
    const i = S.drafts.findIndex(d => d.key === key);
    if (i >= 0) S.drafts.splice(i, 1);
    renderDrafts();
    const last = els.drafts.querySelector(".bb-draft:last-child textarea");
    if (last) last.focus();
  }

  function renderDrafts() {
    if (!S.tree) { els.drafts.innerHTML = ""; updateDraftBar(); return; }
    const hiddenNow = hiddenSet();
    S.drafts = S.drafts.filter(d => turn(d.sourceId) && !hiddenNow.has(d.sourceId));
    const keep = new Set(S.drafts.map(d => d.key));
    els.drafts.querySelectorAll(".bb-draft").forEach(el => { if (!keep.has(el.dataset.key)) el.remove(); });
    S.drafts.forEach((d, i) => {
      let box = els.drafts.querySelector(`[data-key="${d.key}"]`);
      if (!box) {
        box = document.createElement("div");
        box.className = "bb-draft"; box.dataset.key = d.key; box.style.width = DRAFT_W + "px";
        const quote = d.quote ? `<div class="bb-quote" style="--src:${core.sourceColor(S.tree, d.sourceId)}"><b>划中的${d.quote.source === "question" ? "提问" : d.quote.source === "summary" ? "摘要" : "原文"}</b>${esc(d.quote.text)}</div>` : "";
        box.innerHTML = `<div class="bb-draft-head"><span class="bb-chip ${d.quote ? "hl" : ""}"></span><button class="bb-icon small" type="button" data-remove-draft title="删除这个小框（Esc）">×</button></div>
          ${quote}<textarea rows="2" maxlength="20000" placeholder="${d.quote ? "就这段内容问点什么（可留空，只作引用）" : "继续提问…"}"></textarea>`;
        const ta = box.querySelector("textarea");
        ta.value = d.text;
        ta.addEventListener("input", () => { d.text = ta.value; autosizeTextarea(ta); updateDraftBar(); scheduleLayoutDraftsOnly(); });
        ta.addEventListener("keydown", e => {
          if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); sendDrafts(); }
          else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); removeDraft(d.key); }
        });
        box.querySelector("[data-remove-draft]").addEventListener("click", () => removeDraft(d.key));
        els.drafts.append(box);
      }
      box.querySelector(".bb-chip").textContent = `${S.drafts.length > 1 ? `${i + 1}. ` : ""}${d.quote ? "划词" : "继续"} · 回答 #${num(d.sourceId)}`;
    });
    positionDrafts();
    applySelectionClasses();
    updateDraftBar();
    drawWires();
  }
  let draftTimer = 0;
  function scheduleLayoutDraftsOnly() { clearTimeout(draftTimer); draftTimer = setTimeout(drawWires, 60); }
  function autosizeTextarea(ta) { ta.style.height = "auto"; ta.style.height = Math.min(220, ta.scrollHeight + 2) + "px"; }

  function positionDrafts() {
    if (!S.layout) return;
    for (const d of S.drafts) {
      const g = S.layout.cards[d.sourceId];
      const box = els.drafts.querySelector(`[data-key="${d.key}"]`);
      if (!g || !box) continue;
      box.style.left = g.x + d.dx + "px"; box.style.top = g.y + d.dy + "px";
    }
  }

  function updateDraftBar() {
    const n = S.drafts.length;
    els.draftBar.classList.toggle("hidden", !n);
    els.hint.classList.toggle("hidden", !!n || !S.tree || !S.tree.rootId);
    if (!n) return;
    const sources = [...new Set(S.drafts.map(d => d.sourceId))].map(id => "#" + num(id));
    $("bbDraftSummary").innerHTML = `<b>${n} 个问题小框</b> · 来自 ${esc(sources.join("、"))}${n > 1 ? " → 将合成一张新卡片" : ""}`;
    $("bbDraftSend").disabled = S.sending || !S.drafts.some(d => d.text.trim());
  }

  async function sendDrafts() {
    if (S.sending || !S.drafts.length) return;
    if (!S.drafts.some(d => d.text.trim())) { toast("至少在一个小框里写下问题。"); return; }
    const blocked = S.drafts.find(d => !finished(turn(d.sourceId)));
    if (blocked) { toast(`回答 #${num(blocked.sourceId)} 还没完成，完成后再发送。`); return; }
    S.sending = true; updateDraftBar();
    try {
      const questions = S.drafts.map(d => ({ sourceId: d.sourceId, text: d.text.trim(), quote: d.quote || undefined }));
      const { turn: created } = await api("POST", `/trees/${S.tree.id}/turns`, { questions });
      clearDrafts();
      upsertTurn(created);
      select(created.id);
      revealTurn(created.id);
    } catch (err) {
      toast(err.message);
    } finally {
      S.sending = false; updateDraftBar();
    }
  }
  $("bbDraftSend").addEventListener("click", sendDrafts);
  $("bbDraftClear").addEventListener("click", () => { if (S.drafts.length < 2 || confirm(`清空 ${S.drafts.length} 个未发送的问题小框？`)) clearDrafts(); });

  /* ---------- 起点 ---------- */
  async function sendRoot() {
    const question = els.question.value.trim();
    if (!question || els.send.disabled || (S.tree && S.tree.rootId)) return;
    els.send.disabled = true;
    try {
      if (!S.tree) {
        // 新聊天：问出第一个问题时才建树；标题留空，由后端自动命名
        const { tree } = await api("POST", "/trees", {
          cwd: S.newCwd, settings: { model: $("bbModel").value, effort: $("bbEffort").value, permissionMode: $("bbPermission").value },
        });
        await refreshList();
        await openTree(tree.id);
        if (!S.tree) return;
      }
      const { turn: created } = await api("POST", `/trees/${S.tree.id}/turns`, { question });
      els.question.value = "";
      upsertTurn(created);
      select(created.id);
      revealTurn(created.id);
    } catch (err) { toast(err.message); }
    finally { els.send.disabled = false; }
  }
  els.composer.addEventListener("submit", e => { e.preventDefault(); sendRoot(); });
  els.question.addEventListener("input", () => autosizeTextarea(els.question));
  els.question.addEventListener("keydown", e => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); sendRoot(); } });

  /* ---------- 卡片点击、双击与按钮 ---------- */
  els.stage.addEventListener("click", async e => {
    if (suppressClick || e.target.closest(".bb-draft")) return;  // 拖动画布结束时的那次 click 不算点击卡片
    const btn = e.target.closest("button");
    if (btn) { await handleAction(btn, e); return; }
    const card = e.target.closest(".bb-card");
    if (!card) return;
    const sel = window.getSelection();
    if (sel && !sel.isCollapsed && card.contains(sel.anchorNode)) return;  // 正在划词
    const mark = e.target.closest("mark.bb-quote-mark");
    if (mark) { select(mark.dataset.child); revealTurn(mark.dataset.child); return; }
    const quote = e.target.closest(".bb-quote[data-quote-of]");
    if (quote && turn(quote.dataset.quoteOf)) { select(quote.dataset.quoteOf); revealTurn(quote.dataset.quoteOf); return; }
    // 单击卡片：高亮这一轮能看到的上下文；完整详情走「详情」按钮
    select(card.dataset.id);
    if (S.drawerId && S.drawerId !== card.dataset.id) openDrawer(card.dataset.id);
  });
  els.stage.addEventListener("dblclick", e => {
    const card = e.target.closest(".bb-card");
    if (!card || e.target.closest("button, .bb-draft, .bb-perm")) return;
    // 双击卡片：把整个视图定位到这张卡片，问题在顶部，其余往下显示（追问走「继续提问」按钮）
    window.getSelection().removeAllRanges();
    els.ask.classList.add("hidden");
    select(card.dataset.id);
    focusTurn(card.dataset.id);
  });

  async function handleAction(btn, e) {
    const card = btn.closest(".bb-card");
    const tid = (card && card.dataset.id) || S.drawerId;
    try {
      if (btn.dataset.think) {
        const key = btn.dataset.think, [where, id] = key.split(":");
        S.thinkOpen[key] = btn.getAttribute("aria-expanded") !== "true";
        if (where === "drawer") renderDrawer(false);
        else if (turn(id)) { renderTurn(turn(id)); layoutNow(); }
      } else if (btn.dataset.follow) {
        const r = btn.getBoundingClientRect();
        addDraft(btn.dataset.follow, null, toStage(r.left, r.bottom));
      } else if (btn.dataset.details) { select(btn.dataset.details); openDrawer(btn.dataset.details); }
      else if (btn.dataset.fullVersion) { select(btn.dataset.fullVersion); openFullVersion(btn.dataset.fullVersion); }
      else if (btn.dataset.mergeView) { const id = btn.dataset.mergeView; S.showOriginal[id] = !S.showOriginal[id]; renderTurn(turn(id)); layoutNow(); }
      else if (btn.dataset.mergeExpand) { const id = btn.dataset.mergeExpand; S.expandedMerge[id] = !S.expandedMerge[id]; rerenderAll(); revealTurn(id); }
      else if (btn.dataset.expand) {
        const id = btn.dataset.expand;
        if (btn.dataset.to === S.mode) delete S.cardMode[id]; else S.cardMode[id] = btn.dataset.to;
        renderTurn(turn(id)); layoutNow(); revealTurn(id);
      }
      else if (btn.dataset.interrupt) { await api("POST", `/trees/${S.tree.id}/turns/${btn.dataset.interrupt}/interrupt`); toast("已请求中断"); }
      else if (btn.dataset.retry) { upsertTurn((await api("POST", `/trees/${S.tree.id}/turns/${btn.dataset.retry}/retry`)).turn); }
      else if (btn.dataset.permAllow || btn.dataset.permDeny) {
        const allow = !!btn.dataset.permAllow;
        btn.closest(".bb-perm").querySelectorAll("button").forEach(b => { b.disabled = true; });
        await api("POST", `/trees/${S.tree.id}/turns/${tid}/permissions/${encodeURIComponent(btn.dataset.permAllow || btn.dataset.permDeny)}`, { allow });
      }
    } catch (err) { toast(err.message); }
    if (e) e.stopPropagation();
  }

  /* ---------- 划词提问 ---------- */
  function selectionInfo() {
    const sel = window.getSelection();
    if (!sel || sel.isCollapsed || !sel.rangeCount) return null;
    const text = sel.toString().trim();
    if (!text) return null;
    const range = sel.getRangeAt(0);
    const node = range.commonAncestorContainer.nodeType === 1 ? range.commonAncestorContainer : range.commonAncestorContainer.parentElement;
    if (node.closest(".bb-draft")) return null;
    let turnId = null, source = "full";
    const card = node.closest(".bb-card");
    if (card && els.stage.contains(card)) {
      // 回答（摘要 / 预览 / 全文）和问题小框里的文字都可以划词
      turnId = card.dataset.id;
      if (node.closest(".bb-summary")) source = "summary";
      else if (node.closest(".bb-qtext")) source = "question";
      else if (!node.closest(".bb-preview, .bb-answer-full")) return null;
    } else if (node.closest(".bb-answer-full") && S.drawerId) {
      turnId = S.drawerId;
    }
    if (!turnId) return null;
    return { turnId, text: text.slice(0, 4000), source, rect: range.getBoundingClientRect() };
  }

  document.addEventListener("mouseup", e => setTimeout(() => {
    if (e.target.closest && e.target.closest(".bb-ask-selection")) return;
    const info = selectionInfo();
    S.pendingSel = info;
    if (!info) { els.ask.classList.add("hidden"); return; }
    els.ask.style.left = Math.max(8, Math.min(window.innerWidth - 120, info.rect.left + info.rect.width / 2 - 50)) + "px";
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
    const quote = { text: info.text, source: info.source };
    if (info.source === "full" && !(showingMerged(parent) && els.stage.contains(document.elementFromPoint(info.rect.left + 1, info.rect.top + 1)))) {
      const hit = core.locateQuote(parent.answer.markdown, info.text);
      if (hit) { quote.start = hit.start; quote.end = hit.end; }
    }
    window.getSelection().removeAllRanges();
    // 选区在画布上：小框放在选区正下方；在抽屉里：放在卡片右下
    const inStage = els.stage.contains(document.elementFromPoint(info.rect.left + 1, info.rect.top + 1));
    addDraft(info.turnId, quote, inStage ? toStage(info.rect.left, info.rect.bottom) : null);
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
  $("bbDrawerFollow").addEventListener("click", () => { if (S.drawerId) addDraft(S.drawerId, null, null); });
  $("bbDrawerInterrupt").addEventListener("click", () => S.drawerId && handleAction({ dataset: { interrupt: S.drawerId }, closest: () => null }));
  $("bbDrawerRetry").addEventListener("click", () => S.drawerId && handleAction({ dataset: { retry: S.drawerId }, closest: () => null }));
  els.drawer.addEventListener("click", async e => {
    const btn = e.target.closest("button[data-perm-allow], button[data-perm-deny], button[data-think]");
    if (btn) { await handleAction(btn); return; }
    const quote = e.target.closest(".bb-qbox .bb-quote[data-quote-of]");
    if (quote && turn(quote.dataset.quoteOf)) { openDrawer(quote.dataset.quoteOf, S.drawerId); return; }
    const mark = e.target.closest("mark.bb-quote-mark");
    if (mark) { const id = mark.dataset.child; select(id); revealTurn(id); }
  });

  function renderDrawer(fresh, focusChild) {
    const t = turn(S.drawerId);
    if (!t) return;
    const body = els.drawerBody;
    const sel = window.getSelection();
    if (!fresh && sel && !sel.isCollapsed && body.contains(sel.anchorNode)) return;  // 不打断正在进行的划词
    const atBottom = body.scrollTop + body.clientHeight >= body.scrollHeight - 30;
    const scroll = body.scrollTop;
    const a = t.answer, n = num(t.id);
    const srcs = core.sourcesOf(t);
    $("bbDrawerEyebrow").textContent = `第 ${n} 轮 · ${core.KIND_LABEL[t.kind] || ""}${srcs.length ? ` · 接 ${srcs.map(s => "#" + num(s)).join("、")}` : ""}`;
    $("bbDrawerTitle").textContent = (t.question || "").split("\n")[0].slice(0, 80);
    $("bbDrawerFollow").disabled = !finished(t);
    $("bbDrawerFullVersion").classList.toggle("hidden", !(a.status === "done" && hasBranches(t.id)));
    $("bbDrawerInterrupt").classList.toggle("hidden", !running(t));
    $("bbDrawerRetry").classList.toggle("hidden", !((a.status === "error" || a.status === "interrupted") && !hasChildren(t.id)));
    const qs = core.questionsOf(t);
    const questions = qs.map((q, i) => {
      const quote = q.quote && q.quote.text
        ? `<div class="bb-quote" data-quote-of="${esc(q.sourceId || "")}" style="--src:${core.sourceColor(S.tree, q.sourceId)}" title="在来源中定位"><b>摘自 #${num(q.sourceId)} · 点击在原文中定位</b>${esc(q.quote.text)}</div>` : "";
      return `<div class="bb-qbox">${quote}<b>${qs.length > 1 ? `问题 ${i + 1}` : "提问"}${!quote && q.sourceId && qs.length > 1 ? `（接 #${num(q.sourceId)}）` : ""}：</b>${esc(q.text || "（就这段内容展开）")}</div>`;
    }).join("");
    const files = a.filesChanged.length ? `<p class="bb-section-title">修改的文件（分支不隔离文件，主线也能看到这些改动）</p><ul class="bb-files">${a.filesChanged.map(f => `<li><code>${esc(f)}</code></li>`).join("")}</ul>` : "";
    const toolDetails = (ev, label) => {
      const input = typeof ev.input === "string" ? ev.input : JSON.stringify(ev.input, null, 2);
      const flag = ev.approved === true ? ' <span class="ok">已批准</span>' : ev.approved === false ? ' <span class="no">已拒绝</span>' : "";
      const err = ev.isError ? ' <span class="no">失败</span>' : "";
      const hint = (ev.input && (ev.input.file_path || ev.input.command || ev.input.pattern || ev.input.url || ev.input.description)) || "";
      const key = `${t.id}:${ev.id || ev.ts}`;
      return `<details data-key="${esc(key)}"${S.drawerOpen.has(key) ? " open" : ""}><summary>${label || ""}<b>${esc(ev.name)}</b> ${esc(String(hint).slice(0, 120))}${flag}${err}</summary><pre>${esc(input.slice(0, 4000))}${ev.outputPreview ? "\n\n── 结果 ──\n" + esc(ev.outputPreview) : ""}</pre></details>`;
    };
    const steps = stepsOf(t);
    let tools = "";
    if (steps.length) {
      // 过程：按时间顺序列出思考和工具调用；工具调用可以展开看输入和结果
      const byId = Object.fromEntries(a.toolEvents.filter(ev => ev.id).map(ev => [ev.id, ev]));
      const items = steps.map((s, i) => {
        if (s.kind === "tool" && byId[s.id]) {
          const live = !s.end && running(t);
          const icon = live ? '<span class="bb-spinner"></span>' : s.isError ? "✕" : "✓";
          return `<div class="bb-step tool rich${live ? " live" : ""}${s.isError ? " err" : ""}">${toolDetails(byId[s.id], `<span class="bb-step-icon">${icon}</span>`)}</div>`;
        }
        return stepHtml(t, s, i, "drawer");
      }).join("");
      const total = a.startedAt ? ` · ${running(t) ? "已用" : "共"} ${elapsedHtml(a.startedAt, running(t) ? null : a.endedAt)}` : "";
      tools = `<p class="bb-section-title">过程（${steps.length} 步${total}）</p><div class="bb-activity drawer">${items}</div>`;
    } else if (a.toolEvents.length) {
      tools = `<p class="bb-section-title">工具调用（${a.toolEvents.length}）</p><div class="bb-tools">${a.toolEvents.map(ev => toolDetails(ev)).join("")}</div>`;
    }
    const status = running(t) ? `<div class="bb-statusline live"><span class="bb-spinner"></span><span class="bb-status-text">${esc(a.statusLine || "等待 Claude Code…")}</span>${elapsedHtml(a.startedAt, null)}</div>` : "";
    const error = a.status === "error" ? `<p class="bb-error-text">${esc(a.error)}</p>` : "";
    body.innerHTML = `${questions}${files}${tools}${status}${permissionsHtml(t)}${error}
      <div class="bb-answer-full bb-md${running(t) ? " streaming" : ""}">${core.markdownToHtml(a.markdown) || (running(t) ? "" : "<p><i>（没有文字回答）</i></p>")}</div>`;
    body.querySelectorAll("details[data-key]").forEach(d => d.addEventListener("toggle", () => {
      if (d.open) S.drawerOpen.add(d.dataset.key); else S.drawerOpen.delete(d.dataset.key);
    }));
    const focusMark = markChildQuotes(body.querySelector(".bb-answer-full"), t.id, "full", focusChild);
    if (focusMark) focusMark.scrollIntoView({ block: "center" });
    else if (fresh) body.scrollTop = 0;
    else body.scrollTop = atBottom && running(t) ? body.scrollHeight : scroll;
  }

  /** 回显：在一条回答（kind="full"）或它的问题小框（kind="question"）里标出被划词追问过的原文。
      返回 focusChild 对应的 mark。 */
  function markChildQuotes(container, turnId, kind, focusChild) {
    if (!container || !S.tree) return null;
    let focusMark = null;
    for (const cid of S.tree.order) {
      for (const q of core.questionsOf(S.tree.turns[cid])) {
        if (q.sourceId !== turnId || !q.quote || !q.quote.text) continue;
        const src = q.quote.source || "full";
        if ((kind === "question") !== (src === "question") || src === "summary") continue;
        const mark = markText(container, q.quote.text, cid);
        if (cid === focusChild && mark) focusMark = mark;
      }
    }
    return focusMark;
  }

  /** 在渲染后的 DOM 中找到一段文字（可跨元素），用 <mark> 包起来。返回第一个 mark。 */
  function markText(container, text, childId) {
    const norm = s => s.replace(/\s+/g, "");
    const target = norm(text);
    if (!target) return null;
    const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT, {
      acceptNode: n => (n.parentElement && n.parentElement.closest(".bb-quote, .bb-tag, .bb-num") ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT),
    });
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
      mark.className = "bb-quote-mark"; mark.dataset.child = childId; mark.title = `由此提问：#${num(childId)}（点击定位）`;
      try { range.surroundContents(mark); first = mark; } catch (_e) { /* 跨越元素边界的片段跳过 */ }
    }
    return first;
  }

  /* ---------- 完整版 / 合并子树 ----------
     对一张卡片：把它下面的划词子树自底向上整合回来（深的先合并，父卡片用子卡片合并后的内容）。
     预览后可以导出 Markdown，也可以「合并进第 n 轮」——卡片显示合并版，并入的卡片收起。
     默认零 token：分类用规则，修正附在段后。两个按钮才会调用模型：一次调用给全部分支分类；
     逐层改写含修正的段落（先改最深的一层，再改上一层，有缓存）。分类调整和改写结果记在浏览器里。 */
  const FV = { turnId: null, decisions: {}, rewrites: {}, busy: false, result: null };
  const fvKey = () => `branchboard.fv.${S.tree.id}.${FV.turnId}`;
  const estTokens = chars => { const k = Math.max(0.1, chars / 1.6 / 1000); return k < 1 ? `${Math.round(k * 1000)}` : `${k.toFixed(1)}k`; };
  const hasBranches = id => core.fullVersionItems(S.tree, id).length > 0;
  const fvOpts = () => ({ decisions: FV.decisions, rewrites: FV.rewrites });
  const rewritten = g => !!(FV.rewrites[g.nodeId] && typeof FV.rewrites[g.nodeId][g.block] === "string");

  function openFullVersion(turnId) {
    FV.turnId = turnId; FV.busy = false;
    try {
      const saved = JSON.parse(localStorage.getItem(fvKey()) || "{}");
      FV.decisions = saved.decisions || {};
      // 旧格式 {block: text} 只属于这一张卡片
      const rw = saved.rewrites || {};
      FV.rewrites = Object.values(rw).some(v => typeof v === "string") ? { [turnId]: rw } : rw;
    } catch (_e) { FV.decisions = {}; FV.rewrites = {}; }
    $("bbFvTitle").textContent = `第 ${num(turnId)} 轮 · ${(turn(turnId).question || "").split("\n")[0].slice(0, 60)}`;
    renderFullVersion();
    $("bbFvDialog").showModal();
  }
  function saveFv() {
    try { localStorage.setItem(fvKey(), JSON.stringify({ decisions: FV.decisions, rewrites: FV.rewrites })); } catch (_e) { /* 忽略 */ }
  }

  function renderFullVersion() {
    const t = turn(FV.turnId);
    if (!t) return;
    const result = FV.result = core.buildFullVersion(S.tree, FV.turnId, fvOpts());
    const { stats } = result;
    const levels = new Set(result.items.map(it => it.depth)).size;
    $("bbFvStats").textContent = `${stats.total} 个划词分支${levels > 1 ? `（${levels} 层）` : ""} · 修正 ${stats.correct} · 释义 ${stats.explain} · 延伸 ${stats.extend}`
      + (stats.ignore ? ` · 忽略 ${stats.ignore}` : "") + (stats.rewritten ? ` · 已改写 ${stats.rewritten} 段` : "");
    // 分支列表：按所在卡片分组、按深度缩进；每项可以改分类
    let lastNode = null;
    $("bbFvItems").innerHTML = result.items.map(it => {
      const child = turn(it.childId);
      const opts = Object.entries(core.FV_CATEGORIES).map(([v, label]) => `<option value="${v}"${it.category === v ? " selected" : ""}>${label}</option>`).join("");
      const where = it.source === "question" ? "划在提问上" : it.source === "summary" ? "划在摘要上" : it.anchor ? "" : "原文里没找到这段";
      const head = it.nodeId !== lastNode ? `<p class="bb-fv-group" style="margin-left:${it.depth * 14}px">划在 #${it.nodeN} 上${it.depth ? `（第 ${it.depth + 1} 层）` : ""}</p>` : "";
      lastNode = it.nodeId;
      return `${head}<div class="bb-fv-item cat-${it.category}" style="margin-left:${it.depth * 14}px">
        <div class="bb-fv-item-head"><button class="bb-link" type="button" data-fv-goto="${it.childId}">#${it.n}</button>
          <select data-fv-key="${esc(it.key)}"${it.done ? "" : " disabled"} aria-label="分类">${opts}</select>
          ${where ? `<span class="bb-fv-note">${where}</span>` : ""}${!it.done ? '<span class="bb-fv-note">回答未完成</span>' : ""}</div>
        <div class="bb-quote" style="--src:${core.sourceColor(S.tree, it.nodeId)}">${esc(it.quote)}</div>
        <div class="bb-fv-q">${esc(it.question || "（就这段内容展开）")}</div>
        <div class="bb-fv-gist">${esc(child ? core.branchGist(child, 120) : "")}</div></div>`;
    }).join("") || '<p class="bb-hint">这条回答上还没有划词分支。</p>';
    $("bbFvPreview").innerHTML = footnoteHtml(core.markdownToHtml(result.markdown));
    // 两个模型按钮：写明大约会花多少 token
    const items = result.items.filter(it => it.done);
    const answerLen = (id, cap) => Math.min(cap, ((turn(id) || {}).answer || {}).markdown ? turn(id).answer.markdown.length : 0);
    const classifyChars = 500 + items.reduce((n, it) => n + it.quote.length + it.question.length + answerLen(it.childId, 600) + 40, 0);
    const allTargets = core.rewriteTargets(S.tree, FV.turnId, fvOpts());
    const targets = allTargets.filter(g => !rewritten(g));
    const rewriteChars = targets.reduce((n, g) => n + 400 + g.paragraph.length * 2.5 + g.items.reduce((m, i) => m + Math.min(6000, i.childText.length) + 80, 0), 0);
    const cb = $("bbFvClassify"), rb = $("bbFvRewrite");
    cb.textContent = `用模型分类（约 ${estTokens(classifyChars)} token）`;
    cb.disabled = FV.busy || !items.length;
    const doneCorrections = Object.values(FV.rewrites).reduce((n, m) => n + Object.keys(m || {}).length, 0);
    rb.textContent = targets.length ? `用模型改写修正处（${targets.length} 段${new Set(targets.map(g => g.depth)).size > 1 ? "，逐层" : ""}，约 ${estTokens(rewriteChars)} token）`
      : doneCorrections ? "撤销模型改写" : "用模型改写修正处";
    rb.disabled = FV.busy || (!targets.length && !doneCorrections);
    rb.dataset.mode = targets.length ? "rewrite" : "undo";
    // 合并：把合并版写回这张卡片，并入的卡片收起
    const mv = core.mergedVersion(S.tree, FV.turnId, fvOpts());
    const mb = $("bbFvMerge");
    mb.textContent = t.merged ? `更新合并（收起 ${mv.absorbed.length} 张）` : `合并进第 ${num(FV.turnId)} 轮（收起 ${mv.absorbed.length} 张卡片）`;
    mb.disabled = FV.busy || !mv.absorbed.length;
    mb.title = mv.absorbed.length ? "卡片改为显示合并版；并进来的卡片收起，可随时展开或取消合并" : "这张卡片下面没有可以并进来的分支";
    $("bbFvUnmerge").classList.toggle("hidden", !t.merged);
  }

  async function fvClassify() {
    const items = FV.result.items.filter(it => it.done).map(it => ({ key: it.key, nodeId: it.nodeId, childId: it.childId, qi: it.qi }));
    FV.busy = true; renderFullVersion(); $("bbFvClassify").textContent = "正在分类…";
    try {
      const { categories } = await api("POST", `/trees/${S.tree.id}/turns/${FV.turnId}/full-version/classify`, { items });
      Object.assign(FV.decisions, categories);
      saveFv();
      toast(`模型给 ${Object.keys(categories).length} 个分支分了类，可以再手动调整。`);
    } catch (err) { toast(err.message); }
    finally { FV.busy = false; renderFullVersion(); }
  }

  /** 逐层改写：每次只发当前最深一层的段落；改完重新计算，上一层拿到的是已经吸收了下层修正的内容。 */
  async function fvRewrite() {
    if ($("bbFvRewrite").dataset.mode === "undo") { FV.rewrites = {}; saveFv(); renderFullVersion(); return; }
    FV.busy = true;
    let called = 0, cached = 0;
    try {
      for (let round = 0; round < 8; round++) {
        const pending = core.rewriteTargets(S.tree, FV.turnId, fvOpts()).filter(g => !rewritten(g));
        if (!pending.length) break;
        const depth = Math.max(...pending.map(g => g.depth));
        const level = pending.filter(g => g.depth === depth).slice(0, 12);
        renderFullVersion();
        $("bbFvRewrite").textContent = `正在改写 ${level.length} 段（第 ${depth + 1} 层）…`;
        const groups = level.map(g => ({ nodeId: g.nodeId, block: g.block, paragraph: g.paragraph,
          items: g.items.map(i => ({ childId: i.childId, qi: i.qi, childText: i.childText })) }));
        const res = await api("POST", `/trees/${S.tree.id}/turns/${FV.turnId}/full-version/rewrite`, { groups });
        for (const r of res.rewrites) (FV.rewrites[r.nodeId] = FV.rewrites[r.nodeId] || {})[r.block] = r.text;
        called += res.called; cached += res.cached;
        saveFv();
      }
      toast(called ? `改写了 ${called} 段${cached ? `，另有 ${cached} 段用了缓存` : ""}。` : "全部用了缓存，没有花 token。");
    } catch (err) { toast(err.message); }
    finally { FV.busy = false; renderFullVersion(); }
  }

  async function fvMerge() {
    const mv = core.mergedVersion(S.tree, FV.turnId, fvOpts());
    if (!mv.absorbed.length) return;
    FV.busy = true; renderFullVersion();
    try {
      const { turn: updated } = await api("POST", `/trees/${S.tree.id}/turns/${FV.turnId}/merge`,
        { markdown: mv.markdown, absorbed: mv.absorbed, stats: mv.stats });
      delete S.showOriginal[FV.turnId]; delete S.expandedMerge[FV.turnId];
      upsertTurn(updated);
      $("bbFvDialog").close();
      rerenderAll();
      select(FV.turnId); revealTurn(FV.turnId);
      toast(`已合并：第 ${num(FV.turnId)} 轮现在显示合并版，收起了 ${mv.absorbed.length} 张卡片。`);
    } catch (err) { toast(err.message); }
    finally { FV.busy = false; if ($("bbFvDialog").open) renderFullVersion(); }
  }

  async function unmerge(turnId) {
    try {
      const { turn: updated } = await api("DELETE", `/trees/${S.tree.id}/turns/${turnId}/merge`);
      upsertTurn(updated);
      rerenderAll();
      toast(`已取消合并，第 ${num(turnId)} 轮恢复原样。`);
    } catch (err) { toast(err.message); }
  }
  $("bbFvMerge").addEventListener("click", fvMerge);
  $("bbFvUnmerge").addEventListener("click", async () => { await unmerge(FV.turnId); $("bbFvDialog").close(); });

  $("bbFvItems").addEventListener("change", e => {
    const sel = e.target.closest("select[data-fv-key]");
    if (!sel) return;
    FV.decisions[sel.dataset.fvKey] = sel.value;
    saveFv(); renderFullVersion();
  });
  $("bbFvItems").addEventListener("click", e => {
    const btn = e.target.closest("[data-fv-goto]");
    if (!btn) return;
    $("bbFvDialog").close(); select(btn.dataset.fvGoto); revealTurn(btn.dataset.fvGoto);
  });
  $("bbFvClassify").addEventListener("click", fvClassify);
  $("bbFvRewrite").addEventListener("click", fvRewrite);
  $("bbFvClose").addEventListener("click", () => $("bbFvDialog").close());
  $("bbFvCopy").addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(FV.result.markdown); toast("已复制完整版 Markdown"); } catch (_e) { toast("复制失败，请改用导出。"); }
  });
  $("bbFvExport").addEventListener("click", () => {
    if (FV.result) download(`${fileStem()}-第${num(FV.turnId)}轮-完整版.md`, FV.result.markdown, "text/markdown;charset=utf-8");
  });
  $("bbDrawerFullVersion").addEventListener("click", () => S.drawerId && openFullVersion(S.drawerId));

  /* ---------- 新建 / 设置 / 菜单 / 导出 ---------- */
  const keepDrafts = () => S.drafts.length && !confirm(`切换聊天会丢掉 ${S.drafts.length} 个未发送的问题小框，继续？`);
  $("bbNewTree").addEventListener("click", () => { if (!keepDrafts()) newChat(); });
  $("bbSideSearch").addEventListener("input", e => { S.filter = e.target.value; renderList(); });

  /** 改名 / 移除 / 删除：顶栏「⋯」作用于当前聊天，左栏每一项的「⋯」作用于那一项。 */
  async function treeAction(act, id) {
    const meta = S.trees.find(t => t.id === id) || (S.tree && S.tree.id === id ? S.tree : null);
    if (!meta) return;
    const current = !!(S.tree && S.tree.id === id);
    if (act === "rename") {
      const title = prompt("聊天标题（手动改名后不再自动命名）", meta.title);
      if (!title || !title.trim() || title.trim() === meta.title) return;
      const { tree } = await api("PATCH", `/trees/${id}`, { title });
      meta.title = tree.title;
      if (current) { S.tree.title = tree.title; showTitle(true); } else renderList();
    } else if (act === "forget" || act === "delete") {
      const msg = act === "delete" ? `删除「${meta.title}」？对话树文件会被删除，无法恢复（Claude Code 自己的会话记录不受影响）。` : `从列表中移除「${meta.title}」？JSON 文件保留在工作目录的 .branchboard/ 里。`;
      if (!confirm(msg)) return;
      await api("DELETE", `/trees/${id}${act === "forget" ? "?keepFile=1" : ""}`);
      if (current) newChat();
      await refreshList();
    }
  }
  const sideMenu = $("bbSideMenu");
  function closeSideMenu() {
    sideMenu.classList.add("hidden"); S.menuTree = null;
    els.list.querySelectorAll(".menu-open").forEach(el => el.classList.remove("menu-open"));
  }
  els.list.addEventListener("click", e => {
    const item = e.target.closest(".bb-side-item");
    if (!item) return;
    const id = item.dataset.id;
    if (e.target.closest(".bb-side-more")) {
      e.stopPropagation();
      const again = S.menuTree === id;
      closeSideMenu();
      if (again) return;
      S.menuTree = id; item.classList.add("menu-open");
      sideMenu.classList.remove("hidden");
      const r = e.target.closest(".bb-side-more").getBoundingClientRect();
      sideMenu.style.left = Math.min(r.left, window.innerWidth - sideMenu.offsetWidth - 8) + "px";
      sideMenu.style.top = Math.min(r.bottom + 4, window.innerHeight - sideMenu.offsetHeight - 8) + "px";
      return;
    }
    if (S.tree && S.tree.id === id) return;
    if (keepDrafts()) return;
    openTree(id);
    if (window.innerWidth <= 860) setSide(true);  // 窄屏下侧栏盖在画布上，选完就收起
  });
  sideMenu.addEventListener("click", async e => {
    const act = e.target.closest("button") && e.target.closest("button").dataset.act;
    const id = S.menuTree;
    closeSideMenu();
    if (!act || !id) return;
    try { await treeAction(act, id); } catch (err) { toast(err.message); }
  });
  els.list.addEventListener("scroll", closeSideMenu);
  $("bbTitle").addEventListener("click", async () => {
    if (!S.tree) return;
    try { await treeAction("rename", S.tree.id); } catch (err) { toast(err.message); }
  });

  function setSide(collapsed, remember) {
    $("bbShell").classList.toggle("side-collapsed", collapsed);
    if (remember) store(SIDE_KEY, collapsed ? "1" : "0");
    scheduleLayout();
  }
  $("bbSideHide").addEventListener("click", () => setSide(true, true));
  $("bbSideShow").addEventListener("click", () => setSide(false, true));
  setSide(stored(SIDE_KEY) === "1" || (stored(SIDE_KEY) == null && window.innerWidth <= 860));
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
    closeSideMenu();
    if (!e.target.closest("#bbSettings")) $("bbSettings").classList.remove("pop");
  });
  $("bbMoreMenu").addEventListener("click", async e => {
    const act = e.target.closest("button") && e.target.closest("button").dataset.act;
    if (!act || !S.tree) return;
    try {
      if (act === "export-md") $("bbExportMd").click();
      else if (act === "export-html") $("bbExportHtml").click();
      else if (act === "settings") { e.stopPropagation(); $("bbMoreMenu").classList.add("hidden"); $("bbSettings").classList.add("pop"); }
      else await treeAction(act, S.tree.id);
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
      else select(null);
    } else if (!typing && !e.ctrlKey && !e.metaKey && !e.altKey) {
      if (e.key === "f") fitAll();
      else if (e.key === "Enter" && S.drafts.length) { e.preventDefault(); sendDrafts(); }
    }
  });
  window.addEventListener("beforeunload", e => { if (S.drafts.some(d => d.text.trim())) { e.preventDefault(); e.returnValue = ""; } });
  window.addEventListener("resize", scheduleLayout);

  /* ---------- 启动 ---------- */
  applyTransform();
  setMode(S.mode);
  (async () => {
    try { await loadConfig(); } catch (err) { showStatus(err.message); }
    try { await loadTrees(new URLSearchParams(location.search).get("tree")); } catch (err) { toast(err.message); newChat(); }
  })();
})();
