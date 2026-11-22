"""逐步检查：每一步的产出是否合格。不合格就不能进入下一步。

检查只看文件，不联网，不调用 LLM。规则来自 docs/gap-research.md，
每条检查都在注释里写明对应模板的哪一条。

用法：
    report = run(project)               # 检查全部已开始的步骤
    require(project, upto=4)            # 第 0–4 步有任何错误就抛 GateError
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from functools import cached_property
from pathlib import Path

from researchtool.gapr import confirm, rubric, schema
from researchtool.gapr import matrix as mx
from researchtool.gapr.evidence import Evidence
from researchtool.gapr.fulltext import FulltextStore, check_in_pool, context_pool
from researchtool.gapr.project import Project

STEP_TITLES = {
    0: "输入", 1: "综述与节点清单", 2: "方法节点表", 3: "演化边与分歧点",
    4: "属性矩阵", 5: "列出候选空白", 6: "逐条验证", 7: "排序",
}
ERROR, WARN = "错误", "提醒"
SCALE_NODES = {"mini": (8, 12), "quick": (15, 20), "full": (30, 60)}
DATE_BASES = {"arXiv v1", "publicationDate", "year"}
DEFECT_KINDS = {"自述", "他人指出", "审稿"}
CATEGORIES = {"逻辑不成立", "文献里已有但节点表没收录", "简单拼接可解", "疑似真空白"}
SURVEY_VERDICTS = {"确认空白", "部分解决", "已被解决", "争议未决"}
BACKTEST_VERDICTS = {"命中", "部分命中", "未命中"}
CONFIDENCE = {"低": 0, "中": 1, "高": 2}
SEARCH_KINDS = {"phrasing", "forward", "venue", "cross"}
VERIFIED_TAGS = {"论文原文", "检索核对"}
SCORE_COLS = ["score_sources", "score_demand", "score_splice", "score_crowding", "score_feasibility"]
_SURVEY_RE = re.compile(r"survey|review|taxonomy|overview|综述", re.I)


@dataclass
class Issue:
    level: str
    where: str
    msg: str

    def __str__(self) -> str:
        return f"[{self.level}] {self.where}：{self.msg}" if self.where else f"[{self.level}] {self.msg}"


@dataclass
class StepResult:
    step: int
    started: bool
    issues: list[Issue] = field(default_factory=list)

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.level == ERROR]

    @property
    def passed(self) -> bool:
        return self.started and not self.errors


class GateError(Exception):
    pass


def _split_ids(s: str) -> list[str]:
    return [x.strip() for x in re.split(r"[;,，；]", s or "") if x.strip()]


def _valid_date(s: str) -> bool:
    if re.fullmatch(r"\d{4}", s):
        return True
    try:
        date.fromisoformat(s)
        return True
    except ValueError:
        return False


class Ctx:
    """一次检查里共享的数据（按需读取）。"""

    def __init__(self, p: Project):
        self.p = p
        self.root = p.root

    def rows(self, name: str) -> list[dict[str, str]]:
        cache = self.__dict__.setdefault("_rows", {})
        if name not in cache:
            cache[name] = schema.read_rows(self.root / name)
        return cache[name]

    @cached_property
    def nodes(self) -> dict[str, dict[str, str]]:
        return {r["id"]: r for r in self.rows("nodes.csv") if r.get("id")}

    @cached_property
    def defects(self) -> dict[str, dict[str, str]]:
        return {r["id"]: r for r in self.rows("defects.csv") if r.get("id")}

    @cached_property
    def attrs(self) -> list[dict[str, str]]:
        return schema.read_rows(self.root / "rubric" / "attributes.csv")

    @cached_property
    def fulltext(self) -> FulltextStore:
        return FulltextStore(self.root)

    @cached_property
    def citation_pool(self):
        return context_pool(self.root, "citations")

    @cached_property
    def review_pool(self):
        return context_pool(self.root, "reviews")

    @cached_property
    def confirmations(self) -> dict:
        return confirm.load(self.root)

    def paper_key(self, node: dict[str, str]) -> str:
        if node.get("arxiv_id"):
            return "arXiv:" + node["arxiv_id"]
        return node.get("s2_id", "")

    @property
    def ref_date(self) -> date:
        return self.p.cutoff or date.today()


# ---------------------------------------------------------------- 第 0 步

def step0(c: Ctx) -> StepResult:
    r = StepResult(0, True)
    if not c.p.domain:
        r.issues.append(Issue(ERROR, "project.toml", "没有填领域（domain）"))
    if not c.p.start:
        r.issues.append(Issue(ERROR, "project.toml", "没有填时间范围起点（start）"))
    if not c.p.audience:
        r.issues.append(Issue(WARN, "project.toml", "没有填读者（audience），术语解释深度会按默认处理"))
    return r


# ---------------------------------------------------------------- 第 1 步

def step1(c: Ctx, *, include_confirm: bool = True) -> StepResult:
    fams = c.rows("families.csv")
    nodes = c.rows("nodes.csv")
    r = StepResult(1, bool(fams or nodes))
    if not r.started:
        return r
    add = r.issues.append
    fam_names = {f["family"] for f in fams if f.get("family")}
    if not fam_names:
        add(Issue(ERROR, "families.csv", "没有方法族"))

    seen: set[str] = set()
    per_family: dict[str, int] = {}
    for i, n in enumerate(nodes, start=2):
        where = f"nodes.csv 第 {i} 行（{n.get('id') or '无 id'}）"
        nid = n.get("id", "")
        if not nid:
            add(Issue(ERROR, where, "缺 id"))
            continue
        if nid in seen:
            add(Issue(ERROR, where, "id 重复"))
        seen.add(nid)
        if not n.get("name"):
            add(Issue(ERROR, where, "缺名称"))
        fam = n.get("family", "")
        if fam not in fam_names:
            add(Issue(ERROR, where, f"方法族 {fam!r} 不在 families.csv 里"))
        per_family[fam] = per_family.get(fam, 0) + 1
        fp = n.get("first_public", "")
        if not fp or not _valid_date(fp):  # 全程规则 2：首次公开日期
            add(Issue(ERROR, where, f"首次公开日期 {fp!r} 无效，应为 YYYY-MM-DD"))
        elif c.p.is_backtest and c.p.cutoff:
            if len(fp) == 4 and int(fp) == c.p.cutoff.year:
                add(Issue(WARN, where, "只有年份，与截止年相同，无法确认在截止日之前"))
            elif (len(fp) == 4 and int(fp) > c.p.cutoff.year) or (len(fp) > 4 and date.fromisoformat(fp) > c.p.cutoff):
                add(Issue(ERROR, where, f"首次公开于 {fp}，晚于回测截止日 {c.p.cutoff}"))
        if n.get("date_basis") not in DATE_BASES:
            add(Issue(ERROR, where, f"日期依据应为 {'/'.join(sorted(DATE_BASES))}"))
        elif n.get("arxiv_id") and n["date_basis"] != "arXiv v1":
            add(Issue(WARN, where, "有 arXiv id，但日期不是 arXiv v1（用 gapr paper 查 v1 日期）"))
        if not (n.get("url") or n.get("arxiv_id") or n.get("s2_id")):
            add(Issue(ERROR, where, "没有链接、arXiv id 或 S2 id，无法核对"))
        if n.get("source_tag") not in schema.SOURCE_TAGS:  # 全程规则 1
            add(Issue(ERROR, where, f"来源标注应为 {'/'.join(schema.SOURCE_TAGS)}"))

    lo, hi = SCALE_NODES.get(c.p.scale, (1, 10**6))
    if len(seen) < lo:
        add(Issue(ERROR, "nodes.csv", f"规模 {c.p.scale} 至少 {lo} 个节点，现在 {len(seen)} 个"))
    elif len(seen) > hi:
        add(Issue(WARN, "nodes.csv", f"规模 {c.p.scale} 建议不超过 {hi} 个节点，现在 {len(seen)} 个"))
    for fam in sorted(fam_names):  # 第 1 步第 5 条：每个方法族至少 2 个
        if per_family.get(fam, 0) < 2:
            add(Issue(ERROR, "families.csv", f"方法族「{fam}」只有 {per_family.get(fam, 0)} 个节点，至少要 2 个"))

    if not c.rows("neighbor_terms.csv"):  # 第 1 步第 7 条
        add(Issue(ERROR, "neighbor_terms.csv", "邻近领域术语对照表为空"))
    if not any(_SURVEY_RE.search(s.get("query", "")) and not s.get("error") for s in c.rows("search_log.csv")):
        add(Issue(ERROR, "search_log.csv", "没有找综述的检索记录（survey / review / taxonomy）"))

    if include_confirm:
        conf = c.confirmations.get("1")
        if not conf:
            add(Issue(ERROR, "确认点 1", "还没有确认（请你本人在终端运行 python -m researchtool.gapr confirm <项目> 1）"))
        else:
            if conf.get("by") == confirm.AUTO_BY:
                add(Issue(WARN, "确认点 1", "由 Claude 代为确认（自动模式），未经人工审核"))
            now = confirm.snapshot_1(c.root)
            if now["nodes"] != conf["nodes"]:
                added = sorted(set(now["nodes"]) - set(conf["nodes"]))
                removed = sorted(set(conf["nodes"]) - set(now["nodes"]))
                add(Issue(ERROR, "确认点 1", f"确认之后节点有变化（新增 {added or '无'}，删除 {removed or '无'}），需要重新确认"))
            if now["terms_hash"] != conf["terms_hash"]:
                add(Issue(ERROR, "确认点 1", "确认之后术语对照表有变化，需要重新确认"))
    return r


# ---------------------------------------------------------------- 第 2 步

def _check_quote(c: Ctx, kind: str, node: dict[str, str] | None, source_ref: str,
                 quote: str) -> tuple[bool | None, str]:
    if kind == "他人指出":
        return check_in_pool(c.citation_pool, quote)
    if kind == "审稿":
        return check_in_pool(c.review_pool, quote)
    key = source_ref if re.match(r"(?i)(arxiv|acl):", source_ref or "") else (c.paper_key(node) if node else "")
    if not key:
        return None, "不知道该对照哪篇论文（节点没有 arXiv id，source_ref 也没写 arXiv:… / ACL:…）"
    return c.fulltext.check_quote(key, quote)


def step2(c: Ctx) -> StepResult:
    defects = c.rows("defects.csv")
    r = StepResult(2, bool(defects) or any(n.get("problem") for n in c.nodes.values()))
    if not r.started:
        return r
    add = r.issues.append
    for nid, n in c.nodes.items():
        if not n.get("problem"):
            add(Issue(ERROR, f"节点 {nid}", "缺“要解决的问题”"))
        if not n.get("core_idea"):
            add(Issue(ERROR, f"节点 {nid}", "缺“核心思路”"))
    by_node: dict[str, list[dict[str, str]]] = {}
    seen: set[str] = set()
    for i, d in enumerate(defects, start=2):
        where = f"defects.csv 第 {i} 行（{d.get('id') or '无 id'}）"
        if not d.get("id"):
            add(Issue(ERROR, where, "缺 id"))
            continue
        if d["id"] in seen:
            add(Issue(ERROR, where, "id 重复"))
        seen.add(d["id"])
        node = c.nodes.get(d.get("node_id", ""))
        if not node:
            add(Issue(ERROR, where, f"节点 {d.get('node_id')!r} 不存在"))
        else:
            by_node.setdefault(d["node_id"], []).append(d)
        if not d.get("defect"):
            add(Issue(ERROR, where, "缺缺陷描述"))
        if d.get("kind") not in DEFECT_KINDS:
            add(Issue(ERROR, where, f"来源类型应为 {'/'.join(sorted(DEFECT_KINDS))}"))
            continue
        tag = d.get("source_tag", "")
        if tag not in schema.SOURCE_TAGS:
            add(Issue(ERROR, where, f"来源标注应为 {'/'.join(schema.SOURCE_TAGS)}"))
        elif tag in VERIFIED_TAGS:  # 全程规则 1：标了「论文原文/检索核对」就必须能在证据里找到原句
            if not d.get("quote"):
                add(Issue(ERROR, where, f"标为「{tag}」但没有原句"))
            else:
                ok, why = _check_quote(c, d["kind"], node, d.get("source_ref", ""), d["quote"])
                if ok is not True:
                    add(Issue(ERROR, where, f"原句核对未通过：{why}。补齐证据，或把来源标注改为「已有知识」"))
    for nid in c.nodes:
        ds = by_node.get(nid, [])
        if not ds:
            add(Issue(ERROR, f"节点 {nid}", "没有任何遗留缺陷"))
        elif not any(d.get("kind") == "自述" for d in ds):
            add(Issue(WARN, f"节点 {nid}", "没有作者自述的缺陷（Limitations / Conclusion）"))
    for rel in Evidence(c.root).tampered():
        add(Issue(ERROR, f"evidence/{rel}", "证据文件在登记后被改动或删除，不再算作证据"))
    return r


# ---------------------------------------------------------------- 第 3 步

def step3(c: Ctx) -> StepResult:
    edges = c.rows("edges.csv")
    forks = c.rows("forks.csv")
    r = StepResult(3, bool(edges or forks))
    if not r.started:
        return r
    add = r.issues.append
    children: dict[str, set[str]] = {}
    for i, e in enumerate(edges, start=2):
        where = f"edges.csv 第 {i} 行"
        par, ch, did = e.get("parent_id", ""), e.get("child_id", ""), e.get("defect_id", "")
        if par not in c.nodes:
            add(Issue(ERROR, where, f"父节点 {par!r} 不存在"))
        if ch not in c.nodes:
            add(Issue(ERROR, where, f"子节点 {ch!r} 不存在"))
        d = c.defects.get(did)
        if not d:
            add(Issue(ERROR, where, f"缺陷 {did!r} 不存在"))
        elif d.get("node_id") != par:
            add(Issue(ERROR, where, f"缺陷 {did} 属于 {d.get('node_id')}，不是父节点 {par}"))
        if par in c.nodes and ch in c.nodes:
            a, b = c.nodes[par].get("first_public", ""), c.nodes[ch].get("first_public", "")
            if _valid_date(a) and _valid_date(b) and len(a) == len(b) == 10 and b < a:
                add(Issue(WARN, where, f"子方法 {ch}（{b}）早于父方法 {par}（{a}）"))
        tag = e.get("source_tag", "")
        if tag not in schema.SOURCE_TAGS:
            add(Issue(ERROR, where, f"来源标注应为 {'/'.join(schema.SOURCE_TAGS)}"))
        elif tag in VERIFIED_TAGS:  # 证据是子论文里的原句
            if not e.get("evidence_quote"):
                add(Issue(ERROR, where, f"标为「{tag}」但没有子论文原句"))
            elif ch in c.nodes:
                ok, why = _check_quote(c, "自述", c.nodes[ch], "", e["evidence_quote"])
                if ok is not True:
                    add(Issue(ERROR, where, f"子论文原句核对未通过：{why}"))
        if did:
            children.setdefault(did, set()).add(ch)
    forked = set()
    for i, f in enumerate(forks, start=2):
        where = f"forks.csv 第 {i} 行"
        if f.get("defect_id") not in c.defects:
            add(Issue(ERROR, where, f"缺陷 {f.get('defect_id')!r} 不存在"))
        ids = _split_ids(f.get("child_ids", ""))
        if len(ids) < 2:
            add(Issue(ERROR, where, "分歧点至少要有 2 个子方法"))
        for x in ids:
            if x not in c.nodes:
                add(Issue(ERROR, where, f"子方法 {x!r} 不存在"))
        if not f.get("difference"):
            add(Issue(ERROR, where, "没写各条思路的根本差别"))
        forked.add(f.get("defect_id"))
    for did, chs in children.items():  # 第 3 步第 2 条
        if len(chs) >= 2 and did not in forked:
            add(Issue(WARN, f"缺陷 {did}", f"有 {len(chs)} 个子方法（{'、'.join(sorted(chs))}），考虑是否算一个分歧点"))
    return r


# ---------------------------------------------------------------- 第 4 步

def _method_nodes(c: Ctx, method: str) -> list[str] | None:
    by_name = {n.get("name", ""): nid for nid, n in c.nodes.items()}
    out = []
    for part in [x.strip() for x in method.split(" / ")]:
        nid = part if part in c.nodes else by_name.get(part)
        if not nid:
            return None
        out.append(nid)
    return out


def step4(c: Ctx, *, before_freeze: bool = False) -> StepResult:
    attrs = c.attrs
    r = StepResult(4, bool(attrs))
    if not r.started:
        return r
    add = r.issues.append
    ids = [a.get("id", "") for a in attrs]
    if len(set(ids)) != len(ids) or "" in ids:
        add(Issue(ERROR, "attributes.csv", "属性 id 有空值或重复"))
    for a in attrs:
        for col, label in (("name", "名称"), ("question", "能力问题"), ("check_criterion", "✓ 标准")):
            if not a.get(col):
                add(Issue(ERROR, f"属性 {a.get('id')}", f"缺{label}"))
    lo, hi = (5, 10) if c.p.scale == "mini" else (10, 15)
    if not lo <= len(attrs) <= hi:
        add(Issue(WARN, "attributes.csv", f"模板建议 {lo}–{hi} 个属性，现在 {len(attrs)} 个"))
    for row in schema.read_rows(c.root / "rubric" / "incompatible.csv"):
        for k in ("attr_a", "attr_b"):
            if row.get(k) not in ids:
                add(Issue(ERROR, "incompatible.csv", f"属性 {row.get(k)!r} 不存在"))
    if before_freeze:
        return r

    if "2" not in c.confirmations:
        add(Issue(ERROR, "确认点 2", "还没有确认属性（请你本人在终端运行 python -m researchtool.gapr confirm <项目> 2）"))
    elif c.confirmations["2"].get("by") == confirm.AUTO_BY:
        add(Issue(WARN, "确认点 2", "由 Claude 代为确认（自动模式），未经人工审核"))
    try:
        rubric.verify(c.root)
    except rubric.RubricError as e:
        add(Issue(ERROR, "规则文件", str(e)))
    mpath = c.root / "matrix.csv"
    if not mpath.exists():
        add(Issue(ERROR, "matrix.csv", "还没有矩阵（gapr matrix-template 生成）"))
        return r
    try:
        m = mx.load_matrix(mpath)
    except ValueError as e:
        add(Issue(ERROR, "matrix.csv", str(e)))
        return r
    if m.attrs != ids:
        add(Issue(ERROR, "matrix.csv", f"属性列 {m.attrs} 与冻结的属性 {ids} 不一致"))
    covered: set[str] = set()
    for meth in m.methods:
        ns = _method_nodes(c, meth)
        if ns is None:
            add(Issue(ERROR, "matrix.csv", f"方法 {meth!r} 对不上节点表（合并行用“ / ”分隔节点 id 或名称）"))
        else:
            covered.update(ns)
    missing_nodes = sorted(set(c.nodes) - covered)
    if missing_nodes:
        add(Issue(ERROR, "matrix.csv", f"这些节点没有出现在矩阵里：{'、'.join(missing_nodes)}"))
    ev = {(e["method"], e["attr"]): e for e in c.rows("matrix_evidence.csv") if e.get("evidence")}
    no_ev = [f"{meth}×{a}" for (meth, a), v in m.cells.items() if v == mx.YES and (meth, a) not in ev]
    if no_ev:  # 第 4 步第 3 条：每个 ✓ 都记下依据
        add(Issue(ERROR, "matrix_evidence.csv", f"{len(no_ev)} 个 ✓ 没有依据：{'、'.join(no_ev[:15])}"))
    for (meth, a), e in ev.items():
        try:
            v = mx.parse_cell(e.get("value", "") or "✓")
        except ValueError:
            v = None
        if (meth, a) in m.cells and v != m.cells[(meth, a)]:
            add(Issue(WARN, "matrix_evidence.csv", f"{meth}×{a} 的依据写的是 {e.get('value')!r}，与矩阵不一致"))
    return r


# ---------------------------------------------------------------- 第 5 步

def compute_mechanical(c: Ctx) -> tuple[list[dict], mx.Analysis]:
    """第 5 步的机械规则；gapr gaps 和检查共用，保证结果一致。"""
    attrs = c.attrs
    names = {a["id"]: a["name"] for a in attrs}
    m = mx.load_matrix(c.root / "matrix.csv")
    inc = mx.load_incompatible(c.root / "rubric" / "incompatible.csv")
    a = mx.analyze(m, inc)
    r3 = mx.dangling_defects(c.rows("defects.csv"), c.rows("edges.csv"))
    others = [mx.Candidate(r["rule"], (), [], ref=r["id"], statement=r["statement"])
              for r in c.rows("open_problems.csv") if r.get("rule") in ("R4", "R5")]
    rows = []
    for cand in [*a.r1, *a.r2, *a.r2p, *r3, *others]:
        rows.append({"cand_id": f"C{len(rows) + 1:02d}", "rule": cand.rule, "attrs": "×".join(cand.attrs),
                     "statement": mx.describe(cand, names, c.defects, c.nodes),
                     "partial_coverage": "；".join(cand.partial), "ref": cand.ref})
    return rows, a


def step5(c: Ctx) -> StepResult:
    mech = c.rows("gaps_mechanical.csv")
    gaps = c.rows("gaps.csv")
    filtered = c.rows("filtered.csv")
    r = StepResult(5, bool(mech or gaps))
    if not r.started:
        return r
    add = r.issues.append
    try:
        expect, _ = compute_mechanical(c)
    except ValueError as e:
        add(Issue(ERROR, "matrix.csv", str(e)))
        return r
    key = lambda x: (x["cand_id"], x["rule"], x["attrs"], x["statement"], x["ref"])
    if [key(x) for x in expect] != [key(x) for x in mech]:
        add(Issue(ERROR, "gaps_mechanical.csv", "与当前矩阵、缺陷表算出的结果不一致，重新运行 gapr gaps"))
        return r
    rule_of = {x["cand_id"]: x["rule"] for x in mech}
    partial_of = {x["cand_id"]: x["partial_coverage"] for x in mech}

    # 全程规则 6：每条机械候选要么并进某条空白，要么写进已过滤清单，不能悄悄消失
    used: dict[str, str] = {}
    for src, rows in (("gaps.csv", gaps), ("filtered.csv", filtered)):
        for g in rows:
            for cid in _split_ids(g.get("merged_from", "")):
                if cid not in rule_of:
                    add(Issue(ERROR, f"{src} {g.get('id')}", f"merged_from 里的 {cid} 不是机械候选"))
                elif cid in used:
                    add(Issue(ERROR, f"{src} {g.get('id')}", f"{cid} 已经并入 {used[cid]}，不能重复使用"))
                else:
                    used[cid] = f"{src} {g.get('id')}"
    lost = [cid for cid in rule_of if cid not in used]
    if lost:
        add(Issue(ERROR, "第 5 步", f"这些候选既没并入 gaps.csv 也没写进 filtered.csv：{'、'.join(lost)}"))

    order = ["R1", "R2", "R2′", "R3", "R4", "R5"]
    seen: set[str] = set()
    for g in gaps:
        gid = g.get("id", "")
        where = f"gaps.csv {gid or '无 id'}"
        if not gid or gid in seen:
            add(Issue(ERROR, where, "id 为空或重复"))
        seen.add(gid)
        merged = [x for x in _split_ids(g.get("merged_from", "")) if x in rule_of]
        if not merged:
            add(Issue(ERROR, where, "merged_from 为空"))
            continue
        want = sorted({rule_of[x] for x in merged}, key=order.index)
        got = _split_ids(g.get("rules", "").replace("+", ";"))
        if got != want:  # 来源数直接由合并的候选决定，不能手填
            add(Issue(ERROR, where, f"rules 应为 {';'.join(want)}（由 merged_from 推出），现在是 {g.get('rules')!r}"))
        if not g.get("statement"):
            add(Issue(ERROR, where, "缺问题陈述"))
        if not g.get("demand"):
            add(Issue(ERROR, where, "缺需求（写不出来就移到 filtered.csv）"))
        if not g.get("splice_test"):
            add(Issue(ERROR, where, "缺拼接测试结论"))
        if g.get("category") not in CATEGORIES:
            add(Issue(ERROR, where, f"分类应为：{'｜'.join(sorted(CATEGORIES))}"))
        if any(partial_of[x] for x in merged) and not g.get("partial_coverage"):
            add(Issue(ERROR, where, "合并的候选里有 △ 部分覆盖，partial_coverage 不能为空"))
    for f in filtered:
        if not f.get("stage") or not f.get("reason"):
            add(Issue(ERROR, f"filtered.csv {f.get('id')}", "要写明在哪一步被过滤（stage）和理由（reason）"))
    return r


# ---------------------------------------------------------------- 第 6 步

def _search_coverage(c: Ctx, gid: str) -> dict[str, object]:
    logs = [s for s in c.rows("search_log.csv") if s.get("for_gap") == gid and not s.get("error")]
    phr = {s["query"].strip().lower() for s in logs if s.get("kind") == "phrasing"}
    recent_from = c.ref_date - timedelta(days=365)
    recent = False
    for s in logs:
        if s.get("kind") == "phrasing" and s.get("tool") == "arxiv":
            lo = (s.get("time_filter", "").split("~")[0] or "").strip()
            if lo and _valid_date(lo[:10]) and len(lo) >= 10 and date.fromisoformat(lo[:10]) >= recent_from:
                recent = True
    kinds = {s.get("kind") for s in logs}
    return {"phrasing": len(phr), "recent_arxiv": recent, "forward": "forward" in kinds,
            "cross": "cross" in kinds, "venue": "venue" in kinds, "n": len(logs)}


def _achieved(cov: dict) -> int:
    parts = [cov["phrasing"] >= 3 and cov["recent_arxiv"], cov["forward"], cov["cross"]]
    return 2 if all(parts) else (1 if sum(parts) == 2 else 0)


def step6(c: Ctx) -> StepResult:
    gaps = c.rows("gaps.csv")
    r = StepResult(6, any(g.get("verdict") for g in gaps))
    if not r.started:
        return r
    add = r.issues.append
    if c.p.is_backtest:
        lock = c.confirmations.get("candidates")
        if not lock:
            add(Issue(ERROR, "候选锁定", "回测要先锁定候选再查截止日之后的历史（用 gapr search --post-cutoff 时自动锁定）"))
        elif confirm.snapshot_candidates(c.root) != {k: lock[k] for k in ("gaps_hash", "filtered_hash", "n_gaps")}:
            add(Issue(ERROR, "候选锁定", "锁定之后 gaps.csv / filtered.csv 的候选被改过（看到结果后改候选违反回测规则）"))
    for g in gaps:
        gid = g.get("id", "")
        where = f"gaps.csv {gid}"
        v = g.get("verdict", "")
        if c.p.is_backtest:
            if v not in BACKTEST_VERDICTS:
                add(Issue(ERROR, where, f"判定应为：{'｜'.join(sorted(BACKTEST_VERDICTS))}"))
                continue
            if v != "未命中":
                if not g.get("filled_by"):
                    add(Issue(ERROR, where, "命中 / 部分命中要写填上它的论文（filled_by）"))
                if not re.fullmatch(r"\d+(\.\d+)?", g.get("months_after_cutoff", "")):
                    add(Issue(ERROR, where, "要写截止日到首次公开日的月数（months_after_cutoff）"))
            if not g.get("closest_work"):
                add(Issue(ERROR, where, "缺最接近的工作"))
            if _search_coverage(c, gid)["n"] == 0:
                add(Issue(ERROR, where, "没有为它做过的检索记录（gapr search … --for 编号）"))
            continue

        if v not in SURVEY_VERDICTS:
            add(Issue(ERROR, where, f"判定应为：{'｜'.join(sorted(SURVEY_VERDICTS))}"))
            continue
        if g.get("confidence") not in CONFIDENCE:
            add(Issue(ERROR, where, "置信度应为 高 / 中 / 低"))
            continue
        if not g.get("closest_work"):
            add(Issue(ERROR, where, "缺最接近的工作及其不足"))
        rev = g.get("independent_review", "")
        if not rev:
            add(Issue(ERROR, where, "缺独立复核结果（未复核也要写“未复核”）"))
        elif rev.startswith("找到反例") and v == "确认空白":  # 第 6 步第 6 条：反例优先
            add(Issue(ERROR, where, "独立复核找到了反例，不能判为确认空白"))
        cov = _search_coverage(c, gid)
        if v == "确认空白" and not (cov["phrasing"] >= 3 and cov["recent_arxiv"]):
            # 全程规则 3：没有检索记录，不得声称“尚无人研究”
            add(Issue(ERROR, where, f"判为确认空白，但只有 {cov['phrasing']} 种说法的检索"
                                    f"{'' if cov['recent_arxiv'] else '，且没有覆盖最近 12 个月的 arXiv 检索'}"))
        ach = _achieved(cov)
        if CONFIDENCE[g["confidence"]] > ach:
            name = {v_: k for k, v_ in CONFIDENCE.items()}[ach]
            add(Issue(ERROR, where, f"置信度写的是{g['confidence']}，检索记录只支持{name}"
                                    f"（说法 {cov['phrasing']} 种、最近 12 个月 arXiv {'有' if cov['recent_arxiv'] else '无'}、"
                                    f"正向追踪 {'有' if cov['forward'] else '无'}、跨领域 {'有' if cov['cross'] else '无'}）"))
        if not cov["venue"]:
            add(Issue(WARN, where, "没有查最新会议 / workshop 的记录（kind=venue）"))
    missing = [g.get("id") for g in gaps if not g.get("verdict")]
    if missing:
        add(Issue(ERROR, "第 6 步", f"还有 {len(missing)} 条候选没有判定：{'、'.join(missing)}"))
    return r


# ---------------------------------------------------------------- 第 7 步

def step7(c: Ctx) -> StepResult:
    gaps = c.rows("gaps.csv")
    if c.p.is_backtest:
        return StepResult(7, False)
    r = StepResult(7, any(g.get(k) for g in gaps for k in SCORE_COLS))
    if not r.started:
        return r
    for g in gaps:
        if g.get("verdict") not in ("确认空白", "部分解决"):
            continue
        where = f"gaps.csv {g.get('id')}"
        vals = []
        for k in SCORE_COLS:
            v = g.get(k, "")
            if v not in ("1", "2", "3"):
                r.issues.append(Issue(ERROR, where, f"{k} 应为 1–3，现在是 {v!r}"))
            else:
                vals.append(int(v))
        if len(vals) == len(SCORE_COLS) and g.get("score_total") != str(sum(vals)):
            r.issues.append(Issue(ERROR, where, f"score_total 应为 {sum(vals)}"))
    return r


# ---------------------------------------------------------------- 汇总与关卡

STEPS = [step0, step1, step2, step3, step4, step5, step6, step7]


def run(p: Project, only: int | None = None) -> list[StepResult]:
    c = Ctx(p)
    if only is not None:
        return [STEPS[only](c)]
    return [fn(c) for fn in STEPS]


def require(p: Project, upto: int, *, what: str) -> None:
    """进入下一步前的关卡：第 0..upto 步都必须已开始且没有错误。"""
    c = Ctx(p)
    problems: list[str] = []
    for i in range(upto + 1):
        res = STEPS[i](c)
        if not res.started:
            problems.append(f"第 {i} 步（{STEP_TITLES[i]}）还没做")
        problems += [f"第 {i} 步：{x}" for x in res.errors]
    if problems:
        raise GateError(f"不能{what}，前面的步骤还有 {len(problems)} 个问题：\n  " + "\n  ".join(problems[:40]))


def require_custom(p: Project, results: list[StepResult], *, what: str) -> None:
    problems = []
    for res in results:
        if not res.started:
            problems.append(f"第 {res.step} 步（{STEP_TITLES[res.step]}）还没做")
        problems += [f"第 {res.step} 步：{x}" for x in res.errors]
    if problems:
        raise GateError(f"不能{what}，还有 {len(problems)} 个问题：\n  " + "\n  ".join(problems[:40]))
