"""第 5 步的机械规则：在属性矩阵和缺陷表上找空格，不需要判断力。

- R1  整列空白：某个属性整列没有 ✓。
- R2  两两组合空白：在有 ✓ 的属性中，没有任何一行同时为 ✓ 的属性对。
- R2′ 三元组空白：三个属性两两都被某行同时覆盖过，但没有任何一行三者同时 ✓。
      只列这种“最小”三元组；含有空白对的三元组已经由 R2 覆盖，不重复列。
- R3  悬空缺陷：defects.csv 里没有出现在 edges.csv 的 defect_id 中的缺陷。

△ 不算覆盖（模板第 5 步）。但凡有 △ 的地方，都在 partial 里列出对应方法，
便于读者看到“有人部分做过”。partial_counts_as_cover=True 只用于对照测试。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations
from pathlib import Path

from researchtool.gapr import schema

YES, PART, NO = "Y", "P", "N"
DISPLAY = {YES: "✓", PART: "△", NO: "✗"}
_ALIASES = {
    "✓": YES, "✔": YES, "y": YES, "yes": YES, "1": YES,
    "△": PART, "▲": PART, "p": PART, "partial": PART, "~": PART, "0.5": PART,
    "✗": NO, "✘": NO, "×": NO, "x": NO, "n": NO, "no": NO, "0": NO,
}


def parse_cell(raw: str) -> str | None:
    """把格子里的符号统一成 Y / P / N；空格子返回 None。"""
    s = raw.strip()
    if not s:
        return None
    v = _ALIASES.get(s) or _ALIASES.get(s.lower())
    if v is None:
        raise ValueError(f"无法识别的格子值 {raw!r}，只能填 ✓ / △ / ✗（或 Y / P / N）")
    return v


@dataclass
class Matrix:
    attrs: list[str]
    methods: list[str]
    cells: dict[tuple[str, str], str]  # (method, attr) -> Y/P/N

    def get(self, method: str, attr: str) -> str:
        return self.cells[(method, attr)]

    def column(self, attr: str) -> dict[str, str]:
        return {m: self.cells[(m, attr)] for m in self.methods}


def load_matrix(path: Path, *, allow_empty: bool = False) -> Matrix:
    rows = schema.read_rows(path)
    if not rows:
        raise ValueError(f"{path} 没有任何方法行")
    header = list(rows[0].keys())
    if header[0] != "method":
        raise ValueError(f"{path} 第一列必须叫 method，现在是 {header[0]!r}")
    attrs = header[1:]
    cells: dict[tuple[str, str], str] = {}
    methods: list[str] = []
    problems: list[str] = []
    for i, r in enumerate(rows, start=2):
        m = r["method"]
        if not m:
            problems.append(f"第 {i} 行缺 method")
            continue
        if m in methods:
            problems.append(f"第 {i} 行方法 {m!r} 重复")
            continue
        methods.append(m)
        for a in attrs:
            try:
                v = parse_cell(r.get(a, ""))
            except ValueError as e:
                problems.append(f"{m} × {a}：{e}")
                continue
            if v is None:
                if not allow_empty:
                    problems.append(f"{m} × {a}：格子为空")
                continue
            cells[(m, a)] = v
    if problems:
        raise ValueError(f"{path} 有 {len(problems)} 处问题：\n  " + "\n  ".join(problems))
    return Matrix(attrs=attrs, methods=methods, cells=cells)


def matrix_filled_cells(path: Path) -> int:
    """matrix.csv 里已经填了多少个格子（用于冻结检查）。"""
    rows = schema.read_rows(path)
    return sum(1 for r in rows for k, v in r.items() if k != "method" and v.strip())


def load_incompatible(path: Path) -> set[frozenset[str]]:
    return {frozenset((r["attr_a"], r["attr_b"])) for r in schema.read_rows(path)
            if r.get("attr_a") and r.get("attr_b")}


@dataclass
class Candidate:
    rule: str
    attrs: tuple[str, ...] = ()
    partial: list[str] = field(default_factory=list)  # 部分覆盖的方法，例如 "DLinear(L✓ D△)"
    ref: str = ""        # R3：缺陷 id
    statement: str = ""  # 中文陈述，由 describe() 填

    @property
    def key(self) -> str:
        return self.rule + ":" + ("×".join(self.attrs) if self.attrs else self.ref)


def _covers(values: list[str], partial_counts_as_cover: bool) -> bool:
    ok = {YES, PART} if partial_counts_as_cover else {YES}
    return all(v in ok for v in values)


def _partial_rows(m: Matrix, attrs: tuple[str, ...]) -> list[str]:
    """所有格子都是 ✓ 或 △、但不全是 ✓ 的方法。"""
    out = []
    for meth in m.methods:
        vals = [m.get(meth, a) for a in attrs]
        if all(v in (YES, PART) for v in vals) and not all(v == YES for v in vals):
            if len(attrs) == 1:
                out.append(meth)
            else:
                out.append(f"{meth}(" + " ".join(f"{a}{DISPLAY[v]}" for a, v in zip(attrs, vals)) + ")")
    return out


@dataclass
class Analysis:
    r1: list[Candidate]
    r2: list[Candidate]
    r2p: list[Candidate]
    supported: list[str]                 # 至少有一个 ✓ 的属性
    skipped_incompatible: list[tuple[str, ...]]
    covered_pairs: dict[tuple[str, str], list[str]]  # 被覆盖的属性对 -> 同时 ✓ 的方法


def analyze(m: Matrix, incompatible: set[frozenset[str]] | None = None, *,
            partial_counts_as_cover: bool = False, triples: bool = True) -> Analysis:
    incompatible = incompatible or set()
    pc = partial_counts_as_cover

    r1, supported = [], []
    for a in m.attrs:
        if any(_covers([m.get(meth, a)], pc) for meth in m.methods):
            supported.append(a)
        else:
            r1.append(Candidate("R1", (a,), _partial_rows(m, (a,))))

    r2, skipped = [], []
    covered: dict[tuple[str, str], list[str]] = {}
    for a, b in combinations(supported, 2):
        if frozenset((a, b)) in incompatible:
            skipped.append((a, b))
            continue
        both = [meth for meth in m.methods if _covers([m.get(meth, a), m.get(meth, b)], pc)]
        if both:
            covered[(a, b)] = both
        else:
            r2.append(Candidate("R2", (a, b), _partial_rows(m, (a, b))))

    r2p = []
    if triples:
        for t in combinations(supported, 3):
            pairs = list(combinations(t, 2))
            if any(frozenset(p) in incompatible for p in pairs):
                continue  # 含不相容对，整组跳过（不相容对本身已记在 skipped）
            if not all(p in covered for p in pairs):
                continue  # 含有空白对，已由 R2 报告
            if not any(_covers([m.get(meth, a) for a in t], pc) for meth in m.methods):
                r2p.append(Candidate("R2′", t, _partial_rows(m, t)))

    return Analysis(r1=r1, r2=r2, r2p=r2p, supported=supported,
                    skipped_incompatible=skipped, covered_pairs=covered)


def dangling_defects(defects: list[dict[str, str]], edges: list[dict[str, str]]) -> list[Candidate]:
    """R3：没有任何演化边指向它的遗留缺陷。"""
    solved = {e["defect_id"] for e in edges if e.get("defect_id")}
    return [Candidate("R3", (), [], ref=d["id"]) for d in defects
            if d.get("id") and d["id"] not in solved]


def describe(c: Candidate, attr_names: dict[str, str],
             defects: dict[str, dict[str, str]] | None = None,
             nodes: dict[str, dict[str, str]] | None = None) -> str:
    def nm(a: str) -> str:
        return f"「{attr_names.get(a, a)}」({a})"

    if c.rule == "R1":
        return f"没有方法能做到{nm(c.attrs[0])}"
    if c.rule in ("R2", "R2′"):
        return "没有方法能同时做到" + "、".join(nm(a) for a in c.attrs)
    if c.rule == "R3":
        d = (defects or {}).get(c.ref, {})
        node = (nodes or {}).get(d.get("node_id", ""), {}).get("name", d.get("node_id", "?"))
        return f"悬空缺陷：{node} 的「{d.get('defect', c.ref)}」没有后续方法专门解决"
    return c.statement
