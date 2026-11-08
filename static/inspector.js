/* Shared split-layout pieces (macOS-style inspector): tabbed side panels and drag-to-resize columns.
   Both remember their state per module in localStorage, so every module behaves the same way:
     Inspector.tabs(root, { key: "reader", defaultTab: "assistant", onChange })
       root holds [role=tablist] > [data-inspector-tab] buttons and [data-inspector-panel] sections.
     Inspector.resizable(handle, { key, min, max, initial, apply })
       handle is a [role=separator] at the panel's left edge; apply(width) sets the CSS width. */
(() => {
  "use strict";
  const STORE = "research-toolkit.inspector.";
  const read = key => { try { return localStorage.getItem(STORE + key); } catch { return null; } };
  const write = (key, value) => { try { localStorage.setItem(STORE + key, String(value)); } catch {} };

  function tabs(root, options = {}) {
    const buttons = () => [...root.querySelectorAll("[data-inspector-tab]")];
    const names = () => buttons().map(button => button.dataset.inspectorTab);
    const key = options.key ? `${options.key}.tab` : "";
    let current = "";

    function select(tab, { focus = false, silent = false } = {}) {
      if (!names().includes(tab)) return current;
      current = tab;
      buttons().forEach(button => {
        const active = button.dataset.inspectorTab === tab;
        button.classList.toggle("active", active);
        button.setAttribute("aria-selected", String(active));
        button.tabIndex = active ? 0 : -1;
        if (active && focus) button.focus();
      });
      root.querySelectorAll("[data-inspector-panel]").forEach(panel => { panel.hidden = panel.dataset.inspectorPanel !== tab; });
      if (key) write(key, tab);
      if (!silent) options.onChange?.(tab);
      return tab;
    }

    buttons().forEach(button => {
      button.setAttribute("role", "tab");
      button.addEventListener("click", () => select(button.dataset.inspectorTab));
    });
    root.querySelector("[role=tablist]")?.addEventListener("keydown", event => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      const list = names();
      const index = list.indexOf(current);
      const next = event.key === "Home" ? 0 : event.key === "End" ? list.length - 1
        : (index + (event.key === "ArrowRight" ? 1 : list.length - 1)) % list.length;
      event.preventDefault();
      select(list[next], { focus: true });
    });
    const remembered = key ? read(key) : null;
    select(names().includes(remembered) ? remembered : options.defaultTab || names()[0], { silent: true });
    return { select, current: () => current };
  }

  function resizable(handle, options) {
    const { key, min = 260, max = 560, apply } = options;
    const clamp = value => Math.min(max, Math.max(min, Math.round(Number(value) || 0)));
    let width = clamp(read(`${key}.width`) || options.initial || min);
    const set = (value, persist = true) => {
      width = clamp(value);
      apply(width);
      handle.setAttribute("aria-valuenow", String(width));
      if (persist) write(`${key}.width`, width);
    };
    handle.setAttribute("role", "separator");
    handle.setAttribute("aria-orientation", "vertical");
    handle.setAttribute("aria-valuemin", String(min));
    handle.setAttribute("aria-valuemax", String(max));
    handle.tabIndex = 0;
    handle.addEventListener("pointerdown", event => {
      event.preventDefault();
      const startX = event.clientX;
      const startWidth = width;
      handle.setPointerCapture(event.pointerId);
      document.body.classList.add("inspector-resizing");
      const move = moveEvent => set(startWidth + (startX - moveEvent.clientX), false);
      const up = () => {
        handle.removeEventListener("pointermove", move);
        document.body.classList.remove("inspector-resizing");
        set(width);
      };
      handle.addEventListener("pointermove", move);
      handle.addEventListener("pointerup", up, { once: true });
      handle.addEventListener("pointercancel", up, { once: true });
    });
    handle.addEventListener("keydown", event => {
      if (event.key === "ArrowLeft") { event.preventDefault(); set(width + 20); }
      if (event.key === "ArrowRight") { event.preventDefault(); set(width - 20); }
    });
    set(width, false);
    return { set, width: () => width };
  }

  // Collapsible side panel whose open state is remembered (toolbar ⊟ buttons).
  function toggle(button, options) {
    const { key, initial = true, apply } = options;
    const stored = read(`${key}.open`);
    let open = stored === null ? initial : stored === "1";
    const set = value => {
      open = Boolean(value);
      apply(open);
      button.setAttribute("aria-expanded", String(open));
      write(`${key}.open`, open ? "1" : "0");
    };
    button.addEventListener("click", () => set(!open));
    set(open);
    return { set, open: () => open };
  }

  window.Inspector = { tabs, resizable, toggle };
})();
