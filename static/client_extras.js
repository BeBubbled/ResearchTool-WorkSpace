/* Research Toolkit client extras, loaded on every page by _client_nav.html:
     ClientToast     stacked toasts with an action button, and undo for deletions
     ClientActivity  polls GET /api/activity: running counts on the rail, the tab title,
                     and a toast (plus a desktop notification when the tab is hidden) when a job finishes
     page extras     loads per-page add-ons that live outside a module's own files
                     (e.g. /static/paper_revision_extras.js on the revision page) */
(() => {
  "use strict";
  if (window.ClientActivity) return;
  const escapeHtml = value => String(value ?? "").replace(/[&<>'"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[c]));
  const store = {
    get(key, fallback) { try { const value = localStorage.getItem(key); return value === null ? fallback : JSON.parse(value); } catch { return fallback; } },
    set(key, value) { try { localStorage.setItem(key, JSON.stringify(value)); } catch {} },
  };

  // ---- Toasts -------------------------------------------------------------------------------
  let stack = null;
  function ensureStack() {
    if (stack?.isConnected) return stack;
    stack = document.createElement("div");
    stack.className = "client-toast-stack";
    stack.setAttribute("role", "region");
    stack.setAttribute("aria-label", "通知");
    document.body.append(stack);
    return stack;
  }

  /** show(message, {kind, actionLabel, onAction, href, timeout, secondaryLabel, onSecondary}) → {close} */
  function show(message, options = {}) {
    const toast = document.createElement("div");
    toast.className = `client-toast-item ${options.kind || ""}`;
    toast.setAttribute("role", options.kind === "bad" ? "alert" : "status");
    const action = options.href
      ? `<a class="client-toast-action" href="${escapeHtml(options.href)}">${escapeHtml(options.actionLabel || "查看")}</a>`
      : options.actionLabel ? `<button type="button" class="client-toast-action">${escapeHtml(options.actionLabel)}</button>` : "";
    const secondary = options.secondaryLabel ? `<button type="button" class="client-toast-secondary">${escapeHtml(options.secondaryLabel)}</button>` : "";
    toast.innerHTML = `<span class="client-toast-text">${escapeHtml(message)}</span>${action}${secondary}<button type="button" class="client-toast-close" aria-label="关闭">×</button>`;
    let timer = null;
    const close = () => { clearTimeout(timer); toast.remove(); };
    toast.querySelector("button.client-toast-action")?.addEventListener("click", () => { close(); options.onAction?.(); });
    toast.querySelector("a.client-toast-action")?.addEventListener("click", () => close());
    toast.querySelector(".client-toast-secondary")?.addEventListener("click", () => { close(); options.onSecondary?.(); });
    toast.querySelector(".client-toast-close").addEventListener("click", close);
    const timeout = options.timeout ?? 7000;
    const arm = () => { if (timeout > 0) timer = setTimeout(close, timeout); };
    toast.addEventListener("mouseenter", () => clearTimeout(timer));
    toast.addEventListener("mouseleave", arm);
    ensureStack().append(toast);
    arm();
    return { close };
  }

  /** Undo for a deletion that already happened: undo(message, restore) shows 撤销 for 8 seconds. */
  function undo(message, restore, options = {}) {
    return show(message, { actionLabel: "撤销", onAction: async () => {
      try { await restore(); show(options.restoredMessage || "已撤销。", { kind: "ok", timeout: 3000 }); }
      catch (error) { show(`撤销失败：${error?.message || error}`, { kind: "bad" }); }
    }, timeout: options.timeout ?? 8000 });
  }

  window.ClientToast = { show, undo };

  // ---- Activity -----------------------------------------------------------------------------
  const NOTIFIED_KEY = "research-toolkit.activity.notified";
  const DESKTOP_KEY = "research-toolkit.activity.desktop";
  const STATE_TEXT = { done: "已完成", warning: "完成（有提示）", failed: "失败", cancelled: "已停止" };
  const pageLoaded = Date.now() / 1000;
  const baseTitle = document.title;
  let timer = null, unseen = 0, firstPoll = true;

  function notified() { return new Set(store.get(NOTIFIED_KEY, [])); }
  function markNotified(ids) {
    const set = notified();
    ids.forEach(id => set.add(id));
    store.set(NOTIFIED_KEY, [...set].slice(-300));
  }

  function desktopEnabled() { return store.get(DESKTOP_KEY, true) !== false && "Notification" in window && Notification.permission === "granted"; }

  function requestDesktop() {
    if (!("Notification" in window)) return Promise.resolve("unsupported");
    store.set(DESKTOP_KEY, true);
    return Notification.requestPermission();
  }

  function renderBadges(active) {
    document.querySelectorAll(".client-rail-item[data-client-nav]").forEach(link => {
      const key = link.dataset.clientNav;
      const count = active[key] || 0;
      let badge = link.querySelector(".client-rail-badge");
      if (!count) { badge?.remove(); return; }
      if (!badge) { badge = document.createElement("b"); badge.className = "client-rail-badge"; link.append(badge); }
      badge.textContent = count > 9 ? "9+" : String(count);
      badge.title = `${count} 个任务进行中`;
    });
  }

  function renderTitle(total) {
    const prefix = unseen ? `✓${unseen > 1 ? unseen : ""} ` : total ? `(${total}) ` : "";
    document.title = prefix + baseTitle;
  }

  function announce(item) {
    const verb = STATE_TEXT[item.state] || "已结束";
    const kind = item.state === "done" ? "ok" : item.state === "warning" ? "" : "bad";
    const permissionDefault = "Notification" in window && Notification.permission === "default" && store.get(DESKTOP_KEY, true) !== false;
    show(`${item.title} ${verb}${item.detail && item.state !== "done" ? `：${item.detail}` : ""}`, {
      kind, href: item.url || "", actionLabel: "查看", timeout: item.state === "done" ? 9000 : 14000,
      ...(permissionDefault ? { secondaryLabel: "开启桌面通知", onSecondary: requestDesktop } : {}),
    });
    if (document.hidden && desktopEnabled()) {
      try {
        const note = new Notification(`${verb} · Research Toolkit`, { body: item.title, tag: item.id });
        note.onclick = () => { window.focus(); if (item.url) location.href = item.url; note.close(); };
      } catch {}
    }
    if (document.hidden) unseen += 1;
  }

  async function poll() {
    clearTimeout(timer);
    let data = null;
    try {
      const response = await fetch("/api/activity", { cache: "no-store" });
      if (response.ok) data = await response.json();
    } catch {}
    if (data) {
      renderBadges(data.active || {});
      const seen = notified();
      const fresh = (data.items || []).filter(item => item.state !== "active" && !seen.has(item.id));
      // Announce only jobs that finished recently (2 minutes before this page opened, or later),
      // so opening a page never replays a day of old results.
      const recent = fresh.filter(item => (item.finishedAt || 0) >= pageLoaded - 120);
      if (!firstPoll || recent.length) recent.forEach(announce);
      markNotified(fresh.map(item => item.id));
      firstPoll = false;
      renderTitle(data.activeTotal || 0);
      window.dispatchEvent(new CustomEvent("client:activity", { detail: data }));
    }
    const busy = data?.activeTotal > 0;
    timer = setTimeout(poll, busy ? (document.hidden ? 8000 : 4000) : (document.hidden ? 30000 : 15000));
  }

  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) { unseen = 0; renderTitle(0); poll(); }
  });

  window.ClientActivity = { refresh: () => setTimeout(poll, 400), requestDesktop, desktopEnabled, setDesktop: value => store.set(DESKTOP_KEY, Boolean(value)) };

  // ---- Per-page add-ons ---------------------------------------------------------------------
  const PAGE_EXTRAS = { "/paper-revision": "/static/paper_revision_extras.js?v=2" };
  function loadExtras() {
    const src = PAGE_EXTRAS[location.pathname];
    if (!src || document.querySelector(`script[data-page-extras]`)) return;
    const script = document.createElement("script");
    script.src = src;
    script.defer = true;
    script.dataset.pageExtras = "";
    document.body.append(script);
  }

  const start = () => { loadExtras(); poll(); };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
