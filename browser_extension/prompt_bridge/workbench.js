(() => {
  if (location.pathname !== "/prompts" || window.top !== window) return;
  const version = chrome.runtime.getManifest().version;
  window.addEventListener("message", async event => {
    const message = event.data;
    if (event.source !== window || event.origin !== location.origin || message?.source !== "research-prompt-workbench") return;
    if (!["PING", "OPEN_CHATGPT"].includes(message.type) || typeof message.id !== "string") return;
    try {
      const result = await chrome.runtime.sendMessage({ type:message.type, transfer:message.transfer });
      if (!result) throw new Error("扩展后台没有返回响应，请在扩展管理页重新加载扩展，再刷新工作台。");
      window.postMessage({ source:"research-prompt-bridge", id:message.id, version, ...result }, location.origin);
    } catch (error) {
      const invalidated = /Extension context invalidated/i.test(error.message);
      window.postMessage({ source:"research-prompt-bridge", id:message.id, ok:false, version,
        code:invalidated ? "BRIDGE_RELOAD" : "BRIDGE_RUNTIME",
        error:invalidated ? "扩展已更新或重新加载，请刷新工作台以恢复连接；刷新后需重新选择附件。" : error.message }, location.origin);
    }
  });
  // Recover if the page started its check before this content script was injected.
  window.postMessage({ source:"research-prompt-bridge", type:"READY", version }, location.origin);
})();
