/* Office-style ribbon (static/ribbon.css): tabs over groups of compact commands.
   Markup: .ribbon[data-ribbon="<key>"] > .ribbon-tabs > button.ribbon-tab[data-ribbon-tab="<id>"],
           .ribbon > .ribbon-panel[data-ribbon-panel="<id>"][data-popover] > .ribbon-group > .ribbon-group-body + .ribbon-group-label
   - Remembers the open tab and the collapsed state per ribbon (localStorage, cleared by 重置界面布局).
   - Double-click a tab, the ⌃ button or Ctrl+F1 collapses the ribbon to its tabs; a tab click then shows its
     commands until the next click elsewhere (as in Office).
   - button.ribbon-tab[data-ribbon-follow="#id"]: the tab hides while that element is hidden.
   - [data-ribbon-open="<id>"]: any button that opens a tab (e.g. the old 排版 button).
   - [data-ribbon-click="#id"]: a second copy of a command on another tab; clicks the original.
     [data-ribbon-mirror="#id"] copies the original's disabled / hidden state, [data-mirror-text="#id"] its text.
   Panels carry data-popover so the command palette still lists — and shortcuts still reach — commands on closed tabs. */
(() => {
  "use strict";
  if (window.Ribbon) return;
  const store = (key, value) => { try { if (value == null) localStorage.removeItem(key); else localStorage.setItem(key, value); } catch { /* storage blocked */ } };
  const stored = key => { try { return localStorage.getItem(key); } catch { return null; } };
  const ribbons = new Map();

  function setup(ribbon) {
    const key = ribbon.dataset.ribbon;
    const tabKey = `research-toolkit.ribbon.${key}.tab`;
    const collapsedKey = `research-toolkit.ribbon.${key}.collapsed`;
    const tabs = [...ribbon.querySelectorAll(".ribbon-tab[data-ribbon-tab]")];
    const panels = [...ribbon.querySelectorAll(".ribbon-panel[data-ribbon-panel]")];
    const visibleTabs = () => tabs.filter(tab => !tab.hidden);

    tabs.forEach(tab => {
      const id = tab.dataset.ribbonTab;
      tab.setAttribute("role", "tab");
      tab.id ||= `ribbon-${key}-${id}`;
      const panel = panels.find(item => item.dataset.ribbonPanel === id);
      if (panel) { panel.setAttribute("role", "tabpanel"); panel.setAttribute("aria-labelledby", tab.id); tab.setAttribute("aria-controls", panel.id ||= `ribbon-${key}-panel-${id}`); }
    });

    function select(id, { focus = false, remember = true } = {}) {
      const tab = tabs.find(item => item.dataset.ribbonTab === id && !item.hidden) || visibleTabs()[0];
      if (!tab) return;
      tabs.forEach(item => { const on = item === tab; item.setAttribute("aria-selected", String(on)); item.tabIndex = on ? 0 : -1; });
      panels.forEach(panel => { panel.hidden = panel.dataset.ribbonPanel !== tab.dataset.ribbonTab; panel.classList.remove("peeking"); });
      if (remember) store(tabKey, tab.dataset.ribbonTab);
      if (focus) tab.focus();
      return tab;
    }

    function setCollapsed(collapsed) {
      ribbon.classList.toggle("collapsed", collapsed);
      ribbon.classList.remove("peek");
      panels.forEach(panel => panel.classList.remove("peeking"));
      const button = ribbon.querySelector(".ribbon-collapse");
      if (button) {
        button.setAttribute("aria-expanded", String(!collapsed));
        button.title = collapsed ? "展开功能区 (Ctrl+F1)" : "折叠功能区 (Ctrl+F1)";
        button.setAttribute("aria-label", collapsed ? "展开功能区" : "折叠功能区");
      }
      store(collapsedKey, collapsed ? "1" : null);
    }

    function peek(id) {
      const tab = select(id);
      if (!tab) return;
      const panel = panels.find(item => item.dataset.ribbonPanel === tab.dataset.ribbonTab);
      const open = !(ribbon.classList.contains("peek") && panel?.classList.contains("peeking"));
      ribbon.classList.toggle("peek", open);
      panels.forEach(item => item.classList.toggle("peeking", open && item === panel));
    }

    ribbon.querySelector(".ribbon-tabs")?.setAttribute("role", "tablist");
    tabs.forEach(tab => {
      tab.addEventListener("click", () => (ribbon.classList.contains("collapsed") ? peek(tab.dataset.ribbonTab) : select(tab.dataset.ribbonTab)));
      tab.addEventListener("dblclick", () => { setCollapsed(!ribbon.classList.contains("collapsed")); select(tab.dataset.ribbonTab); });
      tab.addEventListener("keydown", event => {
        const list = visibleTabs();
        const index = list.indexOf(tab);
        const step = { ArrowRight: 1, ArrowLeft: -1 }[event.key];
        if (step) { event.preventDefault(); select(list[(index + step + list.length) % list.length].dataset.ribbonTab, { focus: true }); }
        else if (event.key === "Home") { event.preventDefault(); select(list[0].dataset.ribbonTab, { focus: true }); }
        else if (event.key === "End") { event.preventDefault(); select(list.at(-1).dataset.ribbonTab, { focus: true }); }
      });
    });
    ribbon.querySelector(".ribbon-collapse")?.addEventListener("click", () => setCollapsed(!ribbon.classList.contains("collapsed")));
    // A peeked panel closes after a command runs or on a click elsewhere.
    document.addEventListener("pointerdown", event => {
      if (!ribbon.classList.contains("peek") || ribbon.querySelector(".ribbon-tabs").contains(event.target)) return;
      if (!ribbon.contains(event.target)) { ribbon.classList.remove("peek"); panels.forEach(panel => panel.classList.remove("peeking")); }
    });
    ribbon.addEventListener("click", event => {
      if (ribbon.classList.contains("peek") && event.target.closest(".ribbon-panel button, .ribbon-panel a")) {
        setTimeout(() => { ribbon.classList.remove("peek"); panels.forEach(panel => panel.classList.remove("peeking")); }, 0);
      }
    });

    // Tabs that follow another element's visibility (e.g. 朗读 only when the document can be read aloud).
    tabs.filter(tab => tab.dataset.ribbonFollow).forEach(tab => {
      const target = document.querySelector(tab.dataset.ribbonFollow);
      if (!target) return;
      const sync = () => {
        const hidden = target.hidden || target.classList.contains("hidden");
        if (tab.hidden === hidden) return;
        tab.hidden = hidden;
        if (hidden && tab.getAttribute("aria-selected") === "true") select(visibleTabs()[0]?.dataset.ribbonTab, { remember: false });
        else if (!hidden && stored(tabKey) === tab.dataset.ribbonTab) select(tab.dataset.ribbonTab, { remember: false });
      };
      new MutationObserver(sync).observe(target, { attributes: true, attributeFilter: ["hidden", "class"] });
      sync();
    });

    setCollapsed(stored(collapsedKey) === "1");
    select(stored(tabKey) || tabs[0]?.dataset.ribbonTab, { remember: false });
    const api = { select: id => (ribbon.classList.contains("collapsed") ? peek(id) : select(id)), collapse: setCollapsed, element: ribbon };
    ribbons.set(key, api);
    return api;
  }

  function mirror(node) {
    const source = document.querySelector(node.dataset.ribbonMirror || node.dataset.mirrorText);
    if (!source) return;
    const sync = () => {
      if (node.dataset.mirrorText) node.textContent = source.textContent;
      if (node.dataset.ribbonMirror) { node.disabled = source.disabled; node.hidden = source.hidden || source.classList.contains("hidden"); }
    };
    new MutationObserver(sync).observe(source, { attributes: true, childList: true, characterData: true, subtree: true });
    sync();
  }

  function init() {
    document.querySelectorAll(".ribbon[data-ribbon]").forEach(setup);
    document.querySelectorAll("[data-ribbon-mirror], [data-mirror-text]").forEach(mirror);
    document.addEventListener("click", event => {
      const proxy = event.target.closest?.("[data-ribbon-click]");
      if (proxy) { document.querySelector(proxy.dataset.ribbonClick)?.click(); return; }
      const opener = event.target.closest?.("[data-ribbon-open]");
      if (!opener) return;
      const ribbon = opener.closest(".ribbon") || document.querySelector(".ribbon[data-ribbon]");
      ribbons.get(ribbon?.dataset.ribbon)?.select(opener.dataset.ribbonOpen);
    });
    document.addEventListener("keydown", event => {
      if (event.key !== "F1" || !(event.ctrlKey || event.metaKey)) return;
      const first = ribbons.values().next().value;
      if (!first) return;
      event.preventDefault();
      first.collapse(!first.element.classList.contains("collapsed"));
    });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init); else init();

  window.Ribbon = { get: key => ribbons.get(key), select: (key, id) => ribbons.get(key)?.select(id) };
})();
