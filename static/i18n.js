/* Interface language (设置 › 通用 › 外观 › 语言). The client is written in Chinese; with English selected this
   translates the interface in place from the dictionary in static/i18n_en.js: text nodes and the title /
   placeholder / aria-label attributes, matched on the whole (trimmed) string, plus a few patterns for strings
   with numbers. A MutationObserver keeps up with what the modules render later. Paper content, translations,
   notes, comments and anything inside [data-i18n-skip] are never touched. Switching back to Chinese restores
   the original strings without a reload. Saved per browser in localStorage ("research-lang"). */
(() => {
  "use strict";
  if (window.ClientI18n) return;
  const KEY = "research-lang";
  const HAN = /[㐀-鿿]/;
  const ATTRS = ["title", "placeholder", "aria-label"];
  // Reading content and user text: never translated.
  const SKIP = [
    "script", "style", "noscript", "textarea", "code", "pre", "kbd", "samp", "svg", "mjx-container", "math",
    "[data-i18n-skip]", "[contenteditable]", ".paper-content", ".answer-panel", ".notes-panel", "#outline",
    "#paper > :not(.empty)", "#chapterList .chapter-link", "#comments .comment-text", "#selectedQuote", "blockquote",
    ".textLayer", ".pdf-page", "#historyList pre", ".palette-detail",
  ].join(",");
  const DICT_URL = "/static/i18n_en.js?v=2";

  let lang = read();
  let dict = null;
  let patterns = [];
  let fragments = [];
  let observer = null;
  const originalText = new WeakMap();   // Text node → Chinese original
  const originalAttrs = new WeakMap();  // Element → { attr: Chinese original }
  let originalTitle = null;

  function read() { try { return localStorage.getItem(KEY) === "en" ? "en" : "zh"; } catch { return "zh"; } }

  function lookup(text) {
    const key = text.replace(/\s+/g, " ").trim();
    if (!key || !HAN.test(key)) return null;
    if (Object.prototype.hasOwnProperty.call(dict, key)) return dict[key];
    for (const [pattern, replacement] of patterns) {
      if (pattern.test(key)) return fragments.reduce((text, [zh, en]) => text.split(zh).join(en), key.replace(pattern, replacement));
    }
    // "标签：" / "标签:" — translate the label, keep the colon.
    const colon = key.match(/^(.+?)[：:]$/);
    if (colon && Object.prototype.hasOwnProperty.call(dict, colon[1])) return `${dict[colon[1]]}:`;
    // "… ▾" / "… ↗" / "← …" style decorations around a known label.
    const deco = key.match(/^([←→↗▾▸›‹＋+·•✦\s]*)(.+?)([\s…↗▾▸›‹→]*)$/u);
    if (deco && (deco[1] || deco[3]) && Object.prototype.hasOwnProperty.call(dict, deco[2])) return `${deco[1]}${dict[deco[2]]}${deco[3]}`;
    return null;
  }

  function skipped(element) { return Boolean(element?.closest?.(SKIP)); }

  function translateTextNode(node) {
    const current = node.nodeValue;
    if (!current || !HAN.test(current)) return;
    const english = lookup(current);
    if (english == null) return;
    originalText.set(node, current);
    const lead = current.match(/^\s*/)[0], trail = current.match(/\s*$/)[0];
    node.nodeValue = `${lead}${english}${trail}`;
  }

  function translateAttrs(element) {
    for (const attr of ATTRS) {
      const value = element.getAttribute(attr);
      if (!value || !HAN.test(value)) continue;
      const english = lookup(value);
      if (english == null) continue;
      const saved = originalAttrs.get(element) || {};
      saved[attr] = value;
      originalAttrs.set(element, saved);
      element.setAttribute(attr, english);
    }
  }

  function translateTree(root) {
    if (!root) return;
    if (root.nodeType === Node.TEXT_NODE) { if (!skipped(root.parentElement)) translateTextNode(root); return; }
    if (root.nodeType !== Node.ELEMENT_NODE) return;
    if (skipped(root)) { if (root.matches("textarea") && !root.closest("[data-i18n-skip]")) translateAttrs(root); return; }
    translateAttrs(root);
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT, {
      acceptNode: node => {
        if (node.nodeType !== Node.ELEMENT_NODE || !node.matches(SKIP)) return NodeFilter.FILTER_ACCEPT;
        // A text box's content is the user's, but its placeholder / title are interface text.
        if (node.matches("textarea") && !node.closest("[data-i18n-skip]")) translateAttrs(node);
        return NodeFilter.FILTER_REJECT;
      },
    });
    let node;
    while ((node = walker.nextNode())) {
      if (node.nodeType === Node.TEXT_NODE) translateTextNode(node); else translateAttrs(node);
    }
  }

  function translateTitle() {
    const english = lookup(document.title);
    if (english == null) return;
    if (originalTitle == null) originalTitle = document.title;
    document.title = english;
  }

  function onMutations(records) {
    if (lang !== "en" || !dict) return;
    for (const record of records) {
      if (record.type === "childList") record.addedNodes.forEach(node => {
        if (node.parentElement && skipped(node.parentElement)) return;
        translateTree(node);
      });
      else if (record.type === "characterData") {
        const node = record.target;
        if (!skipped(node.parentElement)) translateTextNode(node);
      } else if (record.type === "attributes" && (!skipped(record.target) || record.target.matches("textarea"))) translateAttrs(record.target);
    }
    // Our own writes queue records too; drop them so they don't loop.
    observer.takeRecords();
  }

  function startObserver() {
    if (observer) return;
    observer = new MutationObserver(onMutations);
    observer.observe(document.documentElement, { subtree: true, childList: true, characterData: true, attributes: true, attributeFilter: ATTRS });
  }

  function restore() {
    observer?.disconnect();
    observer = null;
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT);
    let node = document.body;
    do {
      if (node.nodeType === Node.TEXT_NODE) {
        const original = originalText.get(node);
        if (original != null) { node.nodeValue = original; originalText.delete(node); }
      } else {
        const saved = originalAttrs.get(node);
        if (saved) { Object.entries(saved).forEach(([attr, value]) => node.setAttribute(attr, value)); originalAttrs.delete(node); }
      }
    } while ((node = walker.nextNode()));
    if (originalTitle != null) { document.title = originalTitle; originalTitle = null; }
  }

  function loadDictionary() {
    if (dict) return Promise.resolve();
    if (window.I18N_EN) { useDictionary(window.I18N_EN); return Promise.resolve(); }
    return new Promise(resolve => {
      const script = Object.assign(document.createElement("script"), { src: DICT_URL });
      script.onload = () => { useDictionary(window.I18N_EN || { strings: {} }); resolve(); };
      script.onerror = () => { useDictionary({ strings: {} }); resolve(); };
      document.head.append(script);
    });
  }

  function useDictionary(source) {
    dict = source.strings || {};
    patterns = (source.patterns || []).map(([pattern, replacement]) => [new RegExp(pattern, "u"), replacement]);
    fragments = source.fragments || [];
  }

  async function apply(next = read()) {
    lang = next;
    const root = document.documentElement;
    root.lang = lang === "en" ? "en" : "zh-CN";
    if (lang === "en") root.dataset.lang = "en"; else delete root.dataset.lang;
    if (lang !== "en") { restore(); delete root.dataset.i18nPending; notify(); return; }
    try {
      await loadDictionary();
      if (lang !== "en" || !document.body) return;
      translateTitle();
      translateTree(document.body);
      startObserver();
      notify();
    } finally { delete root.dataset.i18nPending; }
  }

  function notify() { window.dispatchEvent(new CustomEvent("client:language", { detail: { lang } })); }

  function set(next) {
    next = next === "en" ? "en" : "zh";
    try { if (next === "zh") localStorage.removeItem(KEY); else localStorage.setItem(KEY, next); } catch { /* storage blocked */ }
    apply(next);
    // The settings window's host page follows at once; other tabs get the storage event.
    try { if (window.parent !== window) window.parent.ClientI18n?.apply(next); } catch { /* cross-origin */ }
    for (const frame of document.querySelectorAll("iframe")) {
      try { frame.contentWindow?.ClientI18n?.apply(next); } catch { /* cross-origin */ }
    }
  }

  // t("中文") for scripts that build strings themselves; returns the Chinese text when no translation exists.
  function t(text) { return lang === "en" && dict ? lookup(text) ?? text : text; }

  window.addEventListener("storage", event => { if (event.key === KEY) apply(read()); });
  window.ClientI18n = { current: () => lang, set, apply, t, languages: [{ id: "zh", name: "简体中文" }, { id: "en", name: "English" }] };
  if (lang === "en") {
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", () => apply("en")); else apply("en");
  }
})();
