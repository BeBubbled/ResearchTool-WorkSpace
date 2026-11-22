"""演化树调研 · 自动模式：由 Claude Code 按 docs/gap-research.md 完成全部步骤，包括原本要人做的选择和确认。

流程（每轮都从头找第一个还没完成的阶段，所以中断后可以接着跑）：

    step0 → step1 → confirm1 → step2 → step3 → attrs → confirm2 → matrix → step5 → step6 → review6 → step7 → report

- 普通阶段：开一个 Claude Code 会话（工作目录 = 项目目录），它用 `gapr` 命令检索、下载全文、核对原句，
  直接写 CSV。阶段结束后由面板跑 gapr 的检查；有错误就把错误发回同一个会话让它修，最多 MAX_ROUNDS 轮。
- 确认点：另开一个没看过推导过程的 Claude（不带工具），只按规则审核清单。审核通过后由面板调用
  gapr.confirm.record_auto 写入“由 Claude 代确认”的记录；不通过就把意见交回负责的会话修改。
- 第 6 步的独立复核（review6）同样是一个全新的会话，只负责找反例。
- 每个决定和理由由 Claude 追加写进项目里的 decisions.md，最后一步生成 report.md。

安全边界：会话只能改项目目录里的文件；Bash 只能运行 `gapr`（不含 `gapr confirm`）；自动模式的开关存在
面板的运行目录里（项目目录之外），会话改不到。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
import json
import os
from pathlib import Path
import re
import sys
import threading
import time
from typing import Any, Callable
import uuid

MAX_ROUNDS = 3           # 一个阶段：首轮 + 最多两轮按检查结果修改
MAX_REVIEW_ROUNDS = 3    # 一个确认点：最多审核几次
MAX_PHASE_RUNS = 40      # 一次自动运行里最多进入多少个阶段（防止来回打转）
MAX_LOG = 600
AGENT_TIMEOUT = 3 * 3600

# dontAsk 模式下只有这些规则放行的操作能执行；路径相对于会话的工作目录（项目目录）。
# 用真实 CLI 验证过：项目目录外的读写、python、curl、gapr confirm 都会被拒绝。
ALLOWED_BASH = ["Bash(gapr:*)", "Write(./**)", "Edit(./**)"]
DENIED_BASH = ["Bash(gapr confirm:*)"]


class Stopped(Exception):
    pass


@dataclass(frozen=True)
class Phase:
    key: str
    title: str
    kind: str = "agent"   # agent / review
    step: int = 0         # 对应 gapr 的第几步（用于检查和反馈）
    survey_only: bool = False


PHASES = [
    Phase("step0", "第 0 步 · 补全输入", step=0),
    Phase("step1", "第 1 步 · 综述与节点清单", step=1),
    Phase("confirm1", "确认点 1 · Claude 审核节点清单", kind="review", step=1),
    Phase("step2", "第 2 步 · 方法节点表", step=2),
    Phase("step3", "第 3 步 · 演化边与分歧点", step=3),
    Phase("attrs", "第 4 步 · 归纳属性", step=4),
    Phase("confirm2", "确认点 2 · Claude 审核属性", kind="review", step=4),
    Phase("matrix", "第 4 步 · 填属性矩阵", step=4),
    Phase("step5", "第 5 步 · 列出候选空白", step=5),
    Phase("step6", "第 6 步 · 逐条验证", step=6),
    Phase("review6", "第 6 步 · 独立复核（找反例）", step=6, survey_only=True),
    Phase("step7", "第 7 步 · 排序", step=7, survey_only=True),
    Phase("report", "生成报告", step=7),
]
PHASE_BY_KEY = {p.key: p for p in PHASES}
PENDING_REVIEW = "待复核"


# ------------------------------------------------------------------ 判断阶段是否完成

def _errors(res) -> list[str]:
    return [str(i) for i in res.errors]


def phase_status(root: Path, state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """每个阶段：done（是否完成）、errors（相关检查的错误，给会话做反馈）。"""
    from researchtool.gapr import checks, project as prj
    p = prj.load(root)
    c = checks.Ctx(p)
    s0 = checks.step0(c)
    s1_pre = checks.step1(c, include_confirm=False)
    s1 = checks.step1(c)
    s2, s3 = checks.step2(c), checks.step3(c)
    s4_pre = checks.step4(c, before_freeze=True)
    s4 = checks.step4(c)
    s5, s6, s7 = checks.step5(c), checks.step6(c), checks.step7(c)
    conf = c.confirmations
    gaps = c.rows("gaps.csv")
    s6_errors = [e for e in _errors(s6) if "缺独立复核结果" not in e]
    reviewed = bool(gaps) and all(g.get("independent_review", "") not in ("", PENDING_REVIEW) for g in gaps)
    report = root / "report.md"
    out = {
        "step0": (s0.passed, _errors(s0)),
        "step1": (s1_pre.passed, _errors(s1_pre)),
        "confirm1": ("1" in conf and s1.passed, _errors(s1)),
        "step2": (s2.passed, _errors(s2)),
        "step3": (s3.passed, _errors(s3)),
        "attrs": (s4_pre.passed, _errors(s4_pre)),
        "confirm2": ("2" in conf, []),
        "matrix": (s4.passed, _errors(s4)),
        "step5": (s5.passed, _errors(s5)),
        "step6": (s6.started and not s6_errors, s6_errors),
        "review6": (reviewed or p.is_backtest, []),
        "step7": (p.is_backtest or s7.passed, _errors(s7)),
        "report": (report.is_file() and report.stat().st_size > 400 and bool(state.get("reportDone")), []),
    }
    return {k: {"done": bool(v[0]), "errors": v[1]} for k, v in out.items()}


def next_phase(root: Path, state: dict[str, Any]) -> tuple[Phase | None, dict[str, dict[str, Any]]]:
    status = phase_status(root, state)
    for phase in PHASES:
        if not status[phase.key]["done"]:
            return phase, status
    return None, status


# ------------------------------------------------------------------ 提示词

def _doc_section(doc: str, start: str, stops: tuple[str, ...] = ("\n### ", "\n## ", "\n---")) -> str:
    i = doc.find(start)
    if i < 0:
        return ""
    ends = [j for j in (doc.find(s, i + len(start)) for s in stops) if j > 0]
    return doc[i:min(ends) if ends else len(doc)].strip()


def _tables_doc() -> str:
    from researchtool.gapr import schema
    lines = [f"- {name}：{', '.join(cols)}" for name, cols in schema.TABLES.items()]
    lines += [f"- rubric/{name}：{', '.join(cols)}" for name, cols in schema.RUBRIC_TABLES.items()]
    lines.append("- matrix.csv：第一列 method，其余每列一个属性 id；格子填 ✓ / △ / ✗（matrix-template 生成）")
    lines += [f"  · {col}：{text}" for col, text in schema.COLUMN_DOCS.items()]
    return "\n".join(lines)


PHASE_TASKS = {
    "step0": """补全第 0 步的输入：project.toml 里 [project] 一节缺的值（start 起始时间、audience 读者等）由你按领域推断后填上，
