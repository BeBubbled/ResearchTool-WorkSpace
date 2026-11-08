/* Hand text (and same-origin attachments) from any client page to the Prompt workbench.
   The ChatGPT bridge extension only runs on /prompts, so pages like 精读 and 改稿 do not talk to
   ChatGPT directly: they leave a one-shot handoff in localStorage and open /prompts?handoff=1,
   where the workbench appends the text, fetches the attachments and the user sends as usual. */
(() => {
  'use strict';
  const KEY = 'research-prompt-handoff-v1';
  const MAX_AGE_MS = 10 * 60 * 1000;
  const MAX_TEXT = 100000;

  function clean(payload) {
    if (!payload || typeof payload.text !== 'string' || !payload.text.trim()) return null;
    const attachments = (Array.isArray(payload.attachments) ? payload.attachments : [])
      // Only this server's own API paths: never fetch an arbitrary URL from storage.
      .filter(item => item && typeof item.url === 'string' && /^\/api\/[^/]/.test(item.url) && typeof item.name === 'string')
      .slice(0, 20)
      .map(item => ({name: item.name.slice(0, 240) || 'attachment', url: item.url}));
    const source = payload.source && typeof payload.source === 'object' ? payload.source : {};
    return {
      text: payload.text.slice(0, MAX_TEXT),
      attachments,
      source: {label: String(source.label || '其他页面').slice(0, 40), title: String(source.title || '').slice(0, 200)},
      created: Number(payload.created) || 0,
    };
  }

  function build(payload, now = Date.now()) {
    const item = clean({...payload, created: now});
    if (!item) throw new Error('没有可发送的文字。');
    return item;
  }

  function send(payload, storage = window.localStorage, open = url => window.open(url, '_blank')) {
    const item = build(payload);
    try { storage.setItem(KEY, JSON.stringify(item)); }
    catch { throw new Error('浏览器存储不可用，无法交给 Prompt 工作台。请复制文字后手动粘贴。'); }
    const url = '/prompts?handoff=1';
    if (!open(url)) window.location.href = url;
    return item;
  }

  // One-shot read on the workbench: returns the handoff once, then forgets it.
  function take(storage = window.localStorage, now = Date.now()) {
    let raw = null;
    try { raw = storage.getItem(KEY); storage.removeItem(KEY); } catch { return null; }
    let item = null;
    try { item = clean(JSON.parse(raw || 'null')); } catch { return null; }
    if (!item || now - item.created > MAX_AGE_MS || item.created > now + 60000) return null;
    return item;
  }

  const api = {KEY, MAX_AGE_MS, build, send, take};
  if (typeof module !== 'undefined') module.exports = api;
  if (typeof window !== 'undefined') window.PromptHandoff = api;
})();
