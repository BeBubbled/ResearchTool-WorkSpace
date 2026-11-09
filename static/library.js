/* 文献库: source list | sortable paper table | inspector (Zotero / Bookends style).
   GET /api/library lists every paper once; the inspector reads GET /api/library/<key>/workspace. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
  const KIND = {pdf: 'PDF', markdown: 'Markdown', html: 'HTML'};
  const STATUS = {completed: '已完成', succeeded: '已完成', failed: '失败', cancelled: '已取消', canceled: '已取消', running: '进行中', queued: '排队中', interrupted: '已中断'};
  const DAY = 86400;
  const SOURCES = [
    {id: 'all', label: '全部论文', test: () => true},
    {id: 'recent', label: '最近 30 天', test: item => item.updatedAt && Date.now() / 1000 - item.updatedAt < 30 * DAY},
    {id: 'pdf', label: '有原始 PDF', test: item => item.hasPdf},
    {id: 'translated', label: '有整篇译文', test: item => item.hasTranslation},
    {id: 'ocr', label: 'OCR 导入', test: item => item.useOcr},
    {id: 'unused', label: '未用于研究项目', test: item => !(item.usedBy?.gaps || []).length},
    {id: 'reading', label: '最近阅读', test: item => Boolean(readingPosition(item.id))},
  ];
  // The reader remembers where each paper was left (static/reader.js, same key); 最近阅读 lists those.
  const POSITION_PREFIX = 'research-toolkit.reader-position.';
  function readingPosition(key) {
    try { const value = JSON.parse(localStorage.getItem(POSITION_PREFIX + key) || 'null'); return value && value.at ? value : null; } catch { return null; }
  }
  const COLUMNS = [
    {id: 'title', label: '标题', value: item => item.title.toLowerCase()},
    {id: 'kind', label: '格式', value: item => item.sourceType || ''},
    {id: 'projects', label: '研究项目', value: item => (item.usedBy?.gaps || []).length},
    {id: 'updated', label: '更新', value: item => item.updatedAt || 0},
  ];
  const STORE = 'research-toolkit.library.';
  const remember = (key, value) => { try { localStorage.setItem(STORE + key, value); } catch {} };
  const recall = key => { try { return localStorage.getItem(STORE + key); } catch { return null; } };

  let documents = [], visible = [], selected = null, inspectorRequest = 0;
  const checked = new Set();
  let source = recall('source') || 'all';
  let sort = {column: recall('sort') || 'updated', descending: recall('descending') !== '0'};

  const date = value => value ? new Date(value * 1000).toLocaleDateString() : '—';
  const projectSources = () => {
    const projects = new Map();
    documents.forEach(item => (item.usedBy?.gaps || []).forEach(project => projects.set(project.id, project.name)));
    return [...projects].map(([id, name]) => ({id: `project:${id}`, label: name, test: item => (item.usedBy?.gaps || []).some(p => p.id === id)}));
  };
  const allSources = () => [...SOURCES, ...projectSources()];

  function renderSources() {
    const sources = allSources();
    if (!sources.some(item => item.id === source)) source = 'all';
    const button = item => {
      const count = documents.filter(item.test).length;
      return `<button type="button" role="option" class="library-source ${item.id === source ? 'active' : ''}" aria-selected="${item.id === source}" data-source="${escapeHtml(item.id)}"><span>${escapeHtml(item.label)}</span><small>${count}</small></button>`;
    };
    const projects = projectSources();
    $('librarySourceList').innerHTML = `<p class="library-source-group">文献库</p>${SOURCES.map(button).join('')}`
      + (projects.length ? `<p class="library-source-group">研究项目</p>${projects.map(button).join('')}` : '');
    $('librarySourceTitle').textContent = sources.find(item => item.id === source)?.label || '全部论文';
  }

  function filterAndSort() {
    const query = $('librarySearch').value.trim().toLowerCase();
    const test = allSources().find(item => item.id === source)?.test || (() => true);
    const column = source === 'reading' ? {value: item => readingPosition(item.id)?.at || 0} : COLUMNS.find(item => item.id === sort.column) || COLUMNS[3];
    const descending = source === 'reading' ? true : sort.descending;
    visible = documents.filter(item => test(item) && (!query || `${item.title} ${item.sourceName}`.toLowerCase().includes(query)));
    visible.sort((a, b) => {
      const left = column.value(a), right = column.value(b);
      const order = left < right ? -1 : left > right ? 1 : 0;
      return descending ? -order : order;
    });
  }

  function renderTable() {
    filterAndSort();
    $('libraryCount').textContent = `${visible.length} 篇`;
    if (!visible.length) {
      const filtered = $('librarySearch').value.trim() || source !== 'all';
      $('libraryList').innerHTML = `<div class="library-empty"><strong>${filtered ? '没有匹配的论文' : '文献库还是空的'}</strong><p>${filtered ? '换个关键词，或在左侧选择“全部论文”。' : '在精读中导入 PDF、Markdown 或 HTML 后，会自动出现在这里。'}</p>${filtered ? '' : '<a class="button primary" href="/reader">去精读导入</a>'}</div>`;
      return;
    }
    const allChecked = visible.every(item => checked.has(item.id));
    const head = `<th scope="col" class="lib-check"><input type="checkbox" data-check-all aria-label="全选当前列表" ${allChecked ? 'checked' : ''}></th>` + COLUMNS.map(column => {
      const active = sort.column === column.id;
      const arrow = active ? (sort.descending ? ' ↓' : ' ↑') : '';
      return `<th scope="col" aria-sort="${active ? (sort.descending ? 'descending' : 'ascending') : 'none'}"><button type="button" data-sort="${column.id}">${column.label}${arrow}</button></th>`;
    }).join('');
    const rows = visible.map(item => {
      const projects = (item.usedBy?.gaps || []).length;
      const flags = [item.hasPdf ? '<span class="lib-flag" title="有原始 PDF">PDF</span>' : '', item.hasTranslation ? '<span class="lib-flag" title="有整篇译文">译</span>' : '', item.useOcr ? '<span class="lib-flag" title="Mathpix OCR">OCR</span>' : ''].join('');
      const position = readingPosition(item.id);
      return `<tr tabindex="-1" data-key="${escapeHtml(item.id)}" aria-selected="${item.id === selected}" class="${item.id === selected ? 'selected' : ''}">
        <td class="lib-check"><input type="checkbox" data-check="${escapeHtml(item.id)}" aria-label="选择这篇论文" ${checked.has(item.id) ? 'checked' : ''}></td>
        <td class="lib-title">${position && source === 'reading' ? `<em class="lib-progress" title="上次读到的位置">${Math.round((position.ratio || 0) * 100)}%</em>` : ''}<span title="${escapeHtml(item.title)}">${escapeHtml(item.title)}</span>${item.sourceName ? `<small title="${escapeHtml(item.sourceName)}">${escapeHtml(item.sourceName)}</small>` : ''}</td>
        <td>${escapeHtml(KIND[item.sourceType] || item.sourceType || '文档')}${flags}</td>
        <td>${projects || '—'}</td>
        <td>${escapeHtml(date(item.updatedAt))}</td>
      </tr>`;
    }).join('');
    $('libraryList').innerHTML = `<table class="library-table" role="grid" aria-label="论文"><thead><tr>${head}</tr></thead><tbody>${rows}</tbody></table>`;
  }

  function render() { renderSources(); renderTable(); renderBatchBar(); }

  // ---- 批量操作: tick papers, then add them to a research project, queue Three-Pass, or export BibTeX.
  let projectsCache = null;
  async function researchProjects() {
    if (!projectsCache) {
      try {
        const response = await fetch('/api/research-gaps/projects');
        const data = await response.json();
        projectsCache = response.ok ? data.projects || [] : [];
      } catch { projectsCache = []; }
    }
    return projectsCache;
  }

  function batchStatus(text, kind = '') {
    const node = $('libraryBatchStatus');
    if (!node) return;
    node.textContent = text;
    node.className = `hint library-batch-status ${kind}`;
  }

  async function renderBatchBar() {
    const bar = $('libraryBatchBar');
    if (!bar) return;
    [...checked].forEach(key => { if (!find(key)) checked.delete(key); });
    bar.hidden = !checked.size;
    $('libraryBatchCount').textContent = `已选 ${checked.size} 篇`;
    const select = $('libraryBatchProject');
    if (!checked.size || select.dataset.loaded) return;
    select.dataset.loaded = '1';
    const projects = await researchProjects();
    select.innerHTML = projects.length
      ? projects.map(project => `<option value="${escapeHtml(project.id)}">${escapeHtml(project.name)}</option>`).join('')
      : '<option value="">还没有研究项目</option>';
    $('libraryBatchAdd').disabled = !projects.length;
  }

  async function batchAddToProject() {
    const projectId = $('libraryBatchProject').value;
    if (!projectId) return;
    const keys = [...checked];
    let added = 0, existing = 0, failed = 0;
    for (const [index, key] of keys.entries()) {
      batchStatus(`正在加入研究项目 ${index + 1}/${keys.length}…`);
      try {
        const response = await fetch(`/api/research-gaps/projects/${encodeURIComponent(projectId)}/papers`, {
          method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({readerCacheKey: key}),
        });
        if (!response.ok) throw new Error();
        if (response.status === 201) added++; else existing++;
      } catch { failed++; }
    }
    batchStatus(`加入完成：新增 ${added} 篇${existing ? `，${existing} 篇已在项目中` : ''}${failed ? `，${failed} 篇失败` : ''}。`, failed ? 'bad' : 'ok');
    projectsCache = null;
    await load();
  }

  async function batchThreePass() {
    const keys = [...checked];
    const message = `将为 ${keys.length} 篇论文排队 Codex Three-Pass 分析。\n\n使用 ChatGPT / Codex 套餐额度，按全局默认模型和推理强度依次运行。确认继续？`;
    if (!window.confirm(message)) return;
    let queued = 0;
    const failures = [];
    for (const [index, key] of keys.entries()) {
      batchStatus(`正在排队 Three-Pass ${index + 1}/${keys.length}…`);
      try {
        const opened = await fetch(`/api/reader/library/${key}/open`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'});
        const readerDocument = await opened.json();
        if (!opened.ok) throw new Error(readerDocument.error || '无法打开');
        const response = await fetch(`/api/reader/documents/${encodeURIComponent(readerDocument.id)}/analyses`, {
          method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({backend: 'codex'}),
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || '无法开始');
        queued++;
      } catch (error) {
        failures.push(`${find(key)?.title || key}：${error.message}`);
      }
    }
    window.ClientActivity?.refresh();
    batchStatus(`已排队 ${queued} 篇，完成时会通知。${failures.length ? ` ${failures.length} 篇失败：${failures.join('；')}` : ''}`, failures.length ? 'bad' : 'ok');
  }

  async function fetchBibtex(keys) {
    batchStatus(`正在检索 ${keys.length} 篇论文的 BibTeX（Crossref → Semantic Scholar）…`);
    const response = await fetch('/api/library/bibtex', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({keys})});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || '无法生成 BibTeX。');
    const note = data.unverified ? `，其中 ${data.unverified} 篇未在线找到，生成了待补全的 @misc 条目` : '';
    return {bibtex: data.bibtex, note};
  }

  async function copyBibtex(keys) {
    try {
      const {bibtex, note} = await fetchBibtex(keys);
      await navigator.clipboard.writeText(bibtex);
      batchStatus(`已复制 ${keys.length} 条 BibTeX${note}。`, note ? 'bad' : 'ok');
    } catch (error) { batchStatus(error.message, 'bad'); }
  }

  async function exportBibtex(keys) {
    try {
      const {bibtex, note} = await fetchBibtex(keys);
      const link = Object.assign(document.createElement('a'), {href: URL.createObjectURL(new Blob([bibtex], {type: 'application/x-bibtex'})), download: 'references.bib'});
      document.body.append(link); link.click(); link.remove();
      setTimeout(() => URL.revokeObjectURL(link.href), 1000);
      batchStatus(`已导出 references.bib（${keys.length} 条${note}）。`, note ? 'bad' : 'ok');
    } catch (error) { batchStatus(error.message, 'bad'); }
  }

  const find = key => documents.find(item => item.id === key);
  const readerLink = (key, tab = '') => `/reader?library=${encodeURIComponent(key)}${tab ? `&inspector=${tab}` : ''}`;

  function menuItems(item) {
    return [
      {label: '在精读中打开', href: readerLink(item.id)},
      {label: '查看笔记', href: readerLink(item.id, 'notes')},
      {label: '查看 Three-Pass', href: readerLink(item.id, 'three-pass')},
      'separator',
      {label: '加入研究空白…', href: item.links.gaps},
      {label: '用于 ResearchStudio…', href: item.links.studio || '#', disabled: !item.links.studio, title: '只有导入了原始 PDF 的论文才能用于 ResearchStudio'},
      'separator',
      {label: '下载原始 PDF', href: `/api/library/${item.id}/pdf`, download: true, disabled: !item.hasPdf},
      {label: '复制标题', run: () => navigator.clipboard?.writeText(item.title)},
      {label: '复制 BibTeX', run: () => copyBibtex([item.id])},
    ];
  }

  function select(key, {focus = false} = {}) {
    selected = key;
    document.querySelectorAll('.library-table tbody tr').forEach(row => {
      const active = row.dataset.key === key;
      row.classList.toggle('selected', active);
      row.setAttribute('aria-selected', String(active));
      row.tabIndex = active ? 0 : -1;
      if (active && focus) { row.focus(); row.scrollIntoView({block: 'nearest'}); }
    });
    renderInspector();
  }

  async function renderInspector() {
    const target = $('libraryInspector');
    const item = find(selected);
    if (!item) { target.innerHTML = '<p class="hint">选择一篇论文查看文件、笔记、Three-Pass、研究项目与成果。</p>'; return; }
    const request = ++inspectorRequest;
    const actions = `<div class="lib-actions">
        <a class="button primary" href="${readerLink(item.id)}">在精读中打开</a>
        <a class="button secondary" href="${escapeHtml(item.links.gaps)}">加入研究空白</a>
        ${item.links.studio ? `<a class="button secondary" href="${escapeHtml(item.links.studio)}">ResearchStudio</a>` : '<span class="button secondary disabled" title="只有导入了原始 PDF 的论文才能用于 ResearchStudio">ResearchStudio</span>'}
      </div>`;
    target.innerHTML = `<h2 class="lib-inspector-title">${escapeHtml(item.title)}</h2>${actions}<p class="hint">正在读取…</p>`;
    let data;
    try {
      const response = await fetch(`/api/library/${item.id}/workspace`);
      data = await response.json();
      if (!response.ok) throw new Error(data.error || '无法读取这篇论文的记录。');
    } catch (error) {
      if (request === inspectorRequest) target.querySelector('.hint').textContent = error.message;
      return;
    }
    if (request !== inspectorRequest) return;
    const list = (items, empty, render) => items.length ? `<ul class="lib-list">${items.map(render).join('')}</ul>` : `<p class="hint">${empty}</p>`;
    target.innerHTML = `<h2 class="lib-inspector-title">${escapeHtml(item.title)}</h2>${actions}
      <dl class="lib-facts">
        <dt>来源</dt><dd>${escapeHtml(item.sourceName || '—')}</dd>
        <dt>格式</dt><dd>${escapeHtml(KIND[item.sourceType] || item.sourceType || '—')}${item.useOcr ? ' · Mathpix OCR' : ''}</dd>
        <dt>译文</dt><dd>${item.hasTranslation ? '已有整篇译文' : '尚无整篇译文'}</dd>
        <dt>更新</dt><dd>${escapeHtml(item.updatedAt ? new Date(item.updatedAt * 1000).toLocaleString() : '—')}</dd>
      </dl>
      ${(data.files || []).map(file => `<a class="button secondary lib-file" href="${escapeHtml(file.url)}" download>${escapeHtml(file.label)} ↓</a>`).join('')}
      <div class="lib-counts">
        <a href="${readerLink(item.id, 'notes')}"><strong>${(data.notes || []).length}</strong><span>笔记</span></a>
        <a href="${readerLink(item.id, 'three-pass')}"><strong>${(data.analyses || []).length}</strong><span>Three-Pass</span></a>
      </div>
      <h3>研究空白</h3>${list(data.gaps || [], '尚未用于研究项目。', project => `<li><a href="/research-gaps?project=${encodeURIComponent(project.id)}">${escapeHtml(project.name)}</a></li>`)}
      <h3>成果生成</h3>${list(data.studio || [], '还没有 ResearchStudio 任务。', job => `<li><a href="/researchstudio?job=${encodeURIComponent(job.id)}">${escapeHtml(job.label || job.id)}</a> <small>${escapeHtml(STATUS[job.status] || job.status || '')}</small></li>`)}`;
  }

  async function load() {
    try {
      const response = await fetch('/api/library');
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || '无法读取文献库。');
      documents = data.documents || [];
      render();
      if (!find(selected)) selected = visible[0]?.id || null;
      select(selected);
    } catch (error) {
      $('libraryList').innerHTML = `<p class="hint error">${escapeHtml(error.message)}</p>`;
    }
  }

  $('librarySearch').addEventListener('input', () => { renderTable(); select(find(selected) && visible.some(item => item.id === selected) ? selected : visible[0]?.id || null); });
  $('librarySearch').addEventListener('keydown', event => {
    if (event.key === 'ArrowDown' && visible.length) { event.preventDefault(); select(visible[0].id, {focus: true}); }
    if (event.key === 'Escape' && $('librarySearch').value) { $('librarySearch').value = ''; renderTable(); }
  });
  $('librarySourceList').addEventListener('click', event => {
    const button = event.target.closest('[data-source]');
    if (!button) return;
    source = button.dataset.source; remember('source', source);
    render();
    select(visible.some(item => item.id === selected) ? selected : visible[0]?.id || null);
  });
  $('libraryList').addEventListener('click', event => {
    const header = event.target.closest('[data-sort]');
    if (header) {
      const column = header.dataset.sort;
      sort = {column, descending: sort.column === column ? !sort.descending : column !== 'title'};
      remember('sort', sort.column); remember('descending', sort.descending ? '1' : '0');
      renderTable(); select(selected);
      return;
    }
    const box = event.target.closest('[data-check], [data-check-all]');
    if (box) {
      if (box.dataset.checkAll !== undefined) visible.forEach(item => (box.checked ? checked.add(item.id) : checked.delete(item.id)));
      else if (box.checked) checked.add(box.dataset.check); else checked.delete(box.dataset.check);
      renderTable(); select(selected); renderBatchBar();
      return;
    }
    const row = event.target.closest('tr[data-key]');
    if (row) select(row.dataset.key);
  });
  $('libraryBatchAdd')?.addEventListener('click', batchAddToProject);
  $('libraryBatchThreePass')?.addEventListener('click', batchThreePass);
  $('libraryBatchCopyBib')?.addEventListener('click', () => copyBibtex([...checked]));
  $('libraryBatchExportBib')?.addEventListener('click', () => exportBibtex([...checked]));
  $('libraryBatchClear')?.addEventListener('click', () => { checked.clear(); render(); select(selected); });
  $('libraryList').addEventListener('dblclick', event => {
    const row = event.target.closest('tr[data-key]');
    if (row) location.href = readerLink(row.dataset.key);
  });
  $('libraryList').addEventListener('contextmenu', event => {
    const row = event.target.closest('tr[data-key]');
    if (!row || !window.ContextMenu) return;
    event.preventDefault();
    select(row.dataset.key);
    window.ContextMenu.open(event, menuItems(find(row.dataset.key)));
  });
  $('libraryList').addEventListener('keydown', event => {
    const row = event.target.closest('tr[data-key]');
    if (!row) return;
    const item = find(row.dataset.key);
    if (window.ContextMenu?.fromKeyboard(event, row, menuItems(item))) return;
    const index = visible.findIndex(entry => entry.id === row.dataset.key);
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      const next = visible[index + (event.key === 'ArrowDown' ? 1 : -1)];
      if (next) select(next.id, {focus: true});
      else if (event.key === 'ArrowUp') $('librarySearch').focus();
    } else if (event.key === 'Enter') {
      location.href = readerLink(row.dataset.key);
    } else if (event.key === ' ' && !event.target.matches('input')) {
      event.preventDefault();
      if (checked.has(row.dataset.key)) checked.delete(row.dataset.key); else checked.add(row.dataset.key);
      renderTable(); select(row.dataset.key, {focus: true}); renderBatchBar();
    }
  });
  window.Inspector?.toggle($('libraryInspectorToggle'), {key: 'library.inspector', apply: open => $('libraryLayout').classList.toggle('inspector-hidden', !open)});
  load();
})();