只改这些值本身，不要改 domain 和 mode，不要动注释和其他设置。推断的理由写进 decisions.md。""",
    "step1": """完成第 1 步：
- 先用 `gapr search . "<领域英文关键词> survey" --tool s2`（或 openalex / arxiv）找综述，检索词里要有 survey / review / taxonomy；按引用数挑 3–5 篇。
- 写 families.csv（方法族｜代表论文｜来自哪篇综述）和 nodes.csv。节点 id 用简短的小写英文；每个节点要有 name、family、
  first_public（YYYY-MM-DD，优先用 `gapr paper . arXiv:<id>` 查到的 arXiv v1 日期）、date_basis、url 或 arxiv_id、source_tag。
  节点数按规模（mini 8–12、quick 15–20、full 30–60），每个方法族至少 2 个节点。
- 写 neighbor_terms.csv（邻近领域术语对照表，不能为空）。
- 综述 future work 里的开放问题写进 open_problems.csv（rule 填 R5）。
- 选哪些综述、为什么选这些节点、术语对照的依据，写进 decisions.md。""",
    "step2": """完成第 2 步：给 nodes.csv 每个节点补 problem（要解决的问题）、core_idea（核心思路）、key_assumptions、improved_by、credibility，
并在 defects.csv 里给每个节点至少写一条遗留缺陷（kind：自述 / 他人指出 / 审稿）。
- 标「论文原文」的缺陷，quote 必须是原文句子：先 `gapr fulltext . arXiv:<id>` 下载全文，再用 `gapr quote . arXiv:<id> "<原句>"` 核对通过后才写。
- 他人指出：用 `gapr contexts . arXiv:<id> --top 10 --alias <方法名>` 抽引用句，quote 写抽到的原句，source_ref 写 citations。
- 拿不到原文的，source_tag 标「已有知识」，quote 可以留空。宁可标「已有知识」，也不要编造原句。""",
    "step3": """完成第 3 步：写 edges.csv（父节点、对应的缺陷 id、子节点、子论文里的证据原句和位置）；
