// 对话树核心测试：node tests/test_branchboard_core.js
const assert = require("node:assert/strict");
const core = require("../static/branchboard_core.js");

// 可复现的伪随机数
function rng(seed) {
  let s = seed >>> 0;
  return () => { s = (s * 1664525 + 1013904223) >>> 0; return s / 2 ** 32; };
}

function randomTree(seed) {
  const rand = rng(seed);
  const tree = { title: "t", cwd: "/", rootId: "n0", turns: {}, order: [] };
  const branchDirs = {};
  // 与后端 create_turn 一致：主父节点 = 最近创建的来源；除第一个普通追问外，方向按 下、上 交替并固定
  const add = (id, questions) => {
    const sources = [...new Set(questions.map(q => q.sourceId).filter(Boolean))];
    const parentId = sources.length ? sources.reduce((a, b) => (tree.order.indexOf(a) > tree.order.indexOf(b) ? a : b)) : null;
    const kind = !parentId ? "root" : questions.length > 1 ? "multi" : questions[0].quote ? "highlight" : "follow";
    const turn = { id, parentId, kind, questions, question: questions.map(q => q.text).join("\n"), answer: { markdown: "" } };
    if (parentId) {
      const children = Object.values(tree.turns).filter(t => t.parentId === parentId);
      const firstFollow = kind === "follow" && !children.some(t => t.kind === "follow");
      if (!firstFollow) {
        const n = branchDirs[parentId] = (branchDirs[parentId] || 0) + 1;
        turn.layout = { dir: n % 2 === 1 ? 1 : -1 };
      }
    }
    tree.turns[id] = turn;
    tree.order.push(id);
  };
  add("n0", [{ text: "root", sourceId: null }]);
  const count = 2 + Math.floor(rand() * 40);
  const pick = () => {
    const ids = tree.order;
    return rand() < 0.5 ? ids[ids.length - 1] : ids[Math.floor(rand() * ids.length)];  // 偏向最近的节点（深层嵌套）
  };
  for (let i = 1; i < count; i++) {
    const r = rand();
    let questions;
    if (r < 0.35) questions = [{ text: "q", sourceId: pick() }];                                  // 普通追问
    else if (r < 0.75) questions = [{ text: "q", sourceId: pick(), quote: { text: "x" } }];      // 单处划词
    else {                                                                                       // 攒多个小框一次问出
      const n = 2 + Math.floor(rand() * 3);
      questions = Array.from({ length: n }, () => ({ text: rand() < 0.3 ? "" : "q", sourceId: pick(), quote: rand() < 0.8 ? { text: "x" } : undefined }));
    }
    add(`n${i}`, questions);
  }
  const sizes = {};
  // 三种显示模式的卡片宽度混在一起（单卡片可以单独展开），高度和问题框挂点随机
  const widths = Object.values(core.LAYOUT.WIDTH);
  for (const id of tree.order) {
    const nq = core.questionsOf(tree.turns[id]).length;
    let y = 10;
    const qms = Array.from({ length: nq }, () => { const m = y + 14 + Math.floor(rand() * 20); y = m + 40; return m; });
    sizes[id] = { w: widths[Math.floor(rand() * widths.length)], h: y + 60 + Math.floor(rand() * 900), qms };
  }
  return { tree, sizes };
}

