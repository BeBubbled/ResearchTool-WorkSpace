/* Research Toolkit client shell: the floating 全局设置 window shared by every module page.
   The rail's 设置 item and links to /settings (e.g. "AI 设置") open the settings in a window over the
   current module instead of leaving it; modified clicks and the settings page itself keep normal navigation.
   The window's page is preloaded while idle (or when the pointer heads for a settings link), so it opens at once,
   and stays loaded between openings. */
(() => {
  "use strict";
  if (location.pathname === "/settings") return;
  let drawer = null;
  let frame = null;
  let changed = false;
  let lastFocus = null;

  function tabFromHref(href) {
    const url = new URL(href, location.href);
    const legacy = url.pathname === "/" && (url.hash === "#llm-settings" || url.hash.startsWith("#settings/"));
    if (url.pathname !== "/settings" && !legacy) return null;
    if (url.hash === "#llm-settings") return "llm";
    return decodeURIComponent(url.hash.replace(/^#(settings\/)?/, ""));
  }

  function ensureDrawer() {
    if (drawer) return drawer;
    drawer = document.createElement("div");
    drawer.className = "client-settings-drawer";
    drawer.hidden = true;
    drawer.innerHTML = `<section class="client-settings-panel" role="dialog" aria-modal="true" aria-labelledby="clientSettingsTitle">
      <header class="client-settings-head"><span id="clientSettingsTitle">全局设置</span>
        <a href="/settings" data-open-full class="client-settings-icon" title="在独立页面打开" aria-label="在独立页面打开"><svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M14 4h6v6M20 4l-8.5 8.5M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/></svg></a>
        <button type="button" data-close-settings class="client-settings-icon" title="关闭 (Esc)" aria-label="关闭"><svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg></button></header>
      <iframe title="全局设置"></iframe>
    </section>`;
    frame = drawer.querySelector("iframe");
    drawer.addEventListener("click", event => { if (event.target === drawer) closeSettings(); });
    drawer.querySelector("[data-close-settings]").addEventListener("click", closeSettings);
    document.body.append(drawer);
    return drawer;
  }

  // Load the settings page in the (hidden) window ahead of time; it reopens on the last pane.
  function preload() {
    ensureDrawer();
    if (!frame.src) frame.src = "/settings?embed=1";
  }

  // tab "" = the pane the user looked at last.
  function openSettings(tab = "") {
    ensureDrawer();
    if (!drawer.hidden && !tab) { closeSettings(); return; }
    lastFocus = drawer.hidden ? document.activeElement : lastFocus;
    const hash = tab ? `#${encodeURIComponent(tab)}` : "";
    drawer.querySelector("[data-open-full]").href = `/settings${hash}`;
    if (!frame.src) frame.src = `/settings?embed=1${hash}`;
    else if (tab) frame.contentWindow?.postMessage({ type: "research-settings-tab", tab }, location.origin);
    drawer.hidden = false;
    railItem()?.setAttribute("aria-expanded", "true");
    drawer.querySelector("[data-close-settings]").focus();
  }

  const railItem = () => document.querySelector('.client-rail [data-client-nav="settings"]');

  function closeSettings() {
    if (!drawer || drawer.hidden) return;
    drawer.hidden = true;
    railItem()?.setAttribute("aria-expanded", "false");
    lastFocus?.focus?.();
    window.dispatchEvent(new CustomEvent("client:settings-closed", { detail: { changed } }));
    if (changed) showReloadToast();
    changed = false;
  }

  function showReloadToast() {
    document.querySelector(".client-toast")?.remove();
    const toast = document.createElement("div");
    toast.className = "client-toast";
    toast.setAttribute("role", "status");
    toast.innerHTML = `<span>设置已保存，AI 引擎状态已自动更新；其他改动刷新页面后生效。</span><button type="button" data-reload>刷新页面</button><button type="button" class="quiet" data-dismiss>稍后</button>`;
    toast.querySelector("[data-reload]").addEventListener("click", () => location.reload());
    toast.querySelector("[data-dismiss]").addEventListener("click", () => toast.remove());
    document.body.append(toast);
  }

  document.addEventListener("click", event => {
    if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const link = event.target.closest?.("a[href]");
    if (!link || link.closest(".client-settings-drawer")) return;
    const fromRail = link.matches('.client-rail [data-client-nav="settings"]');
    if (link.closest(".client-rail") && !fromRail) return;
    const tab = tabFromHref(link.getAttribute("href"));
    if (tab === null) return;
    event.preventDefault();
    openSettings(fromRail ? "" : tab);
  }, true);

  // Warm the window up: when the pointer reaches a settings link, and once the page has gone idle.
  const warm = event => { if (event.target.closest?.('a[href^="/settings"], [data-client-nav="settings"]')) preload(); };
  document.addEventListener("pointerover", warm, { passive: true });
  document.addEventListener("focusin", warm);
  window.addEventListener("load", () => {
    const idle = window.requestIdleCallback || (callback => setTimeout(callback, 1200));
    idle(() => preload(), { timeout: 4000 });
  });

  document.addEventListener("keydown", event => { if (event.key === "Escape" && drawer && !drawer.hidden) closeSettings(); });
  window.addEventListener("message", event => {
    if (event.origin !== location.origin || event.source !== frame?.contentWindow) return;
    if (event.data?.type === "research-settings-saved") changed = true;
    if (event.data?.type === "research-settings-close") closeSettings();
  });

  window.ClientShell = { openSettings, closeSettings, preloadSettings: preload };
})();