同一个缺陷有两个以上思路不同的子方法时写进 forks.csv；结论矛盾的论文对、长期停滞的基准等写进 open_problems.csv（rule 填 R4）。""",
    "attrs": """完成第 4 步的前半部分：把 defects.csv 里反复出现的缺陷改写成“能 / 不能”的能力问题，写进 rubric/attributes.csv
（mini 规模 5–10 个，其他 10–15 个；每个要有 id、name、question、check_criterion，可选 partial_criterion、derived_from），
相容性预检的结果写进 rubric/incompatible.csv。这一阶段不要生成或填写 matrix.csv。属性怎么归纳的写进 decisions.md。""",
    "matrix": """完成第 4 步的后半部分：属性已经冻结，不要改 rubric/ 下的任何文件。
先运行 `gapr matrix-template .` 生成空矩阵，再逐格填 ✓ / △ / ✗（论文把这件事当目标并用实验证明的给 ✓；有相关设计但不是主要目标的给 △；其余 ✗；拿不准给 △）。
每个 ✓ 都要在 matrix_evidence.csv 里写一行依据（论文的哪一节、哪个实验）。""",
    "step5": """完成第 5 步：先运行 `gapr gaps .` 生成 gaps_mechanical.csv（不要手改它）。
然后合并去重，写 gaps.csv（id 用 G01、G02…；merged_from 写全合并进来的候选编号；rules 由 merged_from 推出；
partial_coverage、demand、splice_test、category 都要写），没进入 gaps.csv 的每条机械候选都写进 filtered.csv（写阶段和理由）。
category 只能是：逻辑不成立 / 文献里已有但节点表没收录 / 简单拼接可解 / 疑似真空白。""",
    "step6_survey": """完成第 6 步（正式调研）：对 gaps.csv 的每条候选 Gxx：
- 至少 3 种不同说法的检索：`gapr search . "<说法>" --tool arxiv --from {recent_from} --for Gxx --kind phrasing`（其中至少一次是 arXiv 且从 {recent_from} 起），也可以加 s2 / openalex；
- 正向追踪：`gapr citations . <最接近的论文 id> --for Gxx`（或 `gapr search … --kind forward`）；
- 跨领域：按 neighbor_terms.csv 换成邻近领域术语再搜，`--kind cross`；最新会议 / workshop：`--kind venue`；
- 写 verdict（确认空白 / 部分解决 / 已被解决 / 争议未决）、confidence（高 / 中 / 低，不能高于检索记录支持的程度）、closest_work（最接近的工作及其不足）、next_step（小实验建议）。
- independent_review 一列：还没复核时写“待复核”；已经有复核结果的不要改它。复核找到反例（以“找到反例”开头）的，判定不能是“确认空白”，要按反例改判定。""",
    "step6_backtest": """完成第 6 步（回测）：先用 `gapr search . "<说法>" --post-cutoff --for Gxx --kind phrasing`（第一次会锁定候选，之后不能再改 gaps.csv 的候选内容）
查截止日之后的论文，逐条按 rubric/backtest_rules.md 判为 命中 / 部分命中 / 未命中；命中和部分命中写 filled_by（论文）和 months_after_cutoff（截止日到首次公开的月数）；
每条都写 closest_work 和 independent_review（回测不做独立复核时写“未复核”）。""",
    "review6": """你是独立复核者，没有参与前面的推导。你只有一个任务：为 gaps.csv 里的每条候选找到“已经解决了它的论文”。
- 只看 gaps.csv 的 statement，自己想检索词，用 `gapr search . "<检索词>" --for Gxx --kind phrasing`（可加 `--tool arxiv` 等）和 `gapr citations . <id> --for Gxx` 去找。
- 找到了：independent_review 写“找到反例：<论文 id 与标题>，<为什么算解决了>”；没找到：写“未找到反例：<做了哪些检索>”。
- 只改 independent_review 这一列，不要改 verdict 等其他列。""",
    "step7": """完成第 7 步：按 rubric/scoring.md 的标准，给判定为“确认空白”或“部分解决”的候选打分：score_sources、score_demand、score_splice、
score_crowding、score_feasibility 各 1–3 分，score_total 为五项之和。打分理由写进 decisions.md。""",
    "report": """生成最终报告 report.md（中文），按方法说明“输出”一节的结构：结论先行；范围与方法（明确写出这是自动模式、两个确认点由 Claude 代为审核、
