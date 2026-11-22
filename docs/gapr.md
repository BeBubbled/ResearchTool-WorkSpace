# gapr：研究空白调研

面板入口：`/gap-survey`（演化树）。本文是命令行与检查规则的参考；代码在 `researchtool/gapr/`。

把 [gap-research.md](gap-research.md) 这套“问题驱动的方法演化树”方法做成可执行、可检查的流程。
方法在长期时序预测上的回测见 [backtest-ltsf-2022.md](backtest-ltsf-2022.md)。

## 设计：说明只有一份，每一步由代码把关

- **说明**：只有 `docs/gap-research.md` 一份。不另写 skill，谁来做（Claude、子 Agent、你自己）都按这一份。
- **`gapr`（Python，仅标准库）**：做不需要判断力的事（检索、缓存、检索记录、日期防火墙、规则计算），并**检查每一步的产出**。内部不调用任何 LLM。
- **关卡**：会推进到下一步的命令先检查前面所有步骤，有错误就拒绝执行。

| 关卡 | 检查什么才放行 |
|---|---|
| `confirm 1` | 第 0–1 步无错误 |
| `confirm 2` | 第 0–3 步无错误，属性定义完整 |
| `matrix-template` | 第 0–3 步无错误，确认点 2 已确认 |
| `gaps` | 第 0–4 步无错误（含：规则冻结后没改过、每个 ✓ 有依据） |
| `search --post-cutoff`（回测） | 第 0–5 步无错误；第一次运行时锁定候选清单 |

### 每一步检查的内容

| 步骤 | 主要检查（对应模板条目） |
|---|---|
| 0 输入 | 领域、起始时间已填 |
| 1 节点清单 | 日期格式与依据（全程规则 2）；回测时不晚于截止日；每个方法族至少 2 个节点；节点数符合规模；术语对照表不为空；有找综述的检索记录；**确认点 1 已确认，且之后节点和术语表没变** |
| 2 节点表 | 每个节点有问题、思路、至少一条缺陷；**标「论文原文 / 检索核对」的原句必须在登记过的证据里找得到**（自述 → 论文全文；他人指出 → 引用句；审稿 → 审稿意见）；证据文件登记后没被改过 |
| 3 演化边 | 父子节点存在；缺陷确实属于父节点；子论文原句可核对；分歧点至少 2 个子方法 |
| 4 属性矩阵 | **确认点 2 已确认**；规则文件与冻结时一致；矩阵完整且覆盖所有节点；每个 ✓ 有依据 |
| 5 候选空白 | 机械候选是最新的；**每条机械候选要么并入 gaps.csv，要么写进 filtered.csv**（全程规则 6）；`rules` 由 `merged_from` 推出，不能手填；需求、拼接测试、分类都已写 |
| 6 验证（正式） | 判定和置信度合法；**判为“确认空白”必须有 ≥3 种说法的检索，且含最近 12 个月的 arXiv 检索**（全程规则 3）；**写的置信度不能高于检索记录能支持的**（高 = 说法 + 正向追踪 + 跨领域）；独立复核找到反例就不能判为确认空白 |
| 6 验证（回测） | 候选已锁定且之后没改；命中要写论文和间隔月数；每条都有检索记录 |
| 7 排序 | 五项都是 1–3 分，总分等于各项之和 |

## 确认点只能由你本人确认

`python -m researchtool.gapr confirm <项目> 1|2` 会把清单显示出来，要你输入“确认”。它要求在**交互式终端**里运行：
Claude Code 的命令工具不是终端，模型代跑会被拒绝。仓库的 `.claude/settings.json` 里禁止模型运行 `confirm` 的 deny 规则是第二道保险。
面板（`/gap-survey`）**不提供**确认按钮，只在「逐步检查」里显示要运行的命令，复制到你自己的 PowerShell 里运行（工作目录要是仓库根目录）。

冻结之后确实要改规则：`confirm 2 --change-rules --reason "…"`，这次修改会记下来并写进报告。

## 常用命令

面板新建的项目在 `.runtime/gap-survey/projects/<名字>/`。下面的命令在仓库根目录、用项目的 `.venv` 运行（`DIR` 是项目目录）；面板「检索与命令」页能运行除 `init`、`confirm` 以外的命令。

```powershell
python -m researchtool.gapr init DIR --mode survey --domain "可解释性与分析" --start 2020-01-01 --audience 组会
python -m researchtool.gapr init DIR --mode backtest --cutoff 2022-06-30 --scale mini
python -m researchtool.gapr check DIR            # 看每一步的状态和问题（-v 显示提醒）
```

