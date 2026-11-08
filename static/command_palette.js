/* Command palette (⌘K / Ctrl+K) and shortcut sheet (?) — the web stand-in for the macOS menu bar.
   Pages opt in declaratively, no registration code needed:
     data-command="标题"            the element shows up in the palette; running it clicks the element
     data-command-group="改稿"      optional group label (defaults to the page's module name)
     data-shortcut="mod+alt+\\"     optional keyboard shortcut that clicks the element
   Shortcut syntax: "mod" is ⌘ on macOS and Ctrl elsewhere; keys use KeyboardEvent.code names
   lowered (a, 1, \\, ",", ".", enter). Shortcuts without mod are ignored while typing in a field. */
(() => {
  "use strict";
  if (window.CommandPalette) return;
  const isMac = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);
  const escapeHtml = value => String(value ?? "").replace(/[&<>'"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[c]));
  const SETTINGS_PANES = [
    ["codex", "Codex"], ["claude", "Claude Code"], ["llm", "API 预设"], ["mathpix", "Mathpix OCR"], ["google", "Google 翻译"],
    ["speech", "Azure 语音"], ["github", "GitHub 图片"], ["reader", "精读默认行为"], ["workflow", "双语改稿工作流"], ["three-pass", "Three-Pass 篇幅"], ["studio", "ResearchStudio 环境"],
  ];
  const CODE_KEYS = { Backslash: "\\", Comma: ",", Period: ".", Slash: "/", Enter: "enter", Escape: "escape", BracketLeft: "[", BracketRight: "]", Space: "space" };
  const GLOBAL_SHORTCUTS = [["mod+k", "打开命令面板"], ["?", "显示快捷键"], ["/", "聚焦当前页面的搜索框（工具箱）"], ["escape", "关闭弹层"]];
  let overlay = null, help = null, items = [], filtered = [], cursor = 0, libraryCache = null;

  function comboFromEvent(event) {
    const code = event.code || "";
    let key = CODE_KEYS[code] || (code.startsWith("Key") ? code.slice(3).toLowerCase() : code.startsWith("Digit") ? code.slice(5) : (event.key || "").toLowerCase());
    if (event.key === "?") key = "?";
    const parts = [];
    if (event.metaKey || event.ctrlKey) parts.push("mod");
    if (event.altKey) parts.push("alt");
    if (event.shiftKey && key !== "?") parts.push("shift");
    parts.push(key);
    return parts.join("+");
  }

  function prettyCombo(combo) {
    const names = { mod: isMac ? "⌘" : "Ctrl", alt: isMac ? "⌥" : "Alt", shift: isMac ? "⇧" : "Shift", enter: "↩", escape: "Esc", space: "Space", "\\": "\\" };
    return combo.split("+").map(part => names[part] || part.toUpperCase()).join(isMac ? "" : "+");
  }

  const typing = target => Boolean(target?.closest?.("input, textarea, select, [contenteditable=''], [contenteditable='true']"));
  const visible = node => Boolean(node && !node.closest("[hidden]") && !node.disabled && node.getClientRects().length);
  const moduleName = () => document.querySelector(".module-bar-title")?.textContent.trim() || "当前页面";
  const label = node => node.dataset.command || node.getAttribute("aria-label") || node.title || node.textContent.trim();

  function pageCommands() {
    return [...document.querySelectorAll("[data-command]")].filter(visible).map(node => ({
      title: node.dataset.command, group: node.dataset.commandGroup || moduleName(), shortcut: node.dataset.shortcut || "",
      run: () => node.click(),
    }));
  }

  function navigationCommands() {
    const nav = [...document.querySelectorAll(".client-rail-item")].map(link => ({
      title: `前往${link.querySelector("span")?.textContent.trim() || link.title}`, hint: link.title, group: "前往", run: () => { location.href = link.href; },
    }));
    const open = tab => (window.ClientShell ? window.ClientShell.openSettings(tab) : (location.href = `/settings#${tab}`));
    const settings = SETTINGS_PANES.map(([tab, name]) => ({ title: `设置：${name}`, group: "设置", run: () => open(tab) }));
    return [...nav, ...settings];
  }

  async function libraryCommands() {
    if (!libraryCache) {
      try {
        const response = await fetch("/api/library");
        const data = await response.json();
        libraryCache = (data.documents || []).slice(0, 30).map(doc => ({
          title: `打开论文：${doc.title}`, group: "文献库", run: () => { location.href = `/reader?library=${encodeURIComponent(doc.id)}`; },
        }));
      } catch {
        libraryCache = [];
      }
    }
    return libraryCache;
  }

  function score(item, needle) {
    if (!needle) return 1;
    const text = `${item.title} ${item.group} ${item.hint || ""}`.toLowerCase();
    if (item.title.toLowerCase().startsWith(needle)) return 3;
    if (item.title.toLowerCase().includes(needle)) return 2;
    if (text.includes(needle)) return 1;
    // Every character in order (e.g. "tp" → Three-Pass).
    let index = 0;
    for (const char of needle) { index = text.indexOf(char, index); if (index < 0) return 0; index += 1; }
    return 0.5;
  }

  function render() {
    const needle = overlay.querySelector("input").value.trim().toLowerCase();
    filtered = items.map(item => ({ item, rank: score(item, needle) })).filter(entry => entry.rank > 0)
      .sort((a, b) => b.rank - a.rank).map(entry => entry.item).slice(0, 60);
    cursor = Math.min(cursor, Math.max(filtered.length - 1, 0));
    let lastGroup = "";
    overlay.querySelector(".palette-list").innerHTML = filtered.length ? filtered.map((item, index) => {
      const heading = item.group !== lastGroup ? `<li class="palette-group" role="presentation">${escapeHtml(item.group)}</li>` : "";
      lastGroup = item.group;
      return `${heading}<li role="option" id="palette-option-${index}" class="palette-item ${index === cursor ? "active" : ""}" data-index="${index}" aria-selected="${index === cursor}"><span>${escapeHtml(item.title)}</span>${item.shortcut ? `<kbd>${escapeHtml(prettyCombo(item.shortcut))}</kbd>` : ""}</li>`;
    }).join("") : `<li class="palette-empty">没有匹配的命令。</li>`;
    overlay.querySelector("input").setAttribute("aria-activedescendant", filtered.length ? `palette-option-${cursor}` : "");
    overlay.querySelector(".palette-item.active")?.scrollIntoView({ block: "nearest" });
  }

  function ensureOverlay() {
    if (overlay) return overlay;
    overlay = document.createElement("div");
    overlay.className = "palette-overlay";
    overlay.hidden = true;
    overlay.innerHTML = `<div class="palette" role="dialog" aria-modal="true" aria-label="命令面板">
      <input type="search" placeholder="输入命令、模块、设置或论文标题…" role="combobox" aria-expanded="true" aria-controls="paletteList" autocomplete="off" spellcheck="false">
      <ul id="paletteList" class="palette-list" role="listbox"></ul>
      <footer class="palette-foot"><span>↑↓ 选择 · ↩ 执行 · Esc 关闭</span><button type="button" data-palette-help>快捷键 ?</button></footer>
    </div>`;
    const input = overlay.querySelector("input");
    input.addEventListener("input", () => { cursor = 0; render(); });
    input.addEventListener("keydown", event => {
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        if (!filtered.length) return;
        cursor = (cursor + (event.key === "ArrowDown" ? 1 : filtered.length - 1)) % filtered.length;
        render();
      } else if (event.key === "Enter") {
        event.preventDefault();
        run(filtered[cursor]);
      } else if (event.key === "Escape") {
        event.preventDefault();
        close();
      }
    });
    overlay.addEventListener("click", event => {
      if (event.target === overlay) return close();
      const option = event.target.closest(".palette-item");
      if (option) run(filtered[Number(option.dataset.index)]);
      if (event.target.closest("[data-palette-help]")) { close(); showHelp(); }
    });
    overlay.addEventListener("mousemove", event => {
      const option = event.target.closest(".palette-item");
      if (option && Number(option.dataset.index) !== cursor) { cursor = Number(option.dataset.index); render(); }
    });
    document.body.append(overlay);
    return overlay;
  }

  let lastFocus = null;
  async function open() {
    ensureOverlay();
    lastFocus = document.activeElement;
    items = [...pageCommands(), ...navigationCommands(), { title: "显示快捷键", group: "帮助", shortcut: "?", run: showHelp }];
    overlay.hidden = false;
    const input = overlay.querySelector("input");
    input.value = "";
    cursor = 0;
    render();
    input.focus();
    const papers = await libraryCommands();
    if (!overlay.hidden) { items = [...items, ...papers]; render(); }
  }

  function close() {
    if (!overlay || overlay.hidden) return;
    overlay.hidden = true;
    lastFocus?.focus?.();
  }

  function run(item) {
    if (!item) return;
    close();
    setTimeout(() => item.run(), 0);
  }

  function showHelp() {
    if (!help) {
      help = document.createElement("div");
      help.className = "palette-overlay";
      help.hidden = true;
      help.addEventListener("click", event => { if (event.target === help || event.target.closest("[data-close-help]")) closeHelp(); });
      document.body.append(help);
    }
    const page = [...document.querySelectorAll("[data-shortcut]")].filter(node => !node.closest("[hidden]"))
      .map(node => [node.dataset.shortcut, label(node)]);
    const rows = list => list.map(([combo, text]) => `<tr><td><kbd>${escapeHtml(prettyCombo(combo))}</kbd></td><td>${escapeHtml(text)}</td></tr>`).join("");
    help.innerHTML = `<div class="palette palette-help" role="dialog" aria-modal="true" aria-label="快捷键">
      <header class="palette-help-head"><h2>快捷键</h2><button type="button" data-close-help aria-label="关闭">×</button></header>
      <h3>全局</h3><table>${rows(GLOBAL_SHORTCUTS)}</table>
      ${page.length ? `<h3>${escapeHtml(moduleName())}</h3><table>${rows(page)}</table>` : ""}
      <p class="palette-note">字母和数字快捷键在输入框里不生效；${isMac ? "⌘" : "Ctrl"} 组合键随时可用。</p>
    </div>`;
    help.hidden = false;
    help.querySelector("[data-close-help]").focus();
  }

  function closeHelp() { if (help && !help.hidden) help.hidden = true; }

  document.addEventListener("keydown", event => {
    if (event.defaultPrevented || event.isComposing) return;
    const combo = comboFromEvent(event);
    if (combo === "mod+k") { event.preventDefault(); overlay && !overlay.hidden ? close() : open(); return; }
    if (event.key === "Escape" && help && !help.hidden) { closeHelp(); return; }
    if (overlay && !overlay.hidden) return;
    if (document.querySelector("dialog[open]")) return;
    const inField = typing(event.target);
    if (combo === "?" && !inField) { event.preventDefault(); showHelp(); return; }
    if (inField && !combo.startsWith("mod+")) return;
    const target = [...document.querySelectorAll("[data-shortcut]")].find(node => node.dataset.shortcut === combo && visible(node));
    if (!target) return;
    event.preventDefault();
    target.click();
    if (target.matches("input, textarea, select")) target.focus();
  });

  window.CommandPalette = { open, close, showHelp, prettyCombo };
})();
