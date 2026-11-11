/* Colour theme picker, behaving like VS Code's theme switcher:
   the rail's 主题 item opens a list; the mouse wheel (or ↑/↓) moves through it and previews each theme live,
   a click (or Enter) applies and saves it, Esc / clicking elsewhere reverts to the saved theme.
   The theme is <html data-theme="…">; colours come from static/theme.css. Saved per browser in localStorage. */
(() => {
  "use strict";
  if (window.ClientTheme) return;
  const KEY = "research-theme";
  const DEFAULT = "paper";
  // id, 名称, English, [neutral hue, neutral sat, accent hue, accent sat, accent lightness] — mirrors theme.css
  const THEMES = [
    ["paper", "纸白", "Paper", [48, 20, 152, 13, 38]],
    ["mist", "雾蓝", "Mist", [214, 28, 216, 42, 42]],
    ["graphite", "石墨", "Graphite", [220, 7, 220, 16, 36]],
    ["snow", "素白", "Snow", [0, 0, 214, 68, 44]],
    ["sky", "天青", "Sky", [200, 42, 199, 72, 35]],
    ["lake", "湖青", "Lake", [184, 24, 184, 58, 29]],
    ["mint", "薄荷", "Mint", [150, 26, 158, 48, 30]],
    ["olive", "橄榄", "Olive", [68, 22, 76, 30, 31]],
    ["sand", "砂岩", "Sand", [34, 30, 24, 34, 38]],
    ["apricot", "杏黄", "Apricot", [32, 62, 26, 64, 38]],
    ["coral", "珊瑚", "Coral", [12, 46, 8, 52, 44]],
    ["sakura", "樱粉", "Sakura", [345, 38, 342, 44, 43]],
    ["lavender", "薰衣草", "Lavender", [262, 28, 260, 34, 47]],
  ];
  const ids = THEMES.map(theme => theme[0]);
  const valid = id => ids.includes(id);

  function saved() {
    try { const id = localStorage.getItem(KEY); return valid(id) ? id : DEFAULT; } catch { return DEFAULT; }
  }

  function paint(id, doc = document) {
    const root = doc.documentElement;
    if (id === DEFAULT) delete root.dataset.theme; else root.dataset.theme = id;
    const theme = THEMES.find(item => item[0] === id) || THEMES[0];
    const [th, ts] = theme[3];
    doc.querySelector('meta[name="theme-color"]')?.setAttribute("content", `hsl(${th} ${ts}% 96.2%)`);
  }

  // Same-origin frames (settings drawer, embedded reader) follow previews too.
  function apply(id) {
    if (!valid(id)) id = DEFAULT;
    paint(id);
    for (const frame of document.querySelectorAll("iframe")) {
      try { if (frame.contentDocument?.documentElement) paint(id, frame.contentDocument); } catch { /* cross-origin */ }
    }
  }

  function save(id) {
    try { if (id === DEFAULT) localStorage.removeItem(KEY); else localStorage.setItem(KEY, id); } catch { /* storage blocked */ }
  }

  apply(saved());
  window.addEventListener("storage", event => {
    if (event.key === KEY) apply(saved());
    if (APPEARANCE_KEYS.includes(event.key)) applyAppearance();
  });

  // ---- appearance: interface font and text colour (设置 › 通用 › 外观). Mirrors the early script in _theme_head.html.
  const FONT_KEY = "research-font", FONT_CUSTOM_KEY = "research-font-custom", INK_KEY = "research-ink", CONTENT_INK_KEY = "research-content-ink";
  const APPEARANCE_KEYS = [FONT_KEY, FONT_CUSTOM_KEY, INK_KEY, CONTENT_INK_KEY];
  const validInk = value => INKS.includes(value) || /^#[0-9a-f]{6}$/i.test(value) ? value : "default";
  const FONTS = ["default", "system", "misans", "anthropic", "custom"];
  const INKS = ["default", "black", "graphite"];
  const MISANS_CSS = weight => `https://cdn.jsdelivr.net/npm/misans@4.1.0/lib/Normal/MiSans-${weight}.min.css`;
  const read = (key, fallback = "") => { try { return localStorage.getItem(key) ?? fallback; } catch { return fallback; } };
  const write = (key, value) => { try { if (value === "" || value === "default" || value == null) localStorage.removeItem(key); else localStorage.setItem(key, value); } catch { /* storage blocked */ } };
  const customStack = names => String(names || "").split(",").map(name => name.trim().replace(/["\;{}<>]/g, "")).filter(Boolean).map(name => `"${name}"`).join(",");

  function appearance() {
    const font = read(FONT_KEY, "default");
    return {
      font: FONTS.includes(font) ? font : "default",
      fontCustom: read(FONT_CUSTOM_KEY, ""),
      ink: validInk(read(INK_KEY, "default")),
      contentInk: validInk(read(CONTENT_INK_KEY, "default")),
    };
  }

  function paintAppearance(doc, { font, fontCustom, ink, contentInk }) {
    const root = doc.documentElement;
    const stack = font === "custom" ? customStack(fontCustom) : "";
    if (font === "default" || (font === "custom" && !stack)) delete root.dataset.font; else root.dataset.font = font;
    if (stack) root.style.setProperty("--t-font-ui", `${stack},system-ui,sans-serif`); else root.style.removeProperty("--t-font-ui");
    if (font === "misans" && !doc.querySelector('link[data-webfont="misans"]')) {
      ["Regular", "Semibold"].forEach(weight => {
        const link = Object.assign(doc.createElement("link"), { rel: "stylesheet", href: MISANS_CSS(weight) });
        link.dataset.webfont = "misans";
        doc.head.append(link);
      });
    }
    const paintInk = (value, attr, prop) => {
      const custom = /^#[0-9a-f]{6}$/i.test(value);
      if (value === "default") delete root.dataset[attr]; else root.dataset[attr] = custom ? "custom" : value;
      if (custom) root.style.setProperty(prop, value); else root.style.removeProperty(prop);
    };
    paintInk(ink, "ink", "--t-ink-custom");
    paintInk(contentInk, "contentInk", "--t-content-custom");
  }

  function applyAppearance() {
    const value = appearance();
    paintAppearance(document, value);
    for (const frame of document.querySelectorAll("iframe")) {
      try { if (frame.contentDocument?.documentElement) paintAppearance(frame.contentDocument, value); } catch { /* cross-origin */ }
    }
  }

  function setAppearance(patch) {
    if ("font" in patch) write(FONT_KEY, FONTS.includes(patch.font) ? patch.font : "default");
    if ("fontCustom" in patch) write(FONT_CUSTOM_KEY, String(patch.fontCustom || "").slice(0, 200));
    if ("ink" in patch) write(INK_KEY, validInk(patch.ink));
    if ("contentInk" in patch) write(CONTENT_INK_KEY, validInk(patch.contentInk));
    applyAppearance();
    // Same-origin parent (the settings window's host page) follows at once; other tabs get the storage event.
    try { if (window.parent !== window && window.parent.ClientTheme) window.parent.ClientTheme.refreshAppearance(); } catch { /* cross-origin */ }
  }

  // Is a font installed on this computer (or already loaded as a web font)? Compares glyph widths with fallbacks.
  function fontAvailable(name) {
    const canvas = fontAvailable.canvas || (fontAvailable.canvas = document.createElement("canvas"));
    const context = canvas.getContext("2d");
    const sample = "mmmmmmmmmmlli 永和九年 WQ@#";
    return ["monospace", "serif", "sans-serif"].some(base => {
      context.font = `32px ${base}`;
      const width = context.measureText(sample).width;
      context.font = `32px "${name}",${base}`;
      return context.measureText(sample).width !== width;
    });
  }

  // ---- picker ----
  let picker = null, list = null, trigger = null, cursor = 0, wheelDebt = 0;

  function swatch([th, ts, ah, as, al]) {
    const bg = `hsl(${th} ${ts}% 96.2%)`, ink = `hsl(${th} 14% 16%)`, line = `hsl(${th} ${ts * .55}% 72%)`, accent = `hsl(${ah} ${as}% ${al}%)`;
    return `<span class="theme-swatch" aria-hidden="true" style="--sw-ink:${ink};--sw-line:${line}"><i style="background:${bg}"></i><i style="background:${accent}"></i></span>`;
  }

  function build() {
    picker = document.createElement("div");
    picker.className = "theme-picker";
    picker.hidden = true;
    picker.innerHTML = `<div class="theme-picker-head"><strong>颜色主题</strong><small>滚轮预览 · 单击应用</small></div>
      <ul class="theme-picker-list" role="listbox" aria-label="颜色主题"></ul>
      <div class="theme-picker-foot"><kbd>↑</kbd><kbd>↓</kbd> 预览 · <kbd>Enter</kbd> 应用 · <kbd>Esc</kbd> 还原</div>`;
    list = picker.querySelector("ul");
    list.innerHTML = THEMES.map(([id, name, en, params], index) => `<li role="none"><button type="button" class="theme-option" role="option" data-theme-id="${id}" data-index="${index}">
      ${swatch(params)}<span class="theme-option-name">${name}<small>${en}</small></span><span class="theme-option-check" aria-hidden="true"></span></button></li>`).join("");
    list.addEventListener("click", event => {
      const option = event.target.closest(".theme-option");
      if (option) commit(option.dataset.themeId);
    });
    list.addEventListener("wheel", event => {
      event.preventDefault();
      wheelDebt += event.deltaMode === 1 ? event.deltaY * 33 : event.deltaY;
      // One theme per wheel notch (~100px); trackpads accumulate small deltas.
      while (Math.abs(wheelDebt) >= 50) {
        const step = Math.sign(wheelDebt);
        wheelDebt -= step * 100;
        if (Math.abs(wheelDebt) < 50) wheelDebt = 0;
        preview(cursor + step);
      }
    }, { passive: false });
    picker.addEventListener("keydown", event => {
      const moves = { ArrowDown: 1, ArrowUp: -1, PageDown: 4, PageUp: -4 };
      if (event.key in moves) { event.preventDefault(); preview(cursor + moves[event.key]); }
      else if (event.key === "Home") { event.preventDefault(); preview(0); }
      else if (event.key === "End") { event.preventDefault(); preview(THEMES.length - 1); }
      else if (event.key === "Enter") { event.preventDefault(); commit(ids[cursor]); }
      else if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); cancel(); }
      else if (event.key === "Tab") { cancel(); }
    });
    document.addEventListener("pointerdown", event => {
      if (!picker.hidden && !picker.contains(event.target) && !trigger?.contains(event.target)) cancel();
    }, true);
    window.addEventListener("resize", () => { if (!picker.hidden) place(); });
    document.body.append(picker);
  }

  function mark() {
    const current = saved();
    for (const option of list.querySelectorAll(".theme-option")) {
      const index = Number(option.dataset.index);
      option.classList.toggle("previewing", index === cursor);
      option.setAttribute("aria-selected", String(index === cursor));
      option.querySelector(".theme-option-check").textContent = option.dataset.themeId === current ? "✓" : "";
    }
  }

  function preview(index) {
    cursor = Math.max(0, Math.min(THEMES.length - 1, index));
    apply(ids[cursor]);
    mark();
    const option = list.querySelector(`[data-index="${cursor}"]`);
    option.scrollIntoView({ block: "nearest" });
    option.focus({ preventScroll: true });
  }

  function place() {
    const rect = (trigger || document.body).getBoundingClientRect();
    const width = picker.offsetWidth, height = picker.offsetHeight;
    const docked = window.innerWidth <= 760; // rail sits at the bottom on narrow screens
    let left = docked ? rect.left + rect.width / 2 - width / 2 : rect.right + 8;
    let top = docked ? rect.top - height - 8 : rect.bottom - height;
    left = Math.max(8, Math.min(left, window.innerWidth - width - 8));
    top = Math.max(8, Math.min(top, window.innerHeight - height - 8));
    picker.style.left = `${left}px`;
    picker.style.top = `${top}px`;
  }

  function open(from) {
    if (!picker) build();
    trigger = from || document.querySelector("[data-theme-picker]");
    wheelDebt = 0;
    picker.hidden = false;
    trigger?.setAttribute("aria-expanded", "true");
    place();
    preview(ids.indexOf(saved()));
  }

  function close() {
    if (!picker || picker.hidden) return;
    picker.hidden = true;
    trigger?.setAttribute("aria-expanded", "false");
    if (picker.contains(document.activeElement)) trigger?.focus?.();
  }

  function commit(id) { save(id); apply(id); close(); }
  function cancel() { apply(saved()); close(); }

  function init() {
    for (const button of document.querySelectorAll("[data-theme-picker]")) {
      button.addEventListener("click", () => (picker && !picker.hidden ? cancel() : open(button)));
    }
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init); else init();

  window.ClientTheme = {
    themes: THEMES.map(([id, name, en, [th, ts, ah, as, al]]) => ({ id, name, en, bg: `hsl(${th} ${ts}% 96.2%)`, accent: `hsl(${ah} ${as}% ${al}%)` })), current: saved, apply: id => commit(id), open,
    appearance, setAppearance, refreshAppearance: applyAppearance, fontAvailable,
  };
})();
