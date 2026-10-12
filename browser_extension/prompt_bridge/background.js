/* Only the initiating localhost tab and its assigned ChatGPT tab can use a handoff. */
const storageKey = tabId => `prompt-tab-${tabId}`;
const activeOpens = new Map();
function localWorkbench(sender) {
  const url = new URL(sender.url || "about:blank");
  return sender.frameId === 0 && sender.tab && url.protocol === "http:" && ["127.0.0.1", "localhost"].includes(url.hostname) && url.pathname === "/prompts" ? url.origin : null;
}
function chatgptTab(sender) {
  const url = new URL(sender.url || "about:blank");
  return sender.frameId === 0 && sender.tab && url.origin === "https://chatgpt.com";
}
async function assignment(tabId) { return (await chrome.storage.session.get(storageKey(tabId)))[storageKey(tabId)]; }
async function localRequest(task, suffix = "", options = {}) {
  const response = await fetch(`${task.origin}/api/prompts/transfers/${task.id}${suffix}`, {
    ...options, signal:AbortSignal.timeout(20000), cache:"no-store", credentials:"omit", redirect:"error",
    headers:{ "X-Prompt-Workbench":"1", Authorization:`Bearer ${task.token}`, ...options.headers },
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "无法读取本机提交，请保持工具箱运行。");
  return data;
}
async function update(task, status, message) {
  return localRequest(task, "/status", { method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({status,message}) });
}
async function openChatGPT(message, sender, origin) {
  const transfer = message.transfer;
  if (!transfer || !/^[a-f0-9]{32}$/.test(transfer.id) || !/^[A-Za-z0-9_-]{40,64}$/.test(transfer.token)) throw new Error("无效的提交标识。");
  const task = { origin, id:transfer.id, token:transfer.token, created:Date.now(), claimed:false };
  const data = await localRequest(task);
  if (data.status !== "pending") throw new Error("此提交已处理，请检查已打开的 ChatGPT 标签页。");
  await update(task, "opening", "正在打开 ChatGPT…");
  const tab = await chrome.tabs.create({ url:"about:blank", active:true });
  try {
    await chrome.storage.session.set({ [storageKey(tab.id)]:task });
    await chrome.tabs.update(tab.id, {url:"https://chatgpt.com/"});
  } catch (error) {
    await chrome.storage.session.remove(storageKey(tab.id));
    throw error;
  }
  return {ok:true};
}
async function handle(message, sender) {
  const origin = localWorkbench(sender);
  if (message.type === "PING" && origin) return {ok:true};
  if (message.type === "OPEN_CHATGPT" && origin) {
    // Double clicks and retransmissions in the same worker share a single tab creation.
    const key = `${sender.tab.id}:${message.transfer?.id}`;
    if (!activeOpens.has(key)) activeOpens.set(key, openChatGPT(message, sender, origin).finally(() => activeOpens.delete(key)));
    return activeOpens.get(key);
  }
  if (!chatgptTab(sender)) throw new Error("此页面无权读取提交。");
  const task = await assignment(sender.tab.id);
  if (!task) return {ok:true, task:null};
  if (Date.now() - task.created > 60 * 60 * 1000) {
    await chrome.storage.session.remove(storageKey(sender.tab.id));
    throw new Error("提交已过期，请从工作台重新提交。");
  }
  if (message.type === "GET_TASK") {
    if (task.claimed) {
      await update(task, "error", "ChatGPT 页面在填充过程中刷新。请检查已有文字和附件；为避免重复上传，请从工作台重新提交到新标签页。");
      await chrome.storage.session.remove(storageKey(sender.tab.id));
      return {ok:false, error:"交接过程中页面已刷新，请检查已有内容并从工作台重试。"};
    }
    const data = await localRequest(task);
    if (["completed", "cancelled", "error"].includes(data.status)) {
      await chrome.storage.session.remove(storageKey(sender.tab.id));
      return {ok:true, task:null};
    }
    return {ok:true, task:data};
  }
  if (message.type === "CLAIM") {
    if (task.claimed) throw new Error("本次提交已开始填充，请勿重复执行。");
    const data = await localRequest(task);
    if (["cancelled", "completed", "error"].includes(data.status)) throw new Error("提交已结束，请回到工作台重试。");
    task.claimed = true;
    await chrome.storage.session.set({ [storageKey(sender.tab.id)]:task });
    return {ok:true};
  }
  if (message.type === "FILE_CHUNK") {
    if (!task.claimed || !Number.isInteger(message.index) || message.index < 0 || !Number.isInteger(message.offset) || message.offset < 0) throw new Error("附件请求无效。");
    return {ok:true, ...await localRequest(task, `/files/${message.index}?offset=${message.offset}`)};
  }
  if (message.type === "STATUS") {
    if (!["waiting","filling","uploading","completed","error"].includes(message.status) || typeof message.message !== "string") throw new Error("状态无效。");
    await update(task, message.status, message.message.slice(0,500));
    if (["completed","error"].includes(message.status)) await chrome.storage.session.remove(storageKey(sender.tab.id));
    return {ok:true};
  }
  throw new Error("不支持的扩展操作。");
}
chrome.runtime.onMessage.addListener((message, sender, respond) => {
  handle(message || {}, sender).then(respond, error => respond({ok:false,error:error.message}));
  return true;
});
chrome.tabs.onRemoved.addListener(tabId => {
  (async () => {
    const task = await assignment(tabId);
    if (task) {
      try { await update(task, "cancelled", "ChatGPT 标签页已关闭；可以从工作台重新提交。"); }
      finally { await chrome.storage.session.remove(storageKey(tabId)); }
    }
  })().catch(() => {});
});
