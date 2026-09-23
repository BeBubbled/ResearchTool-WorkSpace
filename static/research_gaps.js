(() => {
  "use strict";

  const API = "/api/research-gaps";
  const state = {
    config: { llmPresets: [] },
    projects: [], project: null, papers: [], gaps: [], review: [], graph: { nodes: [], edges: [] },
    readerLibrary: [], tab: "gaps", selectedPapers: new Set(), activeJob: null, pollTimer: null,
  };
  const dom = {};
  const ids = [
    "emptyState", "projectWorkspace", "projectSelect", "newProjectButton", "emptyCreateButton",
    "projectName", "projectTopic", "paperCount", "gapCount", "reviewCount", "editProjectButton",
    "exportProjectLink", "deleteProjectButton", "semanticStatusText", "viewTitle", "llmPreset",
    "importPaperButton", "analyzeButton", "jobPanel", "jobLabel", "jobProgress", "jobProgressBar",
    "jobLog", "gapTabCount", "paperTabCount", "reviewTabCount", "gapSearch", "gapStatusFilter",
    "gapSort", "gapList", "paperSearch", "selectAllPapers", "paperRows", "reviewSearch", "reviewList",
    "graphCanvas", "fitGraph", "drawerBackdrop", "detailDrawer", "drawerEyebrow", "drawerTitle",
    "drawerBody", "closeDrawer", "projectDialog", "projectForm", "projectDialogTitle", "projectEditId",
    "projectNameInput", "projectTopicInput", "projectQueryInput", "projectFormError", "importDialog",
    "searchImportPanel", "localImportPanel", "paperSearchForm", "paperQuery", "paperSearchStatus",
    "paperSearchResults", "localLibraryResults", "reviewDialog", "reviewForm", "reviewItemId",
    "reviewItemType", "reviewTitleField", "reviewTitleInput", "reviewDetailField", "reviewDetailInput",
    "reviewRelationField", "reviewRelationInput", "reviewEvidence", "toast",
  ];

  function cacheDom() { ids.forEach(id => { dom[id] = document.getElementById(id); }); }

  async function api(path, options = {}) {
    const requestOptions = { ...options, headers: { ...(options.headers || {}) } };
    if (options.body && typeof options.body !== "string") {
      requestOptions.headers["Content-Type"] = "application/json";
      requestOptions.body = JSON.stringify(options.body);
    }
    const response = await fetch(`${API}${path}`, requestOptions);
    let payload = {};
    try { payload = await response.json(); } catch (_) { /* Response may be empty. */ }
    if (!response.ok) throw new Error(payload.error || `请求失败 (${response.status})`);
    return payload;
  }

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>'"]/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[char]));
  }
  function formatDate(value) { return value ? new Intl.DateTimeFormat("zh-CN", { year: "numeric", month: "short", day: "numeric" }).format(new Date(Number(value) * 1000)) : "—"; }
  function clampConfidence(value) { return `${Math.round(Math.max(0, Math.min(1, Number(value) || 0)) * 100)}%`; }
  function statusMeta(status) {
    return {
      OPEN: ["开放", "open"], PARTIALLY_ADDRESSED: ["部分解决", "partial"], MATURE: ["较成熟", "mature"],
    }[status] || [status || "未知", "open"];
  }
  function relationLabel(value) {
    return { proposes: "提出", addresses: "尝试解决", partially_solves: "部分解决", solves: "解决", fails_on: "仍然失败" }[value] || value;
  }
  function kindLabel(value) {
    return { problem: "问题", task: "任务", method: "方法", dataset: "数据集", metric: "指标", contribution: "贡献", limitation: "局限", failure_condition: "失败条件", gap: "研究空白" }[value] || value;
  }
  let toastTimer;
  function toast(message, error = false) {
    clearTimeout(toastTimer);
    dom.toast.textContent = message;
    dom.toast.classList.toggle("error", error);
    dom.toast.classList.remove("hidden");
    toastTimer = setTimeout(() => dom.toast.classList.add("hidden"), 4200);
  }

  function showEmpty(message, detail = "") {
    return `<div class="empty-list"><strong>${escapeHtml(message)}</strong>${detail ? `<span>${escapeHtml(detail)}</span>` : ""}</div>`;
  }

  async function init() {
    cacheDom();
    bindEvents();
    try {
      const [config, projects, library] = await Promise.all([
        api("/config"), api("/projects"), fetch("/api/reader/library").then(response => response.ok ? response.json() : { documents: [] }),
      ]);
      state.config = config;
      state.projects = projects.projects || [];
      state.readerLibrary = library.documents || [];
      renderConfig();
      renderProjectSelect();
      const remembered = localStorage.getItem("researchGapProjectId");
      const initial = state.projects.find(item => item.id === remembered) || state.projects[0];
      if (initial) await selectProject(initial.id);
      else renderNoProjects();
    } catch (error) {
      toast(error.message, true);
      renderNoProjects();
    }
  }

  function renderConfig() {
    const presets = state.config.llmPresets || [];
    dom.llmPreset.innerHTML = presets.length
      ? presets.map(item => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.name)} · ${escapeHtml(item.model)}</option>`).join("")
      : '<option value="">未配置 LLM</option>';
    dom.analyzeButton.disabled = !presets.length;
    dom.semanticStatusText.textContent = state.config.semanticScholarApiKeyConfigured ? "已配置独立 API Key" : "公开共享限流";
  }

  function renderProjectSelect() {
    dom.projectSelect.innerHTML = state.projects.length
      ? state.projects.map(item => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.name)}</option>`).join("")
      : '<option value="">尚无项目</option>';
    dom.projectSelect.disabled = !state.projects.length;
  }

  function renderNoProjects() {
    state.project = null;
    dom.projectWorkspace.classList.add("hidden");
    dom.emptyState.classList.remove("hidden");
  }

  async function selectProject(projectId) {
    clearTimeout(state.pollTimer);
    state.activeJob = null;
    const payload = await api(`/projects/${encodeURIComponent(projectId)}`);
    state.project = payload.project;
    dom.projectSelect.value = projectId;
    localStorage.setItem("researchGapProjectId", projectId);
    dom.emptyState.classList.add("hidden");
    dom.projectWorkspace.classList.remove("hidden");
    await refreshProject();
  }

  async function refreshProject() {
    if (!state.project) return;
    const id = encodeURIComponent(state.project.id);
    const [projectPayload, papers, gaps, review, graph] = await Promise.all([
      api(`/projects/${id}`), api(`/projects/${id}/papers`), api(`/projects/${id}/gaps`),
      api(`/projects/${id}/review-queue`), api(`/projects/${id}/graph`),
    ]);
    state.project = projectPayload.project;
    state.papers = papers.papers || [];
    state.gaps = gaps.gaps || [];
    state.review = review.items || [];
    state.graph = graph;
    state.selectedPapers = new Set([...state.selectedPapers].filter(paperId => state.papers.some(item => item.id === paperId)));
    renderWorkspace();
  }

  function renderWorkspace() {
    const project = state.project;
    dom.projectName.textContent = project.name;
    dom.projectTopic.textContent = project.topic;
    dom.paperCount.textContent = state.papers.length;
    dom.gapCount.textContent = state.gaps.length;
    dom.reviewCount.textContent = state.review.length;
    dom.paperTabCount.textContent = state.papers.length;
    dom.gapTabCount.textContent = state.gaps.length;
    dom.reviewTabCount.textContent = state.review.length;
    dom.exportProjectLink.href = `${API}/projects/${encodeURIComponent(project.id)}/export`;
    renderGaps(); renderPapers(); renderReview(); renderGraph();
  }

  function renderGaps() {
    const query = dom.gapSearch.value.trim().toLocaleLowerCase();
    const status = dom.gapStatusFilter.value;
    const sort = dom.gapSort.value;
    let gaps = state.gaps.filter(item => (!status || item.status === status) && (!query || JSON.stringify(item).toLocaleLowerCase().includes(query)));
    if (sort === "confidence") gaps.sort((a, b) => b.confidence - a.confidence);
    else if (sort === "evidence") gaps.sort((a, b) => b.evidenceCount - a.evidenceCount);
    else if (sort === "title") gaps.sort((a, b) => a.canonicalTitle.localeCompare(b.canonicalTitle, "zh-CN"));
    else gaps.sort((a, b) => b.updatedAt - a.updatedAt);
    if (!gaps.length) {
      dom.gapList.innerHTML = showEmpty(state.papers.length ? "尚未发现符合条件的研究空白" : "先添加论文，再开始分析", state.papers.length ? "可调整筛选条件或分析更多论文。" : "支持 DOI、arXiv、标题检索和本地阅读库。 ");
      return;
    }
    dom.gapList.innerHTML = gaps.map(item => {
      const [label, klass] = statusMeta(item.status);
      return `<article class="gap-row" data-gap-id="${escapeHtml(item.id)}">
        <div class="gap-row-main" tabindex="0" role="button">
          <div class="review-heading"><span class="status-badge ${klass}">${label}</span><h3>${escapeHtml(item.canonicalTitle)}</h3></div>
          <p>${escapeHtml(item.description || item.failureCondition || "尚无补充说明")}</p>
          <div class="tagline">${item.task ? `<span class="tag">${escapeHtml(item.task)}</span>` : ""}${item.domain ? `<span class="tag">${escapeHtml(item.domain)}</span>` : ""}${item.provisional ? '<span class="tag provisional">临时判断 · 待审核</span>' : ""}</div>
        </div>
        <div class="gap-metrics"><div><strong>${item.evidenceCount}</strong><span>有效证据</span></div><div><strong>${clampConfidence(item.confidence)}</strong><span>平均置信度</span></div></div>
      </article>`;
    }).join("");
  }

  function renderPapers() {
    const query = dom.paperSearch.value.trim().toLocaleLowerCase();
    const papers = state.papers.filter(item => !query || JSON.stringify(item).toLocaleLowerCase().includes(query));
    if (!papers.length) {
      dom.paperRows.innerHTML = `<tr><td colspan="6">${showEmpty(state.papers.length ? "没有匹配的论文" : "项目中还没有论文")}</td></tr>`;
      return;
    }
    dom.paperRows.innerHTML = papers.map(item => `<tr data-paper-id="${escapeHtml(item.id)}">
      <td class="check-cell"><input type="checkbox" data-paper-select="${escapeHtml(item.id)}" ${state.selectedPapers.has(item.id) ? "checked" : ""} aria-label="选择 ${escapeHtml(item.title)}"></td>
      <td class="paper-title-cell"><strong>${escapeHtml(item.title)}</strong><span>${escapeHtml([item.year, item.venue, (item.authors || []).slice(0, 3).join(", ")].filter(Boolean).join(" · ") || "本地文档")}</span></td>
      <td><span class="evidence-level ${item.evidenceLevel}">${item.evidenceLevel === "fulltext" ? "全文" : "摘要/弱证据"}</span>${item.error ? `<span class="paper-warning" title="${escapeHtml(item.error)}">需核验</span>` : ""}</td>
      <td>${item.contributionCount || 0} 贡献 · ${item.limitationCount || 0} 局限</td>
      <td>${item.status === "analyzed" ? `已分析<br><small>${formatDate(item.analyzedAt)}</small>` : "待分析"}</td>
      <td><div class="row-actions">${item.url ? `<a href="${escapeHtml(item.url)}" target="_blank" rel="noreferrer" aria-label="打开论文">原文</a>` : ""}<button type="button" data-delete-paper="${escapeHtml(item.id)}">移除</button></div></td>
    </tr>`).join("");
  }

  function renderReview() {
    const query = dom.reviewSearch.value.trim().toLocaleLowerCase();
    const items = state.review.filter(item => !query || JSON.stringify(item).toLocaleLowerCase().includes(query));
    if (!items.length) {
      dom.reviewList.innerHTML = showEmpty("没有待审核内容", "新的 AI 抽取和归并结果会出现在这里。");
      return;
    }
    dom.reviewList.innerHTML = items.map(item => `<article class="review-item" data-review-id="${escapeHtml(item.id)}">
      <div>
        <div class="review-heading"><span class="tag">${item.itemType === "relation" ? relationLabel(item.relationType) : kindLabel(item.kind)}</span><h3>${escapeHtml(item.title)}</h3></div>
        <p class="review-meta">${escapeHtml(item.paperTitle)} · ${item.evidenceLevel === "fulltext" ? "全文" : "摘要证据"} · ${escapeHtml(item.locator || "未定位")} · 置信度 ${clampConfidence(item.confidence)}</p>
        ${item.detail ? `<p class="review-detail">${escapeHtml(item.detail)}</p>` : ""}
        <blockquote class="evidence-quote">${escapeHtml(item.evidence || "无逐字证据")}</blockquote>
      </div>
      <div class="review-actions"><button class="accept" type="button" data-review-action="accepted">接受</button><button type="button" data-review-action="edit">编辑</button><button class="reject" type="button" data-review-action="rejected">驳回</button></div>
    </article>`).join("");
  }

  function visibleGraphTypes() {
    return new Set([...document.querySelectorAll("[data-node-filter]:checked")].map(input => input.dataset.nodeFilter));
  }

  function renderGraph() {
    if (state.tab !== "graph") return;
    const visible = visibleGraphTypes();
    const normalizeType = type => type === "failure_condition" ? "limitation" : type;
    const nodes = (state.graph.nodes || []).filter(item => visible.has(normalizeType(item.type)));
    const nodeIds = new Set(nodes.map(item => item.id));
    const edges = (state.graph.edges || []).filter(item => nodeIds.has(item.source) && nodeIds.has(item.target));
    if (!nodes.length) { dom.graphCanvas.innerHTML = showEmpty("没有可显示的图谱节点", "分析论文后会生成证据关系。"); return; }
    const columns = ["paper", "contribution", "limitation", "gap"];
    const grouped = Object.fromEntries(columns.map(type => [type, nodes.filter(item => normalizeType(item.type) === type)]));
    const x = { paper: 40, contribution: 340, limitation: 640, gap: 940 };
    const positions = new Map();
    let maxRows = 1;
    columns.forEach(type => {
      maxRows = Math.max(maxRows, grouped[type].length);
      grouped[type].forEach((item, index) => positions.set(item.id, { x: x[type], y: 60 + index * 92, type }));
    });
    const width = 1240, height = Math.max(570, 120 + maxRows * 92);
    const defs = '<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="#9aa8b6"></path></marker></defs>';
    const edgeSvg = edges.map(edge => {
      const start = positions.get(edge.source), end = positions.get(edge.target);
      if (!start || !end) return "";
      const startX = start.x + 230, startY = start.y + 29, endX = end.x, endY = end.y + 29;
      const bend = Math.max(40, (endX - startX) / 2);
      return `<path class="graph-edge ${escapeHtml(edge.type)}" d="M${startX},${startY} C${startX + bend},${startY} ${endX - bend},${endY} ${endX},${endY}"><title>${escapeHtml(relationLabel(edge.type))}</title></path>`;
    }).join("");
    const nodeSvg = nodes.map(node => {
      const pos = positions.get(node.id); const label = String(node.label || "");
      const first = label.length > 28 ? `${label.slice(0, 28)}…` : label;
      const second = label.length > 28 ? (label.slice(28, 54) + (label.length > 54 ? "…" : "")) : "";
      return `<g class="graph-node ${escapeHtml(normalizeType(node.type))}" data-graph-node="${escapeHtml(node.id)}" transform="translate(${pos.x},${pos.y})"><rect width="230" height="58" rx="6"></rect><text x="12" y="24"><tspan>${escapeHtml(first)}</tspan>${second ? `<tspan x="12" dy="16">${escapeHtml(second)}</tspan>` : ""}</text></g>`;
    }).join("");
    dom.graphCanvas.innerHTML = `<svg viewBox="0 0 ${width} ${height}" width="${width}" height="${height}" aria-label="研究关系图">${defs}${edgeSvg}${nodeSvg}</svg>`;
  }

  function switchTab(tab) {
    state.tab = tab;
    document.querySelectorAll("[data-tab]").forEach(button => button.classList.toggle("active", button.dataset.tab === tab));
    document.querySelectorAll("[data-view]").forEach(view => view.classList.toggle("active", view.dataset.view === tab));
    dom.viewTitle.textContent = { gaps: "研究空白", papers: "论文", review: "待审核", graph: "关系图" }[tab];
    if (tab === "graph") renderGraph();
  }

  function openProjectDialog(edit = false) {
    dom.projectDialogTitle.textContent = edit ? "编辑研究项目" : "新建研究项目";
    dom.projectEditId.value = edit && state.project ? state.project.id : "";
    dom.projectNameInput.value = edit && state.project ? state.project.name : "";
    dom.projectTopicInput.value = edit && state.project ? state.project.topic : "";
    dom.projectQueryInput.value = edit && state.project ? state.project.query : "";
    dom.projectFormError.classList.add("hidden");
    dom.projectDialog.showModal();
  }

  async function saveProject(event) {
    event.preventDefault();
    const data = { name: dom.projectNameInput.value, topic: dom.projectTopicInput.value, query: dom.projectQueryInput.value };
    const editId = dom.projectEditId.value;
    try {
      const payload = await api(editId ? `/projects/${encodeURIComponent(editId)}` : "/projects", { method: editId ? "PATCH" : "POST", body: data });
      dom.projectDialog.close();
      const listed = await api("/projects");
      state.projects = listed.projects;
      renderProjectSelect();
      await selectProject(payload.project.id);
      toast(editId ? "项目已更新。" : "项目已创建。");
    } catch (error) {
      dom.projectFormError.textContent = error.message;
      dom.projectFormError.classList.remove("hidden");
    }
  }

  function openImportDialog() {
    dom.paperQuery.value = state.project?.query || "";
    dom.paperSearchResults.innerHTML = "";
    renderLocalLibrary();
    dom.importDialog.showModal();
  }

  function renderLocalLibrary() {
    if (!state.readerLibrary.length) {
      dom.localLibraryResults.innerHTML = showEmpty("本地阅读库为空", "先在论文精读工作台导入文档。"); return;
    }
    const imported = new Set(state.papers.map(item => item.readerCacheKey).filter(Boolean));
    dom.localLibraryResults.innerHTML = state.readerLibrary.map(item => `<article class="import-result"><div><strong>${escapeHtml(item.title)}</strong><p>${escapeHtml(item.sourceName)} · ${item.renderKind.toUpperCase()}${item.useOcr ? " · OCR" : ""}</p></div><button class="button quiet" type="button" data-import-reader="${escapeHtml(item.id)}" ${imported.has(item.id) ? "disabled" : ""}>${imported.has(item.id) ? "已添加" : "添加"}</button></article>`).join("");
  }

  async function searchPapers(event) {
    event.preventDefault();
    const query = dom.paperQuery.value.trim();
    if (!query) return;
    dom.paperSearchStatus.textContent = "正在检索…";
    dom.paperSearchResults.innerHTML = "";
    try {
      const payload = await api(`/search?q=${encodeURIComponent(query)}`);
      const results = payload.results || [];
      dom.paperSearchStatus.textContent = results.length ? `找到 ${results.length} 条候选` : "没有找到匹配论文。";
      dom.paperSearchResults.innerHTML = results.map(item => `<article class="import-result"><div><strong>${escapeHtml(item.title)}</strong><p>${escapeHtml([item.year, item.venue, (item.authors || []).slice(0, 4).join(", ")].filter(Boolean).join(" · "))}</p><p>${item.pdfUrl ? "可获取开放 PDF" : item.abstract ? "提供摘要" : "仅元数据"} · 引用 ${item.citationCount || 0}</p></div><button class="button quiet" type="button" data-import-s2="${escapeHtml(item.paperId)}">添加</button></article>`).join("");
    } catch (error) { dom.paperSearchStatus.textContent = error.message; toast(error.message, true); }
  }

  async function importPaper(button, body) {
    const previous = button.textContent; button.disabled = true; button.textContent = "导入中…";
    try {
      const payload = await api(`/projects/${encodeURIComponent(state.project.id)}/papers`, { method: "POST", body });
      await refreshProject();
      renderLocalLibrary();
      button.textContent = payload.created ? "已添加" : "已存在";
      toast(payload.created ? "论文已加入项目。" : "这篇论文已经在项目中。 ");
    } catch (error) { button.disabled = false; button.textContent = previous; toast(error.message, true); }
  }

  async function startAnalysis() {
    if (!state.papers.length) { openImportDialog(); return; }
    if (!dom.llmPreset.value) { toast("请先配置一个 LLM 预设。", true); return; }
    dom.analyzeButton.disabled = true;
    try {
      const paperIds = state.selectedPapers.size ? [...state.selectedPapers] : state.papers.map(item => item.id);
      const payload = await api(`/projects/${encodeURIComponent(state.project.id)}/analysis`, { method: "POST", body: { paperIds, presetId: dom.llmPreset.value } });
      state.activeJob = payload.job;
      renderJob(); pollJob();
    } catch (error) { dom.analyzeButton.disabled = false; toast(error.message, true); }
  }

  function renderJob() {
    const job = state.activeJob;
    if (!job) { dom.jobPanel.classList.add("hidden"); return; }
    dom.jobPanel.classList.remove("hidden");
    dom.jobLabel.textContent = job.status === "completed" ? "分析完成" : job.status === "failed" ? "分析失败" : `正在分析 · ${job.stage}`;
    dom.jobProgress.textContent = `${job.completed} / ${job.total}`;
    dom.jobProgressBar.style.width = `${job.total ? Math.round(job.completed / job.total * 100) : 0}%`;
    dom.jobLog.textContent = job.error || job.logs?.at(-1) || "任务已进入队列。";
  }

  async function pollJob() {
    clearTimeout(state.pollTimer);
    if (!state.activeJob) return;
    try {
      const payload = await api(`/jobs/${encodeURIComponent(state.activeJob.id)}`);
      state.activeJob = payload.job;
      renderJob();
      if (["queued", "running"].includes(payload.job.status)) state.pollTimer = setTimeout(pollJob, 1300);
      else {
        dom.analyzeButton.disabled = !(state.config.llmPresets || []).length;
        await refreshProject();
        toast(payload.job.status === "completed" ? "论文分析完成。" : payload.job.error || "分析任务失败。", payload.job.status !== "completed");
      }
    } catch (error) { dom.analyzeButton.disabled = false; toast(error.message, true); }
  }

  async function reviewAction(item, action) {
    if (action === "edit") { openReviewDialog(item); return; }
    const path = item.itemType === "relation" ? `/relations/${encodeURIComponent(item.id)}` : `/facts/${encodeURIComponent(item.id)}`;
    try { await api(path, { method: "PATCH", body: { reviewStatus: action } }); await refreshProject(); toast(action === "accepted" ? "证据已接受。" : "证据已驳回。 "); }
    catch (error) { toast(error.message, true); }
  }

  function openReviewDialog(item) {
    dom.reviewItemId.value = item.id; dom.reviewItemType.value = item.itemType;
    dom.reviewTitleInput.value = item.title || ""; dom.reviewDetailInput.value = item.detail || "";
    dom.reviewEvidence.textContent = item.evidence || "无逐字证据";
    dom.reviewRelationField.classList.toggle("hidden", item.itemType !== "relation");
    dom.reviewTitleField.classList.toggle("hidden", item.itemType === "relation");
    dom.reviewRelationInput.value = item.relationType || "fails_on";
    dom.reviewDialog.showModal();
  }

  async function saveReview(event) {
    event.preventDefault();
    const id = dom.reviewItemId.value, type = dom.reviewItemType.value;
    const path = type === "relation" ? `/relations/${encodeURIComponent(id)}` : `/facts/${encodeURIComponent(id)}`;
    const body = type === "relation"
      ? { relationType: dom.reviewRelationInput.value, rationale: dom.reviewDetailInput.value, reviewStatus: "accepted" }
      : { title: dom.reviewTitleInput.value, detail: dom.reviewDetailInput.value, reviewStatus: "accepted" };
    try { await api(path, { method: "PATCH", body }); dom.reviewDialog.close(); await refreshProject(); toast("修改已保存并接受。 "); }
    catch (error) { toast(error.message, true); }
  }

  function openGapDrawer(gap) {
    const [label, klass] = statusMeta(gap.status);
    dom.drawerEyebrow.textContent = "RESEARCH GAP";
    dom.drawerTitle.textContent = gap.canonicalTitle;
    dom.drawerBody.innerHTML = `<section class="drawer-section"><div class="tagline"><span class="status-badge ${klass}">${label}</span>${gap.provisional ? '<span class="tag provisional">临时判断</span>' : ""}</div><p>${escapeHtml(gap.description || "尚无说明")}</p></section>
      <section class="drawer-section"><h3>规范信息</h3><form id="gapEditForm" class="drawer-edit"><label>标题<input name="canonicalTitle" value="${escapeHtml(gap.canonicalTitle)}"></label><label>说明<textarea name="description" rows="3">${escapeHtml(gap.description)}</textarea></label><label>领域<input name="domain" value="${escapeHtml(gap.domain)}"></label><label>任务<input name="task" value="${escapeHtml(gap.task)}"></label><label>失败条件<textarea name="failureCondition" rows="2">${escapeHtml(gap.failureCondition)}</textarea></label><button class="button primary" type="submit">保存规范信息</button></form></section>
      <section class="drawer-section"><h3>论文证据 · ${gap.relations.length}</h3>${gap.relations.length ? gap.relations.map(rel => `<article class="relation-card"><header><strong>${escapeHtml(rel.paperTitle)}</strong><span class="tag">${relationLabel(rel.relationType)}</span></header><p>${escapeHtml(rel.evidence)}</p><small>${escapeHtml(rel.locator || "未定位")} · ${rel.evidenceLevel === "fulltext" ? "全文" : "摘要"} · ${rel.reviewStatus === "pending" ? "待审核" : rel.reviewStatus === "accepted" ? "已接受" : "已驳回"}</small></article>`).join("") : "<p>暂无关系证据。</p>"}</section>`;
    dom.drawerBody.querySelector("#gapEditForm").addEventListener("submit", async event => {
      event.preventDefault(); const form = new FormData(event.currentTarget); const payload = Object.fromEntries(form.entries());
      try { await api(`/gaps/${encodeURIComponent(gap.id)}`, { method: "PATCH", body: payload }); await refreshProject(); closeDrawer(); toast("研究空白已更新。 "); }
      catch (error) { toast(error.message, true); }
    });
    openDrawer();
  }

  function openPaperDrawer(paper) {
    dom.drawerEyebrow.textContent = "PAPER"; dom.drawerTitle.textContent = paper.title;
    dom.drawerBody.innerHTML = `<section class="drawer-section"><div class="tagline"><span class="evidence-level ${paper.evidenceLevel}">${paper.evidenceLevel === "fulltext" ? "全文证据" : "摘要/弱证据"}</span><span class="tag">${escapeHtml(paper.status)}</span></div><p>${escapeHtml(paper.abstract || paper.error || "没有摘要。")}</p></section><section class="drawer-section"><h3>元数据</h3><p>${escapeHtml((paper.authors || []).join(", ") || "未知作者")}<br>${escapeHtml([paper.year, paper.venue, paper.doi, paper.arxivId].filter(Boolean).join(" · ") || "本地文档")}</p></section>`;
    openDrawer();
  }
  function openFactDrawer(fact) {
    const paper = state.papers.find(item => item.id === fact.paperId);
    dom.drawerEyebrow.textContent = kindLabel(fact.type).toUpperCase(); dom.drawerTitle.textContent = fact.label;
    dom.drawerBody.innerHTML = `<section class="drawer-section"><div class="tagline"><span class="tag">${kindLabel(fact.type)}</span><span class="tag">置信度 ${clampConfidence(fact.confidence)}</span></div><p>${escapeHtml(fact.detail || "尚无补充说明")}</p></section><section class="drawer-section"><h3>原文证据</h3><blockquote class="evidence-quote">${escapeHtml(fact.evidence || "无逐字证据")}</blockquote><p>${escapeHtml(fact.locator || "未定位")} · ${escapeHtml(paper?.title || "未知论文")}</p></section>`;
    openDrawer();
  }
  function openDrawer() { dom.drawerBackdrop.classList.remove("hidden"); dom.detailDrawer.classList.add("open"); dom.detailDrawer.setAttribute("aria-hidden", "false"); }
  function closeDrawer() { dom.drawerBackdrop.classList.add("hidden"); dom.detailDrawer.classList.remove("open"); dom.detailDrawer.setAttribute("aria-hidden", "true"); }

  function bindEvents() {
    dom.newProjectButton.addEventListener("click", () => openProjectDialog(false));
    dom.emptyCreateButton.addEventListener("click", () => openProjectDialog(false));
    dom.editProjectButton.addEventListener("click", () => openProjectDialog(true));
    dom.projectForm.addEventListener("submit", saveProject);
    dom.projectSelect.addEventListener("change", () => selectProject(dom.projectSelect.value).catch(error => toast(error.message, true)));
    dom.importPaperButton.addEventListener("click", openImportDialog);
    dom.paperSearchForm.addEventListener("submit", searchPapers);
    dom.analyzeButton.addEventListener("click", startAnalysis);
    dom.reviewForm.addEventListener("submit", saveReview);
    dom.closeDrawer.addEventListener("click", closeDrawer); dom.drawerBackdrop.addEventListener("click", closeDrawer);
    document.querySelectorAll("[data-close-dialog]").forEach(button => button.addEventListener("click", () => document.getElementById(button.dataset.closeDialog).close()));
    document.querySelectorAll("[data-tab]").forEach(button => button.addEventListener("click", () => switchTab(button.dataset.tab)));
    document.querySelectorAll("[data-import-tab]").forEach(button => button.addEventListener("click", () => {
      document.querySelectorAll("[data-import-tab]").forEach(item => item.classList.toggle("active", item === button));
      dom.searchImportPanel.classList.toggle("active", button.dataset.importTab === "search");
      dom.localImportPanel.classList.toggle("active", button.dataset.importTab === "local");
    }));
    [dom.gapSearch, dom.gapStatusFilter, dom.gapSort].forEach(input => input.addEventListener("input", renderGaps));
    dom.paperSearch.addEventListener("input", renderPapers); dom.reviewSearch.addEventListener("input", renderReview);
    document.querySelectorAll("[data-node-filter]").forEach(input => input.addEventListener("change", renderGraph));
    dom.fitGraph.addEventListener("click", () => { dom.graphCanvas.scrollTo({ left: 0, top: 0, behavior: "smooth" }); });
    dom.selectAllPapers.addEventListener("click", () => {
      const visible = state.papers.filter(item => !dom.paperSearch.value || JSON.stringify(item).toLocaleLowerCase().includes(dom.paperSearch.value.toLocaleLowerCase()));
      const allSelected = visible.every(item => state.selectedPapers.has(item.id));
      visible.forEach(item => allSelected ? state.selectedPapers.delete(item.id) : state.selectedPapers.add(item.id)); renderPapers();
    });
    dom.gapList.addEventListener("click", event => { const row = event.target.closest("[data-gap-id]"); if (row) openGapDrawer(state.gaps.find(item => item.id === row.dataset.gapId)); });
    dom.gapList.addEventListener("keydown", event => { if (["Enter", " "].includes(event.key)) { const row = event.target.closest("[data-gap-id]"); if (row) openGapDrawer(state.gaps.find(item => item.id === row.dataset.gapId)); } });
    dom.paperRows.addEventListener("change", event => { const id = event.target.dataset.paperSelect; if (id) event.target.checked ? state.selectedPapers.add(id) : state.selectedPapers.delete(id); });
    dom.paperRows.addEventListener("click", async event => {
      const id = event.target.dataset.deletePaper;
      if (id) { if (!confirm("从项目中移除这篇论文及其分析结果？")) return; try { await api(`/projects/${encodeURIComponent(state.project.id)}/papers/${encodeURIComponent(id)}`, { method: "DELETE" }); await refreshProject(); toast("论文已移除。 "); } catch (error) { toast(error.message, true); } return; }
      if (event.target.closest("a,button,input")) return;
      const row = event.target.closest("[data-paper-id]"); if (row) openPaperDrawer(state.papers.find(item => item.id === row.dataset.paperId));
    });
    dom.reviewList.addEventListener("click", event => { const action = event.target.dataset.reviewAction; const row = event.target.closest("[data-review-id]"); if (action && row) { const item = state.review.find(entry => entry.id === row.dataset.reviewId); if (item) reviewAction(item, action); } });
    dom.paperSearchResults.addEventListener("click", event => { const button = event.target.closest("[data-import-s2]"); if (button) importPaper(button, { paperId: button.dataset.importS2 }); });
    dom.localLibraryResults.addEventListener("click", event => { const button = event.target.closest("[data-import-reader]"); if (button) importPaper(button, { readerCacheKey: button.dataset.importReader }); });
    dom.graphCanvas.addEventListener("click", event => {
      const node = event.target.closest("[data-graph-node]"); if (!node) return;
      const gap = state.gaps.find(item => item.id === node.dataset.graphNode); if (gap) openGapDrawer(gap);
      else { const paper = state.papers.find(item => item.id === node.dataset.graphNode); if (paper) openPaperDrawer(paper); else { const fact = (state.graph.nodes || []).find(item => item.id === node.dataset.graphNode); if (fact) openFactDrawer(fact); } }
    });
    dom.deleteProjectButton.addEventListener("click", async () => {
      const confirmation = prompt(`请输入项目名称“${state.project.name}”以确认删除：`); if (confirmation === null) return;
      try { await api(`/projects/${encodeURIComponent(state.project.id)}`, { method: "DELETE", body: { confirmName: confirmation } }); localStorage.removeItem("researchGapProjectId"); const payload = await api("/projects"); state.projects = payload.projects; renderProjectSelect(); if (state.projects[0]) await selectProject(state.projects[0].id); else renderNoProjects(); toast("项目已删除。 "); } catch (error) { toast(error.message, true); }
    });
  }

  document.addEventListener("DOMContentLoaded", init);
})();