decisions.md 里的主要取舍）；演化图（Mermaid，边上标缺陷）；节点表、边表与分歧点、属性矩阵；空白清单；已过滤与已被解决的候选；
检索记录摘要；所有标为「已有知识」的条目。数据一律取自项目里的 CSV，不要编造。""",
}


def system_prompt(root: Path, doc: str) -> str:
    rules = _doc_section(doc, "## 2. 全程规则", ("\n## ", "\n---"))
    toml = (root / "project.toml").read_text(encoding="utf-8")
    return f"""你在**自动模式**下独立完成一次研究空白调研（问题驱动的方法演化树）。用户不在场：方法说明里原本要用户选择、填写或确认的地方，
都由你按最合理的理解决定，并把每个决定和理由追加写进 decisions.md（格式：`## <阶段>` 下面逐条写“决定 / 理由”）。全部用中文。

工作方式：
- 当前目录就是调研项目目录。文献检索、单篇信息、全文、原句核对、被引 / 参考文献、引用句一律用 `gapr <命令> . …`（项目目录写 `.`）。
  它会自动写检索记录、套用回测日期防火墙。可用命令：search、paper、fulltext、quote、citations、references、contexts、reviews、
  matrix-template、gaps、check。`gapr <命令> --help` 可以看参数。
- 不要运行 `gapr confirm`，也不要创建或修改 confirmations.json、rubric.lock.json、evidence/ 下的文件：确认点由面板处理，证据由 gapr 登记。
- 读写文件用 Read / Write / Edit / Glob / Grep，只能改当前目录里的文件；Bash 只能运行 gapr。
- CSV 用 UTF-8，第一行是列名，严格按下面的列定义写，不要增删列。改完用 `gapr check . --step <N> -v` 自查，把错误改到没有为止。
- 不编造论文、日期或原句。查不到的标「已有知识」，并在 decisions.md 里说明。

全程规则（摘自方法说明）：
{rules}

CSV 列定义：
{_tables_doc()}

