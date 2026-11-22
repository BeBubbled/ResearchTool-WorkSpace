"""命令行入口：python -m researchtool.gapr <命令> <项目目录> …

- 所有检索命令都会自动写 search_log.csv；回测项目里自动套用日期防火墙。
- 会推进到下一步的命令（confirm、matrix-template、gaps、越过截止日的检索）
  先跑 checks.require，前面的步骤有错误就拒绝执行。
- confirm 只能由用户本人在交互式终端里运行。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from researchtool.gapr import checks, confirm, fulltext, rubric, schema
from researchtool.gapr import contexts as ctx_mod
from researchtool.gapr import project as prj
from researchtool.gapr.evidence import Evidence
from researchtool.gapr.net import FetchError, Http
from researchtool.gapr.paper import Paper, after_cutoff, apply_cutoff
from researchtool.gapr.sources import arxiv, openalex, openreview, s2

# 非 arXiv 来源的 publicationDate 可能晚于 arXiv v1（会议日期），
# 服务端先放宽这么多天，取回后再按 arXiv v1 日期精确过滤。
LOOSE_DAYS = 548


# ---------------------------------------------------------------- 工具函数

def _date(s: str | None) -> date | None:
    return date.fromisoformat(s) if s else None


def _http(p: prj.Project, args) -> Http:
    return Http(p.cache_dir, refresh=getattr(args, "refresh", False))


def _log(p: prj.Project, **row) -> None:
    row.setdefault("ts", datetime.now().isoformat(timespec="seconds"))
    schema.append_row(p.path("search_log.csv"), schema.TABLES["search_log.csv"], row)


def _firewall(http: Http, papers: list[Paper], cutoff: date | None) -> tuple[list[Paper], list[Paper]]:
    """回测：先把可能被误判的论文换成 arXiv v1 日期，再按截止日过滤。"""
    if cutoff is None:
        return papers, []
    need = [x for x in papers if x.arxiv_id and x.date_basis != "arXiv v1"
            and after_cutoff(x, cutoff) is not False]
    _resolve(http, need)
    return apply_cutoff(papers, cutoff)


def _resolve(http: Http, papers: list[Paper]) -> None:
    """补 arXiv v1 日期；arXiv 不可用时不让整个检索失败，只打标记并警告。
    回测里这样做偏保守：按较晚的发表日期过滤，只会多删、不会泄漏截止日后的论文。"""
    if not papers:
        return
    try:
        arxiv.resolve_dates(http, papers)
    except FetchError as e:
        for x in papers:
            if x.date_basis != "arXiv v1":
                x.flags.append("arXiv 日期未核对")
        print(f"⚠ 补 arXiv v1 日期失败（{e.message}），这些论文暂用发表日期。", file=sys.stderr)


def _fmt(x: Paper) -> str:
    basis = {"arXiv v1": "v1", "publicationDate": "pub", "year": "年"}.get(x.date_basis, "?")
    cites = "" if x.citation_count is None else f" | 被引 {x.citation_count}"
    flags = f" | ⚠{'、'.join(x.flags)}" if x.flags else ""
    venue = f" | {x.venue}" if x.venue else ""
    return f"{x.first_public or '????'}[{basis}] | {x.key} | {x.title}{venue}{cites}{flags}"


def _gap_ids(p: prj.Project) -> set[str]:
    return {g["id"] for g in schema.read_rows(p.path("gaps.csv")) if g.get("id")}


def _check_for_gap(p: prj.Project, args) -> None:
    if getattr(args, "for_gap", None) and args.for_gap not in _gap_ids(p):
        raise ValueError(f"--for {args.for_gap}：gaps.csv 里没有这条候选")


# ---------------------------------------------------------------- 项目与关卡

def cmd_init(args) -> int:
    p = prj.init(args.dir, domain=args.domain, mode=args.mode, start=args.start or "",
                 cutoff=args.cutoff or "", scale=args.scale, audience=args.audience)
    print(f"已创建项目 {p.root}（{'回测，截止 ' + str(p.cutoff) if p.is_backtest else '正式调研'}）")
    print("下一步：检查 project.toml，然后开始第 1 步。")
    return 0


def cmd_confirm(args) -> int:
    p = prj.load(args.dir)
    c = checks.Ctx(p)
    if args.point == "1":
        checks.require_custom(p, [checks.step0(c), checks.step1(c, include_confirm=False)], what="确认第 1 步")
        rec = confirm.confirm_1(p.root, note=args.note or "")
        print(f"已记录确认点 1：{len(rec['nodes'])} 个节点、{rec['n_terms']} 条术语。")
    else:
        if args.change_rules and not args.reason:
            raise ValueError("--change-rules 必须同时写 --reason")
        checks.require(p, 3, what="确认属性")
        checks.require_custom(p, [checks.step4(c, before_freeze=True)], what="确认属性")
        rec = confirm.confirm_2(p.root, note=args.note or "",
                                change_reason=args.reason if args.change_rules else "")
        print(f"已记录确认点 2，并冻结 {len(rec['rubric_files'])} 个规则文件。")
    return 0


def cmd_check(args) -> int:
    p = prj.load(args.dir)
    results = checks.run(p, args.step)
    bad = 0
    for res in results:
        title = f"第 {res.step} 步 {checks.STEP_TITLES[res.step]}"
        if not res.started:
            print(f"— {title}：未开始")
            continue
        state = "通过" if res.passed else f"未通过（{len(res.errors)} 个错误）"
        print(f"{'✓' if res.passed else '✗'} {title}：{state}")
        for i in res.issues:
            if i.level == checks.ERROR or args.verbose or res.passed:
                print(f"    {i}")
        bad += len(res.errors)
    return 1 if bad else 0


def cmd_matrix_template(args) -> int:
    p = prj.load(args.dir)
    checks.require(p, 3, what="生成矩阵")
    if "2" not in confirm.load(p.root):
        raise checks.GateError("确认点 2 还没确认，不能填矩阵")
    rubric.verify(p.root)
    out = p.path("matrix.csv")
    from researchtool.gapr.matrix import matrix_filled_cells
    if out.exists() and matrix_filled_cells(out):
        print("matrix.csv 已有内容，不覆盖。", file=sys.stderr)
        return 1
    attrs = [r["id"] for r in schema.read_rows(p.rubric_dir / "attributes.csv")]
    nodes = [r["id"] for r in schema.read_rows(p.path("nodes.csv")) if r.get("id")]
    schema.write_rows(out, ["method", *attrs], [{"method": n} for n in nodes])
    print(f"已生成 matrix.csv：{len(nodes)} 个方法 × {len(attrs)} 个属性，格子留空待填。")
    return 0


def cmd_gaps(args) -> int:
    p = prj.load(args.dir)
    checks.require(p, 4, what="列出候选空白")
    c = checks.Ctx(p)
    rows, a = checks.compute_mechanical(c)
    schema.write_rows(p.path("gaps_mechanical.csv"), schema.MECHANICAL_COLUMNS, rows)
    print("# 机械规则得出的候选空白\n")
    print("下表每一行都是按规则在表上找到的空格，还没经过合并去重、需求过滤和拼接测试。")
    print("△ 不算覆盖；“部分覆盖”一列列出有 △ 的方法。\n")
    print("| 编号 | 规则 | 候选 | 部分覆盖 |\n|---|---|---|---|")
    for r in rows:
        print(f"| {r['cand_id']} | {r['rule']} | {r['statement']} | {r['partial_coverage'] or '—'} |")
    print(f"\n有 ✓ 的属性：{'、'.join(a.supported) or '无'}")
    if a.skipped_incompatible:
        print("按相容性表跳过：" + "、".join("×".join(t) for t in a.skipped_incompatible))
    forced = rubric.forced_changes(p.root)
    if forced:
        print(f"⚠ 规则文件被强制改过 {len(forced)} 次：" + "；".join(h["reason"] for h in forced))
    print(f"\n已写入 {p.path('gaps_mechanical.csv')}")
    print("下一步：合并去重、需求过滤、拼接测试，写进 gaps.csv（merged_from 写全候选编号）和 filtered.csv。")
    return 0


# ---------------------------------------------------------------- 检索

def cmd_search(args) -> int:
    p = prj.load(args.dir)
    _check_for_gap(p, args)
    http = _http(p, args)
    cutoff = p.firewall
    dfrom, dto = _date(args.date_from), _date(args.date_to)
    if args.post_cutoff:
        # 回测第 6 步：先锁定候选，再看截止日之后的历史
        if not p.is_backtest:
            raise ValueError("--post-cutoff 只用于回测项目")
        checks.require(p, 5, what="查看截止日之后的论文")
        confirm.lock_candidates(p.root)
        dfrom = dfrom or (p.cutoff + timedelta(days=1))
        cutoff = None
    elif cutoff and (dto is None or dto > cutoff):
        dto = cutoff
    tf = f"{dfrom or ''}~{dto or ''}" + (f"（回测截止 {cutoff}）" if cutoff else "")
    extra = {"for_gap": args.for_gap or "", "kind": args.kind or ""}
    try:
        if args.tool == "openreview":
            hits, total, cached = openreview.search(http, args.query, limit=args.limit)
            _log(p, query=args.query, tool="openreview", time_filter="—", n_results=total,
                 n_dropped_cutoff=0, result_ids=";".join(h["forum"] for h in hits), from_cache=cached, **extra)
            for h in hits:
                print(f"{h['forum']} | {h['title']} | {h['venue']} | {h['url']}")
            if cutoff:
                print("⚠ OpenReview 检索结果没有可靠的首次公开日期，回测时需逐篇核对。")
            return 0
        if args.tool == "arxiv":
            papers, total, cached = arxiv.search(http, args.query, limit=args.limit, date_from=dfrom,
                                                 date_to=dto, sort="submittedDate" if args.sort == "date" else "relevance")
        else:
            loose = dto + timedelta(days=LOOSE_DAYS) if cutoff and dto else dto
            fn = s2.search if args.tool == "s2" else openalex.search
            # 回测时服务端日期放宽了，过滤后会少很多，先多取一些
            fetch_n = min(args.limit * 3, 100) if cutoff else args.limit
            papers, total, cached = fn(http, args.query, limit=fetch_n, date_from=dfrom, date_to=loose)
        if cutoff:
            kept, dropped = _firewall(http, papers, cutoff)
            kept = kept[: args.limit]
        else:
            if args.tool != "arxiv":
                _resolve(http, papers)
            kept, dropped = papers, []
    except FetchError as e:
        _log(p, query=args.query, tool=args.tool, time_filter=tf, error=e.message, **extra)
        print(f"检索失败（已记入检索记录）：{e.message}", file=sys.stderr)
        return 2
    _log(p, query=args.query, tool=args.tool, time_filter=tf, n_results=total,
         n_dropped_cutoff=len(dropped), result_ids=";".join(x.key for x in kept), from_cache=cached, **extra)
    if args.json:
        print(json.dumps([x.to_dict() for x in kept], ensure_ascii=False, indent=1))
    else:
        print(f"共 {total} 条，返回 {len(papers)} 条" + (f"，截止日后过滤掉 {len(dropped)} 条" if cutoff else ""))
        for x in kept:
            print(_fmt(x))
    return 0


def cmd_paper(args) -> int:
    p = prj.load(args.dir)
    http = _http(p, args)
    try:
        x = s2.paper(http, args.id)
    except FetchError as e:
        if args.id.lower().startswith("arxiv:"):
            got = arxiv.by_ids(http, [args.id])
            if not got:
                raise
            x = got[0]
            print(f"（S2 不可用，改用 arXiv：{e.message}）", file=sys.stderr)
        else:
            raise
    _resolve(http, [x])
    if p.firewall and after_cutoff(x, p.firewall):
        print(f"⚠ 这篇论文首次公开于 {x.first_public}，晚于回测截止日 {p.firewall}，回测中不得使用。")
    print(json.dumps(x.to_dict(), ensure_ascii=False, indent=1) if args.json else _fmt(x))
    if not args.json and x.abstract:
        print("\n摘要：" + x.abstract)
    return 0


def _source(args) -> str:
    """引用数据来源：auto = 有 S2 key 用 S2，否则 OpenAlex。"""
    if args.source != "auto":
        return args.source
    return "s2" if s2.has_key() else "openalex"


def _post_cutoff_gate(p: prj.Project, args) -> date | None:
    """返回这次要用的截止日；--post-cutoff 时先过第 0–5 步关卡并锁定候选。"""
    if not getattr(args, "post_cutoff", False):
        return p.firewall
    if not p.is_backtest:
        raise ValueError("--post-cutoff 只用于回测项目")
    checks.require(p, 5, what="查看截止日之后的论文")
    confirm.lock_candidates(p.root)
    return None


def _cmd_links(args, kind: str) -> int:
    p = prj.load(args.dir)
    _check_for_gap(p, args)
    cutoff = _post_cutoff_gate(p, args)
    http = _http(p, args)
    src = _source(args)
    if args.critique and src != "s2":
        raise ValueError("OpenAlex 不提供引用句；用 gapr contexts 从引用者全文里抽，或设置 S2_API_KEY")
    mod = s2 if src == "s2" else openalex
    fn = mod.citations if kind == "citations" else mod.references
    extra = {"for_gap": args.for_gap or "", "kind": "forward" if args.for_gap and kind == "citations" else ""}
    try:
        items, cached = fn(http, args.id, max_items=args.max)
    except FetchError as e:
        _log(p, query=f"{kind}:{args.id}", tool=src, error=e.message, **extra)
        print(f"失败（已记入检索记录）：{e.message}", file=sys.stderr)
        return 2
    kept_papers, dropped = _firewall(http, [i["paper"] for i in items], cutoff)
    keep_keys = {x.key for x in kept_papers}
    items = [i for i in items if i["paper"].key in keep_keys]
    if args.critique:
        for i in items:
            i["contexts"] = s2.critique_contexts(i["contexts"], p.critique_keywords)
        items = [i for i in items if i["contexts"]]
    items.sort(key=lambda i: -(i["paper"].citation_count or 0))
    n_ctx, trimmed = 0, []
    for i in items:
        if args.critique and n_ctx >= p.max_contexts_per_paper:
            break
        n_ctx += len(i["contexts"])
        trimmed.append(i)
    tf = f"回测截止 {cutoff}" if cutoff else ("截止日之后" if args.post_cutoff else "—")
    _log(p, query=f"{kind}:{args.id}" + ("（批评句）" if args.critique else ""), tool=src,
         time_filter=tf, n_results=len(items), n_dropped_cutoff=len(dropped),
         result_ids=";".join(i["paper"].key for i in trimmed[:50]), from_cache=cached, **extra)
    data = [{"paper": i["paper"].to_dict(), "contexts": i["contexts"], "intents": i["intents"],
             "influential": i["influential"]} for i in trimmed]
    name = args.id + f".{src}" + (".critique" if args.critique else "") + (".post" if args.post_cutoff else "")
    path = Evidence(p.root).save(kind, name, data, source_url=f"{src}:{kind}:{args.id}",
                                 method="s2-graph" if src == "s2" else "openalex")
    print(f"[{src}] {len(trimmed)} 篇" + (f"，{n_ctx} 条批评句" if args.critique else "")
          + (f"，截止日后过滤掉 {len(dropped)} 篇" if cutoff else "") + f"；完整结果存于 {path}")
    for i in trimmed[: args.show]:
        print("\n" + _fmt(i["paper"]))
        for ctx in i["contexts"][:3]:
            print(f"   “{ctx.strip()}”")
    return 0


def cmd_contexts(args) -> int:
    """他人指出：取引用者全文，抽出引用这篇论文的句子（不需要 S2 key）。"""
    p = prj.load(args.dir)
    _check_for_gap(p, args)
    cutoff = _post_cutoff_gate(p, args)
    http = _http(p, args)
    src = _source(args)
    extra = {"for_gap": args.for_gap or "", "kind": "forward" if args.for_gap else ""}
    try:
        try:
            cited, _ = openalex.work(http, args.id)
        except (FetchError, ValueError):
            cited = s2.paper(http, args.id)
        items, cached = (s2 if src == "s2" else openalex).citations(http, args.id, max_items=args.pool)
    except FetchError as e:
        _log(p, query=f"contexts:{args.id}", tool=f"{src}+fulltext", error=e.message, **extra)
        print(f"失败（已记入检索记录）：{e.message}", file=sys.stderr)
        return 2
    kept, dropped = _firewall(http, [i["paper"] for i in items], cutoff)
    fetchable = [x for x in kept if x.arxiv_id or x.acl_id]
    fetchable.sort(key=lambda x: -(x.citation_count or 0))
    chosen = fetchable[: args.top]
    print(f"被引论文：{_fmt(cited)}", file=sys.stderr)
    print(f"引用者 {len(items)} 篇" + (f"，截止日后过滤掉 {len(dropped)} 篇" if cutoff else "")
          + f"，有 arXiv/ACL 全文来源的 {len(fetchable)} 篇，取被引最多的 {len(chosen)} 篇", file=sys.stderr)

    store = fulltext.FulltextStore(p.root)
    results = []
    for n, x in enumerate(chosen, 1):
        r = ctx_mod.extract(http, store, x, cited, args.alias or [])
        crit = s2.critique_contexts([c["text"] for c in r.contexts], p.critique_keywords)
        results.append((r, crit))
        print(f"  [{n}/{len(chosen)}] {x.key}：{len(r.contexts)} 句，批评 {len(crit)} 句"
              + (f"（{r.source}）" if r.source else "") + (f"；{r.note}" if r.note else ""), file=sys.stderr)

    data = [{"paper": r.citing.to_dict(), "contexts": [c["text"] for c in r.contexts],
             "methods": [c["method"] for c in r.contexts], "critique": crit,
             "fulltext_source": r.source, "note": r.note} for r, crit in results]
    name = args.id + ".fulltext-contexts" + (".post" if args.post_cutoff else "")
    path = Evidence(p.root).save("citations", name, data, source_url=f"{src}:cites:{args.id}+fulltext",
                                 method="fulltext-contexts")
    n_ctx = sum(len(r.contexts) for r, _ in results)
    n_crit = sum(len(c) for _, c in results)
    n_none = sum(1 for r, _ in results if not r.contexts)
    tf = f"回测截止 {cutoff}" if cutoff else ("截止日之后" if args.post_cutoff else "—")
    _log(p, query=f"contexts:{args.id}", tool=f"{src}+fulltext", time_filter=tf, n_results=len(items),
         n_dropped_cutoff=len(dropped), result_ids=";".join(x.key for x in chosen), from_cache=cached, **extra)
    print(f"{len(chosen)} 篇引用者，抽到 {n_ctx} 句引用（{n_none} 篇没抽到），其中含批评词 {n_crit} 句；存于 {path}")
    for r, crit in results:
        for c in crit[: args.show]:
            print(f"\n{r.citing.key}（{r.citing.first_public}）\n   “{c}”")
    return 0


def cmd_reviews(args) -> int:
    p = prj.load(args.dir)
    http = _http(p, args)
    try:
        revs, cached = openreview.reviews(http, args.forum)
    except FetchError as e:
        _log(p, query=f"reviews:{args.forum}", tool="openreview", error=e.message)
        print(f"失败（已记入检索记录）：{e.message}\n论坛页：https://openreview.net/forum?id={args.forum}",
              file=sys.stderr)
        return 2
    _log(p, query=f"reviews:{args.forum}", tool="openreview", time_filter="—",
         n_results=len(revs), n_dropped_cutoff=0, from_cache=cached)
    path = Evidence(p.root).save("reviews", args.forum, revs,
                                 source_url=f"https://openreview.net/forum?id={args.forum}", method="openreview-api")
    print(f"{len(revs)} 条审稿意见，存于 {path}")
    for r in revs:
        print(f"\n[{r['rating']}] {r['url']}")
        for k, v in r["fields"].items():
            print(f"  {k}: {v[:600]}")
    return 0


# ---------------------------------------------------------------- 全文与原句

def cmd_fulltext(args) -> int:
    p = prj.load(args.dir)
    store = fulltext.FulltextStore(p.root)
    if args.import_file:
        if not args.url:
            raise ValueError("--import 必须同时写 --url（全文出处），报告里会列为手动导入")
        text = Path(args.import_file).read_text(encoding="utf-8", errors="replace")
        path = store.add(args.key, text, args.url, "manual")
        print(f"已手动导入 {store.canonical(args.key)}（{len(text)} 字符）→ {path}")
        return 0
    if store.rel(args.key) and not args.refresh:
        print(f"{store.canonical(args.key)} 已有全文（{store.method(args.key)}）")
        return 0
    try:
        text, url, method = fulltext.fetch(_http(p, args), args.key)
    except FetchError as e:
        print(f"获取失败：{e.message}", file=sys.stderr)
        return 2
    path = store.add(args.key, text, url, method)
    print(f"已保存 {store.canonical(args.key)} 的全文（{method}，{len(text)} 字符）→ {path}")
    return 0


def cmd_quote(args) -> int:
    p = prj.load(args.dir)
    if args.key in ("citations", "reviews"):
        ok, why = fulltext.check_in_pool(fulltext.context_pool(p.root, args.key), args.text)
    else:
        ok, why = fulltext.FulltextStore(p.root).check_quote(args.key, args.text)
    if ok:
        print("找到" + (f"（{why}）" if why else ""))
        return 0
    print(("没有可用的证据：" if ok is None else "没找到：") + why)
    return 1


# ---------------------------------------------------------------- 解析器

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="gapr", description="研究空白调研的确定性工具")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="新建调研项目")
    s.add_argument("dir")
    s.add_argument("--domain", default="")
    s.add_argument("--mode", choices=["survey", "backtest"], default="survey")
    s.add_argument("--start", default="")
    s.add_argument("--cutoff", default="")
    s.add_argument("--scale", choices=["mini", "quick", "full"], default="quick")
    s.add_argument("--audience", default="")
    s.set_defaults(fn=cmd_init)

    s = sub.add_parser("confirm", help="确认点（只能由用户本人在终端运行）")
    s.add_argument("dir")
    s.add_argument("point", choices=["1", "2"])
    s.add_argument("--note", default="")
    s.add_argument("--change-rules", action="store_true", help="冻结后修改规则（会写进报告）")
    s.add_argument("--reason", default="")
    s.set_defaults(fn=cmd_confirm)

    s = sub.add_parser("check", help="逐步检查产出")
    s.add_argument("dir")
    s.add_argument("--step", type=int, choices=range(8))
    s.add_argument("-v", "--verbose", action="store_true", help="也显示提醒")
    s.set_defaults(fn=cmd_check)

    s = sub.add_parser("matrix-template", help="按属性和节点生成空的 matrix.csv（需已过确认点 2）")
    s.add_argument("dir")
    s.set_defaults(fn=cmd_matrix_template)

    s = sub.add_parser("gaps", help="第 5 步：机械列出候选空白（需第 0–4 步通过）")
    s.add_argument("dir")
    s.set_defaults(fn=cmd_gaps)

    def net_opts(x):
        x.add_argument("--refresh", action="store_true", help="忽略缓存重新请求")
        x.add_argument("--json", action="store_true")

    s = sub.add_parser("search", help="检索论文（自动记入检索记录）")
    s.add_argument("dir")
    s.add_argument("query")
    s.add_argument("--tool", choices=["arxiv", "s2", "openalex", "openreview"], default="s2")
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--from", dest="date_from")
    s.add_argument("--to", dest="date_to")
    s.add_argument("--sort", choices=["relevance", "date"], default="relevance", help="仅 arXiv")
    s.add_argument("--for", dest="for_gap", help="第 6 步：为哪条候选空白检索（gaps.csv 的 id）")
    s.add_argument("--kind", choices=sorted(checks.SEARCH_KINDS), help="第 6 步检索类型")
    s.add_argument("--post-cutoff", action="store_true", help="回测第 6 步：查截止日之后的论文（会锁定候选）")
    net_opts(s)
    s.set_defaults(fn=cmd_search)

    s = sub.add_parser("paper", help="查单篇论文（S2 id、arXiv:…、ACL:…、DOI:…）")
    s.add_argument("dir")
    s.add_argument("id")
    net_opts(s)
    s.set_defaults(fn=cmd_paper)

    for name, helptext in (("citations", "正向追踪：谁引用了它"), ("references", "向前补漏：它引用了谁")):
        s = sub.add_parser(name, help=helptext)
        s.add_argument("dir")
        s.add_argument("id")
        s.add_argument("--critique", action="store_true", help="只保留含批评词的引用句")
        s.add_argument("--max", type=int, default=2000)
        s.add_argument("--show", type=int, default=15)
        s.add_argument("--for", dest="for_gap", help="第 6 步：为哪条候选空白做正向追踪")
        s.add_argument("--post-cutoff", action="store_true", help="回测第 6 步：不过滤截止日之后的论文")
        s.add_argument("--source", choices=["auto", "s2", "openalex"], default="auto",
                       help="auto：有 S2_API_KEY 用 S2，否则 OpenAlex")
        net_opts(s)
        s.set_defaults(fn=lambda a, k=name: _cmd_links(a, k))

    s = sub.add_parser("contexts", help="他人指出：从引用者全文里抽引用这篇论文的句子（不需要 S2 key）")
    s.add_argument("dir")
    s.add_argument("id", help="被引论文：arXiv:… / ACL:… / DOI:…")
    s.add_argument("--top", type=int, default=30, help="取被引最多的多少篇引用者")
    s.add_argument("--pool", type=int, default=600, help="最多取多少篇引用者来挑选")
    s.add_argument("--alias", action="append", help="方法名，如 PatchTST；可重复")
    s.add_argument("--show", type=int, default=3, help="每篇显示几句批评句")
    s.add_argument("--for", dest="for_gap", help="第 6 步：为哪条候选空白做正向追踪")
    s.add_argument("--post-cutoff", action="store_true", help="回测第 6 步：不过滤截止日之后的论文")
    s.add_argument("--source", choices=["auto", "s2", "openalex"], default="auto")
    s.add_argument("--refresh", action="store_true")
    s.set_defaults(fn=cmd_contexts)

    s = sub.add_parser("reviews", help="OpenReview 审稿意见")
    s.add_argument("dir")
    s.add_argument("forum")
    net_opts(s)
    s.set_defaults(fn=cmd_reviews)

    s = sub.add_parser("fulltext", help="下载并登记论文全文（arXiv:… / ACL:…），或 --import 手动导入")
    s.add_argument("dir")
    s.add_argument("key")
    s.add_argument("--import", dest="import_file")
    s.add_argument("--url", default="")
    s.add_argument("--refresh", action="store_true")
    s.set_defaults(fn=cmd_fulltext)

    s = sub.add_parser("quote", help="核对原句：key 为论文（arXiv:…）或 citations / reviews")
    s.add_argument("dir")
    s.add_argument("key")
    s.add_argument("text")
    s.set_defaults(fn=cmd_quote)
    return ap


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except (checks.GateError, confirm.ConfirmError, rubric.RubricError,
            FileNotFoundError, FileExistsError, ValueError, RuntimeError) as e:
        print(f"错误：{e}", file=sys.stderr)
        return 1
    except FetchError as e:
        print(f"网络请求失败：{e.message}", file=sys.stderr)
        return 2
