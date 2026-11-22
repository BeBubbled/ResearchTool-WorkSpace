"""项目目录：一次调研（或一次回测）对应 projects/ 下的一个文件夹。

    <project>/
      project.toml            第 0 步的输入
      families.csv nodes.csv neighbor_terms.csv            第 1 步
      defects.csv edges.csv forks.csv open_problems.csv    第 2–3 步
      rubric/attributes.csv rubric/incompatible.csv
      rubric/scoring.md [rubric/backtest_rules.md]         规则文件，冻结后不改
      rubric.lock.json                                     冻结记录
      matrix.csv matrix_evidence.csv                       第 4 步
      gaps_mechanical.csv gaps.csv filtered.csv            第 5–7 步
      search_log.csv                                       检索记录
      cache/                                               API 响应缓存（不进 git）
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from researchtool.gapr import schema

DEFAULT_CRITIQUE_KEYWORDS = ["however", "fail", "limitation", "unlike", "overlook"]

PROJECT_TOML = """\
# 第 0 步：输入。改完这个文件再开始第 1 步。
[project]
domain = "{domain}"
mode = "{mode}"            # survey（正式调研）或 backtest（回测）
start = "{start}"          # 时间范围起点
cutoff = "{cutoff}"        # 回测截止日；正式调研填今天
include = ""
exclude = ""
scale = "{scale}"          # mini（约 10 节点）/ quick（15–20）/ full（30–60）
audience = "{audience}"    # 我自己 / 导师 / 组会
language = "zh"

# 邻近领域及其术语（可选）。没填时第 1 步自动生成到 neighbor_terms.csv，
# 在确认点 1 交给用户审核。
neighbor_fields = []

[citation_filter]
# 第 2 步“他人指出”：只保留含这些词的引用句（不区分大小写）
keywords = {keywords}
max_contexts_per_paper = 200
"""

SCORING_MD = """\
# 排序打分标准（第 7 步）

本文件在确认点 2 与属性一起冻结，之后不修改。每项 1–3 分，总分为各项之和。

| 维度 | 1 分 | 2 分 | 3 分 |
|---|---|---|---|
| 来源数（被几条规则命中；只有一次回测支持，权重不宜过高） | 1 条规则 | 2 条规则 | 3 条及以上 |
| 需求强度 | 说得出场景，但没人抱怨 | 场景具体 | 场景具体，且找得到有人公开抱怨（附出处） |
| 拼接失效程度 | 简单拼接大概率够用 | 拿不准，需要小实验 | 说得出拼接为什么会失效 |
| 拥挤度（最近 12 个月相关论文数） | 10 篇以上 | 3–10 篇 | 0–2 篇 |
| 可行性（数据、算力、个人技能） | 缺两项以上 | 缺一项 | 都具备 |
"""

BACKTEST_RULES_MD = """\
# 回测判定规则（看结果前固定）

- 命中：截止后 3 年内有顶会论文把这个空白作为主要动机。
- 部分命中：只被顺带解决，或要超过 2 年才出现专门的方法。
- 未命中：截止前已在邻近领域解决，或一直没有后续。
- 间隔：截止日到首次公开日（arXiv v1）的月数。
"""


@dataclass
class Project:
    root: Path
    domain: str = ""
    mode: str = "survey"
    start: str = ""
    cutoff: date | None = None
    scale: str = "quick"
    audience: str = ""
    neighbor_fields: list[str] = field(default_factory=list)
    critique_keywords: list[str] = field(default_factory=lambda: list(DEFAULT_CRITIQUE_KEYWORDS))
    max_contexts_per_paper: int = 200

    # ---- 路径 ----
    def path(self, name: str) -> Path:
        return self.root / name

    @property
    def rubric_dir(self) -> Path:
        return self.root / "rubric"

    @property
    def cache_dir(self) -> Path:
        return self.root / "cache"

    @property
    def is_backtest(self) -> bool:
        return self.mode == "backtest"

    @property
    def firewall(self) -> date | None:
        """回测模式下的日期防火墙；正式调研不过滤。"""
        return self.cutoff if self.is_backtest else None


def load(root: str | Path) -> Project:
    root = Path(root)
    cfg_path = root / "project.toml"
    if not cfg_path.exists():
        raise FileNotFoundError(f"{cfg_path} 不存在，先运行 gapr init")
    cfg = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
    p = cfg.get("project", {})
    cf = cfg.get("citation_filter", {})
    cutoff = p.get("cutoff") or ""
    mode = p.get("mode", "survey")
    if mode not in ("survey", "backtest"):
        raise ValueError(f"project.toml: mode 只能是 survey 或 backtest，现在是 {mode!r}")
    if mode == "backtest" and not cutoff:
        raise ValueError("project.toml: 回测模式必须填 cutoff")
    return Project(
        root=root,
        domain=p.get("domain", ""),
        mode=mode,
        start=p.get("start", ""),
        cutoff=date.fromisoformat(str(cutoff)) if cutoff else None,
        scale=p.get("scale", "quick"),
        audience=p.get("audience", ""),
        neighbor_fields=list(p.get("neighbor_fields", [])),
        critique_keywords=list(cf.get("keywords", DEFAULT_CRITIQUE_KEYWORDS)),
        max_contexts_per_paper=int(cf.get("max_contexts_per_paper", 200)),
    )


def init(root: str | Path, *, domain: str = "", mode: str = "survey", start: str = "",
         cutoff: str = "", scale: str = "quick", audience: str = "") -> Project:
    root = Path(root)
    if (root / "project.toml").exists():
        raise FileExistsError(f"{root} 已经是一个项目")
    if mode not in ("survey", "backtest"):
        raise ValueError("mode 只能是 survey 或 backtest")
    if mode == "backtest" and not cutoff:
        raise ValueError("回测模式必须给 --cutoff")
    if mode == "survey" and not cutoff:
        cutoff = date.today().isoformat()
    root.mkdir(parents=True, exist_ok=True)
    kw = "[" + ", ".join(f'"{k}"' for k in DEFAULT_CRITIQUE_KEYWORDS) + "]"
    (root / "project.toml").write_text(
        PROJECT_TOML.format(domain=domain, mode=mode, start=start, cutoff=cutoff,
                            scale=scale, audience=audience, keywords=kw),
        encoding="utf-8",
    )
    for name, cols in schema.TABLES.items():
        schema.write_rows(root / name, cols, [])
    rubric = root / "rubric"
    for name, cols in schema.RUBRIC_TABLES.items():
        schema.write_rows(rubric / name, cols, [])
    (rubric / "scoring.md").write_text(SCORING_MD, encoding="utf-8")
    if mode == "backtest":
        (rubric / "backtest_rules.md").write_text(BACKTEST_RULES_MD, encoding="utf-8")
    (root / ".gitignore").write_text("cache/\n", encoding="utf-8")
    return load(root)
