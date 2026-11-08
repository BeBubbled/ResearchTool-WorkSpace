/* Research Toolkit client shell: settings drawer shared by every module page.
   Links to /settings (e.g. "AI 设置") open the settings in place instead of leaving the module;
   the rail's own 设置 item, modified clicks and the settings page itself keep normal navigation. */
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
    return decodeURIComponent(url.hash.replace(/^#(settings\/)?/, "")) || "llm";
  }

  function ensureDrawer() {
    if (drawer) return drawer;
    drawer = document.createElement("div");
    drawer.className = "client-settings-drawer";
    drawer.hidden = true;
    drawer.innerHTML = `<section class="client-settings-panel" role="dialog" aria-modal="true" aria-label="全局设置">
      <header class="client-settings-head"><span>全局设置</span><a href="/settings" data-open-full>在独立页面打开 ↗</a><button type="button" data-close-settings>关闭</button></header>
      <iframe title="全局设置"></iframe>
    </section>`;
    frame = drawer.querySelector("iframe");
    drawer.addEventListener("click", event => { if (event.target === drawer) closeSettings(); });
    drawer.querySelector("[data-close-settings]").addEventListener("click", closeSettings);
    document.body.append(drawer);
    return drawer;
  }

  function openSettings(tab = "llm") {
    ensureDrawer();
    lastFocus = document.activeElement;
    const hash = `#${encodeURIComponent(tab)}`;
    drawer.querySelector("[data-open-full]").href = `/settings${hash}`;
    if (!frame.src) frame.src = `/settings?embed=1${hash}`;
    else frame.contentWindow?.postMessage({ type: "research-settings-tab", tab }, location.origin);
    drawer.hidden = false;
    drawer.querySelector("[data-close-settings]").focus();
  }

  function closeSettings() {
    if (!drawer || drawer.hidden) return;
    drawer.hidden = true;
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
    toast.innerHTML = `<span>设置已保存。部分改动需刷新当前页面后生效。</span><button type="button" data-reload>刷新页面</button><button type="button" class="quiet" data-dismiss>稍后</button>`;
    toast.querySelector("[data-reload]").addEventListener("click", () => location.reload());
    toast.querySelector("[data-dismiss]").addEventListener("click", () => toast.remove());
    document.body.append(toast);
  }

  document.addEventListener("click", event => {
    if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const link = event.target.closest?.("a[href]");
    if (!link || link.closest(".client-rail, .client-settings-drawer")) return;
    const tab = tabFromHref(link.getAttribute("href"));
    if (!tab) return;
    event.preventDefault();
    openSettings(tab);
  }, true);

  document.addEventListener("keydown", event => { if (event.key === "Escape" && drawer && !drawer.hidden) closeSettings(); });
  window.addEventListener("message", event => {
    if (event.origin !== location.origin || event.source !== frame?.contentWindow) return;
    if (event.data?.type === "research-settings-saved") changed = true;
    if (event.data?.type === "research-settings-close") closeSettings();
  });

  window.ClientShell = { openSettings, closeSettings };
})();
