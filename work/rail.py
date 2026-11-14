#!/usr/bin/env python3
"""rail - 在 Markdown 每段 / 每个列表项开头画「逻辑线」（规则六原型）。

用法:
  python rail.py input.md --granularity G2 --single keep -o out/G2-keep.html
  python rail.py input.md --all -o out/
"""
from __future__ import annotations

import argparse
import html as _html
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from markdown_it import MarkdownIt

# ───────────────────────── 绘制常量（见 handoff 1.2） ─────────────────────────
SEG, H, MID, GAP = 16, 22, 11, 4
MAX_FORK = 5
LINE_DARK, LINE_LIGHT = 1.6, 1.0

VERSIONS = [  # (key, granularity, single)
    ("G0", "G0", None),
    ("G1-keep", "G1", "keep"), ("G1-omit", "G1", "omit"),
    ("G2-keep", "G2", "keep"), ("G2-omit", "G2", "omit"),
    ("G3-keep", "G3", "keep"), ("G3-omit", "G3", "omit"),
    ("G3-compact", "G3", "keep"),
]
COMPACT = {"G3-compact"}
VERSION_DESC = {
    "G0": "对照组：不画线，正文原样排版。",
    "G1": "线只到标题层级；同一小节内各段的线完全一样。",
    "G2": "线到标题 + 段；最后一节表示「本小节第几段」。列表整体算一段，线画在列表第一项。",
    "G3": "线到标题 + 段 + 列表项；列表每嵌套一层，线多一节。",
    "G3c": "G3 压缩版：与上一行共同的前缀不再重复画，只画从分歧处开始的部分，并从前一行线末尾对齐处起笔。开启「楼层线」后，被隐藏的列表层级用竖线表示。",
}
CENTERS = ("middle", "parent")


# ───────────────────────── 数据结构 ─────────────────────────
@dataclass
class PN:
    """路径节点：这一层共 n 个兄弟，本段是第 i 个。"""
    label: str       # '#', '##', '段', '项'
    i: int
    n: int
    where: str       # 人类可读的位置描述，用于报告
    title: str = ""   # 标题 / 段首 / 项文字摘录，悬停预览用


@dataclass
class Block:
    kind: str                      # para | list | other
    html: str = ""
    text: str = ""
    otype: str = ""                # other 的类型：table / code / quote / hr / html
    ordered: bool = False
    start: int = 1
    items: list = field(default_factory=list)


@dataclass
class Item:
    blocks: list = field(default_factory=list)


@dataclass
class Section:
    level: int
    html: str = ""
    text: str = ""
    blocks: list = field(default_factory=list)
    children: list = field(default_factory=list)
    virtual: bool = False


@dataclass
class Row:
    kind: str                      # heading | para | item | cont
    html: str = ""
    text: str = ""
    level: int = 0                 # heading 级别
    depth: int = 0                 # 列表缩进深度
    marker: str = ""
    otype: str = ""
    g: dict = field(default_factory=lambda: {"G1": None, "G2": None, "G3": None})
    anc: list = None               # 标题行：它的上级路径（只用于楼层线）


# ───────────────────────── 解析 ─────────────────────────
def make_md() -> MarkdownIt:
    return MarkdownIt("commonmark", {"html": True}).enable("table")


def close_index(tokens, i):
    depth = 0
    for j in range(i, len(tokens)):
        depth += tokens[j].nesting
        if depth == 0:
            return j
    return len(tokens) - 1


def plain(inline) -> str:
    out = []
    for c in inline.children or []:
        if c.type in ("text", "code_inline"):
            out.append(c.content)
        elif c.type in ("softbreak", "hardbreak"):
            out.append(" ")
    return re.sub(r"\s+", " ", "".join(out)).strip()


def parse_blocks(md, tokens, lo, hi):
    out = []
    i = lo
    while i < hi:
        t = tokens[i]
        if t.type == "heading_open":
            j = close_index(tokens, i)
            inl = tokens[i + 1]
            out.append(("heading", int(t.tag[1:]), md.renderer.renderInline(inl.children, md.options, {}), plain(inl)))
        elif t.type == "paragraph_open":
            j = close_index(tokens, i)
            inl = tokens[i + 1]
            out.append(Block("para", html=md.renderer.renderInline(inl.children, md.options, {}), text=plain(inl)))
        elif t.type in ("bullet_list_open", "ordered_list_open"):
            j = close_index(tokens, i)
            b = Block("list", ordered=t.type == "ordered_list_open", start=int(t.attrGet("start") or 1))
            k = i + 1
            while k < j:
                kc = close_index(tokens, k)
                b.items.append(Item(parse_blocks(md, tokens, k + 1, kc)))
                k = kc + 1
            out.append(b)
        else:
            j = close_index(tokens, i) if t.nesting == 1 else i
            otype = {"table_open": "table", "fence": "code", "code_block": "code",
                     "blockquote_open": "quote", "hr": "hr", "html_block": "html"}.get(t.type, t.type)
            out.append(Block("other", html=md.renderer.render(tokens[i:j + 1], md.options, {}), otype=otype))
        i = j + 1
    return out


def iter_sections(sec):
    yield sec
    for c in sec.children:
        yield from iter_sections(c)


def build_sections(md, text, stats):
    tokens = md.parse(text)
    nodes = parse_blocks(md, tokens, 0, len(tokens))
    virtual = Section(0, virtual=True)
    stack = [virtual]
    for nd in nodes:
        if isinstance(nd, tuple):
            _, level, h, txt = nd
            while stack[-1].level >= level:
                stack.pop()
            s = Section(level, h, txt)
            stack[-1].children.append(s)
            stack.append(s)
        else:
            stack[-1].blocks.append(nd)
    n1 = sum(1 for s in iter_sections(virtual) if s.level == 1)
    stats["h1_count"] = n1
    # 根节点：恰有一个 # 且它是唯一的顶层节点时取该 #，否则取整篇文档
    if len(virtual.children) == 1 and virtual.children[0].level == 1:
        root = virtual.children[0]
        root.blocks = virtual.blocks + root.blocks
        stats["root"] = "single-h1"
    else:
        root = virtual
        stats["root"] = "multi-h1" if n1 > 1 else ("no-h1" if n1 == 0 else "mixed")
    return root