let checked = 0, multiSeen = 0, pushed = 0, ideal = 0;
const { GAP, VGAP, HMARGIN } = core.LAYOUT;
const hOverlap = (a, b) => a.x < b.x + b.w + HMARGIN && b.x < a.x + a.w + HMARGIN;
for (let seed = 1; seed <= 200; seed++) {
  const { tree, sizes } = randomTree(seed);
  const layout = core.layoutTree(tree, sizes);
  const rects = core.cardRects(layout);
  for (let i = 0; i < rects.length; i++) {
    for (let j = i + 1; j < rects.length; j++) {
      const a = rects[i], b = rects[j];
      assert.ok(!core.rectsOverlap(a, b), `seed ${seed}: 卡片 ${a.id} 与 ${b.id} 重叠`);
      // 上下相邻的卡片之间至少留 VGAP 的空隙（连线从这里走）
      if (hOverlap(a, b)) assert.ok(a.y + a.h + VGAP <= b.y + 1e-6 || b.y + b.h + VGAP <= a.y + 1e-6, `seed ${seed}: ${a.id} 与 ${b.id} 挨得太近`);
    }
  }
  // 行：行首 + 一路普通追问，整行一起放；placedBefore[id] = 这一行放之前已经放好的卡片
  const rowOf = {}, placedBefore = {};
  const done = [];
  for (const row of layout.rows) {
    for (const id of row) { rowOf[id] = row; placedBefore[id] = done.slice(); }
    done.push(...row.map(id => layout.cards[id]));
  }
  assert.equal(done.length, tree.order.length, `seed ${seed}: 有卡片没进任何一行`);
  for (const id of tree.order) {
    const t = tree.turns[id];
    const g = layout.cards[id];
    assert.equal(g.w, sizes[id].w);
    const srcs = core.sourcesOf(t).map(s => layout.cards[s]);
    if (!srcs.length) continue;
    if (srcs.length > 1) multiSeen++;
    // 新卡片在所有来源卡片右侧一列（箭头一律从左往右指）
    assert.equal(g.x, Math.max(...srcs.map(s => s.x + s.w)) + GAP, `seed ${seed}: ${id} 没有放在来源卡片右侧一列`);
    const p = layout.cards[g.primary];
    // 普通追问（父卡片的第一个）不管卡片多高，都和父卡片顶边对齐
    if (g.inline) {
      assert.equal(t.kind, "follow", `seed ${seed}: 只有普通追问能和父卡片顶边对齐`);
      assert.equal(g.y, p.y, `seed ${seed}: 普通追问 ${id} 没有和前一张水平对齐`);
      continue;
    }
    if (t.kind === "follow" && !(t.layout && t.layout.dir)) assert.fail(`seed ${seed}: 第一个普通追问 ${id} 没有排进同一行`);
    // 行首就近：要么正好在理想位置，要么行里某张卡片紧贴着把它挤开的那张（中间不留多余的空）
    const row = rowOf[id].map(r => layout.cards[r]);
    const before = placedBefore[id];
    if (g.dir < 0) {
      assert.ok(g.y + g.h <= p.y - VGAP + 1e-6, `seed ${seed}: 向上分支 ${id} 不在主父卡片上方`);
      const atIdeal = Math.abs(g.y + g.h - (p.y - VGAP)) < 1e-6;
      const touching = row.some(m => before.some(c => hOverlap(c, m) && Math.abs(c.y - VGAP - (m.y + m.h)) < 1e-6));
      assert.ok(atIdeal || touching, `seed ${seed}: 向上分支 ${id} 离来源太远`);
      atIdeal ? ideal++ : pushed++;
    } else {
      const start = p.y + p.h + VGAP;
      assert.ok(g.y >= start - 1e-6, `seed ${seed}: ${id} 不在主父卡片下方`);
      const atIdeal = Math.abs(g.y - start) < 1e-6;
      const touching = row.some(m => before.some(c => hOverlap(c, m) && Math.abs(c.y + c.h + VGAP - m.y) < 1e-6));
      assert.ok(atIdeal || touching, `seed ${seed}: 分支 ${id} 离来源太远`);
      atIdeal ? ideal++ : pushed++;
    }
  }
  // 有向连线：每个问题小框一条，终点是目标卡片左边缘、对应问题框的挂点；折线只有横竖两种线段
  const wires = core.layoutWires(tree, layout);
  let expected = 0;
  for (const id of tree.order) expected += core.questionsOf(tree.turns[id]).filter(q => q.sourceId).length;
  assert.equal(wires.length, expected, `seed ${seed}: 连线数和问题小框数不一致`);
  for (const w of wires) {
    const g = layout.cards[w.id], s = layout.cards[w.sourceId];
    const q = core.questionsOf(tree.turns[w.id])[w.qi];
    assert.equal(q.sourceId, w.sourceId);
    assert.equal(w.x2, g.x);
    assert.deepEqual(w.pts[w.pts.length - 1], [g.x, w.y2]);
    for (let i = 1; i < w.pts.length; i++) {
      const [ax, ay] = w.pts[i - 1], [bx, by] = w.pts[i];
      assert.ok(ax === bx || ay === by, `seed ${seed}: 连线出现斜线段`);
    }
    if (w.type === "follow") assert.equal(w.y2, s.y + Math.min(s.qms[0], g.qms[w.qi]));
    else assert.equal(w.y2, g.y + g.qms[w.qi]);
    if (w.type === "highlight") {
      assert.ok(q.quote || tree.turns[w.id].kind === "highlight", `seed ${seed}: 没有引用的问题不该画成划词虚线`);
      assert.equal(w.color, core.sourceColor(tree, w.sourceId));
    }
    const below = g.y >= s.y + s.h, above = g.y + g.h <= s.y;
    if (w.type === "highlight" && (below || above)) {
      assert.equal(w.y1, below ? s.y + s.h : s.y);   // 从来源底边 / 顶边引出
      assert.ok(w.x1 > s.x && w.x1 < s.x + s.w);
    } else {
      assert.equal(w.x1, s.x + s.w);                  // 从来源右边缘引出
    }
  }
  // 指向同一张多问题卡片的线各走各的竖向通道
  const byTarget = {};
  for (const w of wires) if (w.pts.length >= 4) (byTarget[w.id] = byTarget[w.id] || []).push(w.pts[w.pts.length - 2][0]);
  for (const [id, lanes] of Object.entries(byTarget)) {
    assert.equal(new Set(lanes).size, lanes.length, `seed ${seed}: 卡片 ${id} 的入线共用了竖向通道`);
  }
  checked++;
}
assert.ok(multiSeen > 50, "随机树里应当有足够多的多来源卡片");
assert.ok(ideal > pushed, `大多数卡片应当正好落在理想位置（理想 ${ideal}，被挤开 ${pushed}）`);

