/* 对话树（Branchboard）核心：布局算法、Markdown 渲染与导出。纯函数，浏览器与 Node 测试共用。 */
(function (root, factory) {
  const api = factory();
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.BranchboardCore = api;
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  const LAYOUT = { QW: 280, AW: 360, QA_GAP: 44, TURN_GAP: 60, ROWGAP: 64, PAD: 48 };

  function childrenOf(tree, id) {
    return tree.order.map(cid => tree.turns[cid]).filter(t => t && t.parentId === id);
  }

  /** 第一步：按创建顺序算每轮的列坐标和行号（与卡片高度无关）。
      - follow：问题接在父回答右侧，同一行；同一父节点的第二个及以后的 follow 另开新行。
      - highlight：问题对齐在被划词回答的正下 / 正上方，按 下、上 交替（方向持久化在 layout.dir），每条分支独占新行。 */
  function layoutGrid(tree, opts = {}) {
    const C = { ...LAYOUT, ...opts };
    const grid = {};
    const hlCount = {};
    const followTaken = {};
    let minRow = 0, maxRow = 0;
    for (const id of tree.order) {
      const t = tree.turns[id];
      if (!t) continue;
      const p = t.parentId ? grid[t.parentId] : null;
      let qx, row, dir = 0;
      if (!p) {
        qx = 0; row = 0;
        if (t.parentId) { row = ++maxRow; }  // 父节点缺失：独占一行，避免重叠
      } else if (t.kind === "highlight") {
        hlCount[t.parentId] = (hlCount[t.parentId] || 0) + 1;
        dir = (t.layout && (t.layout.dir === 1 || t.layout.dir === -1)) ? t.layout.dir : (hlCount[t.parentId] % 2 === 1 ? 1 : -1);
        qx = p.ax;
        row = dir > 0 ? ++maxRow : --minRow;
      } else {
        qx = p.ax + C.AW + C.TURN_GAP;
        if (!followTaken[t.parentId]) { row = p.row; followTaken[t.parentId] = id; }
        else {
          dir = (t.layout && t.layout.dir === -1) ? -1 : 1;
          row = dir > 0 ? ++maxRow : --minRow;
        }
      }
      grid[id] = { id, qx, ax: qx + C.QW + C.QA_GAP, row, dir };
      minRow = Math.min(minRow, row); maxRow = Math.max(maxRow, row);
    }
    return { grid, minRow, maxRow };
  }

  /** 第二步：结合测量到的卡片高度算出像素位置。sizes[id] = {qh, ah}。 */
  function layoutTree(tree, sizes, opts = {}) {
    const C = { ...LAYOUT, ...opts };
    const { grid, minRow, maxRow } = layoutGrid(tree, C);
    const rowH = new Array(maxRow - minRow + 1).fill(0);
    for (const g of Object.values(grid)) {
      const s = sizes[g.id] || { qh: 80, ah: 80 };
      g.qh = s.qh; g.ah = s.ah;
      rowH[g.row - minRow] = Math.max(rowH[g.row - minRow], s.qh, s.ah);
    }
    const rowTop = [];
    let y = C.PAD;
    rowH.forEach((h, i) => { rowTop[i] = y; y += (h || 0) + C.ROWGAP; });
    let width = 0;
    for (const g of Object.values(grid)) {
      g.y = rowTop[g.row - minRow];
      g.qx += C.PAD; g.ax += C.PAD;
      width = Math.max(width, g.ax + C.AW);
    }
    return { cards: grid, width: width + C.PAD, height: y - C.ROWGAP + C.PAD, C };
  }

  /** 连线：问题→回答（同一轮）、follow（实线）、highlight（虚线，垂直）。 */
  function layoutWires(tree, layout) {
    const { cards, C } = layout;
    const LY = 24;
    const wires = [];
    for (const id of tree.order) {
      const g = cards[id];
      const t = tree.turns[id];
      if (!g || !t) continue;
      wires.push({ type: "qa", id, x1: g.qx + C.QW, y1: g.y + LY, x2: g.ax, y2: g.y + LY });
      const p = t.parentId && cards[t.parentId];
      if (!p) continue;
      if (t.kind === "highlight") {
        const x = p.ax + 40;
        const y1 = g.dir > 0 ? p.y + p.ah : p.y;
        const y2 = g.dir > 0 ? g.y : g.y + g.qh;
        wires.push({ type: "highlight", id, x1: x, y1, x2: x, y2 });
      } else if (g.row === p.row) {
        wires.push({ type: "follow", id, x1: p.ax + C.AW, y1: p.y + LY, x2: g.qx, y2: g.y + LY });
      } else {
        wires.push({ type: "fork", id, x1: p.ax + C.AW, y1: p.y + LY, x2: g.qx, y2: g.y + LY });
      }
    }
    return wires;
  }

  function rectsOverlap(a, b) {
    return a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;
  }

  function cardRects(layout) {
    const rects = [];
    for (const g of Object.values(layout.cards)) {
      rects.push({ id: g.id, side: "q", x: g.qx, y: g.y, w: layout.C.QW, h: g.qh });
      rects.push({ id: g.id, side: "a", x: g.ax, y: g.y, w: layout.C.AW, h: g.ah });
    }
    return rects;
  }

  function ancestors(tree, id) {
    const path = [];
    let cur = tree.turns[id];
    while (cur) { path.unshift(cur.id); cur = cur.parentId ? tree.turns[cur.parentId] : null; }
    return path;
  }

  function turnNumber(tree, id) { return tree.order.indexOf(id) + 1; }

  /** 主线末端：从根沿第一个 follow 子节点一直走到底。 */
  function mainLeaf(tree) {
    let cur = tree.rootId && tree.turns[tree.rootId];
    while (cur) {
      const next = childrenOf(tree, cur.id).find(t => t.kind === "follow");
      if (!next) return cur.id;
      cur = next;
    }
    return null;
  }

  /* ---------- Markdown ---------- */
  const escapeHtml = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const safeUrl = url => /^(https?:|mailto:|#|\/)/i.test(url) ? url : "#";

  function inline(text) {
    const codes = [];
    let s = String(text).replace(/`([^`\n]+)`/g, (_m, c) => { codes.push(c); return `${codes.length - 1}`; });
    s = escapeHtml(s);
    s = s.replace(/\[([^\]]+)\]\(([^\s)]+)(?:\s+&quot;[^)]*&quot;)?\)/g, (_m, label, url) => `<a href="${escapeHtml(safeUrl(url.replace(/&amp;/g, "&")))}" target="_blank" rel="noreferrer">${label}</a>`);
    s = s.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>").replace(/__([^_\n]+)__/g, "<strong>$1</strong>");
    s = s.replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>").replace(/(^|[^\w_])_([^_\n]+)_(?![\w])/g, "$1<em>$2</em>");
    s = s.replace(/~~([^~\n]+)~~/g, "<del>$1</del>");
    return s.replace(/(\d+)/g, (_m, i) => `<code>${escapeHtml(codes[Number(i)])}</code>`);
  }

  const cells = line => line.trim().replace(/^\||\|$/g, "").split("|").map(c => c.trim());
  const isDivider = line => { const c = cells(line || ""); return c.length > 0 && c.every(x => /^:?-{3,}:?$/.test(x)); };

  function listHtml(lines) {
    // 按缩进构建嵌套列表
    const rootList = { ordered: /^\s*\d+[.)]\s/.test(lines[0]), items: [], indent: -1 };
    const stack = [rootList];
    for (const line of lines) {
      const m = line.match(/^(\s*)([-+*]|\d+[.)])\s+(.*)$/);
      if (!m) { const top = stack[stack.length - 1]; if (top.items.length) top.items[top.items.length - 1].text += " " + line.trim(); continue; }
      const indent = m[1].replace(/\t/g, "    ").length;
      while (stack.length > 1 && indent <= stack[stack.length - 1].indent) stack.pop();
      let top = stack[stack.length - 1];
      if (top.items.length && indent > (top.childIndent ?? top.indent) + 1 && stack.length < 6) {
        const parentItem = top.items[top.items.length - 1];
        const sub = { ordered: /\d/.test(m[2]), items: [], indent };
        parentItem.sub = sub; stack.push(sub); top = sub;
      }
      top.childIndent = top.childIndent ?? indent;
      top.items.push({ text: m[3] });
    }
    const render = list => `<${list.ordered ? "ol" : "ul"}>${list.items.map(it => `<li>${inline(it.text)}${it.sub ? render(it.sub) : ""}</li>`).join("")}</${list.ordered ? "ol" : "ul"}>`;
    return render(rootList);
  }

  function markdownToHtml(markdown) {
    const lines = String(markdown || "").replace(/\r\n?/g, "\n").split("\n");
    const out = [];
    const blockStart = (l, next) => /^ {0,3}(#{1,6}\s|```|~~~|>|(?:[-*_]\s*){3,}$)/.test(l) || /^\s*(?:[-+*]|\d+[.)])\s+/.test(l) || (l.includes("|") && isDivider(next));
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      if (!line.trim()) { i++; continue; }
      const fence = line.match(/^ {0,3}(```+|~~~+)\s*([\w+-]*)/);
      if (fence) {
        const code = []; i++;
        while (i < lines.length && !lines[i].trim().startsWith(fence[1])) code.push(lines[i++]);
        i++;
        out.push(`<pre><code${fence[2] ? ` class="language-${escapeHtml(fence[2])}"` : ""}>${escapeHtml(code.join("\n"))}</code></pre>`);
        continue;
      }
      const h = line.match(/^ {0,3}(#{1,6})\s+(.+?)\s*#*\s*$/);
      if (h) { const lv = Math.min(6, h[1].length + 2); out.push(`<h${lv}>${inline(h[2])}</h${lv}>`); i++; continue; }
      if (/^ {0,3}(?:[-*_]\s*){3,}$/.test(line)) { out.push("<hr>"); i++; continue; }
      if (line.includes("|") && isDivider(lines[i + 1])) {
        const head = cells(line); i += 2; const rows = [];
        while (i < lines.length && lines[i].includes("|") && lines[i].trim()) rows.push(cells(lines[i++]));
        out.push(`<div class="bb-table"><table><thead><tr>${head.map(c => `<th>${inline(c)}</th>`).join("")}</tr></thead><tbody>${rows.map(r => `<tr>${head.map((_, k) => `<td>${inline(r[k] || "")}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`);
        continue;
      }
      if (/^ {0,3}>/.test(line)) {
        const q = [];
        while (i < lines.length && /^ {0,3}>/.test(lines[i])) q.push(lines[i++].replace(/^ {0,3}>\s?/, ""));
        out.push(`<blockquote>${markdownToHtml(q.join("\n"))}</blockquote>`);
        continue;
      }
      if (/^\s*(?:[-+*]|\d+[.)])\s+/.test(line)) {
        const items = [];
        const ordered = /^\s*\d+[.)]\s/.test(line);
        while (i < lines.length && (/^\s*(?:[-+*]|\d+[.)])\s+/.test(lines[i]) || (/^\s{2,}\S/.test(lines[i]) && items.length))) {
          // 顶层列表类型变了（无序 ↔ 有序）就另起一个列表
          if (items.length && /^\S/.test(lines[i]) && /^\d+[.)]\s/.test(lines[i]) !== ordered) break;
          items.push(lines[i++]);
        }
        out.push(listHtml(items));
        continue;
      }
      const para = [line]; i++;
      while (i < lines.length && lines[i].trim() && !blockStart(lines[i], lines[i + 1])) para.push(lines[i++]);
      out.push(`<p>${para.map(inline).join("<br>")}</p>`);
    }
    return out.join("");
  }

  /** 回答卡上的预览：摘要不可用时取全文前若干个非空行。 */
  function previewMarkdown(markdown, maxLines = 8) {
    const lines = String(markdown || "").replace(/\r\n?/g, "\n").split("\n");
    const kept = [];
    let inFence = false, count = 0;
    for (const line of lines) {
      if (/^ {0,3}(```|~~~)/.test(line)) inFence = !inFence;
      kept.push(line);
      if (line.trim()) count++;
      if (count >= maxLines && !inFence) break;
    }
    if (inFence) kept.push("```");
    return kept.join("\n");
  }

  /** 在全文 markdown 中定位一段（渲染后选中的）文字；空白差异按任意空白匹配。 */
  function locateQuote(markdown, text) {
    const md = String(markdown || "");
    const q = String(text || "").trim();
    if (!q) return null;
    let start = md.indexOf(q);
    if (start >= 0) return { start, end: start + q.length };
    const words = q.split(/\s+/).filter(Boolean).map(w => w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
    if (!words.length) return null;
    try {
      const m = new RegExp(words.join("[\\s>*_`#|-]+")).exec(md);
      if (m) return { start: m.index, end: m.index + m[0].length };
    } catch (_e) { /* 过长的正则 */ }
    return null;
  }

  /* ---------- 导出 ---------- */
  const KIND_LABEL = { root: "起点", follow: "继续提问", highlight: "划词提问" };

  function treeToMarkdown(tree) {
    const out = [`# ${tree.title}`, "", `> 工作目录：\`${tree.cwd}\` · 共 ${tree.order.length} 轮`, ""];
    const visit = (id, level) => {
      const t = tree.turns[id];
      if (!t) return;
      const n = turnNumber(tree, id);
      const parentN = t.parentId ? `（接 #${turnNumber(tree, t.parentId)}）` : "";
      out.push(`${"#".repeat(Math.min(6, level))} #${n} ${KIND_LABEL[t.kind] || ""}${parentN}`, "");
      if (t.quote && t.quote.text) out.push(t.quote.text.split("\n").map(l => `> ${l}`).join("\n"), "");
      out.push(`**问：** ${t.question}`, "");
      const a = t.answer || {};
      if (a.filesChanged && a.filesChanged.length) out.push(`*修改了文件：${a.filesChanged.map(f => "`" + f + "`").join("、")}*`, "");
      out.push(a.markdown ? a.markdown.trim() : `*（${a.status === "error" ? "出错：" + (a.error || "") : "无回答"}）*`, "");
      const kids = childrenOf(tree, id);
      let firstFollow = true;
      for (const k of kids) {
        const sameLine = k.kind === "follow" && firstFollow;
        if (k.kind === "follow") firstFollow = false;
        visit(k.id, sameLine ? level : level + 1);
      }
    };
    if (tree.rootId) visit(tree.rootId, 2);
    return out.join("\n").replace(/\n{3,}/g, "\n\n") + "\n";
  }

  /** 静态 HTML：外观与原型 conversation_tree.html 对齐，布局在打开时用同一算法重算。 */
  function treeToStaticHtml(tree) {
    const data = tree.order.map(id => {
      const t = tree.turns[id];
      const a = t.answer || {};
      return {
        id: turnNumber(tree, id), key: id, parentKey: t.parentId, kind: t.kind, dir: t.layout && t.layout.dir,
        quote: t.quote && t.quote.text, q: t.question,
        summary: a.summary && a.summary.length ? a.summary : null,
        preview: a.summary && a.summary.length ? "" : markdownToHtml(previewMarkdown(a.markdown, 6)),
        full: markdownToHtml(a.markdown || "") || "<p><i>（无回答）</i></p>",
        files: a.filesChanged || [],
      };
    });
    const json = JSON.stringify(data).replace(/</g, "\\u003c");
    const title = escapeHtml(tree.title);
    return `<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>对话树：${title}</title>
<style>
:root{--bg:#f6f5f1;--ink:#1f1f1c;--muted:#6b6a63;--line:#b9b6aa;--q-bg:#fff;--q-bd:#d9d6cc;--a-bg:#fbfaf6;--a-bd:#e3e0d6;--hl:#c2622d;--hl-soft:#f7e6da;--main:#2f5d8a;--main-soft:#e3ecf5}
@media (prefers-color-scheme: dark){:root{--bg:#1b1b19;--ink:#ecebe6;--muted:#a3a198;--line:#55534c;--q-bg:#262623;--q-bd:#3a3935;--a-bg:#22221f;--a-bd:#34332f;--hl:#e48a55;--hl-soft:#3a2a20;--main:#7fb0de;--main-soft:#1f2c3a}}
*{box-sizing:border-box}html,body{margin:0;height:100%;background:var(--bg);color:var(--ink);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
header{position:sticky;top:0;z-index:5;display:flex;align-items:center;gap:16px;flex-wrap:wrap;padding:12px 20px;background:var(--bg);border-bottom:1px solid var(--q-bd)}
header h1{font-size:16px;margin:0;font-weight:600}.legend{display:flex;gap:16px;font-size:12px;color:var(--muted);align-items:center}
.legend i{display:inline-block;width:26px;border-top:2px solid var(--main);vertical-align:middle;margin-right:6px}.legend i.hl{border-top:2px dashed var(--hl)}
.zoom{margin-left:auto;display:flex;gap:6px}.zoom button{border:1px solid var(--q-bd);background:var(--q-bg);color:var(--ink);border-radius:6px;padding:4px 10px;cursor:pointer}
#viewport{position:absolute;top:53px;left:0;right:0;bottom:0;overflow:auto}#stage{position:relative;transform-origin:0 0}#wires{position:absolute;left:0;top:0;pointer-events:none;overflow:visible}
.card{position:absolute;border-radius:10px;padding:12px 14px;font-size:13.5px;line-height:1.55;cursor:pointer}.card:hover{box-shadow:0 4px 14px rgba(0,0,0,.08)}
.q{background:var(--q-bg);border:1px solid var(--q-bd);width:${LAYOUT.QW}px}.a{background:var(--a-bg);border:1px solid var(--a-bd);width:${LAYOUT.AW}px}
.a .pv{max-height:220px;overflow:hidden}.a ul{margin:4px 0 0;padding-left:18px}.a p{margin:4px 0}
.tag{display:inline-block;font-size:11px;font-weight:600;padding:2px 7px;border-radius:999px;margin-bottom:6px}.tag.main{background:var(--main-soft);color:var(--main)}.tag.hl{background:var(--hl-soft);color:var(--hl)}.tag.ans{color:var(--muted);padding-left:0}
.quote{border-left:3px solid var(--hl);background:var(--hl-soft);padding:6px 8px;margin:4px 0 8px;font-size:12.5px;border-radius:0 6px 6px 0;white-space:pre-wrap}.quote b{color:var(--hl);font-size:11px;display:block}
.more{font-size:12px;color:var(--main);margin-top:8px}.num{color:var(--muted);font-size:11px;float:right}.files{font-size:11px;color:var(--hl)}
#drawer{position:fixed;top:0;right:0;bottom:0;width:min(640px,100%);background:var(--q-bg);border-left:1px solid var(--q-bd);transform:translateX(100%);transition:transform .22s;z-index:10;display:flex;flex-direction:column}
#drawer.open{transform:none}#drawer header{position:static}#drawer .close{margin-left:auto;border:none;background:none;font-size:20px;cursor:pointer;color:var(--muted)}
#dbody{overflow:auto;padding:18px 24px 40px;font-size:14.5px;line-height:1.75}#dbody pre{background:var(--a-bg);padding:10px;border-radius:6px;overflow:auto}#dbody table{border-collapse:collapse;font-size:13px}#dbody td,#dbody th{border:1px solid var(--q-bd);padding:6px 8px}
#dbody .qbox{background:var(--a-bg);border:1px solid var(--a-bd);border-radius:8px;padding:10px 12px;margin-bottom:12px;font-size:13.5px;white-space:pre-wrap}
</style></head><body>
<header><h1>对话树 · ${title}</h1><div class="legend"><span><i></i>正常追问（水平延伸）</span><span><i class="hl"></i>划词提问（上下分支）</span><span>点击卡片查看完整内容</span></div>
<div class="zoom"><button id="zout">−</button><button id="zreset">100%</button><button id="zin">＋</button></div></header>
<div id="viewport"><div id="stage"><svg id="wires"></svg></div></div>
<aside id="drawer"><header><h1 id="dtitle"></h1><button class="close" id="dclose">×</button></header><div id="dbody"></div></aside>
<script>
const TURNS=${json};
const QW=${LAYOUT.QW},AW=${LAYOUT.AW},QA_GAP=${LAYOUT.QA_GAP},TURN_GAP=${LAYOUT.TURN_GAP},ROWGAP=${LAYOUT.ROWGAP},PAD=${LAYOUT.PAD};
const byKey=Object.fromEntries(TURNS.map(t=>[t.key,t]));let minRow=0,maxRow=0;const hl={},fol={};
TURNS.forEach(t=>{const p=byKey[t.parentKey];if(!p){t.qx=0;t.row=0;}else if(t.kind==="highlight"){hl[p.key]=(hl[p.key]||0)+1;t.dir=t.dir||(hl[p.key]%2?1:-1);t.qx=p.ax;t.row=t.dir>0?++maxRow:--minRow;}
else{t.qx=p.ax+AW+TURN_GAP;if(!fol[p.key]){fol[p.key]=1;t.row=p.row;}else{t.dir=t.dir===-1?-1:1;t.row=t.dir>0?++maxRow:--minRow;}}t.ax=t.qx+QW+QA_GAP;minRow=Math.min(minRow,t.row);maxRow=Math.max(maxRow,t.row);});
const stage=document.getElementById("stage"),svg=document.getElementById("wires");const esc=s=>String(s).replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
TURNS.forEach(t=>{const tag=t.kind==="highlight"?'<span class="tag hl">划词提问</span>':t.kind==="follow"?'<span class="tag main">继续提问</span>':'<span class="tag main">起点</span>';
const q=document.createElement("div");q.className="card q";q.dataset.key=t.key;q.innerHTML=tag+'<span class="num">#'+t.id+'</span>'+(t.quote?'<div class="quote"><b>划中的原文</b>'+esc(t.quote)+'</div>':"")+'<div style="white-space:pre-wrap">'+esc(t.q)+'</div>';
const a=document.createElement("div");a.className="card a";a.dataset.key=t.key;a.innerHTML='<span class="tag ans">回答 #'+t.id+'</span>'+(t.files.length?'<span class="files"> · 修改了 '+t.files.length+' 个文件</span>':"")+(t.summary?'<ul>'+t.summary.map(s=>'<li>'+esc(s)+'</li>').join("")+'</ul>':'<div class="pv">'+t.preview+'</div>')+'<div class="more">查看完整回答 →</div>';
stage.appendChild(q);stage.appendChild(a);t.qel=q;t.ael=a;});
function layout(){const rowH=new Array(maxRow-minRow+1).fill(0);TURNS.forEach(t=>{const r=t.row-minRow;rowH[r]=Math.max(rowH[r],t.qel.offsetHeight,t.ael.offsetHeight);});
const top=[];let y=PAD;rowH.forEach((h,i)=>{top[i]=y;y+=h+ROWGAP;});let W=0;TURNS.forEach(t=>{t.y=top[t.row-minRow];t.qel.style.left=(PAD+t.qx)+"px";t.qel.style.top=t.y+"px";t.ael.style.left=(PAD+t.ax)+"px";t.ael.style.top=t.y+"px";W=Math.max(W,PAD+t.ax+AW);});
stage.style.width=(W+PAD)+"px";stage.style.height=y+"px";svg.setAttribute("width",W+PAD);svg.setAttribute("height",y);const css=getComputedStyle(document.documentElement);
const cM=css.getPropertyValue("--main").trim(),cH=css.getPropertyValue("--hl").trim(),cL=css.getPropertyValue("--line").trim();let d="";
TURNS.forEach(t=>{const qr=PAD+t.qx+QW,al=PAD+t.ax,yy=t.y+24;d+='<path d="M'+qr+','+yy+' H'+al+'" stroke="'+cL+'" stroke-width="2"/><path d="M'+(al-7)+','+(yy-4)+' L'+al+','+yy+' L'+(al-7)+','+(yy+4)+'" fill="none" stroke="'+cL+'" stroke-width="2"/>';
const p=byKey[t.parentKey];if(!p)return;if(t.kind==="highlight"){const x=PAD+p.ax+40,y1=t.dir>0?p.y+p.ael.offsetHeight:p.y,y2=t.dir>0?t.y:t.y+t.qel.offsetHeight;d+='<path d="M'+x+','+y1+' V'+y2+'" stroke="'+cH+'" stroke-width="2" stroke-dasharray="6 5"/><circle cx="'+x+'" cy="'+y1+'" r="4" fill="'+cH+'"/><circle cx="'+x+'" cy="'+y2+'" r="4" fill="'+cH+'"/>';}
else{const x1=PAD+p.ax+AW,x2=PAD+t.qx,y1=p.y+24,y2=t.y+24,mx=(x1+x2)/2;d+='<path d="M'+x1+','+y1+' H'+mx+' V'+y2+' H'+x2+'" fill="none" stroke="'+cM+'" stroke-width="2"/><circle cx="'+x1+'" cy="'+y1+'" r="4" fill="'+cM+'"/><circle cx="'+x2+'" cy="'+y2+'" r="4" fill="'+cM+'"/>';}});svg.innerHTML=d;}
const drawer=document.getElementById("drawer");stage.addEventListener("click",e=>{const c=e.target.closest(".card");if(!c)return;const t=byKey[c.dataset.key];
document.getElementById("dtitle").textContent="第 "+t.id+" 轮"+(t.kind==="highlight"?" · 划词提问":"");document.getElementById("dbody").innerHTML='<div class="qbox">'+(t.quote?'<div class="quote"><b>划中的原文</b>'+esc(t.quote)+'</div>':"")+'<b>提问：</b>'+esc(t.q)+'</div>'+t.full;drawer.classList.add("open");});
document.getElementById("dclose").onclick=()=>drawer.classList.remove("open");document.addEventListener("keydown",e=>{if(e.key==="Escape")drawer.classList.remove("open");});
let z=1;const applyZ=()=>{stage.style.transform="scale("+z+")";document.getElementById("zreset").textContent=Math.round(z*100)+"%";};
document.getElementById("zin").onclick=()=>{z=Math.min(1.6,z+.1);applyZ();};document.getElementById("zout").onclick=()=>{z=Math.max(.3,z-.1);applyZ();};document.getElementById("zreset").onclick=()=>{z=1;applyZ();};
window.addEventListener("load",layout);window.addEventListener("resize",layout);layout();
</script></body></html>`;
  }

  return { LAYOUT, layoutGrid, layoutTree, layoutWires, cardRects, rectsOverlap, ancestors, childrenOf, mainLeaf,
    turnNumber, markdownToHtml, previewMarkdown, locateQuote, treeToMarkdown, treeToStaticHtml, escapeHtml, KIND_LABEL };
});