# ───────────────────────── 展开为「行」并计算路径 ─────────────────────────
BULLETS = ["●", "○", "■"]


def excerpt(s, n=24):
    s = s.strip()
    return s if len(s) <= n else s[:n] + "…"


def block_title(b):
    if b.kind == "para":
        return excerpt(b.text, 30)
    for it in b.items:
        if it.blocks and it.blocks[0].kind == "para":
            return "列表：" + excerpt(it.blocks[0].text, 26)
    return "列表"


def build_rows(root: Section, stats):
    rows, tree = [], []

    def tl(indent, text):
        tree.append("  " * indent + text)

    def emit_items(items, prefix, depth, ordered, start, where_parent, indent, g12):
        kept = []
        for it in items:
            if not it.blocks:
                stats["empty_items"].append(where_parent)
            else:
                kept.append(it)
        n = len(kept)
        for k, it in enumerate(kept, 1):
            first = it.blocks[0] if it.blocks[0].kind == "para" else None
            node = PN("项", k, n, where_parent, excerpt(first.text, 30) if first else "")
            path = prefix + [node]
            marker = f"{start + k - 1}." if ordered else BULLETS[depth % 3]
            r = Row("item", html=first.html if first else "", text=first.text if first else "",
                    depth=depth, marker=marker)
            r.g["G3"] = path
            if g12 is not None and k == 1:        # 列表第一项：G1 / G2 的线画在这里
                r.g["G1"], r.g["G2"] = g12
            rows.append(r)
            tl(indent, f"[{node.label} {k}/{n}] {excerpt(r.text) or '(无文字)'}")
            for b in it.blocks[1 if first else 0:]:
                if b.kind == "para":
                    rows.append(Row("cont", html=b.html, text=b.text, depth=depth + 1))
                    tl(indent + 1, f"(同一项内的后续段，无线) {excerpt(b.text)}")
                elif b.kind == "list":
                    emit_items(b.items, path, depth + 1, b.ordered, b.start,
                               f"「{excerpt(r.text)}」下的子项", indent + 1, None)
                else:
                    rows.append(Row("cont", html=b.html, depth=depth + 1, otype=b.otype))
                    tl(indent + 1, f"({b.otype}，不画线，跟随上一项)")
                    stats["others"][b.otype] += 1

    def walk(sec: Section, base, selfnode, indent):
        title = sec.text if not sec.virtual else "文档根"
        if not sec.virtual:
            hr = Row("heading", html=sec.html, text=sec.text, level=sec.level)
            hr.anc = base[:-1]
            rows.append(hr)
            lab = f"  [{selfnode.label} {selfnode.i}/{selfnode.n}]" if selfnode else "  [根]"
            tl(indent, f"{'#' * sec.level} {sec.text}{lab}")
        else:
            tl(indent, "（根 = 整篇文档）")
        indent += 1
        counted = [b for b in sec.blocks if b.kind in ("para", "list")]
        nb, bi = len(counted), 0
        seq = [b.kind for b in sec.blocks]
        for q in range(1, len(seq) - 1):
            if seq[q] == "other" and seq[q - 1] == "list" and seq[q + 1] == "list":
                stats["split_lists"].append((title, sec.blocks[q].otype))
        if nb == 0 and not sec.children and not sec.virtual:
            stats["empty_sections"].append(sec.text)
            tl(indent, "(本节没有任何内容)")
        for b in sec.blocks:
            if b.kind == "other":
                rows.append(Row("cont", html=b.html, otype=b.otype))
                tl(indent, f"({b.otype}，不画线，跟随上一段)")
                stats["others"][b.otype] += 1
                if bi == 0:
                    stats["orphan_others"].append((title, b.otype))
                continue
            bi += 1
            bnode = PN("段", bi, nb, f"「{title}」下的段", block_title(b))
            if b.kind == "para":
                r = Row("para", html=b.html, text=b.text)
                r.g["G1"] = list(base)
                r.g["G2"] = base + [bnode]
                r.g["G3"] = base + [bnode]
                rows.append(r)
                tl(indent, f"[段 {bi}/{nb}] {excerpt(r.text)}")
            else:
                tl(indent, f"[段 {bi}/{nb}] 列表（{'有序' if b.ordered else '无序'}）")
                emit_items(b.items, base + [bnode], 0, b.ordered, b.start,
                           f"「{title}」第 {bi} 段（列表）的项", indent + 1,
                           (list(base), base + [bnode]))
        nk = len(sec.children)
        for k, ch in enumerate(sec.children, 1):
            node = PN("#" * ch.level, k, nk, f"「{title}」下的 {'#' * ch.level}", ch.text)
            walk(ch, base + [node], node, indent)

    walk(root, [], None, 0)
    stats["intro_blocks"] = sum(1 for b in root.blocks if b.kind in ("para", "list"))
    return rows, tree


# ───────────────────────── 版本逻辑：keep / omit、坐标文字 ─────────────────────────
def apply_mode(path, mode):
    if path is None:
        return None
    if mode == "omit" and path and path[-1].n == 1:
        return path[:-1]
    return path


def coord_text(path):
    if not path:
        return "—"
    return " · ".join(f"{p.i}/{p.n}" + ("⚠" if p.n > MAX_FORK else "") for p in path)


