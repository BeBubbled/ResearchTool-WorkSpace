/* Shared right-click menu: ContextMenu.open(event | {x, y}, items).
   items: [{ label, run, href?, danger?, disabled? } | "separator"]. Keyboard: ↑/↓, Enter, Esc. */
(() => {
  "use strict";
  if (window.ContextMenu) return;
  let menu = null, lastFocus = null;

  function close() {
    if (!menu || menu.hidden) return;
    menu.hidden = true;
    lastFocus?.focus?.();
  }

  function ensure() {
    if (menu) return menu;
    menu = document.createElement("div");
    menu.className = "context-menu";
    menu.setAttribute("role", "menu");
    menu.hidden = true;
    menu.addEventListener("keydown", event => {
      const entries = [...menu.querySelectorAll("[role=menuitem]:not([aria-disabled=true])")];
      const index = entries.indexOf(document.activeElement);
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        entries[(index + (event.key === "ArrowDown" ? 1 : entries.length - 1)) % entries.length]?.focus();
      } else if (event.key === "Escape" || event.key === "Tab") {
        event.preventDefault();
        close();
      }
    });
    document.addEventListener("pointerdown", event => { if (!menu.hidden && !menu.contains(event.target)) close(); }, true);
    window.addEventListener("blur", close);
    window.addEventListener("resize", close);
    document.addEventListener("scroll", close, true);
    document.body.append(menu);
    return menu;
  }

  function open(position, items) {
    ensure();
    lastFocus = document.activeElement;
    menu.replaceChildren();
    items.forEach(item => {
      if (item === "separator") { menu.append(Object.assign(document.createElement("hr"), { role: "separator" })); return; }
      const entry = document.createElement(item.href ? "a" : "button");
      entry.setAttribute("role", "menuitem");
      entry.className = `context-menu-item${item.danger ? " danger" : ""}`;
      entry.textContent = item.label;
      if (item.href) { entry.href = item.href; if (item.download) entry.setAttribute("download", ""); }
      else entry.type = "button";
      if (item.disabled) { entry.setAttribute("aria-disabled", "true"); entry.tabIndex = -1; if (item.title) entry.title = item.title; }
      entry.addEventListener("click", event => {
        if (item.disabled) { event.preventDefault(); return; }
        if (!item.href) event.preventDefault();
        close();
        item.run?.();
      });
      menu.append(entry);
    });
    menu.hidden = false;
    const x = position.clientX ?? position.x ?? 0, y = position.clientY ?? position.y ?? 0;
    const { innerWidth, innerHeight } = window;
    const rect = menu.getBoundingClientRect();
    menu.style.left = `${Math.max(8, Math.min(x, innerWidth - rect.width - 8))}px`;
    menu.style.top = `${Math.max(8, Math.min(y, innerHeight - rect.height - 8))}px`;
    menu.querySelector("[role=menuitem]:not([aria-disabled=true])")?.focus();
  }

  // Open from a focused element with the keyboard (ContextMenu key or Shift+F10).
  function fromKeyboard(event, element, items) {
    if (!(event.key === "ContextMenu" || (event.shiftKey && event.key === "F10"))) return false;
    event.preventDefault();
    const rect = element.getBoundingClientRect();
    open({ x: rect.left + 24, y: rect.top + rect.height / 2 }, items);
    return true;
  }

  window.ContextMenu = { open, close, fromKeyboard };
})();