项目输入（project.toml）：
```toml
{toml}
```"""


def phase_prompt(phase: Phase, root: Path, doc: str, check_text: str, is_backtest: bool, ref: date) -> str:
    key = phase.key
    if key == "step6":
        key = "step6_backtest" if is_backtest else "step6_survey"
    task = PHASE_TASKS[key].replace("{recent_from}", (ref - timedelta(days=364)).isoformat())
    section = ""
    if 1 <= phase.step <= 7 and phase.key != "report":
        section = _doc_section(doc, f"### 第 {phase.step} 步")
    if phase.key == "report":
        section = _doc_section(doc, "## 5. 输出", ("\n## ", "\n---"))
    return (f"现在的阶段：{phase.title}\n\n{task}\n\n"
            + (f"方法说明原文：\n{section}\n\n" if section else "")
            + f"当前检查结果（gapr check）：\n{check_text or '（无）'}\n\n完成后用两三句话总结这一阶段做了什么。")


def feedback_prompt(errors: list[str], round_no: int) -> str:
    shown = "\n".join(f"- {e}" for e in errors[:60])
    more = f"\n（还有 {len(errors) - 60} 条未列出）" if len(errors) > 60 else ""
    return (f"面板运行 gapr 检查后，这一阶段还有 {len(errors)} 个错误（第 {round_no} 轮反馈）。逐条改正，"
            f"改完再用 `gapr check . --step <N>` 自查：\n{shown}{more}")


def review_material(root: Path, point: str) -> str:
    from researchtool.gapr import schema
    if point == "1":
        nodes = schema.read_rows(root / "nodes.csv")
        fams = schema.read_rows(root / "families.csv")
        terms = schema.read_rows(root / "neighbor_terms.csv")
        lines = ["方法族："] + [f"- {f.get('family')}：{f.get('representative_papers', '')}（{f.get('from_survey', '')}）" for f in fams]
        lines += ["", f"节点（{len(nodes)} 个）："]
        lines += [f"- {n.get('id')} | {n.get('name')} | {n.get('first_public')} | {n.get('family')} | {n.get('arxiv_id') or n.get('url')}" for n in nodes]
        lines += ["", "邻近领域术语对照："] + [f"- {t.get('term')} ↔ {t.get('neighbor_field')}：{t.get('neighbor_term')}" for t in terms]
        return "\n".join(lines)
    attrs = schema.read_rows(root / "rubric" / "attributes.csv")
    inc = schema.read_rows(root / "rubric" / "incompatible.csv")
    defects = schema.read_rows(root / "defects.csv")
    lines = [f"属性（{len(attrs)} 个）："]
    for a in attrs:
        lines.append(f"- {a.get('id')} {a.get('name')}：{a.get('question')}｜✓ 标准：{a.get('check_criterion')}"
                     + (f"｜△ 标准：{a['partial_criterion']}" if a.get("partial_criterion") else ""))
    lines += ["", "不相容的属性对："] + [f"- {r.get('attr_a')} × {r.get('attr_b')}：{r.get('reason')}" for r in inc]
    lines += ["", f"归纳来源：defects.csv 的遗留缺陷（{len(defects)} 条）："]
    lines += [f"- {d.get('node_id')}：{d.get('defect')}" for d in defects[:120]]
    return "\n".join(lines)


def review_prompt(point: str, doc: str, material: str, scale: str) -> list[dict[str, str]]:
    if point == "1":
        rule = _doc_section(doc, "### 第 1 步")
        ask = "审核节点清单和邻近领域术语对照表：方法族是否覆盖领域主流、节点是否有代表性、有没有明显漏掉的重要方法、术语对照是否成立。"
    else:
        rule = _doc_section(doc, "### 第 4 步")
        ask = ("审核属性定义和相容性表：每个属性是否是能回答“能 / 不能”的能力问题、✓ 标准是否清楚可判、是否确实来自遗留缺陷、"
               "有没有重复或明显遗漏、不相容的判断是否成立。确认之后属性就冻结，不能再改。")
    system = ("你是研究空白调研的确认点审核者，代替不在场的用户做确认。你没有参与前面的推导，只根据下面的规则和清单独立判断。"
              "只输出一个 JSON 对象，不要输出其他内容。")
    user = (f"{ask}\n规模：{scale}。\n\n方法说明原文：\n{rule}\n\n待审核的清单：\n{material}\n\n"
            '输出格式：{"approve": true 或 false, "summary": "一两句总体意见", "problems": ["必须修改的问题，approve 为 true 时可以为空"]}'
            "\n只有存在会影响后续结论的问题时才 approve=false；措辞类的小问题写进 summary 即可。")
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def parse_review(text: str) -> dict[str, Any]:
    match = re.search(r"\{.*\}", text or "", re.S)
    try:
        data = json.loads(match.group(0)) if match else {}
    except ValueError:
        data = {}
    if not isinstance(data, dict) or "approve" not in data:
        return {"approve": False, "summary": "审核结果无法解析", "problems": [(text or "")[:500]], "unparsed": True}
    problems = data.get("problems") or []
    return {"approve": bool(data.get("approve")), "summary": str(data.get("summary") or ""),
            "problems": [str(p) for p in problems if str(p).strip()] if isinstance(problems, list) else [str(problems)]}


def summarize_tool(name: str, data: dict[str, Any]) -> str:
    if name == "Bash":
        return str(data.get("command", ""))[:300]
    if name in ("Read", "Write", "Edit"):
        return str(data.get("file_path", ""))[-120:]
    if name in ("Glob", "Grep"):
        return str(data.get("pattern", ""))[:120]
    return json.dumps(data, ensure_ascii=False)[:200]


# ------------------------------------------------------------------ 运行

class AutoRunner:
    """每个项目一个自动运行状态（存在项目目录之外），同一时间每个项目最多一个运行线程。"""

    def __init__(self, state_dir: Path, *, workdir: Path, runner: Any, project_dir: Callable[[str], Path],
                 python: str | None = None):
        self.state_dir = Path(state_dir)
        self.workdir = Path(workdir)
        self.runner = runner
        self.project_dir = project_dir
        self.python = python or sys.executable
        self.lock = threading.Lock()
        self.threads: dict[str, threading.Thread] = {}
        self.stop_flags: dict[str, threading.Event] = {}
        self.cache: dict[str, str] = {}   # project_id → 最近一次保存的状态（JSON 文本）
        self.lock_cache = threading.Lock()

    # ---- 状态文件
    def _path(self, project_id: str) -> Path:
        return self.state_dir / f"{project_id}.json"

    def load(self, project_id: str) -> dict[str, Any]:
        """最新状态：优先用内存里的副本（运行线程每次 save 都会更新），没有时读文件。
        Windows 上读文件可能撞上线程正在替换它，这时退回内存副本。"""
        with self.lock_cache:
            cached = self.cache.get(project_id)
        state = None
        if cached is not None:
            state = json.loads(cached)
        else:
            path = self._path(project_id)
            for _ in range(5):
                try:
                    state = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
                    break
                except (OSError, ValueError):
                    time.sleep(0.05)
            state = state if isinstance(state, dict) else {}
        state.setdefault("enabled", False)
        state.setdefault("status", "idle")
        state.setdefault("log", [])
        state.setdefault("sessions", {})
        if state["status"] == "running" and not self.is_running(project_id):
            state["status"] = "interrupted"   # 面板重启时线程没了
            state["message"] = "面板重启后运行中断，可以点「继续」接着跑。"
        return state

    def save(self, project_id: str, state: dict[str, Any]) -> None:
        state["log"] = state.get("log", [])[-MAX_LOG:]
        text = json.dumps(state, ensure_ascii=False, indent=1)
        with self.lock_cache:
            self.cache[project_id] = text
        self.state_dir.mkdir(parents=True, exist_ok=True)
        tmp = self._path(project_id).with_suffix(f".{threading.get_ident()}.tmp")
        for attempt in range(10):  # 文件被读时 os.replace 在 Windows 上会失败，稍等重试；内存副本已是最新
            try:
                tmp.write_text(text, encoding="utf-8")
                os.replace(tmp, self._path(project_id))
                return
            except OSError:
                time.sleep(0.05 * (attempt + 1))
        tmp.unlink(missing_ok=True)

    def is_enabled(self, project_id: str) -> bool:
        return bool(self.load(project_id).get("enabled"))

    def is_running(self, project_id: str) -> bool:
        thread = self.threads.get(project_id)
        return bool(thread and thread.is_alive())

    def enable(self, project_id: str, enabled: bool = True) -> dict[str, Any]:
        with self.lock:
            if self.is_running(project_id):
                raise ValueError("自动运行进行中，先停止再切换。")
            state = self.load(project_id)
            state["enabled"] = bool(enabled)
            state.setdefault("enabledAt", datetime.now().isoformat(timespec="seconds"))
            self.save(project_id, state)
        return self.public(project_id)

    def public(self, project_id: str) -> dict[str, Any]:
        state = self.load(project_id)
        root = self.project_dir(project_id)
        try:
            status = phase_status(root, state)
        except (ValueError, OSError) as exc:
            status, state["message"] = {}, str(exc)
        from researchtool.gapr import project as prj
        try:
            backtest = prj.load(root).is_backtest
        except (ValueError, OSError):
            backtest = False
        phases = [{"key": p.key, "title": p.title, "done": bool(status.get(p.key, {}).get("done")),
                   "skipped": p.survey_only and backtest} for p in PHASES]
        return {"enabled": state["enabled"], "status": state["status"], "running": self.is_running(project_id),
                "phase": state.get("phase", ""), "message": state.get("message", ""), "model": state.get("model", ""),
                "effort": state.get("effort", ""), "startedAt": state.get("startedAt", ""),
                "finishedAt": state.get("finishedAt", ""), "phases": phases, "log": state["log"][-300:],
                "backtest": backtest}

    # ---- 启停
    def start(self, project_id: str, *, model: str, effort: str) -> dict[str, Any]:
        root = self.project_dir(project_id)
        with self.lock:
            state = self.load(project_id)
            if not state["enabled"]:
                raise ValueError("这个项目没有开启自动模式。")
            if self.is_running(project_id):
                raise ValueError("自动运行已经在进行。")
            status = self.runner.status() if hasattr(self.runner, "status") else {"plan": True}
            if not status.get("plan"):
                raise ValueError(status.get("error") or "Claude Code 未登录 Claude 账号，无法自动运行。")
            state.update(status="running", model=model, effort=effort, message="",
                         startedAt=datetime.now().isoformat(timespec="seconds"), finishedAt="")
            self._log(state, "", "info", f"开始自动运行（{model} · {effort}）")
            self.save(project_id, state)
            stop = threading.Event()
            self.stop_flags[project_id] = stop
            thread = threading.Thread(target=self._run, args=(project_id, root, stop), daemon=True)
            self.threads[project_id] = thread
            thread.start()
        return self.public(project_id)

    def stop(self, project_id: str) -> dict[str, Any]:
        flag = self.stop_flags.get(project_id)
        if flag:
            flag.set()
        return self.public(project_id)

    # ---- 主循环
    def _log(self, state: dict[str, Any], phase: str, kind: str, text: str) -> None:
        state.setdefault("log", []).append({"t": time.time(), "phase": phase, "kind": kind, "text": text[:2000]})

    def _run(self, project_id: str, root: Path, stop: threading.Event) -> None:
        state = self.load(project_id)
        state["status"] = "running"
        try:
            self._loop(project_id, root, state, stop)
        except Stopped:
            state.update(status="stopped", message="已停止。点「继续」会从当前阶段接着跑。")
            self._log(state, state.get("phase", ""), "info", "已停止")
        except Exception as exc:  # noqa: BLE001  一个阶段出错不应让面板崩溃
            state.update(status="failed", message=str(exc)[:500])
            self._log(state, state.get("phase", ""), "error", f"出错：{exc}")
        finally:
            state["finishedAt"] = datetime.now().isoformat(timespec="seconds")
            self.save(project_id, state)

    def _loop(self, project_id: str, root: Path, state: dict[str, Any], stop: threading.Event) -> None:
        from researchtool.gapr import project as prj
        doc = (self.workdir / "docs" / "gap-research.md").read_text(encoding="utf-8")
        attempts: dict[str, int] = {}
        for _ in range(MAX_PHASE_RUNS):
            if stop.is_set():
                raise Stopped()
            phase, status = next_phase(root, state)
            if phase is None:
                state.update(status="done", phase="", message="全部完成，报告在 report.md。")
                self._log(state, "", "info", "全部阶段完成")
                return
            attempts[phase.key] = attempts.get(phase.key, 0) + 1
            if attempts[phase.key] > 3:
                state.update(status="paused", phase=phase.key,
                             message=f"「{phase.title}」反复没有完成，已暂停。看日志和检查结果，修改后点「继续」。")
                self._log(state, phase.key, "error", state["message"])
                return
            state["phase"] = phase.key
            self._log(state, phase.key, "phase", phase.title)
            self.save(project_id, state)
            p = prj.load(root)
            if phase.key == "step0" and not p.domain:
                state.update(status="paused", message="领域（domain）没有填，自动模式不替你决定研究领域。在「编辑项目信息」里填上后点「继续」。")
                return
            if phase.kind == "review":
                self._review(project_id, root, state, phase, doc, p.scale, stop)
            else:
                ok = self._agent_phase(project_id, root, state, phase, doc, status, p, stop)
                if not ok:
                    errors = phase_status(root, state)[phase.key]["errors"]
                    state.update(status="paused", message=f"「{phase.title}」经过 {MAX_ROUNDS} 轮修改仍有 {len(errors)} 个错误，已暂停。"
                                                         "可以手动修改 CSV 后点「继续」。")
                    self._log(state, phase.key, "error", state["message"] + "\n" + "\n".join(errors[:20]))
                    return
                if phase.key == "report":
                    state["reportDone"] = datetime.now().isoformat(timespec="seconds")
            self.save(project_id, state)
        state.update(status="paused", message="阶段切换次数过多，已暂停，请检查日志。")

    def _env(self) -> dict[str, str]:
        """让会话里的 `gapr` 命令指向本仓库的 researchtool.gapr，且用面板同一个 Python。"""
        bin_dir = self.state_dir / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        py = str(Path(self.python)).replace("\\", "/")
        (bin_dir / "gapr").write_text(f'#!/bin/sh\nexec "{py}" -m researchtool.gapr "$@"\n', encoding="utf-8", newline="\n")
        (bin_dir / "gapr.cmd").write_text(f'@"{self.python}" -m researchtool.gapr %*\r\n', encoding="utf-8")
        try:
            os.chmod(bin_dir / "gapr", 0o755)
        except OSError:
            pass
        return {"PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", ""),
                "PYTHONPATH": str(self.workdir) + (os.pathsep + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else ""),
                "PYTHONIOENCODING": "utf-8"}

    def _call_agent(self, project_id: str, root: Path, state: dict[str, Any], phase: Phase, system: str, user: str,
                    *, resume: bool, stop: threading.Event, session_key: str | None = None) -> str:
        key = session_key or phase.key
        if not resume or key not in state["sessions"]:
            state["sessions"][key] = str(uuid.uuid4())
            resume = False
        last_save = [time.monotonic()]

        def on_event(event: dict[str, Any]) -> None:
            if stop.is_set():
                raise Stopped()
            if event["kind"] == "tool":
                self._log(state, phase.key, "tool", f"{event['name']}：{summarize_tool(event['name'], event['input'])}")
            else:
                self._log(state, phase.key, "text", event["text"])
            if time.monotonic() - last_save[0] > 2:
                self.save(project_id, state)
                last_save[0] = time.monotonic()

        try:
            reply = self.runner.run_agent(system, user, cwd=str(root), model=state["model"], effort=state["effort"],
                                          session_id=state["sessions"][key], resume=resume, allowed=ALLOWED_BASH,
                                          disallowed=DENIED_BASH, extra_env=self._env(), permission_mode="dontAsk",
                                          name=f"演化树自动 · {project_id} · {phase.title}", timeout=AGENT_TIMEOUT,
                                          on_event=on_event)
        except Stopped:
            raise
        except ValueError as exc:
            if stop.is_set():
                raise Stopped() from None
            if "会话已不存在" in str(exc) and resume:
                state["sessions"].pop(key, None)
                return self._call_agent(project_id, root, state, phase, system, user, resume=False, stop=stop,
                                        session_key=session_key)
            raise
        self._log(state, phase.key, "reply", reply)
        self.save(project_id, state)
        return reply

    def _check_text(self, root: Path, upto: int) -> str:
        from researchtool.gapr import checks, project as prj
        lines = []
        for res in checks.run(prj.load(root))[: upto + 1]:
            title = f"第 {res.step} 步 {checks.STEP_TITLES[res.step]}"
            if not res.started:
                lines.append(f"— {title}：未开始")
                continue
            lines.append(f"{'✓' if res.passed else '✗'} {title}：{'通过' if res.passed else f'{len(res.errors)} 个错误'}")
            lines += [f"    {i}" for i in res.issues[:30]]
        return "\n".join(lines)

    def _agent_phase(self, project_id: str, root: Path, state: dict[str, Any], phase: Phase, doc: str,
                     status: dict[str, dict[str, Any]], p: Any, stop: threading.Event) -> bool:
        system = system_prompt(root, doc)
        ref = p.cutoff or date.today()
        prompt = phase_prompt(phase, root, doc, self._check_text(root, max(phase.step, 1)), p.is_backtest, ref)
        # review6 必须是一个没看过推导过程的新会话；其他阶段在同一阶段内接着用同一个会话
        self._call_agent(project_id, root, state, phase, system, prompt, resume=False, stop=stop)
        for round_no in range(1, MAX_ROUNDS):
            current = phase_status(root, state)[phase.key]
            if current["done"] or phase.key in ("review6", "report"):
                return current["done"] or phase.key == "report"
            self._log(state, phase.key, "check", f"检查还有 {len(current['errors'])} 个错误，发回修改（第 {round_no} 轮）")
            self._call_agent(project_id, root, state, phase, system, feedback_prompt(current["errors"], round_no),
                             resume=True, stop=stop)
        return phase_status(root, state)[phase.key]["done"] or phase.key == "report"

    def _review(self, project_id: str, root: Path, state: dict[str, Any], phase: Phase, doc: str, scale: str,
                stop: threading.Event) -> None:
        from researchtool.gapr import confirm
        point = "1" if phase.key == "confirm1" else "2"
        fixer = PHASE_BY_KEY["step1" if point == "1" else "attrs"]
        pending: list[str] = []
        review: dict[str, Any] = {}
        for round_no in range(1, MAX_REVIEW_ROUNDS + 1):
            if stop.is_set():
                raise Stopped()
            text = self.runner.run(review_prompt(point, doc, review_material(root, point), scale),
                                   state["model"], state["effort"])
            review = parse_review(text)
            verdict = "通过" if review["approve"] else "不通过"
            self._log(state, phase.key, "review",
                      f"审核第 {round_no} 次：{verdict}。{review['summary']}"
                      + ("\n" + "\n".join(f"- {x}" for x in review["problems"]) if review["problems"] else ""))
            self.save(project_id, state)
            if review["approve"]:
                pending = []
                break
            pending = review["problems"]
            if round_no == MAX_REVIEW_ROUNDS:
                break
            # 意见交回负责这一步的会话修改，改完再审
            msg = ("确认点审核者（独立的 Claude，没看过推导过程）没有通过你的清单，意见如下。逐条处理：采纳的就改，不采纳的在 decisions.md "
                   "里写明理由。改完用 `gapr check . --step " + str(fixer.step) + "` 自查。\n"
                   + "\n".join(f"- {x}" for x in pending))
            self._call_agent(project_id, root, state, fixer, system_prompt(root, doc), msg, resume=True, stop=stop)
            if not phase_status(root, state)[fixer.key]["done"]:
                return  # 改出了检查错误：下一轮主循环会先回到负责的阶段修
        note = f"审核意见：{review.get('summary', '')}"
        if pending:
            note += "｜审核 " + str(MAX_REVIEW_ROUNDS) + " 次仍未通过，按自动模式继续；未解决的意见：" + "；".join(pending)
            self._log(state, phase.key, "error", "审核多次未通过，按自动模式继续，未解决的意见已写进确认记录。")
        confirm.record_auto(root, point, review=note)
        self._log(state, phase.key, "info", f"已记录确认点 {point}（由 Claude 代确认）")