def title_text(path):
    if not path:
        return "（根：整篇文档，无分叉）"
    parts = []
    for p in path:
        if p.label == "段":
            parts.append(f"第 {p.i} 段（共 {p.n} 段）")
        elif p.label == "项":
            parts.append(f"第 {p.i} 项（共 {p.n} 项）")
        else:
            parts.append(f"第 {p.i} 个 {p.label}（共 {p.n} 个）")
    s = coord_text(path) + "   = " + " · ".join(parts)
    if any(p.n > MAX_FORK for p in path):
        s += "   ⚠ 兄弟数超过 5，未画分叉"
    return s


def rail_for(row, gran, mode):
    if gran == "G0":
        return None
    path = row.g.get(gran)
    if path is None:
        return None
    return apply_mode(path, mode)


# ───────────────────────── SVG ─────────────────────────
def _ln(x1, y1, x2, y2, cls):
    return f'<line x1="{x1:g}" y1="{y1:g}" x2="{x2:g}" y2="{y2:g}" class="{cls}"/>'


def common_prefix_len(a, b):
    n = 0
    for x, y in zip(a, b):
        if (x.label, x.i, x.n) != (y.label, y.i, y.n):
            break
        n += 1
    return n


def rail_svg(path, center="middle", label="", hide=0):
    """hide: 前 hide 节不画（与上一行共同的前缀），位置仍按完整路径计算，所以起笔处与前一行线末尾对齐。
    被隐藏的「项」层级会画一条楼层线（class=th，默认由 CSS 隐藏，可开关）。"""
    w = SEG * (len(path) + 1) + 6
    parts = [] if hide else [_ln(0, MID, SEG, MID, "d")]
    threads = []
    py, x = float(MID), float(SEG)
    warn = False
    for idx, nd in enumerate(path):
        n = nd.n
        draw = idx >= hide
        if n > MAX_FORK:
            warn = True
            cy = py
            fork = None
        elif n <= 1:
            cy = py
            fork = None
        else:
            span = (n - 1) * GAP
            c = MID if center == "middle" else min(max(py, 2 + span / 2), H - 2 - span / 2)
            ys = [c + (k - (n - 1) / 2) * GAP for k in range(n)]
            cy = ys[nd.i - 1]
            fork = ys
        if draw:
            if fork:
                parts.append(_ln(x, min(fork + [py]), x, max(fork + [py]), "l"))
                for k, y in enumerate(fork):
                    if k != nd.i - 1:
                        parts.append(_ln(x, y, x + SEG * 0.7, y, "l"))
                if cy != py:
                    parts.append(_ln(x, py, x, cy, "d"))
            parts.append(_ln(x, cy, x + SEG, cy, "d"))
        elif nd.label == "项":
            threads.append(_ln(x, -3, x, H + 3, "t"))
        py, x = cy, x + SEG
    parts.append(f'<circle cx="{x:g}" cy="{py:g}" r="2" class="{"w" if warn else "dot"}"/>')
    th = f'<g class="th">{"".join(threads)}</g>' if threads else ""
    return (f'<svg width="{w}" height="{H}" viewBox="0 0 {w} {H}" role="img" '
            f'aria-label="{_html.escape(label, quote=True)}">' + th + "".join(parts) + "</svg>")


# ───────────────────────── HTML ─────────────────────────
CSS = """
:root{--bg:#fbfbf9;--fg:#1f2328;--muted:#6b7280;--rl:rgba(110,110,110,.55);--red:#c4222d;--warn:#e03131;
--line:#d8dce0;--bar:#f0f1ee;--acc:#2f6fed;--railw:__RW__px}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#15171b;--fg:#e4e6e8;--muted:#9aa1aa;--rl:rgba(170,170,170,.5);
--red:#ff7b72;--warn:#ff6b6b;--line:#363b42;--bar:#1e2126;--acc:#7aa2ff}}
:root[data-theme=dark]{--bg:#15171b;--fg:#e4e6e8;--muted:#9aa1aa;--rl:rgba(170,170,170,.5);--red:#ff7b72;--warn:#ff6b6b;
--line:#363b42;--bar:#1e2126;--acc:#7aa2ff}
*{box-sizing:border-box}
html{background:var(--bg)}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.7 system-ui,-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
.bar{background:var(--bar);border-bottom:1px solid var(--line);font-size:13px;line-height:1.4}
.bar .in,main{max-width:calc(var(--railw) + 14px + 680px);margin:0 auto;padding:0 16px}
.bar .in{padding-top:8px;padding-bottom:8px;display:flex;flex-wrap:wrap;gap:6px 10px;align-items:center}
main{padding-bottom:64px}
.row{display:grid;grid-template-columns:var(--railw) minmax(0,680px);column-gap:14px}
.rail{height:1.7em;display:flex;align-items:center;color:var(--fg);overflow:visible}
.v{align-items:center}
.d{stroke:currentColor;stroke-width:__LD__;fill:none;stroke-linecap:butt}
.l{stroke:var(--rl);stroke-width:__LL__;fill:none}
.dot{fill:currentColor}.w{fill:var(--warn)}
svg{overflow:visible}
.th{display:none}
.th line{stroke:var(--rl);stroke-width:1}
.body{min-width:0;overflow-wrap:anywhere}
.row.heading .body{margin-top:1.4em}
.row.h1 .body{margin-top:1.8em}
h1,h2,h3,h4,h5,h6{line-height:1.35;margin:0}
h1{font-size:1.75em}h2{font-size:1.45em}h3{font-size:1.2em}h4,h5,h6{font-size:1.05em}
.row.para .body,.row.cont .body{margin:.55em 0}
.row.item .body{padding-left:calc(var(--d)*1.6em + 1.4em);text-indent:-1.4em}
.row.cont .body{padding-left:calc(var(--d)*1.6em)}
.mk{display:inline-block;width:1.4em;text-indent:0;color:var(--muted);font-size:.8em}
.red{color:var(--red)}
table{border-collapse:collapse;margin:.4em 0;font-size:.92em}
td,th{border:1px solid var(--line);padding:4px 10px;vertical-align:top}
blockquote{margin:.4em 0;padding-left:1em;border-left:3px solid var(--line);color:var(--muted)}
pre{overflow:auto;background:var(--bar);padding:8px 12px;border-radius:6px}
a{color:var(--acc)}
button{font:inherit;font-size:13px;color:var(--fg);background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:3px 10px;cursor:pointer}
button[aria-pressed=true]{background:var(--fg);color:var(--bg);border-color:var(--fg)}
.sp{flex:1}
.desc{width:100%;color:var(--muted)}
.q{margin-top:56px;border-top:1px solid var(--line);padding-top:20px}
.q fieldset{border:1px solid var(--line);border-radius:8px;margin:0 0 14px;padding:10px 14px}
.q legend{font-weight:600;padding:0 6px}
.q label{margin-right:14px;white-space:nowrap}
.q textarea{width:100%;min-height:54px;margin-top:6px;background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:6px;font:inherit;font-size:14px}
.legend{color:var(--muted);font-size:14px;margin:14px 0 6px}
"""


