/* Workflow editor for 双语改稿: one engine (and model / effort / preset) per workflow step.
   Shared by 全局设置 → 双语改稿 (defaults) and the revision page (this session's choice).
   Each row is an EnginePicker.field (static/engine_picker.js); this file only lays out the stages. */
(() => {
  'use strict';
  const Picker = window.EnginePicker;
  const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text !== undefined) n.textContent = text; return n; };

  function render(container, options, value, onChange) {
    container.replaceChildren();
    container.classList.add('wf-editor');
    const current = JSON.parse(JSON.stringify(value || options.defaults || {}));
    const rows = {};
    (options.stages || []).forEach(stage => {
      const row = el('div', 'wf-row'); row.dataset.stage = stage.id;
      const label = el('label', 'wf-label', stage.label);
      const id = `wf-${container.id || 'editor'}-${stage.id}`;
      label.htmlFor = id;
      row.append(label);
      Picker.field(row, options, {
        engines: stage.engines, value: current[stage.id], recommended: options.recommended?.[stage.id], id,
        onChange: (_item, {engineChanged}) => { if (engineChanged) sync(); onChange?.(read(container)); },
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
    container.querySelectorAll('.wf-row').forEach(row => { workflow[row.dataset.stage] = Picker.read(row); });
    return workflow;
  }

  // Engines a workflow will actually call (the fallback step only runs behind Google).
  function usedEngines(workflow) {
    return Object.entries(workflow || {})
      .filter(([stage]) => stage !== 'translate_fallback' || workflow.translate?.engine === 'google')
      .map(([, item]) => item.engine);
  }

  window.PaperRevisionWorkflow = {
    render, read, usedEngines,
    describe: Picker.describe,
    billing: workflow => Picker.billing(usedEngines(workflow)),
  };
})();
