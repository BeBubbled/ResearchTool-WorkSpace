/* 论文工作区: one paper, every module, as tabs (GET /api/library/<key>/workspace). */
(() => {
  'use strict';
  const key = document.body.dataset.paper;
  const $ = id => document.getElementById(id);
  const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
  const TABS = ['overview', 'read', 'notes', 'analyses', 'gaps', 'studio'];
  const KIND = {pdf: 'PDF', markdown: 'Markdown', html: 'HTML'};
  const STATUS = {
    completed: '已完成', succeeded: '已完成', failed: '失败', cancelled: '已取消', canceled: '已取消',
    running: '进行中', queued: '排队中', interrupted: '已中断',
  };
  let data = null, active = null, loading = null;
  const reports = new Map();

  const when = value => {
    const number = Number(value || 0);
    if (!number) return '';
    return new Date(number > 1e12 ? number : number * 1000).toLocaleString();
  };
  const status = value => `<span class="ws-status ws-status-${escapeHtml(value)}">${escapeHtml(STATUS[value] || value || '未知')}</span>`;

  // Small, escape-first Markdown for notes and Three-Pass reports (headings, lists, emphasis, code).
  function inline(text) {
    return escapeHtml(text)
      .replace(/`([^`]+)`/g, '<code>$1</code>')
      .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
      .replace(/(^|[^*])\*([^*\s][^*]*)\*/g, '$1<em>$2</em>');
  }
  function markdown(source) {
    const out = [];
    let list = null, paragraph = [], code = null;
    const flush = () => {
      if (paragraph.length) out.push(`<p>${paragraph.map(inline).join('<br>')}</p>`);
      paragraph = [];
      if (list) out.push(`</${list}>`);
      list = null;
    };
    for (const line of String(source || '').split(/\r?\n/)) {
      if (code !== null) {
        if (/^```/.test(line)) { out.push(`<pre><code>${escapeHtml(code.join('\n'))}</code></pre>`); code = null; } else code.push(line);
        continue;
      }
      if (/^```/.test(line)) { flush(); code = []; continue; }
      const heading = line.match(/^(#{1,4})\s+(.*)$/);
      const bullet = line.match(/^\s*[-*+]\s+(.*)$/);
      const numbered = line.match(/^\s*\d+[.)]\s+(.*)$/);
      if (heading) { flush(); const level = Math.min(heading[1].length + 1, 5); out.push(`<h${level}>${inline(heading[2])}</h${level}>`); }
      else if (bullet || numbered) {
        const tag = bullet ? 'ul' : 'ol';
        if (paragraph.length) { out.push(`<p>${paragraph.map(inline).join('<br>')}</p>`); paragraph = []; }
        if (list !== tag) { if (list) out.push(`</${list}>`); out.push(`<${tag}>`); list = tag; }
        out.push(`<li>${inline((bullet || numbered)[1])}</li>`);
      } else if (!line.trim()) flush();
      else { if (list) { out.push(`</${list}>`); list = null; } paragraph.push(line); }
    }
    if (code !== null) out.push(`<pre><code>${escapeHtml(code.join('\n'))}</code></pre>`);
    flush();
    return out.join('');
  }

  function empty(title, body, action = '') {
    return `<div class="library-empty"><strong>${escapeHtml(title)}</strong><p>${body}</p>${action}</div>`;
  }

  function renderHeader() {
    const doc = data.document;
    document.title = `${doc.title} · 论文工作区`;
    $('workspaceTitle').textContent = doc.title;
    $('workspaceTitle').title = doc.title;
    const parts = [KIND[doc.sourceType] || doc.sourceType || '文档'];
    if (doc.useOcr) parts.push('OCR');
    if (doc.hasTranslation) parts.push('含译文');
    if (doc.sourceName) parts.push(doc.sourceName);
    $('workspaceMeta').textContent = parts.join(' · ');
    const counts = {notes: data.notes.length, analyses: data.analyses.length, gaps: data.gaps.length, studio: data.studio.length};
    document.querySelectorAll('[data-count]').forEach(node => { node.textContent = counts[node.dataset.count] || ''; });
  }

  function renderOverview() {
    const doc = data.document;
    const tile = (tab, count, label, hint) => `<button type="button" class="ws-tile" data-go="${tab}"><strong>${count}</strong><span>${label}</span><small>${hint}</small></button>`;
    const files = data.files.length
      ? data.files.map(file => `<a class="button secondary" href="${escapeHtml(file.url)}" download>${escapeHtml(file.label)} ↓</a>`).join('')
      : '<span class="hint">没有保存原始 PDF（早期 OCR 导入的论文只缓存了 OCR 结果）。</span>';
    $('panel-overview').innerHTML = `
      <div class="ws-tiles">
        ${tile('read', '▶', '阅读', doc.useOcr ? 'OCR 版式' : (KIND[doc.renderKind] || '原文'))}
        ${tile('notes', data.notes.length, '阅读笔记', '划词与解读')}
        ${tile('analyses', data.analyses.length, 'Three-Pass', '整篇精读报告')}
        ${tile('gaps', data.gaps.length, '研究项目', '研究空白地图')}
        ${tile('studio', data.studio.length, '成果生成', 'ResearchStudio')}
      </div>
      <section class="ws-section">
        <h2>文件</h2>
        <dl class="ws-facts">
          <dt>来源</dt><dd>${escapeHtml(doc.sourceName || '—')}</dd>
          <dt>格式</dt><dd>${escapeHtml(KIND[doc.sourceType] || doc.sourceType || '—')}${doc.useOcr ? ' · Mathpix OCR' : ''}</dd>
          <dt>译文</dt><dd>${doc.hasTranslation ? '已有整篇译文' : '尚无整篇译文'}</dd>
          <dt>更新</dt><dd>${escapeHtml(when(doc.updatedAt) || '—')}</dd>
        </dl>
        <div class="ws-actions">${files}</div>
      </section>`;
  }

  function renderNotes() {
    if (!data.notes.length) {
      $('panel-notes').innerHTML = empty('还没有笔记', '在「阅读」里划选文字，加入笔记或执行解读动作后，会自动出现在这里。', '<button type="button" class="button primary" data-go="read">去阅读</button>');
      return;
    }
    $('panel-notes').innerHTML = `
      <div class="ws-toolbar"><span class="hint">${data.notes.length} 条，最新的在前</span><button type="button" class="button secondary" id="copyWorkspaceNotes">复制 Markdown</button></div>
      <div class="ws-notes">${data.notes.map(note => `
        <article class="ws-note ws-note-${note.kind}">
          <header><span class="ws-chip">${note.kind === 'answer' ? escapeHtml(note.action || '划词解读') : '摘录'}</span>${note.section ? `<span>${escapeHtml(note.section)}</span>` : ''}<time>${escapeHtml(when(note.createdAt))}</time></header>
          ${note.kind === 'answer' && note.selection ? `<blockquote>${escapeHtml(note.selection)}</blockquote>` : ''}
          ${note.question ? `<p class="ws-question">问：${escapeHtml(note.question)}</p>` : ''}
          <div class="ws-markdown">${note.kind === 'answer' ? markdown(note.text) : `<blockquote>${escapeHtml(note.text)}</blockquote>`}</div>
        </article>`).join('')}</div>`;
  }

  function notesMarkdown() {
    const lines = [`# ${data.document.title} · 阅读笔记`, ''];
    for (const note of data.notes) {
      lines.push(`## ${note.kind === 'answer' ? (note.action || '划词解读') : '摘录'}${note.section ? ` · ${note.section}` : ''}`, '');
      if (note.selection && note.kind === 'answer') lines.push(`> ${note.selection.replace(/\n/g, '\n> ')}`, '');
      if (note.question) lines.push(`**问：** ${note.question}`, '');
      lines.push(note.kind === 'answer' ? note.text : `> ${note.text.replace(/\n/g, '\n> ')}`, '');
    }
    return lines.join('\n');
  }

  function renderAnalyses() {
    const start = '<button type="button" class="button primary" data-open-three-pass>在阅读中新建 Three-Pass</button>';
    if (!data.analyses.length) {
      $('panel-analyses').innerHTML = empty('还没有 Three-Pass 分析', '整篇精读在阅读界面右侧的「解读 → Three-Pass」中运行，结果会保存在这篇论文下。', start);
      return;
    }
    $('panel-analyses').innerHTML = `
      <div class="ws-toolbar"><span class="hint">${data.analyses.length} 次分析</span>${start}</div>
      <div class="ws-list">${data.analyses.map(item => `
        <details class="ws-item" data-analysis="${escapeHtml(item.id)}">
          <summary><span class="ws-item-title">${escapeHtml(when(item.createdAt))}</span>${status(item.status)}<span class="hint">${escapeHtml([item.backend === 'api' ? 'API' : 'Codex', item.model, item.phaseLabel].filter(Boolean).join(' · '))}</span></summary>
          <div class="ws-report">${item.reportUrl ? '<p class="hint">正在读取报告…</p>' : '<p class="hint">这次分析没有生成报告。</p>'}</div>
          ${item.reportUrl ? `<div class="ws-actions"><a class="button secondary" href="${escapeHtml(item.reportUrl)}">下载 Markdown</a></div>` : ''}
        </details>`).join('')}</div>`;
  }

  async function loadReport(details) {
    const item = data.analyses.find(entry => entry.id === details.dataset.analysis);
    const target = details.querySelector('.ws-report');
    if (!item?.reportUrl || details.dataset.loaded) return;
    details.dataset.loaded = '1';
    try {
      if (!reports.has(item.id)) {
        const response = await fetch(item.reportUrl);
        if (!response.ok) throw new Error('无法读取报告。');
        reports.set(item.id, await response.text());
      }
      target.innerHTML = `<div class="ws-markdown">${markdown(reports.get(item.id))}</div>`;
    } catch (error) {
      delete details.dataset.loaded;
      target.innerHTML = `<p class="hint error">${escapeHtml(error.message)}</p>`;
    }
  }

  function renderGaps() {
    const add = `<a class="button primary" href="${escapeHtml(data.document.links.gaps)}">加入研究项目</a>`;
    if (!data.gaps.length) {
      $('panel-gaps').innerHTML = empty('尚未用于研究项目', '把论文加入研究空白地图的项目后，可以和其他论文一起抽取方法、数据与局限，寻找研究空白。', add);
      return;
    }
    $('panel-gaps').innerHTML = `
      <div class="ws-toolbar"><span class="hint">这篇论文在 ${data.gaps.length} 个研究项目中</span>${add}</div>
      <div class="ws-list">${data.gaps.map(project => `
        <div class="ws-item ws-row"><span class="ws-item-title">${escapeHtml(project.name)}</span><a class="button secondary" href="/research-gaps?project=${encodeURIComponent(project.id)}">打开项目</a></div>`).join('')}</div>`;
  }

  function renderStudio() {
    const studio = data.document.links.studio;
    const create = studio
      ? `<a class="button primary" href="${escapeHtml(studio)}">用这篇论文生成</a>`
      : '<span class="button disabled" title="只有导入了原始 PDF 的论文才能用于 ResearchStudio">用这篇论文生成</span>';
    if (!data.studio.length) {
      $('panel-studio').innerHTML = empty('还没有生成过成果', studio ? '在 ResearchStudio 里用这篇论文生成选题、海报、博客或视频。' : '这篇论文没有保存原始 PDF，ResearchStudio 暂时用不了。在精读中重新导入 PDF 即可。', create);
      return;
    }
    $('panel-studio').innerHTML = `
      <div class="ws-toolbar"><span class="hint">${data.studio.length} 个任务</span>${create}</div>
      <div class="ws-list">${data.studio.map(job => `
        <div class="ws-item ws-row"><span class="ws-item-title">${escapeHtml(job.label)}</span>${status(job.status)}<span class="hint">${escapeHtml(when(job.createdAt))}${job.error ? ` · ${escapeHtml(job.error)}` : ''}</span><a class="button secondary" href="/researchstudio?job=${encodeURIComponent(job.id)}">查看</a></div>`).join('')}</div>`;
  }

  const RENDER = {overview: renderOverview, notes: renderNotes, analyses: renderAnalyses, gaps: renderGaps, studio: renderStudio};

  function ensureReader() {
    if ($('workspaceReader')) return $('workspaceReader');
    const frame = document.createElement('iframe');
    frame.id = 'workspaceReader';
    frame.title = '阅读';
    frame.src = `/reader?embed=1&library=${encodeURIComponent(key)}`;
    $('panel-read').appendChild(frame);
    return frame;
  }

  // Open the reader's Three-Pass panel inside the embedded reader (same origin).
  function openThreePass() {
    select('read');
    const frame = ensureReader();
    const reveal = () => {
      const doc = frame.contentDocument;
      const panel = doc?.getElementById('threePassPanel');
      if (!panel || doc.getElementById('readerShell')?.classList.contains('hidden')) return false;
      const assistant = doc.getElementById('assistantPanel');
      if (assistant && assistant.offsetParent === null) doc.getElementById('assistantToggle')?.click();
      const tools = doc.getElementById('assistantTools');
      if (tools) tools.open = true;
      panel.open = true;
      panel.scrollIntoView({block: 'start'});
      return true;
    };
    let tries = 0;
    const timer = setInterval(() => { if (reveal() || ++tries > 60) clearInterval(timer); }, 250);
  }

  async function load() {
    if (loading) return loading;
    loading = (async () => {
      const response = await fetch(`/api/library/${encodeURIComponent(key)}/workspace`);
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || '无法读取论文工作区。');
      data = payload;
      renderHeader();
    })();
    try { await loading; } finally { loading = null; }
  }

  async function select(tab, {refresh = true} = {}) {
    if (!TABS.includes(tab)) tab = 'overview';
    active = tab;
    TABS.forEach(name => {
      const on = name === tab;
      $(`panel-${name}`).hidden = !on;
      const button = $(`tab-${name}`);
      button.setAttribute('aria-selected', String(on));
      button.tabIndex = on ? 0 : -1;
    });
    document.body.classList.toggle('workspace-reading', tab === 'read');
    if (location.hash !== `#${tab}`) history.replaceState(null, '', `#${tab}`);
    $(`tab-${tab}`).scrollIntoView({block: 'nearest', inline: 'nearest'});
    if (tab === 'read') { ensureReader(); return; }
    if (!$(`panel-${tab}`).childElementCount) $(`panel-${tab}`).innerHTML = '<p class="hint">正在读取…</p>';
    try {
      // Notes and analyses change while reading, so other tabs reload the workspace when shown.
      if (refresh || !data) await load();
      if (active === tab) RENDER[tab]();
    } catch (error) {
      $(`panel-${tab}`).innerHTML = `<p class="hint error">${escapeHtml(error.message)}</p>`;
    }
  }

  document.querySelector('.workspace-tabs').addEventListener('click', event => {
    const button = event.target.closest('[data-tab]');
    if (button) select(button.dataset.tab);
  });
  document.querySelector('.workspace-tabs').addEventListener('keydown', event => {
    if (!['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
    const next = TABS[(TABS.indexOf(active) + (event.key === 'ArrowRight' ? 1 : TABS.length - 1)) % TABS.length];
    select(next);
    $(`tab-${next}`).focus();
  });
  document.querySelector('.workspace-body').addEventListener('click', async event => {
    const go = event.target.closest('[data-go]');
    if (go) return select(go.dataset.go);
    if (event.target.closest('[data-open-three-pass]')) return openThreePass();
    if (event.target.closest('#copyWorkspaceNotes')) {
      try { await navigator.clipboard.writeText(notesMarkdown()); event.target.textContent = '已复制'; }
      catch { event.target.textContent = '复制失败'; }
    }
  });
  document.querySelector('.workspace-body').addEventListener('toggle', event => {
    if (event.target.matches?.('details[data-analysis]') && event.target.open) loadReport(event.target);
  }, true);
  window.addEventListener('hashchange', () => select(location.hash.slice(1)));

  select(location.hash.slice(1) || 'overview');

})();