// 圆角路径：起点、终点不变，拐角用二次贝塞尔
{
  const d = core.roundedPath([[0, 0], [0, 50], [80, 50]], 10);
  assert.equal(d, "M0,0 L0,40 Q0,50 10,50 L80,50");
  assert.equal(core.roundedPath([[0, 0], [40, 0]], 10), "M0,0 L40,0");
}

// 就近：对同一张卡片划词，新卡片紧贴在它下方（上方），而不是排到整棵树的最底下（最上面）
{
  const tree = { rootId: "r", order: ["r"], turns: { r: { id: "r", parentId: null, kind: "root" } } };
  const add = (id, parentId, kind, dir) => { tree.turns[id] = { id, parentId, kind, layout: dir ? { dir } : undefined, questions: [{ sourceId: parentId, text: id, quote: kind === "highlight" ? { text: "x" } : undefined }] }; tree.order.push(id); };
  add("f1", "r", "follow"); add("f2", "f1", "follow"); add("f3", "f2", "follow");
  for (let i = 0; i < 6; i++) add(`h${i}`, "f2", "highlight", 1);   // 在 f2 下方挂一串分支，把树撑得很高
  add("late", "r", "highlight", 1);                                  // 之后再对根卡片划词
  const sizes = Object.fromEntries(tree.order.map(id => [id, { w: 380, h: 200, qms: [30] }]));
  const L = core.layoutTree(tree, sizes);
  const r = L.cards.r, late = L.cards.late;
  assert.equal(late.y, r.y + r.h + VGAP, "对根卡片划词的新卡片应当紧贴在根卡片下方");
  assert.ok(late.y < L.cards.h5.y, "不应排到整棵树的最底下");
}