def row_html(row: Row, rails, data=None):
    """rails: [(data-k, svg, title)]；data: 对比页用，把各粒度的路径写进 data-p，由页面脚本实时画线。"""
    cls = f"row {row.kind}" + (f" h{row.level}" if row.kind == "heading" else "")
    cells = "".join(f'<span class="v" data-k="{k}" title="{_html.escape(t, quote=True)}">{s}</span>' for k, s, t in rails)
    dattr = ""
    if data:
        dattr = ' data-p="' + _html.escape(json.dumps(data, ensure_ascii=False, separators=(",", ":")), quote=True) + '"'
    rail = f'<div class="rail"{dattr}>{cells}</div>'
    style = f' style="--d:{row.depth}"' if row.kind in ("item", "cont") else ""
    if row.kind == "heading":
        body = f"<h{row.level}>{row.html}</h{row.level}>"
    elif row.kind == "item":
        body = f'<span class="mk">{row.marker}</span>{row.html}'
    else:
        body = row.html
    return f'<div class="{cls}"{style}>{rail}<div class="body">{body}</div></div>'


def rails_for_row(row, version_list, center_list, prev):
    out = []
    for key, gran, mode in version_list:
        if gran == "G0":
            continue
        path = rail_for(row, gran, mode)
        if path is None:
            continue
        hide = 0
        if key in COMPACT:
            hide = min(common_prefix_len(prev.get(key, []), path), len(path) - 1) if path else 0
            prev[key] = path
        t = title_text(path)
        for c in center_list:
            out.append((f"{key}|{c}", rail_svg(path, c, t, hide), t))
    return out


def max_rail_width(rows):
    m = 0
    for r in rows:
        for gran in ("G1", "G2", "G3"):
            p = r.g.get(gran)
            if p is not None:
                m = max(m, len(p))
    return SEG * (m + 1) + 6


def css(rw):
    return CSS.replace("__RW__", str(rw + 2)).replace("__LD__", str(LINE_DARK)).replace("__LL__", str(LINE_LIGHT))


