/* Shared AI engine layer for every client page.
   - EnginePicker.load()      → GET /api/engines (Claude Code, Codex, Google, API presets), cached per page.
   - EnginePicker.onChange(f) → f(catalog) after the settings drawer saved something; pages refresh in place.
   - EnginePicker.field(...)  → one engine + model + effort picker, used by the revision workflow editor
                                and any page that needs "which engine runs this step".
   The catalog shape is engines.py's: {claude, codex, google, api: [...], ready: [...]}. */
(() => {
  'use strict';
  if (window.EnginePicker) return;  // included by the rail and by pages that need it first
  const EFFORTS = {low: '低', medium: '中', high: '高', xhigh: '很高', max: '最高', minimal: '极低', none: '无'};
  const FALLBACK_CLAUDE_MODELS = [{id: 'opus', label: 'Opus · 质量优先'}, {id: 'sonnet', label: 'Sonnet · 更快、更省额度'}];
  const FALLBACK_EFFORTS = ['low', 'medium', 'high', 'xhigh', 'max'];
  const SETTINGS_TAB = {claude: 'claude', codex: 'codex', google: 'google', api: 'llm'};
  const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text !== undefined) n.textContent = text; return n; };
  const option = (value, label, selected, disabled) => { const o = new Option(label, value, false, !!selected); o.disabled = !!disabled; return o; };

  let pending = null, catalog = null;
  const listeners = new Set();

  function load({refresh = false} = {}) {
    if (!refresh && pending) return pending;
    pending = fetch(`/api/engines${refresh ? '?refresh=1' : ''}`)
      .then(r => r.ok ? r.json() : Promise.reject(new Error(`引擎状态不可用（${r.status}）`)))
      .then(data => (catalog = data.engines))
      .catch(error => { pending = null; throw error; });
    return pending;
  }
  function onChange(listener) { listeners.add(listener); return () => listeners.delete(listener); }
  async function refresh() {
    const next = await load({refresh: true});
    listeners.forEach(listener => { try { listener(next); } catch (error) { console.error(error); } });
    return next;
  }
  // Settings drawer (client_shell.js) closed after a save → every engine-aware page refreshes in place.
  window.addEventListener('client:settings-closed', event => { if (event.detail?.changed) refresh().catch(() => {}); });

  /* ---- one engine/model/effort field ---- */
  const ready = (options, engine) => engine === 'api' ? !!options?.api?.length : !!options?.[engine]?.ready;
  const NOT_READY = {claude: '（未登录）', codex: '（未登录）', google: '（未配置 Key）'};

  function choices(options, engines) {
    const list = [];
    (engines || ['claude', 'codex', 'google', 'api']).forEach(engine => {
      if (engine === 'claude') list.push({value: 'claude', label: `Claude Code${ready(options, 'claude') ? '' : NOT_READY.claude}`});
      if (engine === 'codex') list.push({value: 'codex', label: `Codex${ready(options, 'codex') ? '' : NOT_READY.codex}`});
      if (engine === 'google') list.push({value: 'google', label: `Google 翻译 Basic${ready(options, 'google') ? '' : NOT_READY.google}`});
      if (engine === 'api') {
        if (options?.api?.length) options.api.forEach(p => list.push({value: `api:${p.id}`, label: `API · ${p.name}${p.model ? `（${p.model}）` : ''}`}));
        else list.push({value: 'api:', label: 'API（尚无预设）', disabled: true});
      }
    });
    return list;
  }
  const engineValue = item => item?.engine === 'api' ? `api:${item.preset || ''}` : item?.engine || '';

  function detail(options, item, recommended) {
    const box = el('span', 'wf-detail');
    if (item.engine === 'claude') {
      const models = options?.claude?.models?.length ? options.claude.models : FALLBACK_CLAUDE_MODELS;
      const efforts = options?.claude?.efforts?.length ? options.claude.efforts : FALLBACK_EFFORTS;
      const model = el('select', 'wf-model'); model.setAttribute('aria-label', '模型');
      models.forEach(m => model.add(option(m.id, m.label + (recommended?.engine === 'claude' && recommended.model === m.id ? '（推荐）' : ''), m.id === item.model)));
      const effort = el('select', 'wf-effort'); effort.setAttribute('aria-label', '推理强度');
      efforts.forEach(e => effort.add(option(e, `推理 ${EFFORTS[e] || e}`, e === item.effort)));
      if (!models.some(m => m.id === item.model)) model.value = models[0].id;
      if (!efforts.includes(item.effort)) effort.value = efforts.includes('medium') ? 'medium' : efforts[0];
      box.append(model, effort);
    } else if (item.engine === 'codex') {
      const codex = options?.codex || {};
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
      box.append(el('span', 'wf-note', ready(options, 'google') ? '快；对不齐的句子交给下一行补译' : '需在 全局设置 → Google 翻译 配置 Key'));
    } else if (item.engine === 'api') {
      box.append(el('span', 'wf-note', '模型与并发由 API 预设决定'));
    }
    return box;
  }

  function readField(row) {
    const value = row.querySelector('.wf-engine').value;
    if (value.startsWith('api:')) return {engine: 'api', preset: value.slice(4)};
    const item = {engine: value};
    const model = row.querySelector('.wf-model'), effort = row.querySelector('.wf-effort');
    if (model) item.model = model.value;
    if (effort) item.effort = effort.value;
    return item;
  }

  /* Build one field inside `row`: <select.wf-engine> + model/effort detail.
     Calls onChange(item) on any user change; returns {read()}. */
  function field(row, options, {engines, value, recommended, id, onChange} = {}) {
    const engine = el('select', 'wf-engine');
    if (id) engine.id = id;
    choices(options, engines).forEach(c => engine.add(option(c.value, c.label, c.value === engineValue(value), c.disabled)));
    const missing = engineValue(value);
    if (missing && ![...engine.options].some(o => o.value === missing)) {
      engine.add(option(missing, missing.startsWith('api:') ? `API · ${missing.slice(4)}（预设已不存在）` : missing, true));
    }
    row.append(engine, detail(options, value || {engine: engine.value}, recommended));
    engine.addEventListener('change', () => {
      const next = readField(row);
      if (next.engine === 'claude') Object.assign(next, recommended?.engine === 'claude' ? recommended : {model: 'sonnet', effort: 'medium'}, {engine: 'claude'});
      if (next.engine === 'codex') { next.model = ''; next.effort = ''; }
      row.querySelector('.wf-detail').replaceWith(detail(options, next, recommended));
      onChange?.(readField(row), {engineChanged: true});
    });
    row.addEventListener('change', event => {
      if (event.target === engine) return;
      if (event.target.classList.contains('wf-model') && engine.value === 'codex') {
        const next = readField(row); next.effort = '';
        row.querySelector('.wf-detail').replaceWith(detail(options, next, recommended));
      }
      onChange?.(readField(row), {engineChanged: false});
    });
    return {engine, read: () => readField(row)};
  }

  function describe(options, item) {
    if (!item) return '';
    if (item.engine === 'google') return 'Google 翻译';
    if (item.engine === 'claude') return `Claude ${item.model || ''}·${EFFORTS[item.effort] || item.effort || ''}`;
    if (item.engine === 'codex') return `Codex ${item.model || options?.codex?.defaultModel || '默认'}`;
    if (item.engine === 'api') return `API ${(options?.api || []).find(p => p.id === item.preset)?.name || item.preset}`;
    return item.engine;
  }

  // What is not usable among the engines a task will call: [{engine, message, settingsTab}].
  function issues(options, engines) {
    const out = [];
    new Set(engines).forEach(engine => {
      if (engine === 'api' || ready(options, engine)) return;
      const fallback = {claude: 'Claude Code 未登录', codex: 'Codex 未登录', google: 'Google 翻译未配置 Key'}[engine];
      if (fallback) out.push({engine, message: options?.[engine]?.error || fallback, settingsTab: SETTINGS_TAB[engine]});
    });
    return out;
  }

  function billing(engines) {
    const used = new Set(engines), parts = [];
    if (used.has('claude')) parts.push('消耗 Claude 套餐额度');
    if (used.has('codex')) parts.push('消耗 Codex 套餐额度');
    if (used.has('api')) parts.push('API 独立计费');
    if (used.has('google')) parts.push('Google Basic 按字符计费，每月前 50 万字符免费');
    return parts.join('；');
  }

  // Per-engine cost acknowledgement (设置 › 费用确认), kept in this browser. When every engine a submission
  // uses is acknowledged, modules pre-tick their usage checkbox; the checkbox stays visible and the server
  // still requires it, so the user can always untick it for one run.
  const ACK_KEY = 'research-toolkit.billing-ack';
  const billingAck = {
    get() { try { const value = JSON.parse(localStorage.getItem(ACK_KEY) || '{}'); return value && typeof value === 'object' ? value : {}; } catch { return {}; } },
    set(engine, value) { const next = {...billingAck.get(), [engine]: Boolean(value)}; try { localStorage.setItem(ACK_KEY, JSON.stringify(next)); } catch {} return next; },
    covers(engines) { const ack = billingAck.get(); const list = [...new Set((engines || []).filter(Boolean))]; return list.length > 0 && list.every(engine => ack[engine] === true); },
  };

  window.EnginePicker = {
    EFFORTS, SETTINGS_TAB, load, refresh, onChange, current: () => catalog,
    choices, field, read: readField, describe, issues, billing, ready, billingAck,
  };
})();