| 命令 | 用途 |
|---|---|
| `search DIR "关键词" --tool s2/arxiv/openalex/openreview` | 检索，自动写入 `search_log.csv`。第 6 步加 `--for G01 --kind phrasing/forward/venue/cross` |
| `paper DIR arXiv:… / ACL:… / DOI:…` | 单篇信息，日期优先用 arXiv v1 |
| `fulltext DIR arXiv:… / ACL:…` | 下载并登记全文（arXiv HTML → ar5iv → PDF；ACL Anthology PDF）。其他来源用 `--import 文件 --url 出处` 手动导入，报告里会单独列出 |
| `quote DIR arXiv:… "原句"` | 核对原句（`…` 可省略中间部分）；key 写 `citations` 或 `reviews` 则在引用句 / 审稿意见里找 |
| `contexts DIR ID --top 30 [--alias 方法名]` | **他人指出（不需要 S2 key）**：取被引最多的引用者全文，抽出引用这篇论文的句子，并标出含批评词的句子。抽出的句子来自登记过的全文，可以通过原句核对 |
| `citations DIR ID [--source auto/s2/openalex]` | 正向追踪（被引列表）；`--for G01` 记为第 6 步的正向追踪。`--critique` 只有 S2 能用 |
| `references DIR ID` | 向前补漏（参考文献） |
| `reviews DIR FORUM_ID` | OpenReview 审稿意见（见下方限制） |

矩阵格子可填 `✓ △ ✗`，也可填 `Y P N`。找空白时 △ 不算覆盖，但会在“部分覆盖”一列列出对应方法。

## 环境变量

面板里运行的命令沿用面板的 `.env`。OpenAlex Key 在「设置 → OpenAlex 文献」里填写和验证；Semantic Scholar 用面板已有的 `SEMANTIC_SCHOLAR_API_KEY`（单独用命令行时也认 `S2_API_KEY`）。

| 变量 | 用途 |
|---|---|
| `S2_API_KEY` / `SEMANTIC_SCHOLAR_API_KEY` | 可选。Semantic Scholar API key，有了之后 `citations`/`references` 自动改用 S2，`citations --critique` 可直接取引用句。没有时几乎每次都会 429。申请：https://www.semanticscholar.org/product/api#api-key-form |
| `OPENALEX_API_KEY` | OpenAlex 免费 key（https://openalex.org/settings/api）。没有时按 IP 共享每天 0.10 美元额度，且不能用 cursor 翻页；有 key 时每天 1 美元。key 只加在请求上，不写进缓存 |
| `OPENREVIEW_TOKEN` | 可选。OpenReview 匿名访问审稿意见会被人机验证拦下 |

## 已知限制

- **`contexts` 的两种定位方式**：arXiv HTML 里按参考文献链接精确定位（html-bib）；PDF 只能按“姓 et al. 年份”匹配（pdf-author-year），数字编号格式（[12]）的论文抽不到，会注明。按关键词（however、fail…）标出的“批评句”只是粗筛，有不少其实是称赞，要逐句判断。
- **OpenAlex 的参考文献有错配**：实测 PatchTST 的参考文献里混进了一篇 1970 年的金融论文。向前补漏时把它当线索，不当事实。

- **OpenReview**：标题检索可用；取审稿意见被人机验证拦下（2026-11 实测），工具不绕过。ACL Rolling Review 的审稿意见大多本来就不公开。
- **arXiv 限流**：每 3 秒最多 1 次请求，连续调用会被临时封禁。限速状态存在 `cache/throttle.json`，跨进程生效。
- **原句核对**：只比较字母和数字，不受空格、标点、换行、连字影响；少于 20 个字母数字的原句不核对。公式多的段落可能因为 HTML 和 PDF 的公式文本不同而对不上，这时把来源标为「已有知识」。
- **日期**：非 arXiv 论文没有 v1 日期，用 publicationDate；只有年份且与截止年相同的，会给出提醒。
- **事后偏差**：日期防火墙只挡检索，挡不住模型自己知道的后来的事。

## 测试

```powershell
python -m unittest discover -s tests -p "test_gap*.py"
```

需要 Python 3.11+（用到 `tomllib`）。面板本身支持 3.10，在 3.10 下打开演化树页面会提示升级。

`tests/fixtures/gapr_ltsf_2022/` 是回测文档里的属性矩阵。测试通过只说明代码算对了，不说明方法有效。