// 卡片变高（部分回答 / 完全展开）时结构不变：先对 a 划词，再普通追问 a；追问很高也仍和 a 水平对齐，划词卡片让到下面
{
  const tree = { rootId: "r", order: ["r"], turns: { r: { id: "r", parentId: null, kind: "root" } } };
  const add = (id, parentId, kind, dir) => { tree.turns[id] = { id, parentId, kind, layout: dir ? { dir } : undefined, questions: [{ sourceId: parentId, text: id, quote: kind === "highlight" ? { text: "x" } : undefined }] }; tree.order.push(id); };
  add("a", "r", "follow"); add("h", "a", "highlight", 1); add("b", "a", "follow"); add("hf", "h", "follow");
  for (const tall of [80, 900]) {
    const sizes = Object.fromEntries(tree.order.map(id => [id, { w: 380, h: id === "b" ? tall : 100, qms: [30] }]));
    const L = core.layoutTree(tree, sizes);
    const c = L.cards;
    assert.equal(c.a.y, c.r.y); assert.equal(c.b.y, c.a.y, "普通追问应当和前一张水平对齐");
    assert.equal(c.hf.y, c.h.y, "划词卡片上的普通追问也应当水平对齐");
    assert.ok(c.h.y >= c.a.y + c.a.h + VGAP, "划词卡片在来源下方");
    if (tall > 100) assert.equal(c.h.y, c.b.y + c.b.h + VGAP, "被很高的追问挤开时紧贴在它下方");
  }
}

// 同一回答多次划词：下、上、下、上
{
  const tree = { rootId: "r", turns: { r: { id: "r", parentId: null, kind: "root" } }, order: ["r"] };
  ["a", "b", "c", "d"].forEach(id => { tree.turns[id] = { id, parentId: "r", kind: "highlight" }; tree.order.push(id); });
  const { grid } = core.layoutGrid(tree);
  assert.deepEqual(["a", "b", "c", "d"].map(id => grid[id].dir), [1, -1, 1, -1]);
  const L = core.layoutTree(tree, Object.fromEntries(tree.order.map(id => [id, { w: 380, h: 100, qms: [30] }])));
  const y = id => L.cards[id].y;
  assert.ok(y("d") < y("b") && y("b") < y("r") && y("r") < y("a") && y("a") < y("c"), "下、上交替，并依次往外排");
  assert.equal(y("a"), y("r") + 100 + core.LAYOUT.VGAP);   // 第一条向下分支紧贴根卡片
  assert.equal(y("b"), y("r") - 100 - core.LAYOUT.VGAP);   // 第一条向上分支紧贴根卡片
}

// 方向持久化：已保存的 dir 优先，插入新分支不会让旧分支翻转
{
  const tree = { rootId: "r", turns: { r: { id: "r", parentId: null, kind: "root" },
    a: { id: "a", parentId: "r", kind: "highlight", layout: { dir: -1 } } }, order: ["r", "a"] };
  assert.equal(core.layoutGrid(tree).grid.a.dir, -1);
}

// 祖先路径与主线末端
{
  const tree = { rootId: "r", order: ["r", "f", "h", "f2"], turns: {
    r: { id: "r", parentId: null, kind: "root" }, f: { id: "f", parentId: "r", kind: "follow" },
    h: { id: "h", parentId: "r", kind: "highlight" }, f2: { id: "f2", parentId: "f", kind: "follow" } } };
  assert.deepEqual(core.ancestors(tree, "f2"), ["r", "f", "f2"]);
  assert.deepEqual(core.ancestors(tree, "h"), ["r", "h"]);
  assert.equal(core.mainLeaf(tree), "f2");
}

