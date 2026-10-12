(() => {
  if (window.top !== window) return;
  const sleep = ms => new Promise(resolve => setTimeout(resolve,ms));
  async function send(type, payload = {}) {
    const result = await chrome.runtime.sendMessage({type,...payload});
    if (!result?.ok) throw new Error(result?.error || "浏览器扩展连接中断，请回到工作台重试。");
    return result;
  }
  let banner;
  function notice(message, failed = false) {
    if (!banner) {
      banner = document.createElement("div"); banner.setAttribute("role","status");
      banner.id = "research-prompt-bridge-status";
      Object.assign(banner.style, {position:"fixed",top:"14px",right:"18px",maxWidth:"400px",padding:"14px 18px",borderRadius:"10px",font:"13px/1.7 system-ui, sans-serif",zIndex:"2147483647",boxShadow:"0 6px 24px #0002",whiteSpace:"pre-wrap"});
      document.documentElement.append(banner);
    }
    banner.textContent = `Prompt 工作台\n${message}`;
    banner.style.background = failed ? "#fff0e9" : "#edf6f1"; banner.style.color = failed ? "#863c29" : "#204d3e";
  }
  async function status(value, message) { notice(message, value === "error"); await send("STATUS",{status:value,message}); }
  function editor() {
    // The current composer uses data-composer-markdown, with no prompt-textarea ID.
    // Scope fallbacks to ChatGPT's composer so extension search/notes boxes are ignored.
    const selectors = [
      '[data-chatgpt-composer] [data-composer-markdown][contenteditable], [data-composer-input] [data-composer-markdown][contenteditable]',
      '[data-chatgpt-composer] [contenteditable][role="textbox"], [data-chatgpt-composer] textarea',
      '#prompt-textarea, #mobile-composer-prompt, textarea[data-testid="prompt-textarea"], [contenteditable="true"][data-lexical-editor="true"]',
    ];
    for (const selector of selectors) {
      const node = [...document.querySelectorAll(selector)].find(node => {
        const style = getComputedStyle(node), rect = node.getBoundingClientRect();
        return node.getClientRects().length && rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.visibility !== "collapse" &&
          !node.disabled && !node.readOnly && node.getAttribute("aria-disabled") !== "true" && !node.closest('[inert], [hidden], [aria-hidden="true"]') &&
          (node.tagName === "TEXTAREA" || node.isContentEditable);
      });
      if (node) return node;
    }
  }
  function editorText(node) { return node.tagName === "TEXTAREA" ? node.value : node.innerText; }
  const normalize = text => text.replace(/\r\n/g,"\n").replace(/\u00a0/g," ").trim();
  function fill(node, text) {
    if (editorText(node).trim()) throw new Error("ChatGPT 输入框已有内容，为保留现有草稿，已停止填充。请回工作台重新提交到新标签页。");
    node.focus();
    if (node.tagName === "TEXTAREA") {
      Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,"value").set.call(node,text);
      node.dispatchEvent(new Event("input",{bubbles:true}));
    } else {
      const selection = window.getSelection(), range = document.createRange();
      range.selectNodeContents(node); selection.removeAllRanges(); selection.addRange(range);
      // Let ProseMirror/Lexical handle plain-text paste through their own input pipeline.
      const clipboard = new DataTransfer(); clipboard.setData("text/plain",text);
      node.dispatchEvent(new ClipboardEvent("paste",{bubbles:true,cancelable:true,clipboardData:clipboard}));
      if (!editorText(node)) {
        // Explicit line breaks avoid Chromium insertText adding extra blank div lines.
        const html = text.replace(/[&<>"']/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[char])).replace(/\r\n/g,"\n").replace(/\n/g,"<br>");
        if (!document.execCommand("insertHTML",false,html)) {
          node.textContent = text;
          node.dispatchEvent(new InputEvent("input",{bubbles:true,inputType:"insertText",data:text}));
        }
      }
    }
    if (normalize(editorText(node)) !== normalize(text)) throw new Error("无法完整填充 ChatGPT 输入框，请检查页面并从工作台复制文字。");
  }
  async function readFile(meta) {
    const chunks = []; let offset = 0, done = false;
    while (!done) {
      const part = await send("FILE_CHUNK",{index:meta.index,offset});
      const bytes = Uint8Array.from(atob(part.data), char => char.charCodeAt(0));
      if (part.nextOffset !== offset + bytes.length || (!part.done && part.nextOffset <= offset)) throw new Error("附件传输不完整，请重新提交。");
      chunks.push(bytes); offset = part.nextOffset; done = part.done;
    }
    if (offset !== meta.size) throw new Error(`附件 ${meta.name} 的大小校验失败。`);
    return new File(chunks,meta.name,{type:meta.type});
  }
  function accepts(input, files) {
    const types = (input.accept || "").toLowerCase().split(",").map(value => value.trim()).filter(Boolean);
    return !types.length || files.every(file => types.some(type => type === "*/*" || (type.startsWith(".") ? file.name.toLowerCase().endsWith(type) : type.endsWith("/*") ? file.type.startsWith(type.slice(0,-1)) : file.type === type)));
  }
  function attach(node, files) {
    const transfer = new DataTransfer(); files.forEach(file => transfer.items.add(file));
    const input = [...document.querySelectorAll('input[type="file"]')].find(node => !node.disabled && (node.multiple || files.length === 1) && accepts(node,files));
    if (input) {
      input.files = transfer.files;
      input.dispatchEvent(new Event("change",{bubbles:true}));
    } else {
      // ChatGPT also accepts pasted files; no clipboard permission or OS dialog is needed.
      node.focus();
      node.dispatchEvent(new ClipboardEvent("paste",{bubbles:true,cancelable:true,clipboardData:transfer}));
    }
  }
  function attachmentEvidence(node, files) {
    const scope = node.closest("form") || node.parentElement?.parentElement;
    if (!scope) return false;
    // Text in the prompt itself must never be treated as evidence of an uploaded attachment.
    const outside = scope.cloneNode(true);
    outside.querySelectorAll("textarea, [contenteditable]").forEach(el => el.remove());
    const names = [outside.textContent, ...[...outside.querySelectorAll("[title], [aria-label], img[alt]")].map(el => `${el.getAttribute("title") || ""}\n${el.getAttribute("aria-label") || ""}\n${el.getAttribute("alt") || ""}`)].join("\n");
    return files.every(file => names.includes(file.name));
  }
  async function run() {
    const result = await send("GET_TASK"); if (!result.task) return;
    const task = result.task;
    await status("waiting","正在检测 ChatGPT 输入框，请保持本地工具箱运行…");
    let node;
    const started = Date.now();
    let needsLogin = false, lastProgress = -1;
    while (true) {
      node = editor();
      needsLogin = task.files.length && [...document.querySelectorAll("button")].some(button => button.getClientRects().length && /^(登录|Log in|Sign in)$/i.test(button.textContent.trim()));
      if (node && !needsLogin) break;
      node = null;
      const elapsed = Math.floor((Date.now() - started) / 1000), limit = needsLogin ? 180 : 30;
      if (elapsed >= limit) break;
      const progress = `${needsLogin ? "login" : "editor"}:${Math.floor(elapsed / 5)}`;
      if (progress !== lastProgress) {
        lastProgress = progress;
        await status("waiting", needsLogin
          ? `当前页面尚未登录；本次包含附件，请先登录 ChatGPT。等待登录 ${elapsed} / ${limit} 秒。`
          : `正在等待网页输入框就绪：${elapsed} / ${limit} 秒。若页面已显示输入框仍无法识别，超时后会报告错误。`);
      }
      await sleep(700);
    }
    if (!node) throw new Error(needsLogin
      ? "等待登录已超时。请在此浏览器中登录 ChatGPT，再回到工作台重新提交。"
      : "30 秒内未找到可编辑的 ChatGPT 输入框。请确认网页已正常加载；若输入框可见，请更新扩展后重新提交，也可从工作台复制文字。");
    await send("CLAIM");
    await status("filling","正在填入 Prompt…");
    fill(node, task.prompt);
    // React may replace the node while accepting the editor transaction.
    await sleep(400); node = editor();
    if (!node || normalize(editorText(node)) !== normalize(task.prompt)) throw new Error("ChatGPT 没有保留完整文字，请回工作台复制或重新提交。");
    if (task.files.length) {
      const files = [];
      for (const meta of task.files) {
        await status("uploading",`正在传递附件 ${files.length + 1} / ${task.files.length}：${meta.name}`);
        files.push(await readFile(meta));
      }
      attach(node, files);
      await status("uploading","已将附件交给 ChatGPT，正在检查附件卡片…");
      const uploadDeadline = Date.now() + 45000;
      while (Date.now() < uploadDeadline) {
        node = editor();
        if (node && attachmentEvidence(node,files)) break;
        await sleep(600);
      }
      if (!node || !attachmentEvidence(node,files)) throw new Error("文字已填入，但无法确认所有附件已添加。请检查 ChatGPT 的附件卡片及错误提示；缺失附件可在该页手动添加，或从工作台重新提交。");
    }
    await status("completed", task.files.length
      ? `文字已填入，已检测到 ${task.files.length} 个附件。请等待 ChatGPT 完成处理，检查附件卡片或错误提示后点击发送。`
      : "Prompt 已填入。请检查内容后在 ChatGPT 中点击发送。");
  }
  run().catch(async error => {
    notice(error.message,true);
    try { await send("STATUS",{status:"error",message:error.message}); } catch { /* Keep the visible error even if the server stopped. */ }
  });
})();