def single_page(rows, gran, mode, center, rw, doc_title, key=None, thread=False):
    key = key or ("G0" if gran == "G0" else f"{gran}-{mode}")
    prev = {}
    body = "\n".join(row_html(r, rails_for_row(r, [(key, gran, mode)], [center], prev)) for r in rows)
    cap = f"{key} · 分叉居中：{'中线' if center == 'middle' else '父分支'}" + (" · 楼层线：开" if thread and key in COMPACT else "")
    shown = f'<style>.v{{display:flex}}{".th{display:inline}" if thread else ""}</style>'
    desc = VERSION_DESC["G3c"] if key in COMPACT else VERSION_DESC[gran]
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_html.escape(doc_title)} · {key}</title><style>{css(rw)}</style>{shown}</head>
<body><div class="bar"><div class="in"><b>{cap}</b><span>{desc}</span></div></div>
<main>
{body}
</main></body></html>"""


QUESTIONS = [
    ("q1", "哪个档位往下扫左栏时最容易看出章节切换？", ["G1", "G2", "G3", "都不明显"]),
    ("q2", "G3 在嵌套列表很深的地方是否过于拥挤？", ["是", "否", "局部拥挤"]),
    ("q3", "单段小节用 keep 还是 omit？", ["keep", "omit"]),
    ("q4", "分叉是以中线为中心，还是以父分支为中心更好读？", ["中线", "父分支"]),
    ("q5", "有没有线让人误判结构的地方？", []),
]

COMPARE_JS = """
(function(){
 var SEG=16,H=22,GAP=4,TALL_H=38,TALL_GAP=7,MAXF=5, root=document.documentElement;
 function g(k){try{return localStorage.getItem(k)}catch(e){return null}}
 function s(k,v){try{localStorage.setItem(k,v)}catch(e){}}
 function q(sel){return [].slice.call(document.querySelectorAll(sel))}
 var st={master:1,para:1,items:0,compact:1,thread:1,keep:1,tall:1,center:'parent'};
 try{var sv=JSON.parse(g('rail.st')||'{}'); for(var k in sv) if(k in st) st[k]=sv[k]}catch(e){}
 var theme=g('rail.t')||'auto';

 function fmt(v){return String(parseFloat(v.toPrecision(6)))}
 function ln(x1,y1,x2,y2,c){return '<line x1="'+fmt(x1)+'" y1="'+fmt(y1)+'" x2="'+fmt(x2)+'" y2="'+fmt(y2)+'" class="'+c+'"/>'}
 function esc(t){return t.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;')}
 function coord(p){return p.length? p.map(function(n){return n[1]+'/'+n[2]+(n[2]>MAXF?'⚠':'')}).join(' · ') : '—'}
 function title(p){
  if(!p.length) return '（根：整篇文档，无分叉）';
  var parts=p.map(function(n){
   if(n[0]==='段') return '第 '+n[1]+' 段（共 '+n[2]+' 段）';
   if(n[0]==='项') return '第 '+n[1]+' 项（共 '+n[2]+' 项）';
   return '第 '+n[1]+' 个 '+n[0]+'（共 '+n[2]+' 个）'});
  var t=coord(p)+'   = '+parts.join(' · ');
  if(p.some(function(n){return n[2]>MAXF})) t+='   ⚠ 兄弟数超过 5，未画分叉';
  return t;
 }
 function common(a,b){var n=0; for(var i=0;i<Math.min(a.length,b.length);i++){ if(a[i][0]!==b[i][0]||a[i][1]!==b[i][1]||a[i][2]!==b[i][2]) break; n++} return n}
 function svg(path,center,label,hide,tall){
  var h=tall?TALL_H:H, GP=tall?TALL_GAP:GAP, mid=h/2;
  var w=SEG*(path.length+1)+6, parts=hide?[]:[ln(0,mid,SEG,mid,'d')], threads=[], py=mid, x=SEG, warn=false;
  path.forEach(function(nd,idx){
   var n=nd[2], i=nd[1], draw=idx>=hide, cy=py, fork=null;
   if(n>MAXF){warn=true}
   else if(n>1){
    var span=(n-1)*GP, c=center==='middle'?mid:Math.min(Math.max(py,2+span/2),h-2-span/2);
    fork=[]; for(var k=0;k<n;k++) fork.push(c+(k-(n-1)/2)*GP);
    cy=fork[i-1];
   }
   if(draw){
    if(fork){
     var ys=fork.concat([py]);
     parts.push(ln(x,Math.min.apply(null,ys),x,Math.max.apply(null,ys),'l'));
     fork.forEach(function(y,k){ if(k!==i-1) parts.push(ln(x,y,x+SEG*0.7,y,'l')) });
     if(cy!==py) parts.push(ln(x,py,x,cy,'d'));
    }
    parts.push(ln(x,cy,x+SEG,cy,'d'));
   } else if(n>1){ threads.push(x) }
   py=cy; x+=SEG;
  });
  parts.push('<circle cx="'+fmt(x)+'" cy="'+fmt(py)+'" r="2" class="'+(warn?'w':'dot')+'"/>');
  return ['<svg width="'+w+'" height="'+h+'" viewBox="0 0 '+w+' '+h+'" role="img" aria-label="'+esc(label)+'">'+parts.join('')+'</svg>', threads, w];
 }
 function thlayer(xs,w){
  if(!xs.length) return '';
  return '<svg class="ths" width="'+w+'" height="100%" aria-hidden="true">'+
   xs.map(function(x){return '<line x1="'+fmt(x)+'" y1="0" x2="'+fmt(x)+'" y2="100%" class="t"/>'}).join('')+'</svg>';
 }
 function eff(){var m=!!st.master,p=m&&!!st.para,i=p&&!!st.items,c=p&&!!st.compact,t=c&&!!st.thread;return {m:m,p:p,i:i,c:c,t:t}}

 var rails=q('.rail[data-p]'), data=rails.map(function(r){return JSON.parse(r.dataset.p)});
 function render(){
  var e=eff(), prev=[];
  root.dataset.th=e.t?'on':'off';
  rails.forEach(function(el,idx){
   var d=data[idx], path=null;
   if(d.anc){
    el._path=null;
    var axs=[]; d.anc.forEach(function(nd,i){ if(nd[2]>1) axs.push(SEG*(i+1)) });
    el.innerHTML = e.t ? thlayer(axs, SEG*(d.anc.length+1)+6) : '';
    return;
   }
   if(e.m) path = e.i ? d.g3 : (e.p ? d.g2 : d.g1);
   if(!path){el.innerHTML='';el._path=null;return}
   if(!st.keep && path.length && path[path.length-1][2]===1) path=path.slice(0,-1);
   el._path=path;
   var hide=0;
   if(e.c){ hide = path.length ? Math.min(common(prev,path), path.length-1) : 0; prev=path; }
   var tall=e.m && !!st.tall && !(e.i && d.item);
   var r=svg(path,st.center,title(path),hide,tall);
   el.innerHTML=(e.t?thlayer(r[1],r[2]):'')+'<span class="v" style="--h:'+(tall?TALL_H:H)+'px">'+r[0]+'</span>';
  });
 }
 var pop=document.getElementById('pop');
 function hidePop(){pop.hidden=true}
 function showPop(el){
  var p=el._path; if(!p){hidePop();return}
  var h='<div class="pc">'+esc(coord(p))+'</div>';
  if(!p.length) h+='<div>整篇文档（根）</div>';
  p.forEach(function(nd,i){
   h+='<div style="padding-left:'+(i*10)+'px"><span class="pl">'+esc(nd[0])+' '+nd[1]+'/'+nd[2]+'</span> '+esc(nd[3]||'')+'</div>';
  });
  pop.innerHTML=h; pop.hidden=false;
  var sv=el.querySelector('svg'), r=(sv||el).getBoundingClientRect(), pr=pop.getBoundingClientRect();
  var left=Math.min(r.right+10, innerWidth-pr.width-8), top=r.top+r.height/2-pr.height/2;
  top=Math.max(8,Math.min(top,innerHeight-pr.height-8));
  pop.style.left=Math.max(8,left)+'px'; pop.style.top=top+'px';
 }
 document.addEventListener('mouseover',function(ev){
  var el=ev.target.closest&&ev.target.closest('.rail[data-p]');
  if(el&&el._path) showPop(el); else hidePop();
 });
 document.addEventListener('scroll',hidePop,true);
 function syncUI(){
  var e=eff();
  var en={master:true,para:e.m,keep:e.m,tall:e.m,items:e.p,compact:e.p,thread:e.c};
  q('input[data-s]').forEach(function(inp){var k=inp.dataset.s; inp.checked=!!st[k]; inp.disabled=!en[k]; inp.closest('label').classList.toggle('off',!en[k])});
  q('input[data-r]').forEach(function(inp){inp.checked=inp.value===st.center; inp.disabled=!e.m; inp.closest('label').classList.toggle('off',!e.m)});
  var d;
  if(!e.m) d='逻辑线：关（G0 对照组）';
  else d='标题层级'+(e.p?' + 段':'')+(e.i?' + 列表项':'')+(e.c?'，压缩共同前缀':'')+(e.t?'，楼层线':'')+'；单段小节'+(st.keep?'保留':'省略')+'；分叉以'+(st.center==='parent'?'父分支':'中线')+'居中';
  document.getElementById('pdesc').textContent=d;
 }
 function apply(){ syncUI(); render(); if(theme==='auto') root.removeAttribute('data-theme'); else root.dataset.theme=theme;
  document.getElementById('theme').textContent='主题：'+({auto:'自动',light:'亮',dark:'暗'})[theme]; }
 q('input[data-s]').forEach(function(inp){inp.onchange=function(){st[inp.dataset.s]=inp.checked?1:0; s('rail.st',JSON.stringify(st)); apply()}});
 q('input[data-r]').forEach(function(inp){inp.onchange=function(){st.center=inp.value; s('rail.st',JSON.stringify(st)); apply()}});
 document.getElementById('theme').onclick=function(){theme=({auto:'light',light:'dark',dark:'auto'})[theme];s('rail.t',theme);apply()};

 var saved={}; try{saved=JSON.parse(g('rail.answers')||'{}')}catch(e){}
 function collect(){var o={};
  q('.q input[type=radio]:checked').forEach(function(i){o[i.name]=i.value});
  q('.q textarea[data-note]').forEach(function(t){if(t.value) o[t.dataset.note+'_note']=t.value}); o.switches=st; return o}
 q('.q input[type=radio]').forEach(function(i){if(saved[i.name]===i.value) i.checked=true; i.onchange=function(){s('rail.answers',JSON.stringify(collect()))}});
 q('.q textarea[data-note]').forEach(function(t){t.value=saved[t.dataset.note+'_note']||''; t.oninput=function(){s('rail.answers',JSON.stringify(collect()))}});
 document.getElementById('copy').onclick=function(){
  var txt=JSON.stringify(collect(),null,2), out=document.getElementById('out'), m=document.getElementById('copied');
  function fb(){out.hidden=false;out.value=txt;out.select();m.textContent='请手动复制下面的文本'}
  try{navigator.clipboard.writeText(txt).then(function(){m.textContent='已复制'},fb)}catch(e){fb()}
 };
 apply();
})();
"""


def row_data(row):
    if row.kind == "heading":
        return {"anc": [[n.label, n.i, n.n, n.title] for n in row.anc]} if row.anc else None
    d = {k: ([[n.label, n.i, n.n, n.title] for n in row.g[k]] if row.g[k] is not None else None) for k in ("G1", "G2", "G3")}
    if all(v is None for v in d.values()):
        return None
    return {"g1": d["G1"], "g2": d["G2"], "g3": d["G3"], "item": row.kind == "item"}


COMPARE_CSS = """
.bar{position:sticky;top:0;z-index:5}
.rail{height:auto;min-height:1.7em;display:block;position:relative}
.v{display:block;margin-top:calc((1.7em - var(--h,22px))*.5)}
.ths{position:absolute;left:0;top:0;height:100%;overflow:visible}
.t{stroke:var(--rl);stroke-width:1}
.panel{background:var(--bar);border:1px solid var(--line);border-radius:8px;padding:8px 14px;font-size:14px;line-height:1.55;
 max-width:calc(var(--railw) + 14px + 680px);margin:12px 0}
.panel summary{cursor:pointer;font-weight:600}
.sw{display:block;margin:3px 0;cursor:pointer}
.sw.off{opacity:.4;cursor:not-allowed}
.sw input{margin-right:6px}
.sub{margin-left:10px;padding-left:12px;border-left:1px solid var(--line)}
.note{color:var(--muted);font-size:12.5px}
#pdesc{color:var(--muted);font-size:12.5px;margin-top:6px}
.rowopt{margin:3px 0}
.pop{position:fixed;z-index:20;background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:8px;padding:8px 12px;
 font-size:13px;line-height:1.5;max-width:380px;box-shadow:0 6px 24px rgba(0,0,0,.2);pointer-events:none}
.pop .pc{color:var(--muted);margin-bottom:4px}
.pop .pl{color:var(--muted);font-variant-numeric:tabular-nums}
@media (min-width:1400px){.panel{position:fixed;right:16px;top:64px;width:260px;margin:0;z-index:6}}
"""

PANEL = """<details class="panel" open><summary>逻辑线设置</summary>
<label class="sw"><input type="checkbox" data-s="master">逻辑线</label>
<div class="sub">
 <div class="note">标题层级（# ## ###）随总开关，始终包含</div>
 <label class="sw"><input type="checkbox" data-s="para">段与段之间</label>
 <div class="sub">
  <label class="sw"><input type="checkbox" data-s="items">列表项</label>
  <label class="sw"><input type="checkbox" data-s="compact">压缩共同前缀</label>
  <div class="sub"><label class="sw"><input type="checkbox" data-s="thread">楼层线</label></div>
 </div>
 <label class="sw"><input type="checkbox" data-s="keep">单段小节保留一节（关 = omit）</label>
 <label class="sw"><input type="checkbox" data-s="tall">加高段落线（列表项线不受影响）</label>
 <div class="rowopt">分叉居中：
  <label class="sw" style="display:inline"><input type="radio" name="center" data-r="1" value="parent">父分支</label>
  <label class="sw" style="display:inline"><input type="radio" name="center" data-r="1" value="middle">中线</label></div>
</div>
<div id="pdesc"></div>
</details>"""

COMPARE_TMPL = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__ · 逻辑线开关</title>
<style>__CSS__</style></head>
<body><div class="bar"><div class="in"><b>逻辑线</b><span>按层级开关组合，线在页面里实时绘制。</span><span class="sp"></span>
<button id="theme">主题：自动</button></div></div>
<main>
__PANEL__
<div class="legend"><span style="display:inline-flex;vertical-align:middle;color:var(--fg)">__SAMPLE__</span>
 深色 = 本段所在分支；浅色分支数 = 这一层的兄弟数；红点 = 兄弟超过 5 个（未画分叉）。</div>
__BODY__
<section class="q"><h2>需要你判断的问题</h2>
__QS__
<button id="copy">复制全部答案</button> <span id="copied" style="color:var(--muted);font-size:13px"></span>
<textarea id="out" hidden></textarea>
</section>
</main>
<div id="pop" class="pop" hidden></div>
<script>__JS__</script></body></html>"""


def compare_page(rows, rw, doc_title):
    body = "\n".join(row_html(r, [], row_data(r)) for r in rows)
    qs = []
    for qid, text, opts in QUESTIONS:
        radios = "".join(f'<label><input type="radio" name="{qid}" value="{o}"> {o}</label>' for o in opts)
        qs.append(f'<fieldset><legend>{text}</legend>{radios}<textarea data-note="{qid}" placeholder="备注（可选）"></textarea></fieldset>')
    sample = rail_svg([PN("##", 2, 3, ""), PN("###", 2, 2, ""), PN("段", 1, 2, "")], "parent", "示例")
    return (COMPARE_TMPL.replace("__TITLE__", _html.escape(doc_title)).replace("__CSS__", css(rw) + COMPARE_CSS)
            .replace("__PANEL__", PANEL).replace("__SAMPLE__", sample).replace("__BODY__", body)
            .replace("__QS__", "".join(qs)).replace("__JS__", COMPARE_JS))


# ───────────────────────── 纯文本版 / 报告 ─────────────────────────
def text_version(rows, gran="G2", mode="keep"):
    out = [f"# {gran}-{mode} 文字坐标版",
           "# 每个 i/n = 第 i 个（共 n 个）；各节依次为 标题层级 · … · 段。⚠ = 兄弟数超过 5。", ""]
    for r in rows:
        if r.kind == "heading":
            out += ["", "#" * r.level + " " + r.text]
        elif r.kind == "para":
            out.append(f"{coord_text(rail_for(r, gran, mode))}   {r.text}")
        elif r.kind == "item":
            p = rail_for(r, gran, mode)
            pre = f"{coord_text(p)}   " if p is not None else ""
            out.append(f"{pre}{'  ' * r.depth}- {r.text}")
        elif r.kind == "cont":
            out.append(f"      [{r.otype or '续段'}]")
    return "\n".join(out) + "\n"


def make_report(src, rows, tree, stats, rw):
    L = [f"# 结构报告：{src.name}", "", "## 1. 概要", ""]
    root_desc = {"single-h1": "恰好一个 `#`：以它为根（根线是一根直线，不分叉）",
                 "multi-h1": f"{stats['h1_count']} 个 `#`：根 = 整篇文档，每个 `#` 作为第一层分叉",
                 "no-h1": "没有 `#`：根 = 整篇文档", "mixed": "根 = 整篇文档"}[stats["root"]]
    n12 = sum(1 for r in rows if r.g["G2"] is not None)
    n3 = sum(1 for r in rows if r.g["G3"] is not None)
    L += [f"- 根节点：{root_desc}",
          f"- 有线的行数：G1 / G2 各 {n12} 行，G3 {n3} 行",
          f"- 左栏宽度（取所有版本里最长的线）：{rw}px；每节 {SEG}px，线高 {H}px", ""]
    L += ["## 2. 解析出的结构树", "",
          "方括号是该节点在兄弟里的位置，格式 `[类型 i/n]`。标题后的 `[# i/n]` 是它在父节点下排第几。", "", "```text"]
    L += tree
    L += ["```", ""]

    warn = {}
    for key, gran, mode in VERSIONS:
        if gran == "G0" or key in COMPACT:
            continue
        for r in rows:
            for nd in rail_for(r, gran, mode) or []:
                if nd.n > MAX_FORK:
                    warn.setdefault((nd.where, nd.label, nd.n), set()).add(key)
    L += ["## 3. 兄弟超过 5 个的分叉（违反约束）", ""]
    if not warn:
        L += ["无。", ""]
    else:
        L += ["这些分叉没有被硬画：该节画成直线，线末端的圆点变红。", "",
              "| 位置 | 节点类型 | 兄弟数 | 受影响的版本 | 建议 |", "| --- | --- | --- | --- | --- |"]
        for (where, label, n), vs in warn.items():
            sug = ("把一部分并列段落归入 `###` 小节，或合并为列表" if label == "段"
                   else "把列表拆成两组，或按主题加一层嵌套" if label == "项"
                   else "增加一层标题，把并列项分组")
            L.append(f"| {where} | {label} | {n} | {'、'.join(sorted(vs))} | {sug} |")
        L.append("")

    h1 = ("有，" + str(stats["h1_count"]) + " 个 #") if stats["root"] == "multi-h1" else ("无 #" if stats["root"] == "no-h1" else "否")
    L += ["## 4. 边界情况与处理方式", "",
          "| 情况 | 本文档中的出现 | 处理方式 |", "| --- | --- | --- |",
          f"| 没有 `#` 或有多个 `#` | {h1} | 根 = 整篇文档；多个 `#` 各自成为第一层分叉（路径多一节） |",
          f"| 第一个标题之前的引言段 | 根下直接有 {stats['intro_blocks']} 段 | 作为根的直接子段，按「段 i/n」计数，与同层标题分开计数 |",
          "| 小节下直接是段落、没有 `###` | 见结构树 | 按实际层级画，不补空节，所以这些段的线比有 `###` 的段短一节 |",
          "| 一个小节里既有段、又有子标题 | 见结构树 | 段与子标题分开计数；段在前、子标题在后 |",
          f"| 代码块 / 引用块 / 表格 | {'、'.join(f'{k} {v} 个' for k, v in stats['others'].items()) or '无'} | 不画线，跟随上一段（该行左栏留空）；不计入兄弟数 |"]
    if stats["split_lists"]:
        L.append("| 列表被表格 / 代码块 / 引用块截成两段 | " + "；".join(f"「{t}」中被 {o} 截断" for t, o in stats["split_lists"]) +
                 " | 按 Markdown 解析结果处理：截断后的两个列表是各自独立的段，第二个列表的缩进层级丢失，成为顶层列表。"
                 "如果原意是同一个嵌套列表，应在源文件里给表格加缩进，让它留在列表项内部 |")
    if stats["orphan_others"]:
        L.append(f"| 非段落块出现在小节最前面 | {stats['orphan_others']} | 前面没有段可跟随，左栏留空 |")
    L += [f"| 空的列表项 | {len(stats['empty_items'])} 个：{'；'.join(stats['empty_items']) or '无'} | 丢弃，不计入兄弟数 |",
          f"| 没有内容的标题 | {'；'.join(stats['empty_sections']) or '无'} | 该节没有线可画，只保留标题 |",
          "| 一个列表项里有多个段落 | 规则 | 第一段带线；后续段落按「同一项的续段」处理，不画线 |",
          "| 一个列表项里有多个子列表 | 规则 | 每个子列表各自独立编号 |",
          "| 列表在 G1 / G2 中 | 规则 | 整个列表算一段，线只画在列表第一项；每一项各画线是 G3 的事 |",
          "| keep / omit 的范围 | 规则 | 只作用于路径的最后一节：最后一节兄弟数为 1 时，omit 把它去掉 |",
          "| 标题行本身 | 规则 | 不画线（handoff 只要求段和列表项） |", ""]

    L += ["## 5. 对齐影响", "", "| 版本 | 线的节数分布（节数: 行数） |", "| --- | --- |"]
    for key, gran, mode in VERSIONS:
        if gran == "G0" or key in COMPACT:
            continue
        c = Counter(len(rail_for(r, gran, mode)) for r in rows if rail_for(r, gran, mode) is not None)
        L.append(f"| {key} | " + "，".join(f"{k}: {v}" for k, v in sorted(c.items())) + " |")
    L += ["", "每节宽度固定 16px，线都从左栏左边缘开始，所以第 k 节在所有行里 x 位置相同；节数不同只表现为线末端停在不同位置，不会错位。",
          "代价是：路径较短的行，圆点停在更靠左的位置，读者要靠「节数」而不是「x 位置」来比较层级。", ""]
    return "\n".join(L)


# ───────────────────────── CLI ─────────────────────────
def main(argv=None):
    ap = argparse.ArgumentParser(prog="rail", description="Markdown 逻辑线原型")
    ap.add_argument("input")
    ap.add_argument("--granularity", choices=["G0", "G1", "G2", "G3"], default="G2")
    ap.add_argument("--single", choices=["keep", "omit"], default="keep")
    ap.add_argument("--center", choices=list(CENTERS), default="parent")
    ap.add_argument("--compact", action="store_true", help="G3 压缩版：隐藏与上一行共同的前缀")
    ap.add_argument("--thread", action="store_true", help="压缩版里显示楼层线")
    ap.add_argument("--all", action="store_true", help="生成全部 7 个版本 + 对比页 + 文字版 + 报告")
    ap.add_argument("-o", "--output", required=True)
    a = ap.parse_args(argv)

    src = Path(a.input)
    text = src.read_text(encoding="utf-8")
    md = make_md()
    stats = {"empty_items": [], "empty_sections": [], "others": Counter(), "orphan_others": [], "split_lists": []}
    root = build_sections(md, text, stats)
    rows, tree = build_rows(root, stats)
    rw = max_rail_width(rows)
    doc_title = next((r.text for r in rows if r.kind == "heading"), src.stem)

    if a.all:
        out = Path(a.output)
        out.mkdir(parents=True, exist_ok=True)
        for key, g, m in VERSIONS:
            (out / f"{key}.html").write_text(single_page(rows, g, m, a.center, rw, doc_title, key=key, thread=a.thread), encoding="utf-8")
        (out / "index.html").write_text(compare_page(rows, rw, doc_title), encoding="utf-8")
        (out / "G2-keep.txt").write_text(text_version(rows), encoding="utf-8")
        (out / "report.md").write_text(make_report(src, rows, tree, stats, rw), encoding="utf-8")
        print(f"已生成 {len(VERSIONS)} 个版本 + index.html + G2-keep.txt + report.md -> {out}")
    else:
        key = "G0" if a.granularity == "G0" else f"{a.granularity}-{a.single}"
        if a.compact:
            assert a.granularity == "G3", "--compact 只用于 G3"
            key = "G3-compact"
        p = Path(a.output)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(single_page(rows, a.granularity, a.single, a.center, rw, doc_title, key=key, thread=a.thread), encoding="utf-8")
        print(f"已生成 {key} -> {p}")


if __name__ == "__main__":
    main()
