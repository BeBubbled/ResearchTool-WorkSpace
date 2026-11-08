(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const base = '/api/paper-revision/projects';
  let project = null, snapshot = null, selected = [], pollTimer = null;
  let requestSerial = 0, busy = false;
  let importFiles = [], importReading = false, engineOptions = null, workflowTouched = false;
  const scopes = {auto: '自动 · 至多同节相邻段落', paragraph: '仅选中段落', section: '所在章节 / 小节', document: '全文'};
  const statusNames = {queued: '排队中', running: '执行中', completed: '已完成', failed: '失败', cancelled: '已停止', interrupted: '已中断'};
  const active = () => ['queued', 'running'].includes(project?.job?.status);
  const elem = (tag, cls, text) => { const node = document.createElement(tag); if (cls) node.className = cls; if (text !== undefined) node.textContent = text; return node; };
  const fail = error => { $('error').textContent = error.message || String(error); $('error').hidden = false; };
  const clearError = () => { $('error').hidden = true; $('error').textContent = ''; };
  async function api(url, options = {}) {
    const response = await fetch(url, {...options, headers: {'X-Paper-Revision': '1', ...(options.body instanceof FormData ? {} : {'Content-Type': 'application/json'}), ...options.headers}});
    const result = await response.json().catch(() => ({error: `请求失败 (${response.status})`}));
    if (!response.ok) throw new Error(result.error || `请求失败 (${response.status})`);
    return result;
  }
  const post = (suffix, data) => api(`${base}/${project.id}/${suffix}`, {method: 'POST', body: JSON.stringify(data)});
  async function list(preferred) {
    const result = await api(base);
    const chosen = preferred || project?.id || localStorage.getItem('paperRevisionProject');
    const placeholder = new Option('选择或导入论文', '', true, true); placeholder.disabled = true;
    $('projectSelect').replaceChildren(placeholder);
    result.projects.forEach(p => $('projectSelect').add(new Option(p.title, p.id)));
    setEngineOptions(result.options);
    if (result.projects.some(p => p.id === chosen)) { $('projectSelect').value = chosen; await openProject(chosen); }
    else if (!result.projects.length) $('importPanel').hidden = false;
  }
  // ---- Each visit: re-read the paper from disk, start without last visit's comments (kept as a backup).
  let visitNotes = [];
  async function openProject(id, resolution) {
    let opened;
    try { opened = await api(`${base}/${id}/open`, {method: 'POST', body: JSON.stringify(resolution ? {resolution} : {})}); }
    catch (error) {
      if (!String(error.message).includes('(404)')) throw error;
      // Page code is newer than the running backend: still open the project, and say why sync is missing.
      visitNotes = [{kind: 'warn', text: '后端仍在运行旧版本代码（没有“打开项目”接口）：本次未从磁盘重新读取、旧批注也未移入备份。请重启 web_panel.py 后刷新页面。'}];
      return load(id);
    }
    if (opened.status === 'conflict') {
      $('syncText').textContent = `${opened.conflict.path} 在别处被修改过，而本工具里还有 v${opened.conflict.tool_revisions.join('、v')} 的修订没有放回该文件。` +
        '以磁盘为准：重新读取文件，英文没变的句子保留中文，变化的句子需要补译。继续用本工具版本：忽略这次磁盘改动（文件再变时会再次提示）。';
      $('syncDialog').returnValue = '';
      $('syncDialog').showModal();
      const choice = await new Promise(resolve => $('syncDialog').addEventListener('close', () => resolve($('syncDialog').returnValue), {once: true}));
      return openProject(id, choice === 'tool' ? 'tool' : 'disk');
    }
    visitNotes = [];
    if (opened.status === 'synced') visitNotes.push({kind: 'ok', text: `已从磁盘重新读取（${opened.sync.files_changed.join('、') || '文件'}）：${opened.sync.changed} 句英文有变化，其余 ${opened.sync.reused} 句保留原有中文。` +
      (opened.sync.changed ? '点击“补译缺失中文”只翻译变化的句子。' : '')});
    if (opened.status === 'kept_tool') visitNotes.push({kind: 'warn', text: '已保留本工具的版本；磁盘上的改动未读入。导出 LaTeX 后再放回原项目，可让两边一致。'});
    if (opened.status === 'disk_missing') visitNotes.push({kind: 'warn', text: `无法读取原文件（${opened.error || '路径不存在'}），本次使用上次保存的版本。`});
    await load(id);
  }
  function renderBanners() {
    const box = $('visitBanners'); box.replaceChildren();
    visitNotes.forEach(note => box.append(elem('div', `visit-banner ${note.kind}`, note.text)));
    const backup = [...(project?.comment_backups || [])].reverse().find(b => !b.dismissed && b.comments.length);
    if (!backup) return;
    const banner = elem('div', 'visit-banner backup');
    banner.append(`有 ${backup.comments.length} 条之前的批注没有带到这次（${new Date(backup.time * 1000).toLocaleString()} 备份）：`);
    const restore = elem('button', 'quiet', '恢复到本次'), download = elem('button', 'quiet', '下载备份'), dismiss = elem('button', 'quiet', '忽略');
    restore.addEventListener('click', async () => {
      try {
        const result = await post('comments/import', {revision: project.revision, backup_id: backup.id});
        project = {...result.project, session_folder: project.session_folder};
        visitNotes.push({kind: result.unplaced.length ? 'warn' : 'ok', text: `已恢复 ${result.imported} 条批注` +
          (result.unplaced.length ? `；${result.unplaced.length} 条因原文已改动无法定位：${result.unplaced.join('；').slice(0, 300)}` : '。')});
        renderComments(); renderControls(); markComments(); renderBanners(); $('commentDrawer').hidden = false;
      } catch (error) { fail(error); }
    });
    download.addEventListener('click', () => saveJson({format: 'paper-revision-comments', version: 1, paper: project.title, revision: backup.revision,
      exported_at: backup.time, comments: backup.comments}, `${project.title}-comments-backup.json`));
    dismiss.addEventListener('click', async () => { try { project = {...await post(`comments/backups/${backup.id}/dismiss`, {}), session_folder: project.session_folder}; renderBanners(); } catch (error) { fail(error); } });
    banner.append(restore, download, dismiss);
    box.append(banner);
  }
  function saveJson(data, name) {
    const link = elem('a'); link.href = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], {type: 'application/json'}));
    link.download = name.replace(/[\\/:*?"<>|]+/g, '_'); document.body.append(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(link.href), 1000);
  }
  const commentsUnexported = () => !!project?.comments.length &&
    project.comments.map(c => c.id).join() !== (project.comments_exported || []).join();
  async function load(id, quiet = false) {
    if (!id) return;
    const serial = ++requestSerial;
    try {
      const result = await api(`${base}/${id}`);
      if (serial !== requestSerial) return;
      const redraw = !quiet || project?.id !== id || project?.revision !== result.project.revision;
      project = result.project; snapshot = result.snapshot;
      localStorage.setItem('paperRevisionProject', id);
      if (redraw) { selected = []; $('selectionButton').hidden = true; if ($('commentDialog').open) $('commentDialog').close(); renderPaper(); renderHistory(); renderContext(); }
      if (active()) applyPreview(project.job.preview);
      renderControls(); renderComments(); renderBanners();
      if (active()) { clearTimeout(pollTimer); pollTimer = setTimeout(() => load(id, true).catch(fail), 1800); }
    } catch (error) { if (serial === requestSerial) throw error; }
  }
  function renderControls() {
    const running = active();
    $('projectSelect').disabled = busy;
    $('revision').textContent = `v${project.revision}`;
    const translateOn = project.translate !== false;
    const units = snapshot.blocks.flatMap(b => b.units), missing = units.filter(u => !u.zh).length;
    const kind = project.comments.length ? 'revise' : translateOn && missing ? 'translate' : null;
    $('submit').textContent = running ? '执行中…' : kind === 'translate' ? (missing === units.length ? '生成中文对照' : `补译缺失中文（${missing} 句）`)
      : `提交本轮 ${project.comments.length} 条批注并修订${translateOn ? '' : '（不翻译）'}`;
    $('submit').disabled = busy || running || !kind;
    $('submit').title = $('usage').checked ? '' : '请先勾选左侧“提交时使用所选模型处理论文与批注”';
    $('translateToggle').checked = translateOn; $('translateToggle').disabled = busy || running;
    $('retranslate').hidden = !translateOn || !units.some(u => u.zh); $('retranslate').disabled = busy || running;
    $('projectSettings').disabled = busy || running;
    $('exportComments').href = `${base}/${project.id}/comments/export`;
    $('exportComments').classList.toggle('attention-soft', commentsUnexported());
    $('zhHeading').textContent = translateOn ? '中文对照 · 圈选后添加批注' : '中文翻译已关闭 · 请在英文上圈选批注';
    $('cancel').hidden = !running;
    $('export').hidden = false; $('export').href = `${base}/${project.id}/export`;
    $('commentCount').textContent = project.comments.length;
    $('workflowEditor').querySelectorAll('select').forEach(select => { select.disabled = running; });
    renderSession();
    const job = project.job;
    $('notice').classList.toggle('warning', !!job?.warning);
    $('notice').textContent = job ? `${statusNames[job.status] || job.status} · ${job.progress}${job.warning ? ` · ⚠ ${job.warning}` : ''}${jobEngine(job)}`
      : translateOn ? '原稿已保存。生成中文对照后在中文上圈选批注，也可直接在英文上圈选。' : '原稿已保存（未开启翻译）。直接在左侧英文上圈选文字添加批注。';
    if (job?.error) fail(new Error(job.error));
  }
  // ---- Revision hover cards: every rewritten sentence shows what it replaced, in both languages.
  const tip = elem('div', 'revision-tip'); tip.hidden = true; tip.setAttribute('role', 'tooltip'); tip.id = 'revisionTip';
  document.body.append(tip);
  let tipShowTimer = null, tipHideTimer = null;
  const kinds = {replace: '改写', split: '一句拆为多句', merge: '多句合并 / 重组', insert: '新增句子', delete: '删除句子'};
  const currentText = (unit, side) => side === 'en' ? (unit.display_en ?? unit.en) : unit.zh;
  const diffTokens = (text, side) => side === 'zh' ? Array.from(text) : (text.match(/\s+|[\p{L}\p{N}'’-]+|[^\s\p{L}\p{N}]/gu) || []);
  function diffNode(before, after, side) {
    const a = diffTokens(before, side), b = diffTokens(after, side), box = elem('div', 'tip-diff');
    if (a.length * b.length > 200000) { box.append(elem('del', '', before), elem('ins', '', after)); return box; }
    const n = a.length, m = b.length, dp = Array.from({length: n + 1}, () => new Uint16Array(m + 1));
    for (let i = n - 1; i >= 0; i--) for (let j = m - 1; j >= 0; j--) dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
    const ops = [], push = (type, text) => { const last = ops.at(-1); if (last?.type === type) last.text += text; else ops.push({type, text}); };
    let i = 0, j = 0;
    while (i < n && j < m) { if (a[i] === b[j]) { push('same', a[i]); i++; j++; } else if (dp[i + 1][j] >= dp[i][j + 1]) push('del', a[i++]); else push('ins', b[j++]); }
    while (i < n) push('del', a[i++]); while (j < m) push('ins', b[j++]);
    ops.forEach(o => box.append(o.type === 'same' ? document.createTextNode(o.text) : elem(o.type, '', o.text)));
    return box;
  }
  function commentLines(entry) {
    const list = project.history.find(r => r.revision === entry.revision)?.comments || [];
    return (entry.comment_ids || []).map(id => {
      const k = list.findIndex(c => c.id === id), c = list[k];
      return c ? `#${k + 1}「${c.anchors.map(a => a.quote).join('').slice(0, 40)}」${c.text}` : '（批注记录不可用）';
    });
  }
  function entryView(entry, after, side) {
    const wrap = elem('section', 'tip-entry');
    wrap.append(elem('div', 'tip-head', `v${entry.from_revision} → v${entry.revision} · ${kinds[entry.kind] || '改写'}`));
    const old = entry[side] || '';
    if (side === 'zh' && entry.kind !== 'insert' && !old) {
      wrap.append(elem('p', 'tip-note', '改动时这句还没有中文翻译。'));
      after = undefined;
    } else if (side === 'zh' && after === '' && entry.kind !== 'delete') {
      wrap.append(elem('div', 'tip-label', '旧中文（原样）'), elem('p', 'tip-old', old), elem('p', 'tip-note', '新句尚未翻译（翻译已关闭）。'));
    } else if (entry.kind === 'insert') wrap.append(elem('p', 'tip-note', `本句在 v${entry.revision} 新增，之前没有对应句子。`));
    else if (entry.kind === 'delete') wrap.append(elem('div', 'tip-label', '被删除的原文'), elem('p', 'tip-old', old));
    else {
      wrap.append(elem('div', 'tip-label', side === 'en' ? '旧英文（原样）' : '旧中文（原样）'), elem('p', 'tip-old', old));
      if (after !== undefined) wrap.append(elem('div', 'tip-label', '差异'), diffNode(old, after, side));
    }
    if (entry.group) wrap.append(elem('p', 'tip-note', '这段旧文由同组相邻的新句共同替换（模型做了拆分或合并），已一并高亮。'));
    const lines = commentLines(entry);
    if (lines.length) { wrap.append(elem('div', 'tip-label', lines.length > 1 ? `由 ${lines.length} 条批注共同改动（同一轮合并为一次修改）` : '来源批注')); lines.forEach(l => wrap.append(elem('p', 'tip-comment', l))); }
    if (entry.reason) wrap.append(elem('p', 'tip-reason', `修改说明：${entry.reason}`));
    return wrap;
  }
  function unitTip(unit, side) {
    const lineage = unit.lineage || [], box = document.createDocumentFragment();
    box.append(entryView(lineage[0], currentText(unit, side), side));
    if (lineage.length > 1) {
      const more = elem('details', 'tip-more'); more.append(elem('summary', '', `更早版本（${lineage.length - 1}）· 最底部为最早的原文`));
      lineage.slice(1).forEach((entry, k) => more.append(entryView(entry, lineage[k].kind === 'insert' ? undefined : lineage[k][side], side)));
      box.append(more);
    }
    return box;
  }
  function placeTip(node, event) {
    const rects = Array.from(node.getClientRects());
    const rect = (event && rects.find(r => event.clientY >= r.top - 2 && event.clientY <= r.bottom + 2)) || rects[0] || node.getBoundingClientRect();
    tip.style.left = '0px'; tip.style.top = '0px'; tip.hidden = false;
    const width = tip.offsetWidth, height = tip.offsetHeight;
    tip.style.left = `${Math.min(Math.max(rect.left, 8), window.innerWidth - width - 8)}px`;
    tip.style.top = `${rect.bottom + 6 + height > window.innerHeight && rect.top - height - 6 > 0 ? rect.top - height - 6 : rect.bottom + 6}px`;
  }
  function hideTip() {
    clearTimeout(tipShowTimer); clearTimeout(tipHideTimer); tip.hidden = true;
    document.querySelectorAll('.unit.group-hover').forEach(n => n.classList.remove('group-hover'));
  }
  function attachTip(node, build, group) {
    const show = event => {
      clearTimeout(tipHideTimer); clearTimeout(tipShowTimer);
      tipShowTimer = setTimeout(() => {
        if ($('commentDialog').open || !window.getSelection()?.isCollapsed) return;
        hideTip(); tip.replaceChildren(build()); placeTip(node, event);
        if (group) document.querySelectorAll(`.unit[data-group="${CSS.escape(group)}"][data-side="${node.dataset.side}"]`).forEach(n => n.classList.add('group-hover'));
      }, event?.type === 'focus' ? 0 : 220);
    };
    const leave = () => { clearTimeout(tipShowTimer); tipHideTimer = setTimeout(hideTip, 260); };
    node.addEventListener('mouseenter', show); node.addEventListener('mouseleave', leave);
    node.addEventListener('focus', show); node.addEventListener('blur', leave);
    node.setAttribute('aria-describedby', 'revisionTip');
  }
  tip.addEventListener('mouseenter', () => clearTimeout(tipHideTimer));
  tip.addEventListener('mouseleave', () => { tipHideTimer = setTimeout(hideTip, 260); });
  document.addEventListener('keydown', e => { if (e.key === 'Escape') hideTip(); });
  window.addEventListener('scroll', hideTip, {passive: true, capture: true});
  function removedNode(entries, side) {
    const mark = elem('span', 'removed-mark', '⌫'); mark.tabIndex = 0;
    mark.setAttribute('aria-label', `此处删除了 ${entries.length} 处原文`);
    if (entries.some(e => e.revision === project.revision)) mark.classList.add('latest');
    attachTip(mark, () => { const box = document.createDocumentFragment(); [...entries].reverse().forEach(e => box.append(entryView(e, undefined, side))); return box; });
    return mark;
  }
  function unitNode(unit, side, bid) {
    const text = currentText(unit, side);
    const node = elem('span', text ? 'unit' : 'unit untranslated', text || '〔未翻译〕');
    node.dataset.unit = unit.id; node.dataset.block = bid; node.dataset.side = side;
    node.addEventListener('click', () => linkUnit(unit.id));
    const latest = unit.lineage?.[0];
    if (latest) {
      node.classList.add(latest.revision === project.revision ? 'revised' : 'revised-earlier');
      if (latest.group) node.dataset.group = latest.group;
      node.tabIndex = 0;
      attachTip(node, () => unitTip(unit, side), latest.group);
    }
    return node;
  }
  function unitNodes(unit, side, bid) {
    const nodes = [];
    if (unit.removed_before?.length) nodes.push(removedNode(unit.removed_before, side));
    nodes.push(unitNode(unit, side, bid));
    if (unit.removed_after?.length) nodes.push(removedNode(unit.removed_after, side));
    return nodes;
  }
  const collapsedSections = new Set();
  const sectionKey = block => `${block.file}|${block.section}`;
  function sectionGroups() {
    const groups = [];
    snapshot.blocks.forEach(block => {
      const key = sectionKey(block);
      if (groups.at(-1)?.key !== key) {
        const known = snapshot.section_titles?.[block.section];
        const fallback = block.section.endsWith(':preamble') ? `${block.file} · 开头` : (block.units[0]?.display_en || '').trim().slice(0, 60);
        groups.push({key, title: known?.title || fallback, level: known?.level || 0, file: block.file, blocks: []});
      }
      groups.at(-1).blocks.push(block);
    });
    return groups;
  }
  function sectionProgress(group) {
    const units = group.blocks.flatMap(b => b.units), done = units.filter(u => u.zh).length;
    return project.translate === false && !done ? `${group.blocks.length} 段` : `${group.blocks.length} 段 · 中文 ${done}/${units.length} 句`;
  }
  function fillZh(prose, block) {
    prose.replaceChildren();
    if (!block.units.some(u => u.zh)) prose.append(elem('span', 'pending', project.translate !== false ? '〔待翻译〕' : '〔未开启翻译〕'));
    else block.units.forEach(u => prose.append(...unitNodes(u, 'zh', block.id)));
  }
  function applyPreview(preview) {
    if (!preview || !snapshot) return;
    const owners = new Map(snapshot.blocks.flatMap(b => b.units.map(u => [u.id, [b, u]])));
    const dirty = new Map(), fresh = new Set();
    Object.entries(preview).forEach(([id, [masked, zh]]) => {
      const [block, unit] = owners.get(id) || [];
      if (!unit || unit.masked !== masked) return;  // only sentences whose English is unchanged
      if (!unit.zh) unit.zh = zh;
      const node = document.querySelector(`.side.zh .unit[data-unit="${CSS.escape(id)}"]`);
      if (!node || node.classList.contains('untranslated')) { dirty.set(block.id, block); fresh.add(id); }
    });
    dirty.forEach(block => {
      const prose = document.querySelector(`.side.zh .prose[data-block="${CSS.escape(block.id)}"]`);
      if (!prose) return;
      fillZh(prose, block);
      prose.querySelectorAll('.unit').forEach(n => { if (fresh.has(n.dataset.unit)) n.classList.add('fresh'); });
    });
    if (dirty.size) {
      sectionGroups().forEach(group => { const badge = document.querySelector(`.paper-section[data-key="${CSS.escape(group.key)}"] .section-progress`); if (badge) badge.textContent = sectionProgress(group); });
      markComments();
    }
  }
  function renderPaper() {
    hideTip(); const container = $('paper'); container.replaceChildren();
    const changed = new Set((project.history.at(-1)?.changes || []).map(c => c.block_id));
    sectionGroups().forEach(group => {
      const section = elem('details', `paper-section level-${Math.min(group.level, 2)}`); section.dataset.key = group.key;
      section.open = !collapsedSections.has(group.key);
      section.addEventListener('toggle', () => { if (section.open) collapsedSections.delete(group.key); else collapsedSections.add(group.key); });
      const summary = elem('summary', 'section-head');
      summary.append(elem('span', 'section-title', group.title || '（无标题）'), elem('span', 'section-progress', sectionProgress(group)));
      if (group.blocks.some(b => changed.has(b.id))) summary.append(elem('span', 'change-label', '本轮已更新'));
      section.append(summary);
      section.append(sectionBody(group, changed));
      container.append(section);
    });
    markComments();
  }
  // One continuous reading flow per section: each paragraph's English and Chinese share a grid
  // row (so the two languages stay level), without per-paragraph headers or dividers.
  function sectionBody(group, changed) {
    const body = elem('article', 'pair section-pair');
    group.blocks.forEach(block => {
      ['en', 'zh'].forEach(side => {
        const cell = elem('div', `side ${side}${changed.has(block.id) ? ' changed' : ''}`);
        if (side === 'en') { cell.id = `block-${block.id}`; cell.title = `${block.id} / ${block.file}`; }
        const prose = elem('div', 'prose'); prose.dataset.block = block.id;
        if (side === 'zh') fillZh(prose, block);
        else block.units.forEach(u => prose.append(...unitNodes(u, side, block.id)));
        cell.append(prose);
        body.append(cell);
      });
    });
    const detail = elem('details', 'source-details section-source'); detail.append(elem('summary', '', '查看本节 LaTeX 源码'));
    detail.append(elem('pre', '', group.blocks.map(b => b.units.map(u => u.en).join('')).join('\n\n'))); body.append(detail);
    return body;
  }
  function linkUnit(id) {
    document.querySelectorAll('.unit.linked').forEach(n => n.classList.remove('linked'));
    document.querySelectorAll('.unit').forEach(n => { if (n.dataset.unit === id) n.classList.add('linked'); });
  }
  function markComments() {
    if (!snapshot) return;
    const byId = new Map(snapshot.blocks.flatMap(b => b.units).map(u => [u.id, u]));
    const owner = new Map(snapshot.blocks.flatMap(b => b.units.map((u, i) => [u.id, [b, i]])));
    // A paragraph that starts with its \section title: keep the title on its own (bold) line even
    // though single line breaks are joined. Only a <br> is added, so text offsets are unchanged.
    const headingBlock = block => block.section.startsWith('section:') && snapshot.blocks[Number(block.section.split(':')[1])]?.id === block.id;
    document.querySelectorAll('.prose .unit:not(.untranslated)').forEach(node => {
      const unit = byId.get(node.dataset.unit), side = node.dataset.side;
      if (!unit) return;
      const text = Array.from(currentText(unit, side)), marks = Array(text.length).fill(false);
      project.comments.forEach(c => c.anchors.filter(a => a.unit_id === unit.id && (a.side || 'zh') === side).forEach(a => { for (let i = a.start; i < a.end; i++) marks[i] = true; }));
      const [block, position] = owner.get(unit.id) || [];
      const newline = text.indexOf('\n');
      const breakAt = block && position === 0 && headingBlock(block) && newline > 0 && newline < 150 && newline < text.length - 1 ? newline + 1 : -1;
      if (position === 0) node.closest('.prose')?.classList.toggle('heading-block', breakAt > 0);
      node.replaceChildren();
      let start = 0;
      while (start < text.length) {
        let end = start + 1;
        while (end < text.length && marks[end] === marks[start] && end !== breakAt) end++;
        const value = text.slice(start, end).join('');
        node.append(marks[start] ? elem('mark', '', value) : document.createTextNode(value));
        if (end === breakAt) node.append(elem('br', 'heading-break'));
        start = end;
      }
    });
  }
  function renderComments() {
    $('comments').replaceChildren();
    if (!project.comments.length) $('comments').append(elem('p', 'hint', '尚无批注。在中文或英文上圈选文字后，点击“添加批注”。也可使用键盘选中文字。'));
    project.comments.forEach((comment, index) => {
      const card = elem('article', 'comment-card');
      card.append(elem('strong', '', `${index + 1}. ${scopes[comment.scope]}`));
      const quote = elem('blockquote', '', comment.anchors.map(a => a.quote).join(''));
      quote.tabIndex = 0;
      const locate = () => { document.getElementById(`block-${comment.anchors[0].block_id}`)?.scrollIntoView({behavior: 'smooth', block: 'center'}); linkUnit(comment.anchors[0].unit_id); };
      quote.addEventListener('click', locate); quote.addEventListener('keydown', e => { if (e.key === 'Enter') locate(); });
      card.append(quote, elem('p', '', comment.text), elem('small', 'hint', `v${comment.revision} · ${[...new Set(comment.anchors.map(a => a.block_id))].join(', ')}`));
      const remove = elem('button', 'quiet', '删除批注'); remove.disabled = active();
      remove.addEventListener('click', async () => { remove.disabled = true; try { project = await api(`${base}/${project.id}/comments/${comment.id}`, {method: 'DELETE', body: JSON.stringify({revision: project.revision})}); renderComments(); renderControls(); markComments(); } catch (error) { fail(error); remove.disabled = false; } });
      card.append(remove); $('comments').append(card);
    });
  }
  function renderHistory() {
    $('history').hidden = false; $('historyList').replaceChildren();
    [...project.history].reverse().forEach(record => {
      const article = elem('article', 'history-record'), header = elem('header');
      const labels = {import: '导入原稿', translate: '生成中文', revise: '根据批注修订', restore: `恢复 v${record.from_revision}`,
        sync: `从磁盘重新读取（${record.changed || 0} 句变化）`, clear_translation: '清空中文，准备重新翻译'};
      header.append(elem('strong', '', `v${record.revision} · ${labels[record.kind] || record.kind} · ${new Date(record.time * 1000).toLocaleString()}`));
      const actions = elem('div'); const download = elem('a', 'quiet', '下载此版本'); download.href = `${base}/${project.id}/export?revision=${record.revision}`; actions.append(download);
      if (record.revision !== project.revision) {
        const restore = elem('button', 'quiet', '恢复为新版本'); restore.addEventListener('click', async () => {
          restore.disabled = true; clearError();
          try { await post('restore', {revision: project.revision, target: record.revision}); await load(project.id); }
          catch (error) { fail(error); restore.disabled = false; }
        }); actions.append(restore);
      }
      header.append(actions); article.append(header);
      if (record.context_mode) article.append(elem('p', 'hint', `上下文：${{hierarchical: '全文背景 + 分批原文修订', session: 'Claude Code 项目会话（全文已在会话中）'}[record.context_mode] || '全文原文'} · ${(record.audit || []).length} 处修改完成模型一致性检查`));
      (record.outcomes || []).forEach(o => {
        const c = (record.comments || []).find(c => c.id === o.comment_id);
        if (c) article.append(elem('blockquote', '', `“${c.anchors.map(a => a.quote).join('')}”\n批注：${c.text}`));
        article.append(elem('p', 'hint', `${o.status === 'no_change' ? '无需改动' : '已处理'}：${o.reason}`));
      });
      (record.changes || []).forEach(change => {
        const details = elem('details'); details.append(elem('summary', '', `${change.block_id} · ${change.file} · ${change.reason}`));
        const diff = elem('div', 'diff-grid'); diff.append(elem('pre', 'before', change.before), elem('pre', 'after', change.after)); details.append(diff);
        (change.evidence || []).forEach(e => details.append(elem('p', 'hint evidence', `项目证据（已核对原文）：${e.path}「${e.quote}」`)));
        article.append(details);
      });
      $('historyList').append(article);
    });
  }
  function renderContext() {
    $('contextPanel').hidden = false;
    const context = snapshot.context;
    $('contextSummary').textContent = `${snapshot.files.length} 份正文 · ${(snapshot.bibliography || []).length} 份参考文献库 · ${Object.keys(snapshot.references || {}).length} 条文献元数据。${project.origin?.path ? ` 只读来源：${project.origin.path}。` : ''} ` +
      (context ? `全文背景覆盖 ${context.covered_sources} 个原文片段。${context.notice}` : '首次生成中文前，将阅读全文建立背景和共享术语；改稿后按新版本更新。');
    $('importWarnings').replaceChildren();
    (snapshot.import_warnings || []).forEach(w => $('importWarnings').append(elem('li', 'hint', w)));
    (context?.term_conflicts || []).forEach(t => $('importWarnings').append(elem('li', 'hint', `术语 ${t.en} 存在不同译法，本版本采用“${t.chosen}”（另有“${t.alternative}”）。`)));
    $('contextTerms').replaceChildren(); $('contextClaims').replaceChildren();
    if (!context) return;
    if (context.terms.length) {
      $('contextTerms').append(elem('h3', '', '本版本共享术语'));
      const list = elem('dl', 'glossary');
      context.terms.forEach(t => list.append(elem('dt', '', t.en), elem('dd', '', t.zh)));
      $('contextTerms').append(list);
    }
    const roles = {problem: '研究问题', method: '方法', result: '结果', limitation: '局限', argument: '论证', background: '背景'};
    context.chunks.forEach(chunk => chunk.claims.forEach(c => {
      const detail = elem('details', 'context-claim'); detail.append(elem('summary', '', `${roles[c.role]} · ${c.statement}`));
      detail.append(elem('p', 'hint', `原文定位：${c.source_id}`), elem('blockquote', '', c.quote));
      $('contextClaims').append(detail);
    }));
  }
  function captureSelection() {
    if (!project || active() || $('commentDialog').open) return;
    const selection = window.getSelection();
    if (!selection?.rangeCount || selection.isCollapsed) { $('selectionButton').hidden = true; return; }
    const range = selection.getRangeAt(0);
    const asElement = n => n.nodeType === Node.ELEMENT_NODE ? n : n.parentElement;
    const sideOf = n => asElement(n)?.closest('.en .prose') ? 'en' : asElement(n)?.closest('.zh .prose') ? 'zh' : null;
    const side = sideOf(range.startContainer);
    if (!side || side !== sideOf(range.endContainer)) { $('selectionButton').hidden = true; return; }
    const anchors = [];
    document.querySelectorAll(`.${side} .unit:not(.untranslated)`).forEach(node => {
      if (!range.intersectsNode(node)) return;
      const part = document.createRange(); part.selectNodeContents(node);
      if (node.contains(range.startContainer)) part.setStart(range.startContainer, range.startOffset);
      if (node.contains(range.endContainer)) part.setEnd(range.endContainer, range.endOffset);
      const quote = part.toString(); if (!quote) return;
      const prefix = document.createRange(); prefix.selectNodeContents(node); prefix.setEnd(part.startContainer, part.startOffset);
      const start = Array.from(prefix.toString()).length;
      anchors.push({side, unit_id: node.dataset.unit, block_id: node.dataset.block, start, end: start + Array.from(quote).length, quote});
    });
    if (!anchors.length) return;
    selected = anchors;
    const rect = range.getBoundingClientRect();
    $('selectionButton').style.left = `${Math.min(Math.max(rect.left, 10), window.innerWidth - 145)}px`;
    $('selectionButton').style.top = `${Math.min(Math.max(rect.bottom + 8, 10), window.innerHeight - 50)}px`;
    $('selectionButton').hidden = false;
  }
  $('selectionButton').addEventListener('pointerdown', e => e.preventDefault());
  $('selectionButton').addEventListener('click', () => {
    if (!selected.length) return;
    $('selectedQuote').textContent = selected.map(a => a.quote).join('');
    $('selectedLocation').textContent = `v${project.revision} · ${[...new Set(selected.map(a => a.block_id))].join(', ')} · ${selected.length} 个对齐单元`;
    $('commentText').value = ''; $('commentError').textContent = ''; $('scope').value = 'auto';
    $('selectionButton').hidden = true; $('commentDialog').showModal(); $('commentText').focus();
  });
  $('paper').addEventListener('pointerup', () => setTimeout(captureSelection, 0));
  $('paper').addEventListener('keyup', captureSelection);
  document.addEventListener('selectionchange', () => { if (window.getSelection()?.isCollapsed && !$('commentDialog').open) $('selectionButton').hidden = true; });
  window.addEventListener('scroll', () => { $('selectionButton').hidden = true; }, {passive: true});
  $('closeDialog').addEventListener('click', () => $('commentDialog').close());
  $('commentForm').addEventListener('submit', async e => {
    e.preventDefault(); const button = e.submitter; button.disabled = true;
    try { project = await post('comments', {revision: project.revision, text: $('commentText').value, scope: $('scope').value, anchors: selected}); $('commentDialog').close(); window.getSelection()?.removeAllRanges(); selected = []; renderComments(); renderControls(); markComments(); $('commentDrawer').hidden = false; }
    catch (error) { $('commentError').textContent = error.message; }
    finally { button.disabled = false; }
  });
  $('toggleImport').addEventListener('click', () => { $('importPanel').hidden = !$('importPanel').hidden; });
  $('commentsToggle').addEventListener('click', () => { $('commentDrawer').hidden = !$('commentDrawer').hidden; });
  $('closeComments').addEventListener('click', () => { $('commentDrawer').hidden = true; });
  // ---- Workflow: defaults come from 全局设置 → 双语改稿; edits here apply to this session only.
  const Workflow = window.PaperRevisionWorkflow;
  function jobEngine(job) {
    const stages = job?.config?.stages;
    if (!stages) return job?.config?.model ? ` · ${job.config.model}` : '';
    const stage = job.kind === 'translate' ? 'translate' : 'revise';
    return ` · ${Workflow.describe(engineOptions, stages[stage])}`;
  }
  function settingsLink(tab, text) { const link = elem('a', 'settings-link', text); link.href = `/settings#${tab}`; link.target = '_blank'; link.rel = 'noopener'; return link; }
  function setEngineOptions(options) {
    if (!options) return;
    const keep = workflowTouched && engineOptions ? Workflow.read($('workflowEditor')) : options.defaults;
    engineOptions = options;
    Workflow.render($('workflowEditor'), options, keep, () => { workflowTouched = true; syncWorkflow(); });
    syncWorkflow();
  }
  const folderReasons = {'CLAUDE.md': '含 CLAUDE.md', '.claude': '含 .claude 目录', history: '已有 Claude Code 会话记录',
                         manual: '手动指定', paper_folder: '论文所在目录（未找到所属的 Claude Code 项目，可更改）', workspace: '网页上传的论文没有本地目录，使用本工具的工作区'};
  function sessionCommand(session) { return `cd "${session.cwd}" && claude --resume ${session.id}`; }
  function renderSession() {
    if (!project || !engineOptions) return;
    const workflow = Workflow.read($('workflowEditor'));
    const claude = workflow.revise?.engine === 'claude';
    $('sessionBox').hidden = !claude;
    if (!claude) return;
    const on = project.claude_session_mode !== false, session = project.claude_session, folder = project.session_folder;
    const running = active();
    $('sessionMode').checked = on;
    ['sessionMode', 'sessionFolder', 'sessionReset'].forEach(id => { $(id).disabled = running || busy; });
    const lines = [];
    if (folder) lines.push(`项目目录：${folder.path}（${folderReasons[folder.reason] || folder.reason}）`);
    if (!on) lines.push('已关闭：每轮修订都是独立的新请求（同样带全文），不保存会话。');
    else if (session?.id && (!folder || session.cwd === folder.path)) {
      lines.push(`会话「${session.name}」· 已进行 ${session.rounds || 0} 轮 · 下一轮继续这个会话，只发送新批注和有变化的段落。`);
      lines.push(`在终端查看或继续：${sessionCommand(session)}`);
      if (session.last_error) lines.push(`⚠ 上一轮未应用：${session.last_error.slice(0, 160)}（下一轮会告知模型并同步真实文本）`);
    } else lines.push('下一轮将新建会话「双语改稿 · ' + project.title + '」，先发送全文。');
    $('sessionInfo').textContent = lines.join('\n');
    $('sessionReset').hidden = !session?.id;
    $('sessionCopy').hidden = !(on && session?.id);
  }
  async function updateSession(data) {
    clearError();
    try { const updated = await post('claude-session', {revision: project.revision, ...data}); project = {...updated, session_folder: undefined}; await load(project.id, true); }
    catch (error) { fail(error); }
  }
  $('sessionMode').addEventListener('change', () => updateSession({mode: $('sessionMode').checked}));
  $('sessionReset').addEventListener('click', () => { if (confirm('重新开始会话？下一轮会新建会话并重新发送全文；旧会话仍保留在 Claude Code 中。')) updateSession({reset: true}); });
  $('sessionFolder').addEventListener('click', async () => {
    try { const picked = await LocalPaths.pick({kind: 'folder', title: '选择论文所属的 Claude Code 项目目录'}); if (picked?.path) await updateSession({folder: picked.path}); }
    catch (error) { fail(error); }
  });
  $('sessionCopy').addEventListener('click', async () => {
    try { await navigator.clipboard.writeText(sessionCommand(project.claude_session)); $('sessionCopy').textContent = '已复制'; setTimeout(() => { $('sessionCopy').textContent = '复制打开命令'; }, 1500); }
    catch (error) { fail(new Error('无法写入剪贴板，请手动复制上方命令。')); }
  });
  function syncWorkflow() {
    if (!engineOptions) return;
    renderSession();
    const workflow = Workflow.read($('workflowEditor'));
    $('workflowSummary').textContent = `翻译 ${Workflow.describe(engineOptions, workflow.translate)}` +
      (workflow.translate.engine === 'google' ? `（补译 ${Workflow.describe(engineOptions, workflow.translate_fallback)}）` : '') +
      ` · 修订 ${Workflow.describe(engineOptions, workflow.revise)}${workflowTouched ? ' · 本次已改动' : ''}`;
    const issues = window.EnginePicker.issues(engineOptions, Workflow.usedEngines(workflow));
    $('workflowHint').replaceChildren(issues.length ? `⚠ ${issues.map(i => i.message).join('；')} · ` : '默认值来自 ',
      settingsLink(issues.length ? issues[0].settingsTab : 'workflow', issues.length ? '前往全局设置' : '全局设置 → 双语改稿'),
      issues.length ? '' : '；这里的改动只对当次会话有效。');
    $('workflowHint').classList.toggle('warning', issues.length > 0);
    $('billing').textContent = Workflow.billing(workflow) || '所选引擎';
  }
  $('translateToggle').addEventListener('change', async () => {
    const wanted = $('translateToggle').checked; $('translateToggle').disabled = true; clearError();
    try { project = await post('settings', {revision: project.revision, translate: wanted}); renderPaper(); }
    catch (error) { fail(error); }
    finally { renderControls(); }
  });
  $('usage').addEventListener('change', () => { if (project) renderControls(); });
  $('projectSelect').addEventListener('change', () => { clearTimeout(pollTimer); clearError(); if ($('projectSelect').value) openProject($('projectSelect').value).catch(fail); });
  // Settings drawer saved (e.g. logged in to Claude, added a Google key) → refresh engines without a reload.
  // EnginePicker already refreshed the engine caches server-side, so a plain read is current.
  window.EnginePicker.onChange(() => { api(base).then(r => setEngineOptions(r.options)).catch(() => {}); });
  $('refresh').addEventListener('click', () => {
    clearError();
    api(`${base}?refresh=1`).then(r => setEngineOptions(r.options)).catch(() => {});
    if (project) load(project.id).catch(fail); else list().catch(fail);
  });
  function showImportFiles(files) {
    importFiles = files;
    $('importFiles').textContent = files.length ? `已选择 ${files.length} 份文件：\n${files.map(f => f.name).join('\n')}` : '尚未选择文件。';
    refreshMainChoices().catch(() => {});
  }
  // ---- Main .tex file: a folder often holds several (main.tex, main_backup.tex, supplementary.tex …).
  const isMainTex = text => /\\documentclass\s*(?:\[[^\]]*\])?\s*\{/.test(text.replace(/(^|[^\\])%[^\n]*/g, '$1'));
  function rankMain(names) {
    const key = name => {
      const stem = name.split('/').pop().replace(/\.tex$/i, '').toLowerCase();
      return [stem !== 'main', /backup|old|copy|draft|bak|tmp/.test(stem), name.split('/').length, name.length, name.toLowerCase()];
    };
    return [...names].sort((a, b) => { const x = key(a), y = key(b); for (let i = 0; i < x.length; i++) if (x[i] !== y[i]) return x[i] < y[i] ? -1 : 1; return 0; });
  }
  let mainRequest = 0;
  async function refreshMainChoices() {
    const request = ++mainRequest, path = $('localPath').value.trim();
    let candidates = [];
    if (importFiles.length) {
      for (const item of importFiles.filter(f => /\.tex$/i.test(f.name))) if (isMainTex(await item.file.text())) candidates.push(item.name);
      candidates = rankMain(candidates);
    } else if (path && !/\.tex"?$/i.test(path)) {
      try { candidates = (await api('/api/paper-revision/local-candidates', {method: 'POST', body: JSON.stringify({path})})).candidates; }
      catch { candidates = []; }
    }
    if (request !== mainRequest) return;  // a newer selection superseded this one
    const previous = $('mainTex').value;
    $('mainTex').replaceChildren(...candidates.map(name => new Option(name, name)));
    if (candidates.includes(previous)) $('mainTex').value = previous;
    $('mainTexField').hidden = !candidates.length;
    $('mainTexHint').textContent = candidates.length > 1
      ? `找到 ${candidates.length} 个含 \\documentclass 的文件。只导入所选主文件，以及它通过 \\input / \\include / 参考文献引用的文件。`
      : candidates.length ? '只导入这个主文件及其引用的文件。' : '';
  }
  $('localPath').addEventListener('change', () => refreshMainChoices().catch(() => {}));
  // ---- Project settings: name, local source and main .tex of an existing project.
  async function candidatesFor(path) {
    if (!path || /\.tex"?$/i.test(path)) return [];
    try { return (await api('/api/paper-revision/local-candidates', {method: 'POST', body: JSON.stringify({path})})).candidates; }
    catch { return []; }
  }
  async function refreshProjectMain(selected) {
    const candidates = await candidatesFor($('projectPath').value.trim());
    $('projectMain').replaceChildren(...candidates.map(name => new Option(name, name)));
    if (selected && candidates.includes(selected)) $('projectMain').value = selected;
    $('projectMainField').hidden = !candidates.length;
  }
  $('projectSettings').addEventListener('click', async () => {
    const origin = project.origin || {};
    $('projectTitle').value = project.title; $('projectError').textContent = '';
    $('projectPath').value = origin.path || '';
    $('projectSourceInfo').textContent = origin.path
      ? `当前：${origin.path}${origin.main ? ` · 主文件 ${origin.main}` : ''}（每次打开时从这里重新读取）`
      : '当前：网页上传的副本，没有本地路径。填写论文所在目录后，以后每次打开都会从磁盘重新读取。';
    await refreshProjectMain(origin.main);
    $('projectDialog').showModal(); $('projectTitle').focus();
  });
  $('projectPath').addEventListener('change', () => refreshProjectMain($('projectMain').value));
  for (const [id, kind] of [['projectBrowseFolder', 'folder'], ['projectBrowseFile', 'file']]) {
    $(id).addEventListener('click', async () => {
      try {
        const picked = await LocalPaths.pick(kind === 'folder' ? {kind, title: '选择论文所在目录'} : {kind, title: '选择主 .tex 文件', suffixes: ['.tex']});
        if (picked?.path) { $('projectPath').value = picked.path; await refreshProjectMain(); }
      } catch (error) { $('projectError').textContent = error.message; }
    });
  }
  ['closeProjectDialog', 'cancelProjectDialog'].forEach(id => $(id).addEventListener('click', () => $('projectDialog').close()));
  $('projectForm').addEventListener('submit', async e => {
    e.preventDefault(); const button = e.submitter; button.disabled = true; $('projectError').textContent = '';
    const origin = project.origin || {}, path = $('projectPath').value.trim();
    const main = $('projectMainField').hidden ? null : $('projectMain').value;
    const data = {revision: project.revision, title: $('projectTitle').value};
    if (path && (path !== (origin.path || '') || (main || null) !== (origin.main || null))) Object.assign(data, {path, main});
    try {
      const result = await post('source', data);
      if (result.sync) visitNotes.push({kind: 'ok', text: `已按新的来源重新读取：${result.sync.changed} 句英文有变化，其余 ${result.sync.reused} 句保留原有中文。`});
      $('projectDialog').close();
      const option = [...$('projectSelect').options].find(o => o.value === project.id); if (option) option.textContent = result.project.title;
      await load(project.id);
    } catch (error) { $('projectError').textContent = error.message; }
    finally { button.disabled = false; }
  });
  // ---- Reading font size (this browser only).
  const fontSteps = [0.8, 0.9, 1, 1.1, 1.2, 1.35, 1.5, 1.7];
  function applyFontScale(scale) {
    document.documentElement.style.setProperty('--reading-scale', String(scale));
    $('fontScaleLabel').textContent = `${Math.round(scale * 100)}%`;
    $('fontSmaller').disabled = scale <= fontSteps[0]; $('fontLarger').disabled = scale >= fontSteps.at(-1);
    try { localStorage.setItem('paperRevisionFontScale', String(scale)); } catch { /* preference only */ }
  }
  const currentScale = () => Number(getComputedStyle(document.documentElement).getPropertyValue('--reading-scale')) || 1;
  $('fontSmaller').addEventListener('click', () => applyFontScale([...fontSteps].reverse().find(v => v < currentScale() - 1e-6) ?? fontSteps[0]));
  $('fontLarger').addEventListener('click', () => applyFontScale(fontSteps.find(v => v > currentScale() + 1e-6) ?? fontSteps.at(-1)));
  $('fontReset').addEventListener('click', () => applyFontScale(1));
  applyFontScale((() => { try { const v = Number(localStorage.getItem('paperRevisionFontScale')); return fontSteps.includes(v) ? v : 1; } catch { return 1; } })());
  $('files').addEventListener('change', () => { showImportFiles([]); $('folder').value = ''; try { showImportFiles(PaperRevisionImport.fromFiles($('files').files)); } catch (e) { fail(e); } });
  $('folder').addEventListener('change', () => { showImportFiles([]); $('files').value = ''; try { showImportFiles(PaperRevisionImport.fromFiles($('folder').files, true)); } catch (e) { fail(e); } });
  async function browseLocalPath(kind) {
    clearError();
    try {
      const result = await LocalPaths.pick(kind === 'folder' ? {kind, title: '选择 LaTeX 项目目录'} : {kind, title: '选择主 .tex 文件', suffixes: ['.tex']});
      if (result) { $('localPath').value = result.path; $('files').value = ''; $('folder').value = ''; showImportFiles([]); }
    } catch (error) { fail(error); }
  }
  $('browseLocalFile').addEventListener('click', () => browseLocalPath('file'));
  $('browseLocalFolder').addEventListener('click', () => browseLocalPath('folder'));
  $('clearFiles').addEventListener('click', () => { showImportFiles([]); $('files').value = ''; $('folder').value = ''; });
  $('dropZone').addEventListener('click', () => $('files').click());
  $('dropZone').addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); $('files').click(); } });
  ['dragenter', 'dragover'].forEach(name => $('dropZone').addEventListener(name, e => { e.preventDefault(); $('dropZone').classList.add('dragging'); }));
  $('dropZone').addEventListener('dragleave', () => $('dropZone').classList.remove('dragging'));
  $('dropZone').addEventListener('drop', async e => {
    e.preventDefault(); $('dropZone').classList.remove('dragging'); clearError();
    if (importReading) return;
    importReading = true; $('importStatus').textContent = '正在读取拖入的项目…';
    try {
      if (e.dataTransfer.files.length || Array.from(e.dataTransfer.items || []).some(i => i.kind === 'file')) {
        showImportFiles([]);
        const files = await PaperRevisionImport.fromDrop(e.dataTransfer);
        if (!files.length) throw new Error('拖入内容中没有 .tex / .bib 文件。');
        showImportFiles(files); $('files').value = ''; $('folder').value = '';
      } else {
        const path = e.dataTransfer.getData('text/plain').trim();
        if (!path || /\r|\n/.test(path) || !/^(?:[A-Za-z]:[\\/]|\/|\\\\|file:)/i.test(path)) throw new Error('拖入的文字不是绝对路径。请拖入文件、目录，或粘贴完整的本地路径。');
        $('localPath').value = path;
        refreshMainChoices().catch(() => {});
      }
    } catch (error) { fail(error); }
    finally { importReading = false; $('importStatus').textContent = ''; }
  });
  window.addEventListener('dragover', e => { if (Array.from(e.dataTransfer.types || []).includes('Files')) e.preventDefault(); });
  window.addEventListener('drop', e => { if (Array.from(e.dataTransfer.types || []).includes('Files')) e.preventDefault(); });
  $('importForm').addEventListener('submit', async e => {
    e.preventDefault(); clearError(); const button = e.submitter; button.disabled = true;
    try {
      if (importReading) throw new Error('正在读取目录，请稍后再导入。');
      const data = new FormData(); data.set('title', $('title').value); data.set('translate', $('importTranslate').checked ? '1' : '0');
      const path = $('localPath').value.trim(), source = $('source').value.trim();
      if ([!!importFiles.length, !!path, !!source].filter(Boolean).length !== 1) throw new Error('请选择一种导入方式：文件/目录、本地路径、或粘贴正文。');
      if (!$('mainTexField').hidden && $('mainTex').value && !/\.tex"?$/i.test(path)) data.set('main', $('mainTex').value);
      if (path) data.set('local_path', path);
      else if (importFiles.length) importFiles.forEach(item => data.append('files', item.file, item.name));
      else data.set('source', $('source').value);
      const created = await api(base, {method: 'POST', body: data});
      $('importPanel').hidden = true; $('importForm').reset(); showImportFiles([]); await list(created.id);
    } catch (error) { fail(error); } finally { button.disabled = false; }
  });
  $('submit').addEventListener('click', async () => {
    if (!project || busy) return;
    if (!$('usage').checked) {
      const consent = document.querySelector('.consent');
      consent.classList.remove('attention'); void consent.offsetWidth; consent.classList.add('attention');
      $('usage').focus();
      fail(new Error('请先勾选“提交时使用所选模型处理论文与批注”，确认使用该模型后再提交。'));
      return;
    }
    busy = true; clearError(); renderControls();
    try { project = await post('submit', {revision: project.revision, comment_ids: project.comments.map(c => c.id), workflow: Workflow.read($('workflowEditor')), confirm_usage: $('usage').checked}); $('commentDrawer').hidden = true; await load(project.id); }
    catch (error) { fail(error); } finally { busy = false; renderControls(); }
  });
  $('cancel').addEventListener('click', async () => { try { await post('cancel', {}); await load(project.id); } catch (error) { fail(error); } });
  window.addEventListener('beforeunload', e => {
    if (($('commentDialog').open && $('commentText').value.trim()) || commentsUnexported()) { e.preventDefault(); e.returnValue = ''; }
  });
  $('exportComments').addEventListener('click', () => { setTimeout(() => load(project.id, true).catch(() => {}), 800); });
  // Second opinion from ChatGPT: comments + current English source go to the Prompt workbench (it owns the bridge).
  function sendCommentsToPrompts() {
    if (!project || !window.PromptHandoff) return;
    if (!project.comments.length) { fail(new Error('本轮还没有批注。先在中文或英文上圈选文字添加批注，再送到 ChatGPT。')); return; }
    const sideName = {zh: '中文对照', en: '英文原文'};
    const blocks = project.comments.map((comment, index) => {
      const sides = [...new Set(comment.anchors.map(a => sideName[a.side || 'zh']))].join(' / ');
      const quote = comment.anchors.map(a => a.quote).join(' … ');
      return `【批注 ${index + 1}】（${sides} · 范围：${scopes[comment.scope] || comment.scope}）\n引用：「${quote}」\n批注：${comment.text}`;
    });
    const text = [
      `请审阅论文《${project.title}》（v${project.revision}）的以下修改批注。对每条批注，给出对应英文句子的修改建议：保留 LaTeX 命令、引用键和公式，只改与批注相关的内容，并用中文简要说明理由。附件 ZIP 中的 source/ 是当前英文 LaTeX 原稿。`,
      ...blocks,
    ].join('\n\n');
    const safeTitle = (project.title || 'paper').replace(/[\\/:*?"<>|\s]+/g, ' ').trim().slice(0, 100) || 'paper';
    try {
      window.PromptHandoff.send({text, source: {label: '双语改稿', title: project.title},
        attachments: [{name: `${safeTitle}-v${project.revision}.zip`, url: `${base}/${encodeURIComponent(project.id)}/export?revision=${project.revision}`}]});
    } catch (error) { fail(error); }
  }
  $('sendCommentsToPrompts').addEventListener('click', sendCommentsToPrompts);
  $('importComments').addEventListener('click', () => $('importCommentsFile').click());
  $('importCommentsFile').addEventListener('change', async () => {
    const file = $('importCommentsFile').files[0]; $('importCommentsFile').value = '';
    if (!file) return;
    try {
      const result = await post('comments/import', {revision: project.revision, data: JSON.parse(await file.text())});
      project = {...result.project, session_folder: project.session_folder};
      visitNotes.push({kind: result.unplaced.length ? 'warn' : 'ok', text: `已导入 ${result.imported} 条批注` +
        (result.unplaced.length ? `；${result.unplaced.length} 条因原文已改动无法定位：${result.unplaced.join('；').slice(0, 300)}` : '。')});
      renderComments(); renderControls(); markComments(); renderBanners();
    } catch (error) { fail(error instanceof SyntaxError ? new Error('批注文件不是有效的 JSON。') : error); }
  });
  $('retranslate').addEventListener('click', async () => {
    if (!confirm('清空全部中文并重新翻译整篇？（旧译文保留在历史版本中；之后点“生成中文对照”开始翻译）')) return;
    clearError();
    try { await post('retranslate', {revision: project.revision}); await load(project.id); }
    catch (error) { fail(error); }
  });
  list().catch(fail);
})();
