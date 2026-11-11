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
  const add = (id, parentId, kind) => {
    const turn = { id, parentId, kind, question: id, answer: { markdown: "" } };
    if (kind === "highlight") {
      // 与后端一致：方向在创建时按已有划词数交替并固定
      const n = Object.values(tree.turns).filter(t => t.parentId === parentId && t.kind === "highlight").length;
      turn.layout = { dir: n % 2 === 0 ? 1 : -1 };
    } else if (kind === "follow" && rand() < 0.3) {
      turn.layout = { dir: rand() < 0.5 ? 1 : -1 };
    }
    tree.turns[id] = turn;
    tree.order.push(id);
  };
  add("n0", null, "root");
  const count = 2 + Math.floor(rand() * 40);
  for (let i = 1; i < count; i++) {
    const ids = tree.order;
    // 偏向最近的节点（深层嵌套），也经常对同一回答多次划词
    const parent = rand() < 0.5 ? ids[ids.length - 1] : ids[Math.floor(rand() * ids.length)];
    add(`n${i}`, parent, rand() < 0.55 ? "highlight" : "follow");
  }
  const sizes = {};
  for (const id of tree.order) sizes[id] = { qh: 60 + Math.floor(rand() * 300), ah: 60 + Math.floor(rand() * 500) };
  return { tree, sizes };
}

let checked = 0;
for (let seed = 1; seed <= 200; seed++) {
  const { tree, sizes } = randomTree(seed);
  const layout = core.layoutTree(tree, sizes);
  const rects = core.cardRects(layout);
  for (let i = 0; i < rects.length; i++) {
    for (let j = i + 1; j < rects.length; j++) {
      assert.ok(!core.rectsOverlap(rects[i], rects[j]),
        `seed ${seed}: ${rects[i].id}.${rects[i].side} 与 ${rects[j].id}.${rects[j].side} 重叠`);
    }
  }
  for (const id of tree.order) {
    const t = tree.turns[id];
    const g = layout.cards[id];
    if (t.kind === "highlight") {
      const p = layout.cards[t.parentId];
      assert.equal(g.qx, p.ax, `seed ${seed}: 划词问题 ${id} 没有对齐被划词的回答`);
      if (g.dir > 0) assert.ok(g.y >= p.y + p.ah, `seed ${seed}: 向下分支 ${id} 不在父回答下方`);
      else assert.ok(g.y + g.qh <= p.y, `seed ${seed}: 向上分支 ${id} 不在父回答上方`);
    }
    if (t.kind === "follow") {
      const p = layout.cards[t.parentId];
      assert.ok(g.qx > p.ax, `seed ${seed}: 继续提问 ${id} 没有在父回答右侧`);
    }
    assert.equal(g.ax, g.qx + core.LAYOUT.QW + core.LAYOUT.QA_GAP);
  }
  checked++;
}

// 同一回答多次划词：下、上、下、上
{
  const tree = { rootId: "r", turns: { r: { id: "r", parentId: null, kind: "root" } }, order: ["r"] };
  ["a", "b", "c", "d"].forEach(id => { tree.turns[id] = { id, parentId: "r", kind: "highlight" }; tree.order.push(id); });
  const { grid } = core.layoutGrid(tree);
  assert.deepEqual(["a", "b", "c", "d"].map(id => grid[id].dir), [1, -1, 1, -1]);
  assert.deepEqual(["a", "b", "c", "d"].map(id => grid[id].row), [1, -1, 2, -2]);
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
  assert.match(html, /const TURNS=/);
  assert.doesNotMatch(html, /<\/script><script/);
}

console.log(`branchboard core: ${checked} 棵随机树布局不变式通过，其余用例通过`);