// 多来源卡片：上下文 = 主父链 + 拼接进来的其他分支来源（不含那些来源的祖先）
{
  const tree = { rootId: "r", order: ["r", "a", "b", "b2", "m", "f"], turns: {
    r: { id: "r", parentId: null, kind: "root" },
    a: { id: "a", parentId: "r", kind: "highlight", quote: { text: "x" } },
    b: { id: "b", parentId: "r", kind: "highlight", quote: { text: "y" } },
    b2: { id: "b2", parentId: "b", kind: "follow" },
    m: { id: "m", parentId: "b2", kind: "multi", questions: [
      { text: "1", sourceId: "a", quote: { text: "x" } }, { text: "2", sourceId: "b2", quote: { text: "z" } }] },
    f: { id: "f", parentId: "m", kind: "follow" } } };
  assert.deepEqual(core.sourcesOf(tree.turns.m), ["a", "b2"]);
  assert.deepEqual(core.contextIds(tree, "m"), ["r", "b", "b2", "m", "a"]);
  assert.deepEqual(core.contextIds(tree, "f"), ["r", "b", "b2", "m", "a", "f"]);
  assert.ok(!core.contextIds(tree, "b2").includes("a"));
  const md = core.treeToMarkdown({ ...tree, title: "T", cwd: "/" , turns: Object.fromEntries(Object.entries(tree.turns).map(([k, v]) => [k, { ...v, question: v.question || k, answer: { markdown: "A" + k } }])) });
  assert.match(md, /#5 多处提问（接 #2、#4）/);
  assert.match(md, /> 摘自 #2：\n> x\n\n\*\*问 1：\*\* 1/);
}

// 完整版：分支按 修正 / 释义 / 延伸 各放各的位置
{
  const md = "柏拉图表征假说认为模型表征会趋同。\n\n语言模型里有视觉知识，规模越大越强。\n\n```py\nx = 1\n\ny = 2\n```\n\n结论：融合是方向。";
  const done = (markdown, summary) => ({ status: "done", markdown, summary });
  const tree = { title: "T", rootId: "r", order: ["r", "a", "b", "c", "d", "e", "m"], turns: {
    r: { id: "r", parentId: null, kind: "root", question: "介绍柏拉图表征假说", answer: done(md) },
    a: { id: "a", parentId: "r", kind: "highlight", questions: [{ sourceId: "r", text: "这个说法准确吗？", quote: { text: "语言模型里有视觉知识", source: "full" } }],
      answer: done("上一轮说得偏强了：语言模型只学到粗粒度的关系结构。", ["只学到粗粒度关系结构", "细粒度不收敛"]) },
    b: { id: "b", parentId: "r", kind: "highlight", questions: [{ sourceId: "r", text: "柏拉图表征假说是什么？", quote: { text: "柏拉图表征假说", source: "full" } }],
      answer: done("MIT Isola 组 2024 年提出的假说。\n\n## 细节\n更多内容。") },
    c: { id: "c", parentId: "r", kind: "highlight", questions: [{ sourceId: "r", text: "融合有哪些具体做法", quote: { text: "融合是方向", source: "full" } }],
      answer: done("## 做法\nTransfusion、Show-o。") },
    d: { id: "d", parentId: "c", kind: "highlight", questions: [{ sourceId: "c", text: "Show-o 是谁做的", quote: { text: "Show-o", source: "full" } }],
      answer: done("NUS Show Lab。") },
    e: { id: "e", parentId: "r", kind: "highlight", questions: [{ sourceId: "r", text: "这个提问本身合理吗", quote: { text: "介绍", source: "question" } }],
      answer: done("合理。") },
    m: { id: "m", parentId: "r", kind: "follow", questions: [{ sourceId: "r", text: "普通追问不算进完整版" }], answer: done("略") },
  } };
  assert.equal(core.classifyBranch("这个说法准确吗？", ""), "correct");
  assert.equal(core.classifyBranch("X 是什么？", ""), "explain");
  assert.equal(core.classifyBranch("展开讲讲应用", "其实并不成立……上一轮说错了"), "correct");
  assert.equal(core.classifyBranch("展开讲讲应用", "应用很多"), "extend");

  const items = core.fullVersionItems(tree, "r");
  assert.deepEqual(items.map(it => [it.childId, it.category]), [["a", "correct"], ["b", "explain"], ["c", "extend"], ["e", "correct"]]);
  assert.ok(items.find(it => it.childId === "a").anchor, "划在全文上的引用应能定位");
  assert.equal(items.find(it => it.childId === "e").anchor, null, "划在提问上的引用不在回答全文里定位");

  const { markdown, stats } = core.buildFullVersion(tree, "r", { decisions: { "e:0": "explain" } });
  // 释义：脚注挂在被划中的文字后面
  assert.match(markdown, /柏拉图表征假说\[\^2\]认为/);
  assert.match(markdown, /\[\^2\]: 柏拉图表征假说是什么？ MIT Isola 组 2024 年提出的假说。（#3）/);
  // 划在提问上的释义：标记插在提问行里（提问行在最前面，脚注编号最小）
  assert.match(markdown, /\*\*问：\*\* 介绍\[\^1\]柏拉图表征假说/);
  // 修正（零 token）：原文不动，段后附「后来的修正」，用分支摘要
  assert.match(markdown, /语言模型里有视觉知识，规模越大越强。\n\n> \*\*后来的修正（#2）\*\*：针对「语言模型里有视觉知识」——只学到粗粒度关系结构；细粒度不收敛/);
  // 延伸：正文留标记，全文进延伸阅读（标题降级）；它自己的划词分支先合并进去（这里是释义 → 脚注，全文统一编号）
  assert.match(markdown, /融合是方向 ⟶ §1。/);
  assert.match(markdown, /## 延伸阅读\n\n### §1 融合有哪些具体做法\n\n> 划自「融合是方向」 · 第 4 轮\n\n##### 做法\nTransfusion、Show-o\[\^3\]。/);
  assert.match(markdown, /\[\^3\]: Show-o 是谁做的 NUS Show Lab。（#5）/);
  assert.ok(!markdown.includes("§1.1"));
  // 代码块原样保留；普通追问不出现
  assert.ok(markdown.includes("```py\nx = 1\n\ny = 2\n```"));
  assert.ok(!markdown.includes("普通追问不算进完整版"));
  assert.deepEqual([stats.correct, stats.explain, stats.extend, stats.ignore], [1, 3, 1, 0]);  // 统计覆盖整棵子树
  assert.match(markdown, /## 修订记录\n\n[\s\S]*- 「语言模型里有视觉知识」：附上 #2 的修正（未改写原文）/);

  // 延伸标记放在句末，不把词句拦腰截断；释义脚注紧跟被划中的文字
  {
    const t2 = JSON.parse(JSON.stringify(tree));
    t2.turns.c.questions[0].quote = { text: "融合", source: "full" };
    t2.turns.e.questions[0].quote = { text: "柏拉图", source: "question" };
    const out = core.buildFullVersion(t2, "r", { decisions: { "e:0": "extend" } }).markdown;
    // 小节按阅读顺序编号：提问行在最前面
    assert.match(out, /\*\*问：\*\* 介绍柏拉图表征假说 ⟶ §1\n/);
    assert.match(out, /结论：融合是方向 ⟶ §2。/);
    assert.equal(core.classifyBranch("为什么叫柏拉图？", ""), "explain");
  }

  // 用模型改写：只替换含修正的那一段；该段的其他标记挂在段末
  const targets = core.rewriteTargets(tree, "r");
  assert.deepEqual(targets.map(g => [g.nodeId, g.block, g.paragraph, g.items.map(i => i.childId)]), [["r", 1, "语言模型里有视觉知识，规模越大越强。", ["a"]]]);
  const re = core.buildFullVersion(tree, "r", { rewrites: { r: { 1: "语言模型只学到了视觉世界粗粒度的关系结构。" } } });
  assert.ok(re.markdown.includes("语言模型只学到了视觉世界粗粒度的关系结构。"));
  assert.ok(!re.markdown.includes("后来的修正（#2）"));
  assert.ok(!re.markdown.includes("规模越大越强"));
  assert.equal(re.stats.rewritten, 1);
  assert.match(re.markdown, /第 1 轮第 2 段：依据 #2「语言模型里有视觉知识」改写（模型）/);

  // 忽略：不出现；全部忽略时没有延伸阅读和修订记录
  const none = core.buildFullVersion(tree, "r", { decisions: { "a:0": "ignore", "b:0": "ignore", "c:0": "ignore", "e:0": "ignore" } });
  assert.ok(!/后来的修正|\[\^1\]|延伸阅读|修订记录/.test(none.markdown));
  assert.ok(none.markdown.includes("语言模型里有视觉知识，规模越大越强。"));
}

// 子树合并：自底向上，孙分支的修正先进子分支，再随子分支一起进根卡片
{
  const done = (markdown, summary) => ({ status: "done", markdown, summary });
  const q = (sourceId, text, quote) => [{ sourceId, text, quote: quote ? { text: quote, source: "full" } : undefined }];
  const tree = { title: "T", rootId: "r", order: ["r", "a", "g", "af", "afh", "y", "x", "m", "ext"], turns: {
    r: { id: "r", parentId: null, kind: "root", question: "根问题", answer: done("视觉知识随规模增强。\n\n第二段：融合是方向。") },
    a: { id: "a", parentId: "r", kind: "highlight", questions: q("r", "准确吗", "视觉知识随规模增强"), answer: done("只学到粗粒度结构，细节不收敛。\n\n类别级邻域一致。") },
    g: { id: "g", parentId: "a", kind: "highlight", questions: q("a", "粗粒度结构可靠吗", "粗粒度结构"), answer: done("其实并不可靠：大样本下对齐只有 0.8%。") },
    af: { id: "af", parentId: "a", kind: "follow", questions: q("a", "那细节怎么学"), answer: done("需要视觉数据训练。") },
    afh: { id: "afh", parentId: "af", kind: "highlight", questions: q("af", "视觉数据是什么", "视觉数据"), answer: done("图像和视频。") },
    y: { id: "y", parentId: "r", kind: "follow", questions: q("r", "主线上的下一个问题"), answer: done("主线回答") },
    x: { id: "x", parentId: "y", kind: "multi", questions: [{ sourceId: "a", text: "跨子树", quote: { text: "类别级邻域", source: "full" } }, { sourceId: "y", text: "外部", quote: { text: "主线回答", source: "full" } }], answer: done("跨子树的回答") },
    m: { id: "m", parentId: "r", kind: "follow", questions: q("r", "第二个普通追问"), answer: done("略") },
    ext: { id: "ext", parentId: "r", kind: "highlight", questions: q("r", "融合怎么做", "融合是方向"), answer: done("Transfusion 等。") },
  } };
  // 范围：分支、分支的分支、分支里的追问；根卡片的普通追问（主线）和同时挂在子树外的卡片都不算
  assert.deepEqual(core.subtreeMembers(tree, "r"), ["a", "g", "af", "afh", "ext"]);
  // 只合并 a 时，a 仍然显示，它自己的普通追问 af 是从 a 出发的新问题，不算 a 的回答；
  // 从根合并时 a 整个被收起，a 后面接着问的 af、afh 才跟着并进来
  assert.deepEqual(core.subtreeMembers(tree, "a"), ["g"]);

  const mv = core.mergedVersion(tree, "r");
  // 孙分支 g 的修正随 a 一起进来（零 token 模式下带在要点后面）
  assert.match(mv.markdown, /> \*\*后来的修正（#2）\*\*：针对「视觉知识随规模增强」——只学到粗粒度结构，细节不收敛。（其后又经 #3：其实并不可靠：大样本下对齐只有 0\.8%。）/);
  // a 被当作修正只取了要点：它后面接着问的内容进延伸阅读，并且那里的划词（释义）也合并成脚注
  assert.match(mv.markdown, /### §\d #2 之后的追问\n\n> 接在「视觉知识随规模增强」的分支后面\n\n\*\*接着问（#4）：\*\* 那细节怎么学\n\n需要视觉数据\[\^\d\]训练。/);
  assert.match(mv.markdown, /\[\^\d\]: 视觉数据是什么 图像和视频。（#5）/);
  // 跨子树的多来源卡片 x：内容作为 a 的分支参与合并，但不算子树成员（合并后仍单独显示）
  assert.deepEqual(mv.absorbed.sort(), ["a", "af", "afh", "ext", "g"]);
  assert.ok(!mv.members.includes("x"));
  assert.ok(!mv.markdown.includes("主线回答"), "主线上的普通追问不进合并版");
  assert.ok(!mv.markdown.startsWith("#"), "合并版不带标题，用来替换卡片正文");
  assert.match(mv.markdown, /融合是方向 ⟶ §1。/);

  // 模型改写：从最深一层开始；上一层收到的是下层合并后的内容
  const targets = core.rewriteTargets(tree, "r");
  assert.deepEqual(targets.map(g => [g.nodeId, g.depth]), [["a", 1], ["r", 0]]);
  assert.ok(targets[1].items[0].childText.includes("后来的修正（#3）"));
  const again = core.rewriteTargets(tree, "r", { rewrites: { a: { 0: "其实连粗粒度结构也不稳定。" } } });
  assert.ok(again.find(g => g.nodeId === "r").items[0].childText.startsWith("其实连粗粒度结构也不稳定。"));
  const both = core.mergedVersion(tree, "r", { rewrites: { a: { 0: "其实连粗粒度结构也不稳定。" }, r: { 0: "视觉知识只在粗粒度上随规模增强。" } } });
  assert.ok(both.markdown.startsWith("视觉知识只在粗粒度上随规模增强。"));
  assert.equal(both.stats.rewritten, 2, "两层各改写了一段（a 的改写喂给了根卡片）");
  assert.ok(!both.markdown.includes("后来的修正（#2）"), "根卡片那段已被改写，不再附注");

  // 只合并某个中间节点（深度 1 的 a）：它的子树只有 g；跨子树的 x 参与内容但不收起
  const sub = core.mergedVersion(tree, "a");
  assert.match(sub.markdown, /^只学到粗粒度结构/);
  assert.match(sub.markdown, /> \*\*后来的修正（#3）\*\*/);
  assert.ok(!sub.markdown.includes("接着问（#4）"));
  assert.deepEqual(sub.absorbed, ["g"]);
}

// Markdown 渲染与转义
{
  const html = core.markdownToHtml("# 标题\n\n- **粗** `x<y>`\n  - 子项\n1. 一\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n```js\n<script>\n```\n\n<img onerror=1>");
  assert.match(html, /<h3>标题<\/h3>/);
  assert.match(html, /<strong>粗<\/strong> <code>x&lt;y&gt;<\/code>/);
  assert.match(html, /<ul><li>.*<ul><li>子项<\/li><\/ul><\/li><\/ul>/);
  assert.match(html, /<table>/);
  assert.match(html, /&lt;script&gt;/);
  assert.doesNotMatch(html, /<img/);
  assert.doesNotMatch(core.markdownToHtml("[x](javascript:alert(1))"), /javascript:/);
}

// 划词定位：渲染后的文字在 markdown 原文中的偏移
{
  const md = "第一段。\n\n这里有 **加粗的结论** 和后文。";
  assert.deepEqual(core.locateQuote(md, "第一段"), { start: 0, end: 3 });
  const hit = core.locateQuote(md, "加粗的结论 和后文");
  assert.ok(hit && md.slice(hit.start, hit.end).includes("加粗的结论"));
  assert.equal(core.locateQuote(md, "不存在"), null);
}

// 导出 Markdown：DFS 顺序，分支加深标题层级
{
  const tree = { title: "T", cwd: "/w", rootId: "r", order: ["r", "f", "h"], turns: {
    r: { id: "r", parentId: null, kind: "root", question: "Q1", answer: { markdown: "A1" } },
    f: { id: "f", parentId: "r", kind: "follow", question: "Q2", answer: { markdown: "A2" } },
    h: { id: "h", parentId: "r", kind: "highlight", question: "Q3", quote: { text: "A1" }, answer: { markdown: "A3" } } } };
  const md = core.treeToMarkdown(tree);
  assert.ok(md.indexOf("## #1 起点") < md.indexOf("## #2 继续提问"));
  assert.ok(md.indexOf("## #2") < md.indexOf("### #3 划词提问"));
  assert.match(md, /> A1\n\n\*\*问：\*\* Q3/);
  const html = core.treeToStaticHtml(tree);
  assert.match(html, /const TREE=/);
  assert.match(html, /function layoutTree\(/);  // 与应用共用同一套布局函数
  assert.match(html, /data-mode="question"[\s\S]*data-mode="partial"[\s\S]*data-mode="full"/);
  assert.doesNotMatch(html, /<\/script><script/);
}

console.log(`branchboard core: ${checked} 棵随机树（含 ${multiSeen} 张多来源卡片；${ideal} 张落在理想位置，${pushed} 张被就近挤开）布局不变式通过，其余用例通过`);
