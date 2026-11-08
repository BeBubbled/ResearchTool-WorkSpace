/* 文献库: the shared paper library (GET /api/library), with links into each module. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
  const KIND = {pdf: 'PDF', markdown: 'Markdown', html: 'HTML'};
  let documents = [], timer = null;

  function describe(item) {
    const parts = [KIND[item.sourceType] || item.sourceType || '文档'];
    if (item.useOcr) parts.push('OCR');
    if (item.hasTranslation) parts.push('含译文');
    if (item.updatedAt) parts.push(new Date(item.updatedAt * 1000).toLocaleDateString());
    return parts.join(' · ');
  }

  function usedBy(item) {
    const gaps = item.usedBy?.gaps || [];
    if (!gaps.length) return '<span class="library-unused">尚未用于研究项目</span>';
    return `研究空白地图：${gaps.map(p => `<a href="/research-gaps">${escapeHtml(p.name)}</a>`).join('、')}`;
  }

  function render() {
    $('libraryCount').textContent = `${documents.length} 篇`;
    if (!documents.length) {
      const filtered = $('librarySearch').value.trim() || $('libraryPdfOnly').checked;
      $('libraryList').innerHTML = `<div class="library-empty"><strong>${filtered ? '没有匹配的论文' : '文献库还是空的'}</strong><p>${filtered ? '换个关键词，或取消筛选。' : '在精读中导入 PDF、Markdown 或 HTML 后，会自动出现在这里。'}</p></div>`;
      return;
    }
    $('libraryList').innerHTML = documents.map(item => `
      <article class="library-card">
        <div class="library-card-main">
          <h3>${escapeHtml(item.title)}</h3>
          <p class="library-meta">${escapeHtml(describe(item))}${item.sourceName ? ` · <span title="${escapeHtml(item.sourceName)}">${escapeHtml(item.sourceName)}</span>` : ''}</p>
          <p class="library-usage">${usedBy(item)}</p>
        </div>
        <div class="library-actions">
          <a class="button primary" href="${escapeHtml(item.links.reader)}">精读</a>
          <a class="button secondary" href="${escapeHtml(item.links.gaps)}" title="加入当前研究项目">加入研究空白</a>
          ${item.links.studio ? `<a class="button secondary" href="${escapeHtml(item.links.studio)}">ResearchStudio</a>` : '<span class="button secondary disabled" title="只有导入了原始 PDF 的论文才能用于 ResearchStudio">ResearchStudio</span>'}
        </div>
      </article>`).join('');
  }

  async function load() {
    const params = new URLSearchParams();
    const query = $('librarySearch').value.trim();
    if (query) params.set('q', query);
    if ($('libraryPdfOnly').checked) params.set('pdf', '1');
    try {
      const response = await fetch(`/api/library${params.size ? `?${params}` : ''}`);
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || '无法读取文献库。');
      documents = data.documents || [];
      render();
    } catch (error) {
      $('libraryList').innerHTML = `<p class="hint error">${escapeHtml(error.message)}</p>`;
    }
  }

  $('librarySearch').addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(load, 200); });
  $('libraryPdfOnly').addEventListener('change', load);
  load();
})();
