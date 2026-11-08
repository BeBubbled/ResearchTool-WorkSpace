/* Workflow editor for 双语改稿: one engine (and model / effort / preset) per workflow step.
   Shared by 全局设置 → 双语改稿 (defaults) and the revision page (this session's choice). */
(() => {
  'use strict';
  const EFFORTS = {low: '低', medium: '中', high: '高', xhigh: '很高', max: '最高', minimal: '极低', none: '无'};
  const FALLBACK_CLAUDE_MODELS = [{id: 'opus', label: 'Opus · 质量优先'}, {id: 'sonnet', label: 'Sonnet · 更快、更省额度'}];
  const FALLBACK_EFFORTS = ['low', 'medium', 'high', 'xhigh', 'max'];
  const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text !== undefined) n.textContent = text; return n; };
  const option = (value, label, selected, disabled) => { const o = new Option(label, value, false, !!selected); o.disabled = !!disabled; return o; };

  function engineChoices(options, stage) {
    const choices = [];
    stage.engines.forEach(engine => {
      if (engine === 'claude') choices.push({value: 'claude', label: `Claude Code${options.claude?.plan ? '' : '（未登录）'}`});
      if (engine === 'codex') choices.push({value: 'codex', label: `Codex${options.codex?.ready ? '' : '（未登录）'}`});
      if (engine === 'google') choices.push({value: 'google', label: `Google 翻译 Basic${options.google?.configured ? '' : '（未配置 Key）'}`});
      if (engine === 'api') {
        if (options.api?.length) options.api.forEach(p => choices.push({value: `api:${p.id}`, label: `API · ${p.name}${p.model ? `（${p.model}）` : ''}`}));
        else choices.push({value: 'api:', label: 'API（尚无预设）', disabled: true});
      }
    });
    return choices;
  }
  const engineValue = item => item?.engine === 'api' ? `api:${item.preset || ''}` : item?.engine || '';

  function detailFor(options, item, recommended) {
    const box = el('span', 'wf-detail');
    if (item.engine === 'claude') {
      const models = options.claude?.models?.length ? options.claude.models : FALLBACK_CLAUDE_MODELS;
      const efforts = options.claude?.efforts?.length ? options.claude.efforts : FALLBACK_EFFORTS;
      const model = el('select', 'wf-model'); model.setAttribute('aria-label', '模型');
      models.forEach(m => model.add(option(m.id, m.label + (recommended?.engine === 'claude' && recommended.model === m.id ? '（推荐）' : ''), m.id === item.model)));
      const effort = el('select', 'wf-effort'); effort.setAttribute('aria-label', '推理强度');
      efforts.forEach(e => effort.add(option(e, `推理 ${EFFORTS[e] || e}`, e === item.effort)));
      if (!models.some(m => m.id === item.model)) model.value = models[0].id;
      if (!efforts.includes(item.effort)) effort.value = efforts.includes('medium') ? 'medium' : efforts[0];
      box.append(model, effort);
    } else if (item.engine === 'codex') {
      const codex = options.codex || {};
      const model = el('select', 'wf-model'); model.setAttribute('aria-label', 'Codex 模型');
      model.add(option('', `全局默认${codex.defaultModel ? `（${codex.defaultModel}）` : ''}`, !item.model));
      (codex.models || []).forEach(m => model.add(option(m.id, m.label, m.id === item.model)));
      if (item.model && ![...model.options].some(o => o.value === item.model)) model.add(option(item.model, `${item.model}（当前不可用）`, true));
      const chosen = (codex.models || []).find(m => m.id === (item.model || codex.defaultModel));
      const effort = el('select', 'wf-effort'); effort.setAttribute('aria-label', 'Codex 推理强度');
      effort.add(option('', `推理 默认${codex.defaultEffort ? `（${EFFORTS[codex.defaultEffort] || codex.defaultEffort}）` : ''}`, !item.effort));
      (chosen?.efforts?.length ? chosen.efforts : []).forEach(e => effort.add(option(e, `推理 ${EFFORTS[e] || e}`, e === item.effort)));
      if (item.effort && ![...effort.options].some(o => o.value === item.effort)) effort.add(option(item.effort, `推理 ${EFFORTS[item.effort] || item.effort}`, true));
      box.append(model, effort);
    } else if (item.engine === 'google') {
      box.append(el('span', 'wf-note', options.google?.configured ? '快；对不齐的句子交给下一行补译' : '需在 全局设置 → Google 翻译 配置 Key'));
    } else if (item.engine === 'api') {
      box.append(el('span', 'wf-note', '模型与并发由 API 预设决定'));
    }
    return box;
  }

  function readRow(row) {
    const value = row.querySelector('.wf-engine').value;
    if (value.startsWith('api:')) return {engine: 'api', preset: value.slice(4)};
    const item = {engine: value};
    const model = row.querySelector('.wf-model'), effort = row.querySelector('.wf-effort');
    if (model) item.model = model.value;
    if (effort) item.effort = effort.value;
    return item;
  }

  function render(container, options, value, onChange) {
    container.replaceChildren();
    container.classList.add('wf-editor');
    const current = JSON.parse(JSON.stringify(value || options.defaults || {}));
    const rows = {};
    (options.stages || []).forEach(stage => {
      const row = el('div', 'wf-row'); row.dataset.stage = stage.id;
      const label = el('label', 'wf-label', stage.label);
      const engine = el('select', 'wf-engine'); engine.id = `wf-${container.id || 'editor'}-${stage.id}`; label.htmlFor = engine.id;
      engineChoices(options, stage).forEach(c => engine.add(option(c.value, c.label, c.value === engineValue(current[stage.id]), c.disabled)));
      if (![...engine.options].some(o => o.value === engineValue(current[stage.id]))) {
        const missing = engineValue(current[stage.id]);
        if (missing) engine.add(option(missing, missing.startsWith('api:') ? `API · ${missing.slice(4)}（预设已不存在）` : missing, true));
      }
      const item = current[stage.id] || {engine: engine.value};
      row.append(label, engine, detailFor(options, item, options.recommended?.[stage.id]));
      engine.addEventListener('change', () => {
        const next = readRow(row);
        if (next.engine === 'claude') Object.assign(next, options.recommended?.[stage.id]?.engine === 'claude' ? options.recommended[stage.id] : {model: 'sonnet', effort: 'medium'}, {engine: 'claude'});
        if (next.engine === 'codex') { next.model = ''; next.effort = ''; }
        row.querySelector('.wf-detail').replaceWith(detailFor(options, next, options.recommended?.[stage.id]));
        sync(); onChange?.(read(container));
      });
      row.addEventListener('change', event => {
        if (event.target.classList.contains('wf-model') && row.querySelector('.wf-engine').value === 'codex') {
          const next = readRow(row); next.effort = '';
          row.querySelector('.wf-detail').replaceWith(detailFor(options, next, options.recommended?.[stage.id]));
        }
        if (!event.target.classList.contains('wf-engine')) onChange?.(read(container));
      });
      rows[stage.id] = row;
      container.append(row);
    });
    function sync() {
      const google = rows.translate?.querySelector('.wf-engine').value === 'google';
      if (rows.translate_fallback) rows.translate_fallback.hidden = !google;
    }
    sync();
  }

  function read(container) {
    const workflow = {};
    container.querySelectorAll('.wf-row').forEach(row => { workflow[row.dataset.stage] = readRow(row); });
    return workflow;
  }

  function describe(options, item) {
    if (!item) return '';
    if (item.engine === 'google') return 'Google 翻译';
    if (item.engine === 'claude') return `Claude ${item.model || ''}·${EFFORTS[item.effort] || item.effort || ''}`;
    if (item.engine === 'codex') return `Codex ${item.model || options?.codex?.defaultModel || '默认'}`;
    if (item.engine === 'api') return `API ${(options?.api || []).find(p => p.id === item.preset)?.name || item.preset}`;
    return item.engine;
  }

  function billing(workflow) {
    const used = new Set(Object.entries(workflow || {}).filter(([stage]) => stage !== 'translate_fallback' || workflow.translate?.engine === 'google').map(([, item]) => item.engine));
    const parts = [];
    if (used.has('claude')) parts.push('消耗 Claude 套餐额度');
    if (used.has('codex')) parts.push('消耗 Codex 套餐额度');
    if (used.has('api')) parts.push('API 独立计费');
    if (used.has('google')) parts.push('Google Basic 按字符计费，每月前 50 万字符免费');
    return parts.join('；');
  }

  window.PaperRevisionWorkflow = {render, read, describe, billing};
})();
