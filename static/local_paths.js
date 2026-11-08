// Shared helpers for turning browser-selected files into real local paths.
// The browser never exposes absolute paths, so the local backend either opens
// a native OS dialog or finds a dropped file by its name, size and mtime.
(function () {
  async function post(url, body) {
    const response = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || `请求失败（${response.status}）`);
    return payload;
  }

  /** Open a native dialog. Resolves to {path, name?, size?, token?} or null when cancelled. */
  async function pick({ kind = "file", title = "", suffixes = [] } = {}) {
    const result = await post("/api/local-paths/pick", { kind, title, suffixes });
    return result.cancelled ? null : result;
  }

  /** Download a natively picked file so it can also be uploaded like a dropped one. */
  async function fetchPicked(result) {
    const response = await fetch(`/api/local-paths/picked/${encodeURIComponent(result.token)}`);
    if (!response.ok) throw new Error("无法读取所选文件，请重新选择。");
    const blob = await response.blob();
    return new File([blob], result.name, { type: blob.type || "application/octet-stream" });
  }

  /** Find a dropped File on disk. Resolves to {path, candidates, timedOut}. */
  async function locate(file) {
    return post("/api/local-paths/locate", { name: file.name, size: file.size, lastModified: file.lastModified });
  }

  window.LocalPaths = { pick, fetchPicked, locate };
})();
