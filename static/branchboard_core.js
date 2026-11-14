/* 对话树（Branchboard）核心：布局算法、Markdown 渲染与导出。纯函数，浏览器与 Node 测试共用。 */
(function (root, factory) {
  const api = factory();
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.BranchboardCore = api;
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  /* 每一轮 = 一张卡片（大框 = 回答），顶部是一个或多个问题小框。
     一轮可以有多个来源：在几张卡片里各自划词，攒起来一次问出 → 多张卡片的有向箭头指向同一张新卡片里各自的问题小框。
     WIDTH 是三种显示模式下的卡片宽度；QMID 是问题框中线距卡片顶部的默认距离（连线挂在这里）。 */
  const MODES = ["question", "partial", "full"];
  /* GAP：列间距（新卡片在来源右侧这么远）；VGAP：上下相邻卡片的最小间距；HMARGIN：判断上下是否相邻时的水平余量；
     WIDTH：三种显示模式下的卡片宽度；QMID：问题框中线距卡片顶部的默认距离（连线挂在这里）。 */
  const LAYOUT = { GAP: 72, VGAP: 40, HMARGIN: 16, PAD: 48, QMID: 34, WIDTH: { question: 300, partial: 380, full: 600 } };
  /* 划词连线按来源卡片着色：同一张卡片引出的线颜色相同，目标卡片里对应的引用框也用这个颜色。 */
  const SOURCE_COLORS = ["#c2622d", "#2f7f86", "#7a52b3", "#b8437a", "#5d7f1f", "#2f63b5", "#a5781a", "#4b6e8c"];

  /** 一轮里的问题小框；旧数据只有一个 question / quote。 */
  function questionsOf(t) {
    if (Array.isArray(t.questions) && t.questions.length) return t.questions;
    return [{ id: "q0", text: t.question || "", sourceId: t.parentId || null, quote: t.quote }];
  }

  /** 本轮指向的来源卡片（去重，保持顺序）。 */
  function sourcesOf(t) {
    const out = [];
    for (const q of questionsOf(t)) if (q.sourceId && !out.includes(q.sourceId)) out.push(q.sourceId);
    return out;
  }

  function childrenOf(tree, id) {
    return tree.order.map(cid => tree.turns[cid]).filter(t => t && t.parentId === id);
  }

  /** 来源卡片的连线颜色（按创建顺序轮换）。 */
  function sourceColor(tree, id) {
    const i = tree.order.indexOf(id);
    return SOURCE_COLORS[(i < 0 ? 0 : i) % SOURCE_COLORS.length];
  }

  /** 第一步：按创建顺序决定每张卡片的列位置 x、主父节点、方向（与高度无关）。widths[id] 为卡片宽度。
      - 新卡片总在所有来源卡片右侧一列：x = 最右来源的右边缘 + GAP，所以箭头一律从左往右。
      - inline：父卡片的第一个普通追问（单来源、无划词），和父卡片顶边对齐（主线水平延伸）。
      - 其余（划词、多问题、多来源、第二个追问）：dir = 1 放在主父卡片下方、-1 放在上方，
        按 下、上 交替（方向持久化在 layout.dir，插入新分支不会让旧分支翻转）。 */
  function layoutGrid(tree, widths = {}, opts = {}) {
    const C = { ...LAYOUT, ...opts };
    const grid = {};
    const branchCount = {};
    const followTaken = {};
    for (const id of tree.order) {
      const t = tree.turns[id];
      if (!t) continue;
      const w = widths[id] || C.WIDTH.partial;
      const srcs = sourcesOf(t).filter(s => grid[s]);
      let x = 0, dir = 0, inline = false, primary = null;
      if (srcs.length) {
        primary = grid[t.parentId] ? t.parentId : srcs[srcs.length - 1];
        x = Math.max(...srcs.map(s => grid[s].x + grid[s].w)) + C.GAP;
        const qs = questionsOf(t);
        const plainFollow = srcs.length === 1 && qs.length === 1 && !(qs[0].quote && qs[0].quote.text) && t.kind !== "highlight" && t.kind !== "multi";
        if (plainFollow && !followTaken[primary] && !(t.layout && t.layout.dir)) {
          inline = true; followTaken[primary] = id;
        } else {
          branchCount[primary] = (branchCount[primary] || 0) + 1;
          dir = (t.layout && (t.layout.dir === 1 || t.layout.dir === -1)) ? t.layout.dir : (branchCount[primary] % 2 === 1 ? 1 : -1);
        }
      }
      grid[id] = { id, x, w, dir, inline, primary, srcs };
    }
    return { grid };
  }

  /** 第二步：就近放置。sizes[id] = {w, h, qms}（qms[i]：第 i 个问题框中线的 y 偏移）。
      按创建顺序逐张放：先试理想位置（inline：与父卡片顶边对齐；向下分支：紧贴主父卡片下方；向上分支：紧贴上方），
      和已放好的卡片挨得太近就沿该方向挪到刚好让开的位置。卡片之间至少隔 VGAP，因此不会重叠，
      新卡片也总是尽量靠近它的来源。 */
  function layoutTree(tree, sizes, opts = {}) {
    const C = { ...LAYOUT, ...opts };
    const widths = {};
    for (const [id, s] of Object.entries(sizes)) if (s && s.w) widths[id] = s.w;
    const { grid } = layoutGrid(tree, widths, C);
    const placed = [];
    const clash = (x, w, y, h) => placed.filter(c =>
      c.x < x + w + C.HMARGIN && x < c.x + c.w + C.HMARGIN && c.y < y + h + C.VGAP && y < c.y + c.h + C.VGAP);
    for (const id of tree.order) {
      const g = grid[id];
      if (!g) continue;
      const s = sizes[id] || {};
      g.h = s.h || 120;
      g.qms = s.qms && s.qms.length ? s.qms : [s.qm || C.QMID];
      let y, down = true;
      if (!g.primary) {
        y = placed.length ? Math.max(...placed.map(c => c.y + c.h)) + C.VGAP : 0;  // 根（或来源缺失）
      } else {
        const p = grid[g.primary];
        if (g.inline) y = p.y;
        else if (g.dir > 0) y = p.y + p.h + C.VGAP;
        else { y = p.y - g.h - C.VGAP; down = false; }
      }
      for (let hits = clash(g.x, g.w, y, g.h); hits.length; hits = clash(g.x, g.w, y, g.h)) {
        y = down ? Math.max(...hits.map(c => c.y + c.h)) + C.VGAP : Math.min(...hits.map(c => c.y)) - g.h - C.VGAP;
      }
      g.y = y;
      if (g.inline && g.y !== grid[g.primary].y) g.inline = false;  // 被挤开了，就不再算同一行
      placed.push(g);
    }
    const minY = placed.length ? Math.min(...placed.map(c => c.y)) : 0;
    let width = 0, height = 0;
    for (const g of placed) {
      g.x += C.PAD; g.y += C.PAD - minY;
      width = Math.max(width, g.x + g.w); height = Math.max(height, g.y + g.h);
    }
    return { cards: grid, width: width + C.PAD, height: height + C.PAD, C };
  }

  /** 把折线的拐角改成圆角（半径不超过相邻两段的一半）。 */
  function roundedPath(pts, r) {
    const p = pts.filter((pt, i) => i === 0 || pt[0] !== pts[i - 1][0] || pt[1] !== pts[i - 1][1]);
    if (p.length < 3) return `M${p[0][0]},${p[0][1]}` + (p[1] ? ` L${p[1][0]},${p[1][1]}` : "");
    let d = `M${p[0][0]},${p[0][1]}`;
    for (let i = 1; i < p.length - 1; i++) {
      const [ax, ay] = p[i - 1], [bx, by] = p[i], [cx, cy] = p[i + 1];
      const l1 = Math.hypot(bx - ax, by - ay), l2 = Math.hypot(cx - bx, cy - by);
      const rr = Math.min(r, l1 / 2, l2 / 2);
      if (rr < 0.5) { d += ` L${bx},${by}`; continue; }
      const sx = bx - (bx - ax) / l1 * rr, sy = by - (by - ay) / l1 * rr;
      const ex = bx + (cx - bx) / l2 * rr, ey = by + (cy - by) / l2 * rr;
      d += ` L${+sx.toFixed(1)},${+sy.toFixed(1)} Q${bx},${by} ${+ex.toFixed(1)},${+ey.toFixed(1)}`;
    }
    const last = p[p.length - 1];
    return d + ` L${last[0]},${last[1]}`;
  }

  /** 有向连线：每个问题小框一条，从来源卡片指向本卡片里对应的问题小框（终点画箭头）。
      - follow：父卡片的第一个普通追问，从来源右边缘水平指过来（实线）；
      - highlight：划词提问（虚线，按来源着色）；fork：其余普通追问（实线）。
      划词线从来源卡片底边（目标在下方）或顶边（在上方）引出，先落到卡片间的空隙，
      再沿目标左侧各自的一条竖向通道走到对应的问题小框；目标与来源上下重叠时从来源右边缘引出。
      每条线带 pts（折线顶点）和 d（圆角路径）。 */
  function layoutWires(tree, layout) {
    const { cards, C } = layout;
    const wires = [];
    for (const id of tree.order) {
      const g = cards[id];
      const t = tree.turns[id];
      if (!g || !t) continue;
      const lane = {};
      const qs = questionsOf(t);
      qs.forEach((q, qi) => {
        const s = q.sourceId && cards[q.sourceId];
        if (!s) return;
        const k = lane[q.sourceId] = (lane[q.sourceId] ?? -1) + 1;  // 同一来源指向本卡片的第 k 条线，错开一点
        const qm = g.qms[Math.min(qi, g.qms.length - 1)];
        const y2 = g.y + qm;
        const quoted = !!(q.quote && q.quote.text) || t.kind === "highlight";
        const base = { id, qi, sourceId: q.sourceId, x2: g.x, y2, color: sourceColor(tree, q.sourceId) };
        // 目标左侧的竖向通道：每个问题小框一条，互不重叠
        const lx = g.x - Math.round(C.GAP / 2) - Math.round((qs.length - 1) * 5) + qi * 10;
        let pts, type;
        if (g.inline && q.sourceId === g.primary && qs.length === 1) {
          const y = s.y + Math.min(s.qms[0], qm);
          type = "follow"; pts = [[s.x + s.w, y], [g.x, y]];
          base.y2 = y;
        } else if (quoted && (g.y >= s.y + s.h || g.y + g.h <= s.y)) {
          const below = g.y >= s.y + s.h;
          const bx = s.x + Math.min(s.w * 0.3, 110) + k * 14;
          const y1 = below ? s.y + s.h : s.y;
          const ys = below ? y1 + Math.round(C.VGAP / 2) - qi * 3 : y1 - Math.round(C.VGAP / 2) + qi * 3;
          type = "highlight"; pts = [[bx, y1], [bx, ys], [lx, ys], [lx, y2], [g.x, y2]];
        } else {
          const x1 = s.x + s.w;
          const y1 = Math.max(s.y + 12, Math.min(y2, s.y + s.h - 12));
          type = quoted ? "highlight" : "fork";
          pts = y1 === y2 ? [[x1, y1], [g.x, y2]] : [[x1, y1], [lx, y1], [lx, y2], [g.x, y2]];
        }
        wires.push({ ...base, type, x1: pts[0][0], y1: pts[0][1], pts, d: roundedPath(pts, 10) });
      });
    }
    return wires;
  }

  function rectsOverlap(a, b) {
    return a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;
  }

  function cardRects(layout) {
    return Object.values(layout.cards).map(g => ({ id: g.id, x: g.x, y: g.y, w: g.w, h: g.h }));
  }

  /** 主父链（会话分叉链）：根 → … → id。 */
  function ancestors(tree, id) {
    const path = [];
    let cur = tree.turns[id];
    while (cur) { path.unshift(cur.id); cur = cur.parentId ? tree.turns[cur.parentId] : null; }
    return path;
  }

  /** 这一轮「看得到」的轮次：主父链上的每一轮，加上它们从其他分支拼接进来的来源卡片。 */
  function contextIds(tree, id) {
    const out = [];
    for (const tid of ancestors(tree, id)) {
      for (const cid of [tid, ...sourcesOf(tree.turns[tid])]) if (tree.turns[cid] && !out.includes(cid)) out.push(cid);
    }
    return out;
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
  const KIND_LABEL = { root: "起点", follow: "继续提问", highlight: "划词提问", multi: "多处提问" };

  function treeToMarkdown(tree) {
    const out = [`# ${tree.title}`, "", `> 工作目录：\`${tree.cwd}\` · 共 ${tree.order.length} 轮`, ""];
    const visit = (id, level) => {
      const t = tree.turns[id];
      if (!t) return;
      const n = turnNumber(tree, id);
      const srcs = sourcesOf(t);
      const parentN = srcs.length ? `（接 ${srcs.map(s => "#" + turnNumber(tree, s)).join("、")}）` : "";
      out.push(`${"#".repeat(Math.min(6, level))} #${n} ${KIND_LABEL[t.kind] || ""}${parentN}`, "");
      const qs = questionsOf(t);
      qs.forEach((q, i) => {
        if (q.quote && q.quote.text) out.push(`> 摘自 #${turnNumber(tree, q.sourceId)}：`, q.quote.text.split("\n").map(l => `> ${l}`).join("\n"), "");
        out.push(`**问${qs.length > 1 ? " " + (i + 1) : ""}：** ${q.text || "（就这段内容展开）"}`, "");
      });
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

  /** 静态 HTML：与应用里同样的单卡片树（问题小框 + 回答大框），三种显示模式可切换，点击卡片单独展开 / 收起。
      布局在打开时用同一套 layoutGrid / layoutTree / layoutWires 重算（直接嵌入它们的源码）。 */
  function treeToStaticHtml(tree) {
    const turns = {};
    for (const id of tree.order) {
      const t = tree.turns[id];
      const a = t.answer || {};
      turns[id] = {
        id, n: turnNumber(tree, id), parentId: t.parentId, kind: t.kind, layout: t.layout || null,
        questions: questionsOf(t).map(q => ({ text: q.text, sourceId: q.sourceId, quote: q.quote && q.quote.text ? { text: q.quote.text } : null,
          from: q.sourceId ? turnNumber(tree, q.sourceId) : 0 })),
        summary: a.summary && a.summary.length ? a.summary : null,
        preview: markdownToHtml(previewMarkdown(a.markdown, 8)),
        full: markdownToHtml(a.markdown || "") || "<p><i>（无回答）</i></p>",
        files: a.filesChanged || [],
      };
    }
    const data = JSON.stringify({ title: tree.title, rootId: tree.rootId, order: tree.order.filter(id => turns[id]), turns }).replace(/</g, "\\u003c");
    const lib = [questionsOf, sourcesOf, sourceColor, layoutGrid, layoutTree, roundedPath, layoutWires].map(fn => fn.toString()).join("\n").replace(/<\/script/gi, "<\\/script");
    const title = escapeHtml(tree.title);
    return `<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>对话树：${title}</title>
<style>
:root{--bg:#f6f5f1;--ink:#1f1f1c;--muted:#6b6a63;--card:#fbfaf6;--card-bd:#e0ddd3;--qbox:#fff;--qbox-bd:#d6d3c9;--hl:#c2622d;--hl-soft:#f7e6da;--main:#2f5d8a;--main-soft:#e3ecf5;--code:#efede6}
@media (prefers-color-scheme: dark){:root{--bg:#1b1b19;--ink:#ecebe6;--muted:#a3a198;--card:#22221f;--card-bd:#3a3935;--qbox:#2a2a27;--qbox-bd:#45443f;--hl:#e48a55;--hl-soft:#3a2a20;--main:#7fb0de;--main-soft:#1f2c3a;--code:#2c2c29}}
*{box-sizing:border-box}html,body{margin:0;height:100%;background:var(--bg);color:var(--ink);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
header{position:sticky;top:0;z-index:5;display:flex;align-items:center;gap:14px;flex-wrap:wrap;padding:10px 16px;background:var(--bg);border-bottom:1px solid var(--card-bd)}
header h1{font-size:16px;margin:0;font-weight:600}.legend{display:flex;gap:14px;font-size:12px;color:var(--muted);align-items:center}
.legend i{display:inline-block;width:24px;border-top:2px solid var(--main);vertical-align:middle;margin-right:5px}.legend i.hl{border-top:2px dashed var(--hl)}
.bar{margin-left:auto;display:flex;gap:6px;flex-wrap:wrap}.bar button{border:1px solid var(--card-bd);background:var(--qbox);color:var(--ink);border-radius:6px;padding:4px 10px;cursor:pointer;font:inherit;font-size:13px}.bar button.on{background:var(--main);border-color:var(--main);color:#fff}
#viewport{position:absolute;top:50px;left:0;right:0;bottom:0;overflow:auto}#stage{position:relative;transform-origin:0 0}#wires{position:absolute;left:0;top:0;pointer-events:none;overflow:visible}
.card{position:absolute;border:1px solid var(--card-bd);border-radius:12px;background:var(--card);padding:10px;font-size:13.5px;line-height:1.55;cursor:pointer;overflow-wrap:anywhere}.card:hover{box-shadow:0 4px 14px rgba(0,0,0,.08)}
.qbox{border:1px solid var(--qbox-bd);border-radius:9px;background:var(--qbox);padding:8px 10px;white-space:pre-wrap}.qbox+.qbox{margin-top:6px}
.tag{display:inline-block;font-size:11px;font-weight:600;padding:1px 7px;border-radius:999px;margin-bottom:5px}.tag.main{background:var(--main-soft);color:var(--main)}.tag.hl{background:var(--hl-soft);color:var(--hl)}.num{float:right;color:var(--muted);font-size:11px}
.quote{border-left:3px solid var(--hl);background:var(--hl-soft);padding:5px 8px;margin:2px 0 6px;font-size:12.5px;border-radius:0 6px 6px 0}.quote b{color:var(--hl);font-size:11px;display:block}
.ans{padding:8px 4px 2px}.ans ul{margin:2px 0;padding-left:18px}.ans p{margin:4px 0 8px}.ans pre{background:var(--code);padding:8px;border-radius:6px;overflow:auto;font-size:12px}.ans table{border-collapse:collapse;font-size:12.5px}.ans td,.ans th{border:1px solid var(--card-bd);padding:4px 7px}
.pv{max-height:260px;overflow:hidden}.files{font-size:11px;color:var(--hl);margin-top:4px}.meta{font-size:11.5px;color:var(--muted)}
</style></head><body>
<header><h1>对话树 · ${title}</h1><div class="legend"><span><i></i>继续提问</span><span><i class="hl"></i>划词提问</span><span>点击卡片展开 / 收起</span></div>
<div class="bar"><button data-mode="question">仅问题</button><button data-mode="partial">问题 + 部分回答</button><button data-mode="full">完全展开</button><span style="width:8px"></span><button id="zout">−</button><button id="zreset">100%</button><button id="zin">＋</button></div></header>
<div id="viewport"><div id="stage"><svg id="wires"></svg></div></div>
<script>
const LAYOUT=${JSON.stringify(LAYOUT)};const SOURCE_COLORS=${JSON.stringify(SOURCE_COLORS)};
${lib}
const TREE=${data};
const stage=document.getElementById("stage"),svg=document.getElementById("wires");const esc=s=>String(s).replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
let mode="partial";const own={};const els={};
function body(t,m){if(m==="question")return '<div class="meta">回答 #'+t.n+(t.summary?' · '+t.summary.length+' 条要点':'')+'</div>';if(m==="full")return t.full;return t.summary?'<ul>'+t.summary.map(s=>'<li>'+esc(s)+'</li>').join("")+'</ul>':'<div class="pv">'+t.preview+'</div>';}
function render(){TREE.order.forEach(id=>{const t=TREE.turns[id];let el=els[id];if(!el){el=els[id]=document.createElement("div");el.className="card";el.dataset.id=id;stage.appendChild(el);}
const m=own[id]||mode;el.style.width=LAYOUT.WIDTH[m]+"px";const tag=t.kind==="highlight"?'<span class="tag hl">划词提问</span>':t.kind==="multi"?'<span class="tag hl">多处提问</span>':t.kind==="follow"?'<span class="tag main">继续提问</span>':'<span class="tag main">起点</span>';
el.innerHTML=t.questions.map((q,i)=>'<div class="qbox">'+(i?'':tag+'<span class="num">#'+t.n+'</span>')+(q.quote?'<div class="quote" style="border-left-color:'+sourceColor(TREE,q.sourceId)+'"><b style="color:'+sourceColor(TREE,q.sourceId)+'">摘自 #'+q.from+'</b>'+esc(q.quote.text)+'</div>':(t.questions.length>1&&q.from?'<div class="meta">接 #'+q.from+'</div>':''))+esc(q.text||'（就这段内容展开）')+'</div>').join('')+'<div class="ans">'+body(t,m)+(t.files.length?'<div class="files">修改了 '+t.files.length+' 个文件</div>':"")+'</div>';});layout();}
function layout(){const sizes={};TREE.order.forEach(id=>{const el=els[id];sizes[id]={w:el.offsetWidth,h:el.offsetHeight,qms:[...el.querySelectorAll(".qbox")].map(q=>q.offsetTop+Math.min(q.offsetHeight/2,26))};});
const L=layoutTree(TREE,sizes);Object.values(L.cards).forEach(g=>{els[g.id].style.left=g.x+"px";els[g.id].style.top=g.y+"px";});stage.style.width=L.width+"px";stage.style.height=L.height+"px";svg.setAttribute("width",L.width);svg.setAttribute("height",L.height);
const css=getComputedStyle(document.documentElement),cM=css.getPropertyValue("--main").trim(),cH=css.getPropertyValue("--hl").trim();
svg.innerHTML='<defs>'+[cM,...SOURCE_COLORS].map((c,i)=>'<marker id="ar'+i+'" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path d="M0,0 L10,5 L0,10 z" fill="'+c+'"/></marker>').join('')+'</defs>'+layoutWires(TREE,L).map(w=>{const hl=w.type==="highlight",c=hl?w.color:cM,mi=hl?1+SOURCE_COLORS.indexOf(w.color):0;return '<path d="'+w.d+'" fill="none" stroke="'+c+'" stroke-width="1.8" opacity=".85"'+(hl?' stroke-dasharray="6 4"':'')+' marker-end="url(#ar'+mi+')"><title>#'+TREE.turns[w.sourceId].n+' → #'+TREE.turns[w.id].n+'</title></path><circle cx="'+w.x1+'" cy="'+w.y1+'" r="3" fill="'+c+'"/>';}).join("");}
document.querySelectorAll("[data-mode]").forEach(b=>b.onclick=()=>{mode=b.dataset.mode;for(const k in own)delete own[k];document.querySelectorAll("[data-mode]").forEach(x=>x.classList.toggle("on",x===b));render();});
document.querySelector('[data-mode="partial"]').classList.add("on");
stage.addEventListener("click",e=>{const c=e.target.closest(".card");if(!c||String(getSelection()).trim())return;const id=c.dataset.id;own[id]=(own[id]||mode)==="full"?(mode==="full"?"partial":mode):"full";render();});
let z=1;const applyZ=()=>{stage.style.transform="scale("+z+")";document.getElementById("zreset").textContent=Math.round(z*100)+"%";};
document.getElementById("zin").onclick=()=>{z=Math.min(1.6,z+.1);applyZ();};document.getElementById("zout").onclick=()=>{z=Math.max(.3,z-.1);applyZ();};document.getElementById("zreset").onclick=()=>{z=1;applyZ();};
window.addEventListener("resize",layout);render();window.addEventListener("load",layout);
</script></body></html>`;
  }

  /* ---------- 完整版：把划词分支反向整合回一条回答 ----------
     每个划词分支按作用分三类，各放各的位置，正文的长度和结构基本不变：
       correct 修正 → 正文里被划中的那段：零 token 模式在段后附「后来的修正」；用模型改写时直接改写这一段；
       explain 释义 → 脚注，挂在被划中的文字后面；
       extend  延伸 → 文末「延伸阅读」，正文只留一个指向它的标记；
       ignore  忽略 → 不出现。
     子树里更深的分支先合并（自底向上），父卡片用子卡片合并后的内容；脚注和延伸阅读在全文统一编号。
     mergedVersion 用来更新卡片本身（「合并子树」），buildFullVersion 再加上标题和提问行用于导出。 */
  const FV_CATEGORIES = { correct: "修正", explain: "释义", extend: "延伸", ignore: "忽略" };
  const FV_EXPLAIN_Q = /(是什么|是啥|什么意思|指什么|为什么叫|为何叫|为啥叫|名字由来|命名|怎么算|如何计算|怎么计算|定义|出处|来源|出自|哪来的|哪里来的|全称|缩写|谁做的|谁提出|哪篇|解释一下|啥意思)/;
  const FV_CORRECT_Q = /(真的吗|确定吗|对吗|是否准确|准确吗|有没有问题|有证据吗|可靠吗|靠谱吗|合理吗|是不是说错|说反了|矛盾|不对|有误|夸大|太强|过强|站得住吗|核实|查证|验证一下)/;
  const FV_CORRECT_A = /(说得偏强|说得太满|不准确|并不准确|需要更正|更正|纠正|修正一下|其实并不|并非如此|有误|不成立|争议(相当|很)?大|应改为|上一轮.{0,12}(偏|不|错)|之前.{0,8}(说法|结论).{0,8}(偏|不|错))/;

  /** 零 token 的规则分类：问题像「X 是什么 / 出处」→ 释义；问题在质疑、或回答在纠正 → 修正；其余 → 延伸。 */
  function classifyBranch(question, answerMarkdown) {
    const q = String(question || ""), a = String(answerMarkdown || "").slice(0, 1500);
    if (FV_CORRECT_Q.test(q) || FV_CORRECT_A.test(a)) return "correct";
    if (!q.trim() || FV_EXPLAIN_Q.test(q)) return "explain";
    return "extend";
  }

  /** 按空行把 Markdown 切成段落块（围栏代码块整体算一块），保留每块在原文中的偏移。 */
  function markdownBlocks(markdown) {
    const md = String(markdown || "");
    const blocks = [];
    let pos = 0, start = -1, inFence = false;
    const lines = md.split("\n");
    lines.forEach((line, i) => {
      const lineEnd = pos + line.length;
      if (/^ {0,3}(```|~~~)/.test(line)) inFence = !inFence;
      if (!line.trim() && !inFence) {
        if (start >= 0) { blocks.push({ start, end: pos - 1 }); start = -1; }
      } else if (start < 0) start = pos;
      pos = lineEnd + 1;
      if (i === lines.length - 1 && start >= 0) blocks.push({ start, end: lineEnd });
    });
    return blocks.map((b, i) => ({ index: i, start: b.start, end: b.end, text: md.slice(b.start, b.end) }));
  }

  /** 分支的要点：优先用卡片摘要，否则取回答第一段（去掉标题符号），截断。 */
  function branchGist(turn, limit = 320) {
    const a = turn.answer || {};
    let text = a.summary && a.summary.length ? a.summary.join("；") : "";
    if (!text) {
      const first = markdownBlocks(a.markdown || "").find(b => !/^ {0,3}(#|```|~~~|\|)/.test(b.text));
      text = first ? first.text.replace(/\s*\n\s*/g, " ").replace(/^[-*>\d.)\s]+/, "") : "";
    }
    return text.length > limit ? text.slice(0, limit) + "…" : text;
  }

  /** 收集直接划在 turnId 上的分支（一个问题小框一项）。 */
  function fullVersionItems(tree, turnId) {
    const t = tree.turns[turnId];
    if (!t) return [];
    const md = (t.answer && t.answer.markdown) || "";
    const items = [];
    for (const cid of tree.order) {
      const c = tree.turns[cid];
      if (!c || cid === turnId) continue;
      questionsOf(c).forEach((q, qi) => {
        if (q.sourceId !== turnId || !q.quote || !q.quote.text) return;
        const source = q.quote.source || "full";
        let anchor = null;
        if (source === "full") {
          const { start, end } = q.quote;
          if (Number.isInteger(start) && Number.isInteger(end) && end > start && end <= md.length) anchor = { start, end };
          else anchor = locateQuote(md, q.quote.text);
        }
        const done = c.answer && c.answer.status === "done";
        items.push({
          key: `${cid}:${qi}`, childId: cid, qi, n: turnNumber(tree, cid), quote: q.quote.text, source,
          question: q.text || "", anchor, done,
          category: done ? classifyBranch(q.text, c.answer.markdown) : "ignore",
        });
      });
    }
    return items;
  }

  /** 从 at 往后找到这句话的结尾（句末标点之前，或换行 / 文本结尾）：延伸标记放在这里，不把词句拦腰截断。 */
  function sentenceEnd(text, at) {
    const m = /[。！？；!?;]|\.(?=\s|$)|\n/.exec(text.slice(at));
    return m ? at + m.index : text.length;
  }
  /** 把释义标记插在被划中文字之后、延伸标记插在句末；从后往前插，偏移不受影响。 */
  function insertMarks(text, marks) {
    for (const { at, mark } of marks.slice().sort((x, y) => y.at - x.at)) text = text.slice(0, at) + mark + text.slice(at);
    return text;
  }
  const markPos = (text, it, end) => (it.category === "extend" ? sentenceEnd(text, end) : end);

  const demoteHeadings =(md, by) => String(md || "").replace(/^ {0,3}(#{1,6})(\s)/gm, (_m, h, s) => "#".repeat(Math.min(6, h.length + by)) + s);
  const oneLine = (s, n = 60) => { const t = String(s || "").replace(/\s+/g, " ").trim(); return t.length > n ? t.slice(0, n) + "…" : t; };

  /* ---------- 子树合并：自底向上，把一张卡片和隶属于它的更深部分合成一份 ----------
     先合并最深的卡片，父卡片用子卡片「合并后的内容」：孙分支的修正会先进子分支，再一层层收进来。
     各层的脚注、延伸阅读统一编号；正文里用占位符引用，最后渲染时再编号。 */
  const PH_RE = /([FS])([^]+)/g;
  const ph = (kind, id) => `${kind}${id}`;
  const stripPH = s => String(s || "").replace(PH_RE, "");

  /** 子树成员：从 rootId 上的划词分支出发，按创建顺序收进「所有来源都已在子树里」的卡片
      （分支、分支的追问、分支的分支……）。rootId 自己的普通追问是主线上的新问题，不算；
      同时挂在子树外卡片上的卡片也不算（它们仍然各自显示）。 */
  function subtreeMembers(tree, rootId) {
    const inside = new Set([rootId]);
    const members = [];
    for (const id of tree.order) {
      const t = tree.turns[id];
      if (!t || id === rootId) continue;
      const srcs = sourcesOf(t);
      if (!srcs.length || !srcs.every(s => inside.has(s))) continue;
      if (srcs.includes(rootId) && !questionsOf(t).some(q => q.sourceId === rootId && q.quote && q.quote.text)) continue;
      inside.add(id); members.push(id);
    }
    return members;
  }

  /** 合并一张卡片（递归）。返回 {body, notes, sections, questionLine, changed}，并把用到的分支、统计、日志记进 ctx。 */
  function foldTurn(tree, id, ctx, depth = 0) {
    if (ctx.memo.has(id)) return ctx.memo.get(id);
    const t = tree.turns[id];
    const md = (t.answer && t.answer.markdown) || "";
    const blocks = markdownBlocks(md);
    const items = fullVersionItems(tree, id).map(it => ({ ...it, nodeId: id, nodeN: turnNumber(tree, id), depth,
      category: ctx.decisions[it.key] || it.category }));
    ctx.items.push(...items);
    const live = items.filter(it => it.category !== "ignore");
    const notes = [], sections = [];
    // 子卡片先合并；它的脚注、延伸阅读只收进来一次（同一张多来源卡片可能被几处引用）
    const take = cid => {
      const cf = foldTurn(tree, cid, ctx, depth + 1);
      ctx.absorbed.add(cid);
      if (!ctx.claimed.has(cid)) { ctx.claimed.add(cid); return { cf, notes: cf.notes, sections: cf.sections }; }
      return { cf, notes: [], sections: [] };
    };
    const gistOf = (cid, cf) => {
      if (!cf.changed) return branchGist(tree.turns[cid]);
      const plain = stripPH(cf.body);
      const first = markdownBlocks(plain).find(b => !/^ {0,3}(#|```|~~~|\||>)/.test(b.text));
      let text = first ? first.text.replace(/\s*\n\s*/g, " ") : branchGist(tree.turns[cid]);
      text = text.length > 320 ? text.slice(0, 320) + "…" : text;
      // 这个分支自己也被更深的分支修正过：把那些修正一并带上，否则在零 token 模式下会丢掉
      const deeper = [...plain.matchAll(/^> \*\*后来的修正（(#\d+)）\*\*：[^\n]*?——([^\n]*)/gm)].map(m => `${m[1]}：${oneLine(m[2], 120)}`);
      return deeper.length ? `${text}（其后又经 ${deeper.join("；")}）` : text;
    };
    const blockOf = it => (it.anchor ? blocks.find(b => it.anchor.start >= b.start && it.anchor.start < b.end + 1) : null);
    for (const it of live) {
      const got = take(it.childId);
      it.gist = gistOf(it.childId, got.cf);
      notes.push(...got.notes);
      if (it.category === "explain") {
        notes.push({ id: it.key, text: `${it.question ? oneLine(it.question, 50) + " " : ""}${it.gist}（#${it.n}）` });
        it.mark = ph("F", it.key);
        sections.push(...got.sections);
      } else if (it.category === "extend") {
        sections.push({ id: it.key, title: oneLine(it.question || "（就这段内容展开）", 60),
          meta: `划自「${oneLine(it.quote, 50)}」 · 第 ${it.n} 轮`, body: got.cf.body, sections: got.sections });
        it.mark = ph("S", it.key);
      } else {
        sections.push(...got.sections);  // 修正：只取要点进正文，分支下面展开的内容仍放进延伸阅读
      }
      // 修正 / 释义只取了要点：分支里接着问的内容不能丢，放进延伸阅读
      if (it.category !== "extend" && got.cf.followBody && !it.followSec) {
        it.followSec = true;
        sections.push({ id: `${it.key}~f`, title: `#${it.n} 之后的追问`, meta: `接在「${oneLine(it.quote, 40)}」的分支后面`, body: got.cf.followBody, sections: [] });
      }
    }

    // 划在提问上的标记：顶层卡片插进提问行，其余放到「另见」
    let questionLine = (t.question || questionsOf(t).map(q => q.text).join(" / ")).trim();
    if (depth === 0) {
      const qMarks = [];
      for (const it of live.filter(x => x.source === "question" && x.category !== "correct")) {
        const hit = locateQuote(questionLine, it.quote);
        if (hit) { qMarks.push({ at: markPos(questionLine, it, hit.end), mark: it.mark }); it.placed = true; }
      }
      questionLine = insertMarks(questionLine, qMarks);
    }

    const out = [];
    const rewrites = ctx.rewrites[id] || {};
    for (const b of blocks) {
      const here = live.filter(it => blockOf(it) === b);
      const corrections = here.filter(it => it.category === "correct");
      const markers = here.filter(it => it.category !== "correct");
      let text;
      if (corrections.length && typeof rewrites[b.index] === "string" && rewrites[b.index].trim()) {
        text = rewrites[b.index].trim() + markers.map(it => it.mark).join("");
        corrections.forEach(it => { it.placed = true; it.rewritten = true; });
        ctx.stats.rewritten++;
        ctx.log.push(`- 第 ${turnNumber(tree, id)} 轮第 ${b.index + 1} 段：依据 ${corrections.map(it => `#${it.n}「${oneLine(it.quote, 24)}」`).join("、")}改写（模型）`);
      } else {
        text = insertMarks(b.text, markers.map(it => ({ at: markPos(b.text, it, it.anchor.end - b.start), mark: it.mark })));
        for (const it of corrections) {
          text += `\n\n> **后来的修正（#${it.n}）**：针对「${oneLine(it.quote, 40)}」——${it.gist}`;
          ctx.log.push(`- 「${oneLine(it.quote, 24)}」：附上 #${it.n} 的修正（未改写原文）`);
          it.placed = true;
        }
      }
      markers.forEach(it => { it.placed = true; });
      out.push(text);
    }
    const loose = live.filter(it => !it.placed);
    for (const it of loose.filter(x => x.category === "correct")) {
      out.push(`> **后来的修正（#${it.n}）**：针对${it.source === "summary" ? "摘要中的" : it.source === "question" ? "提问中的" : ""}「${oneLine(it.quote, 40)}」——${it.gist}`);
      ctx.log.push(`- 「${oneLine(it.quote, 24)}」：附上 #${it.n} 的修正（未定位到原文）`);
      it.placed = true;
    }
    const looseMarks = loose.filter(x => x.category !== "correct" && !x.placed);
    if (looseMarks.length) out.push(`*另见：${looseMarks.map(it => `「${oneLine(it.quote, 24)}」${it.mark}`).join("　")}*`);

    // 分支里接着问的普通追问（只算子树成员）：按顺序接在后面，它们自己的分支同样先合并
    const followers = depth > 0 || ctx.includeFollows ? tree.order.filter(fid => {
      const f = tree.turns[fid];
      if (!f || !ctx.scope.has(fid) || f.parentId !== id) return false;
      const qs = questionsOf(f);
      return qs.length === 1 && qs[0].sourceId === id && !(qs[0].quote && qs[0].quote.text);
    }) : [];
    const followParts = [];
    for (const fid of followers) {
      const got = take(fid);
      const f = tree.turns[fid];
      followParts.push(`**接着问（#${turnNumber(tree, fid)}）：** ${f.question || questionsOf(f).map(x => x.text).join(" / ")}`, got.cf.body);
      notes.push(...got.notes); sections.push(...got.sections);
    }
    out.push(...followParts);

    for (const it of live) {
      if (it.category === "correct") ctx.stats.correct++;
      else if (it.category === "explain") { ctx.stats.explain++; ctx.log.push(`- 「${oneLine(it.quote, 24)}」：#${it.n} 作为脚注`); }
      else if (it.category === "extend") { ctx.stats.extend++; ctx.log.push(`- 「${oneLine(it.quote, 24)}」：#${it.n} 移到延伸阅读`); }
    }
    ctx.stats.ignore += items.length - live.length;
    const result = { body: out.filter(Boolean).join("\n\n"), followBody: followParts.filter(Boolean).join("\n\n"), notes, sections,
      questionLine, changed: live.length > 0 || followers.length > 0 };
    ctx.memo.set(id, result);
    return result;
  }

  /** 把合并结果渲染成 Markdown：脚注按出现顺序编号，延伸阅读按层级编号（§1、§1.1……）。 */
  function renderFold(fold) {
    // 小节顺序：正文里引用到的按出现先后排在前面，其余（从下层收上来的）排在后面
    const refOrder = text => [...String(text || "").matchAll(PH_RE)].filter(m => m[1] === "S").map(m => m[2]);
    const ordered = (secs, text) => {
      const refs = refOrder(text);
      const rank = s => { const i = refs.indexOf(s.id); return i < 0 ? refs.length + secs.indexOf(s) : i; };
      return secs.slice().sort((a, b) => rank(a) - rank(b)).map(s => ({ ...s, sections: ordered(s.sections, s.body) }));
    };
    fold = { ...fold, sections: ordered(fold.sections, `${fold.questionLine}\n${fold.body}`) };
    const secNum = new Map();
    const numberSections = (secs, prefix) => secs.forEach((s, i) => {
      const label = prefix ? `${prefix}.${i + 1}` : `${i + 1}`;
      secNum.set(s.id, label); numberSections(s.sections, label);
    });
    numberSections(fold.sections, "");
    const fnNum = new Map();
    const resolve = text => String(text || "").replace(PH_RE, (_m, kind, pid) => {
      if (kind === "S") return secNum.has(pid) ? ` ⟶ §${secNum.get(pid)}` : "";
      if (!fnNum.has(pid)) fnNum.set(pid, fnNum.size + 1);
      return `[^${fnNum.get(pid)}]`;
    });
    const questionLine = resolve(fold.questionLine);  // 提问行在文档最前面，先编号
    const body = resolve(fold.body);
    const secLines = [];
    const renderSecs = (secs, level) => secs.forEach(s => {
      secLines.push(`${"#".repeat(Math.min(6, level))} §${secNum.get(s.id)} ${s.title}`, "", `> ${s.meta}`, "",
        demoteHeadings(resolve(s.body), level).trim(), "");
      renderSecs(s.sections, level + 1);
    });
    renderSecs(fold.sections, 3);
    const byId = new Map(fold.notes.map(n => [n.id, n]));
    const noteLines = [...fnNum.entries()].filter(([pid]) => byId.has(pid)).map(([pid, k]) => `[^${k}]: ${stripPH(byId.get(pid).text)}`);
    // 没有被正文引用到的脚注（例如它所在的分支被当作「修正」只取了要点）：作为补充说明保留
    const orphans = fold.notes.filter(n => !fnNum.has(n.id)).map(n => `- ${stripPH(n.text)}`);
    const parts = [body, ""];
    if (noteLines.length) parts.push(noteLines.join("\n"), "");
    if (orphans.length) parts.push("**补充说明**", "", ...orphans, "");
    if (secLines.length) parts.push("---", "", "## 延伸阅读", "", ...secLines);
    return { markdown: parts.join("\n").replace(/\n{3,}/g, "\n\n").trim(), questionLine };
  }

  function foldContext(tree, turnId, { decisions = {}, rewrites = {}, includeFollows = false } = {}) {
    return { decisions, rewrites, includeFollows, memo: new Map(), claimed: new Set(), absorbed: new Set(), items: [], log: [],
      scope: new Set(subtreeMembers(tree, turnId)), stats: { correct: 0, explain: 0, extend: 0, ignore: 0, rewritten: 0 } };
  }

  /** 合并版正文（不含标题和提问行）：用来更新卡片本身。
      返回 {markdown, items, stats, members, absorbed, log}；absorbed 是真正并进来、合并后可以折叠起来的子树成员。 */
  function mergedVersion(tree, turnId, opts = {}) {
    const ctx = foldContext(tree, turnId, opts);
    const fold = foldTurn(tree, turnId, ctx, 0);
    const { markdown, questionLine } = renderFold(fold);
    const members = [...ctx.scope];
    const stats = { ...ctx.stats, total: ctx.items.length, branches: ctx.items.length };
    let full = markdown;
    if (ctx.log.length) full += `\n\n---\n\n## 修订记录\n\n${ctx.log.join("\n")}`;
    return { markdown: full + "\n", body: markdown, questionLine, items: ctx.items, stats, members,
      absorbed: members.filter(m => ctx.absorbed.has(m)), log: ctx.log };
  }

  /** 完整版导出：合并版 + 标题和提问行。 */
  function buildFullVersion(tree, turnId, opts = {}) {
    const t = tree.turns[turnId];
    const mv = mergedVersion(tree, turnId, opts);
    const live = mv.items.filter(it => it.category !== "ignore").length;
    const head = [`# ${oneLine(stripPH(mv.questionLine).replace(/\[\^\d+\]| ⟶ §[\d.]+/g, ""), 60)}（完整版）`, "",
      `> 来自对话树「${tree.title}」第 ${turnNumber(tree, turnId)} 轮 · 整合了 ${live} 个划词分支（修正 ${mv.stats.correct} · 释义 ${mv.stats.explain} · 延伸 ${mv.stats.extend}）`, "",
      `**问：** ${mv.questionLine || t.question || ""}`, ""];
    return { ...mv, markdown: [...head, mv.markdown].join("\n").replace(/\n{3,}/g, "\n\n").trim() + "\n" };
  }

  /** 需要模型改写的段落（整个子树里含「修正」分支的段落），带深度：应当从最深的一层开始改，
      改完再算上一层，这样上一层拿到的是已经吸收了下层修正的内容。
      每段：{nodeId, depth, block, paragraph, items: [{childId, qi, quote, childText}]}。 */
  function rewriteTargets(tree, turnId, opts = {}) {
    const ctx = foldContext(tree, turnId, opts);
    foldTurn(tree, turnId, ctx, 0);
    const groups = new Map();
    for (const it of ctx.items) {
      if (it.category !== "correct" || !it.anchor) continue;
      const node = tree.turns[it.nodeId];
      const b = markdownBlocks(node.answer.markdown).find(bl => it.anchor.start >= bl.start && it.anchor.start < bl.end + 1);
      if (!b) continue;
      const k = `${it.nodeId}:${b.index}`;
      if (!groups.has(k)) groups.set(k, { nodeId: it.nodeId, depth: it.depth, block: b.index, paragraph: b.text, items: [] });
      const cf = ctx.memo.get(it.childId);
      const childText = stripPH(cf && cf.changed ? cf.body : tree.turns[it.childId].answer.markdown);
      groups.get(k).items.push({ childId: it.childId, qi: it.qi, quote: it.quote, childText: childText.slice(0, 6000) });
    }
    return [...groups.values()].sort((a, b) => b.depth - a.depth);
  }

  return { LAYOUT, MODES, SOURCE_COLORS, sourceColor, roundedPath, questionsOf, sourcesOf, contextIds, layoutGrid, layoutTree, layoutWires, cardRects, rectsOverlap, ancestors, childrenOf, mainLeaf,
    turnNumber, markdownToHtml, previewMarkdown, locateQuote, treeToMarkdown, treeToStaticHtml, escapeHtml, KIND_LABEL,
    FV_CATEGORIES, classifyBranch, markdownBlocks, branchGist, fullVersionItems, buildFullVersion, rewriteTargets,
    subtreeMembers, mergedVersion, stripPH };
});
