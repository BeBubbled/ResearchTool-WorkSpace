# 核验说明：gapr 是否符合 docs/gap-research.md

这份文件让不熟悉代码的人也能核验：**`gapr` 是否落实了 [gap-research.md](gap-research.md) 里的规则**。
每条规则都对应到实现它的代码、证明它的测试，以及一个“故意违规，看会不会被拦下”的人工核验步骤。

它只回答“代码是否按规则办事”，不回答“这套方法能不能找到真问题”。后者见第 6 节。

核验结果记录于 2026-11-21，来自 Research_Ideas 仓库 `gapr-core` 分支的提交 `17ca0cf`；环境为 Windows 11，Python 3.13。
迁入本仓库后，代码在 `researchtool/gapr/`，测试文件名加了 `gapr_` 前缀，下面的命令和测试名都已按本仓库改写。

---

## 1. 三步快速核验

在仓库根目录、用项目的 `.venv` 运行（需要 Python ≥ 3.11，不需要联网）：

```powershell
python -m unittest discover -s tests -p "test_gapr_*.py"
```

预期：`Ran 80 tests` 和 `OK`。（面板接入部分另有 `tests/test_gap_survey.py`。）

```powershell
python -m tests.gapr_mutation_check
```

预期：最后一行 `10/10 个变异被测试抓到`。它把 10 条关键规则的实现**故意改坏**（只在内存里改，不动源文件），对应的测试必须失败。如果改坏了测试还通过，说明测试是摆设。详见第 4 节。

```powershell
python -m tests.gapr_make_demo demo-verify
python -m researchtool.gapr check demo-verify
```

预期：第 0–4 步都显示“通过”，第 5–7 步显示“未开始”。这是一个合格的迷你回测项目，用来做第 3 节的人工核验。

---

## 2. 追溯表

**状态**：

| 标记 | 含义 |
|---|---|
| ✅ 强制 | 违反时代码报错，并拒绝进入下一步 |
| ◐ 部分 | 规则的一部分由代码强制，另一部分没有（写在“说明”里） |
| ✋ 判断 | 需要人或模型判断，代码只能保证留下记录，保证不了判断正确 |
| ✗ 未实现 | 还没有对应代码 |

**测试名**写成 `文件::类::方法`，都在 `tests/` 下。**人工核验**一列的 V 编号见第 3 节，M 编号见第 4 节。

### 2.1 全程规则（模板第 2 节）

| 编号 | 目标（模板原文） | 实现 | 自动测试 | 人工核验 | 状态与说明 |
|---|---|---|---|---|---|
| G1 | 每个事实都要有来源；标注「检索核对 / 论文原文 / 已有知识」；报告末尾集中列出「已有知识」 | `checks.step1/2/3` 检查来源标注；`checks._check_quote` 核对原句；`evidence.Evidence.verified` 只认登记过且未被改动的证据；`evidence.quote_in` 做匹配 | `test_gapr_workflow::TestStep2Quotes::test_claimed_quote_must_be_found`、`::test_tampered_evidence`；`test_gapr_evidence::TestProvenance::*`、`::TestNormalizeAndMatch::*` | V3、V3b；M3 | ◐ 标了「论文原文 / 检索核对」的原句必须在登记过的证据里找得到。**报告末尾的「已有知识」清单还没实现**（报告生成未做） |
| G2 | 日期用首次公开日期（arXiv Submitted on），不用会议年份 | `sources/arxiv.parse_feed` 取 v1 日期；`sources/arxiv.resolve_dates` 给其他来源补 v1 日期；`checks.step1` 检查格式和依据 | `test_gapr_sources::TestArxiv::test_parse`；`test_gapr_workflow::TestCoverageGaps::test_arxiv_id_without_v1_date_warns` | V7 | ◐ 检索结果自动用 v1 日期。`nodes.csv` 里手填的日期只检查格式；有 arXiv id 但日期不是 v1 时只给提醒，**不会自动去 arXiv 核对**（可用 `gapr paper` 手动核对） |
| G3 | 每次检索写进检索记录；没有检索记录不得声称“尚无人研究” | `cli._log` 在 `search` / `citations` / `references` / `contexts` / `reviews` 中自动记录，失败也记；`checks._search_coverage` 统计每条候选的检索；`checks.step6` 拦截无检索的“确认空白” | `test_gapr_workflow::TestSearchLogging::test_success_and_failure_logged`；`::TestSurveyStep6And7::test_confirmed_gap_needs_searches` | V10；M8 | ✅ 只有经过 `gapr` 的检索才会被记录和计数。用网页搜索等其他方式查到的不算，这个方向偏严 |
| G4 | 属性的 ✓ 标准、空白判定标准、打分标准先写下来，之后不修改 | `rubric.freeze` / `rubric.verify`（冻结 `rubric/` 下全部文件）；`confirm.confirm_2`；矩阵已填格子后再改，必须写理由（`--change-rules --reason`），并留下记录；回测在查截止日之后的论文前锁定候选（`confirm.lock_candidates`） | `test_gapr_workflow::TestGatesAndGaps::test_rubric_change_after_confirm2`、`::test_reconfirm_after_matrix_needs_reason`、`::test_crlf_does_not_break_lock`；`::TestStep5::test_backtest_lock` | V2；M2、M10 | ✅ 空白的判定标准写死在代码里（`matrix.analyze`），用户不能改 |
| G5 | 拿不准就给 △；找空白时 △ 不算覆盖 | `matrix.analyze`：△ 不算覆盖，并在 `partial` 里列出有 △ 的方法 | `test_gapr_matrix::TestLtsfBacktestMatrix::test_partial_as_cover_would_hide_S_V_Z`、`::test_r1_partial_coverage_listed` | V1；M1 | “△ 不算覆盖” ✅。“拿不准就给 △” ✋：格子填什么由人判断 |
| G6 | 删掉的和没找到的也要写出来 | `checks.step5`：每条机械候选必须并入 `gaps.csv` 或写进 `filtered.csv`（要写阶段和理由）；检索失败也写进记录 | `test_gapr_workflow::TestStep5::test_candidate_cannot_vanish`、`::test_complete_accounting_passes`；`::TestSearchLogging::*` | V5；M5 | ◐ 数据层面不会丢。**报告里的“已过滤与已被解决”一节还没实现** |
| G7 | 用读者的语言，少用代称 | — | — | — | ✋ 写作要求，代码管不到 |

### 2.2 确认点（模板第 3 节）

| 编号 | 目标 | 实现 | 自动测试 | 人工核验 | 状态与说明 |
|---|---|---|---|---|---|
| C1 | 第 1 步结束时，用户确认节点清单和术语对照表 | `confirm.confirm_1` 记录节点 id 和术语表的快照；`checks.step1` 发现确认后有变化就要求重新确认 | `test_gapr_workflow::TestStep1::test_unconfirmed_is_error`、`::test_node_added_after_confirm`；`::TestConfirm::test_confirm1_requires_valid_step1` | V4a | ✅ |
| C2 | 第 4 步归纳出属性之后、填格子之前，用户确认属性清单 | `cli.cmd_confirm` 先要求第 0–3 步通过；`confirm.confirm_2` 确认后冻结规则 | `test_gapr_workflow::TestCoverageGaps::test_confirm2_requires_steps_0_to_3`；`::TestGatesAndGaps::test_matrix_and_gaps_refused_before_confirm2` | V2 | ✅ |
| C3 | 确认只能由用户本人做，模型不能代劳 | `confirm._require_terminal`：不是交互式终端就拒绝（在显示清单之前）；`.claude/settings.json` 的 deny 规则是第二道保险；面板 `/gap-survey` 不提供确认入口，可运行的命令白名单里没有 `confirm` | `test_gapr_workflow::TestConfirm::test_model_cannot_confirm`、`::test_cancel_writes_nothing`；`test_gap_survey::GapSurveyTests::test_argv_whitelist` | V4b；M4 | ◐ 两道保险都只防误操作，防不住刻意绕过：有意伪造终端的程序可以绕开检查，deny 规则也只拦它列出的那几种写法。`tests/gapr_make_demo.py` 为了生成演示数据会代填确认，但它只能新建目录，碰不到已有的项目 |

### 2.3 各步骤（模板第 4 节）

| 编号 | 目标 | 实现 | 自动测试 | 人工核验 | 状态与说明 |
|---|---|---|---|---|---|
| S0 | 第 0 步输入：领域、范围不能缺 | `checks.step0` | `test_gapr_workflow::TestMoreStepChecks::test_step0_requires_domain_and_start` | — | ✅ 领域和起始时间缺失时报错；“推断不了就先问一次”是对话行为 ✋ |
| S1a | 找综述（survey / review / taxonomy） | `checks.step1` 要求检索记录里有找综述的检索 | `test_gapr_workflow::TestStep1::test_family_needs_two_and_survey_search` | — | ◐ 检查有没有检索过综述；“按引用数挑 3–5 篇、合并分类表”是 ✋ |
| S1b | 每个方法族至少 2 个节点；规模符合选择 | `checks.step1` | `::TestStep1::test_family_needs_two_and_survey_search`；`::TestMoreStepChecks::test_scale_lower_bound` | — | ✅ 规模上限只给提醒 |
| S1c | 邻近领域术语对照表 | `checks.step1` 要求不为空 | `::TestMoreStepChecks::test_neighbor_terms_required` | V4a | ◐ 只检查不为空，术语对不对 ✋ |
| S1d | 每个已知缺陷至少保留一个专门解决它的节点 | — | — | — | ✗ 没有检查 |
| S1e | 回测只用截止日之前的论文 | `paper.apply_cutoff`（检索结果）；`checks.step1`（节点日期） | `test_gapr_sources::TestFirewall::*`；`test_gapr_workflow::TestStep1::test_backtest_date_after_cutoff` | V7；M7 | ✅ 挡得住检索，**挡不住模型自己知道的后来的事**（见第 5 节） |
| S2a | 每个节点有要解决的问题、核心思路、遗留缺陷 | `checks.step2` | `test_gapr_workflow::TestMoreStepChecks::test_node_needs_problem_and_defect` | — | ✅ 没有作者自述的缺陷时只给提醒 |
| S2b | 缺陷分三种来源：自述（论文原文）、他人指出（引用句）、审稿指出 | `checks._check_quote` 按来源分别对照全文、引用句、审稿意见；`contexts.extract` 从引用者全文抽引用句 | `test_gapr_workflow::TestStep2Quotes::*`；`test_gapr_contexts::*`；`test_gapr_evidence::TestProvenance::test_citation_context_pool` | V3、V3b | ◐ 审稿意见基本取不到：OpenReview 有人机验证，ARR 审稿意见大多不公开 |
| S3a | 演化边：父方法、缺陷、子方法、子论文原句 | `checks.step3`：节点存在、缺陷属于父节点、原句可核对 | `test_gapr_workflow::TestStep3::test_defect_must_belong_to_parent` | — | ✅ |
| S3b | 分歧点至少两个思路不同的子方法 | `checks.step3` | `::TestCoverageGaps::test_fork_needs_two_children` | — | ◐ 数量 ✅；“思路不同”✋ |
| S3c | 标出悬空缺陷（R3） | `matrix.dangling_defects` | `test_gapr_matrix::TestDangling::test_r3`；`test_gapr_workflow::TestGatesAndGaps::test_full_flow_reproduces_backtest` | V1 | ✅ |
| S4a | 属性 10–15 个，每个有 ✓ 标准 | `checks.step4` | `test_gapr_workflow::TestGatesAndGaps::test_full_flow_reproduces_backtest` | — | ◐ 缺 ✓ 标准时报错；个数只给提醒；属性选得好不好 ✋ |
| S4b | 相容性预检，第 5 步跳过不相容组合 | `matrix.analyze(incompatible=…)` | `test_gapr_matrix::TestLtsfBacktestMatrix::test_incompatible_pair_skipped` | — | ✅ 跳过 ✅；哪些不相容 ✋ |
| S4c | 矩阵完整，覆盖所有节点；每个 ✓ 有依据 | `matrix.load_matrix`；`checks.step4` | `test_gapr_matrix::TestParsing::*`；`test_gapr_workflow::TestCoverageGaps::test_matrix_must_cover_every_node`；`::TestGatesAndGaps::test_missing_evidence_blocks_gaps` | V8；M9 | ◐ 依据必须有，但依据的内容是否支持 ✓ 是 ✋ |
| S5a | R1 整列空白、R2 两两组合空白、R2′ 三元组 | `matrix.analyze`（R2′ 只列两两都被覆盖、三者没有同时 ✓ 的最小三元组） | `test_gapr_matrix::*`（以回测矩阵为标准答案） | V1；M1 | ✅ 回测文档手工得出的 S、V、Z 和 L×D、L×P、D×P、P×H 被精确复现 |
| S5b | R4 争议停滞、R5 综述开放问题进入候选 | `checks.compute_mechanical` 读取 `open_problems.csv` | `test_gapr_workflow::TestCoverageGaps::test_r4_r5_enter_candidates` | — | ◐ 进入候选 ✅；找出争议和开放问题 ✋ |
| S5c | 合并去重，来源一列写全所有规则 | `checks.step5`：`rules` 必须等于 `merged_from` 里候选的规则 | `test_gapr_workflow::TestStep5::test_rules_derived_from_merged` | V6 | ◐ 来源列不能手填 ✅；哪些候选算同一个空白 ✋ |
| S5d | 需求过滤、拼接测试、四类归类 | `checks.step5` 要求都填 | `test_gapr_workflow::TestStep5::test_complete_accounting_passes` | — | ◐ 必填 ✅；写得对不对 ✋ |
| S5e | 机械候选与当前矩阵一致 | `checks.step5` 重新计算并对比 | `test_gapr_workflow::TestStep5::test_stale_mechanical` | — | ✅ |
| S6a | 正式调研：三种以上说法、覆盖最近 12 个月 arXiv、正向追踪、跨领域；置信度按做了几项定 | `checks._search_coverage` / `checks._achieved` / `checks.step6` | `test_gapr_workflow::TestSurveyStep6And7::test_confirmed_gap_needs_searches`、`::test_confidence_levels` | V10；M6、M8 | ◐ 按检索记录计数 ✅。跨领域检索只检查标了 `kind=cross`，**不核对是否真的用了术语对照表**；检索结果是否被认真读过 ✋ |
| S6b | 独立复核找到的反例优先 | `checks.step6` | `::TestSurveyStep6And7::test_counterexample_overrides` | — | ◐ 反例优先 ✅；“复核者没看过推导过程”无法由代码核实，只能记录 |
| S6c | 回测：先锁定候选再看截止日之后的历史；命中要写论文和间隔 | `cli._post_cutoff_gate`（要求第 0–5 步通过并锁定）；`checks.step6` | `test_gapr_workflow::TestCoverageGaps::test_post_cutoff_search_requires_step5`、`::test_backtest_hit_needs_paper_and_months`；`::TestStep5::test_backtest_lock` | V9；M10 | ✅ |
| S7 | 五项各 1–3 分，打分标准事先写下 | `checks.step7`；打分标准在 `rubric/scoring.md`，随确认点 2 冻结 | `test_gapr_workflow::TestSurveyStep6And7::test_scores` | — | ◐ 分数格式 ✅；分数是否按标准打 ✋ |

### 2.4 输出（模板第 5 节）与其他约定

| 编号 | 目标 | 实现 | 自动测试 | 状态与说明 |
|---|---|---|---|---|
| O1 | 报告：结论先行、演化图 / 泳道图、各表、空白清单、已过滤、检索记录、「已有知识」清单 | — | — | ✗ **未实现**。数据都在 CSV 里，报告生成命令还没写 |
| X1 | `gapr` 内部不调用 LLM，只用标准库 | 全部代码 | — | ✅ 可人工核对：`Select-String -Path researchtool\gapr\*.py,researchtool\gapr\sources\*.py -Pattern "^(import|from) "` 只会列出标准库和 `researchtool.gapr` 自己的模块 |
| X2 | API key 不写进缓存、日志或错误信息 | `net.Http.get_text(secret_params=…)` | `test_gapr_contexts::TestSecretsNotCached::test_api_key_not_in_cache` | ✅ |
| X3 | 证据文件被改动后不再算数 | `evidence.Evidence.verified` / `tampered` | `test_gapr_evidence::TestProvenance::test_fulltext_store_and_tamper`、`::test_unregistered_file_ignored` | ✅ |

---

## 3. 人工核验：故意违规，看会不会被拦下

每个场景都先用 `python -m tests.gapr_make_demo <新目录>` 生成合格的演示项目（场景 V5、V6 加 `--upto 5`），再按“怎么改坏”动手修改（用 Excel 或文本编辑器改 CSV 即可），最后运行命令。下面的“实际输出”是 2026-11-21 的真实运行结果，节选了关键行。

| 编号 | 怎么改坏 | 运行 | 实际输出（节选） |
|---|---|---|---|
| V0 | 不改 | `python -m researchtool.gapr check <目录>` | 第 0–4 步 `通过`，第 5–7 步 `未开始`，退出码 0 |
| V1 | 不改 | `python -m researchtool.gapr gaps <目录>` | C01–C03 为 R1：S、V、Z（列出有 △ 的方法，如 S 列：`N-BEATS / N-HiTS；Autoformer / FEDformer`）；C04–C07 为 R2：L×D（`DLinear(L✓ D△)`）、L×P、D×P、P×H；另有 10 条 R3 悬空缺陷 |
| V2 | 改 `rubric/attributes.csv` 里任一属性的 ✓ 标准 | `python -m researchtool.gapr gaps <目录>` | `不能列出候选空白…规则文件在冻结后发生变化（被修改：attributes.csv）。恢复原文件，或由你本人运行 python -m researchtool.gapr confirm <项目> 2 --change-rules --reason "…" 记录这次修改。` |
| V3 | `defects.csv` 第一行：`source_tag` 改为 `论文原文`，`quote` 写 `We only consider short horizons in this work.` | `python -m researchtool.gapr check <目录> --step 2` | `原句核对未通过：arXiv:1704.04110 没有登记的全文（先运行 gapr fulltext）` |
| V3b（联网） | 接 V3，再运行 `python -m researchtool.gapr fulltext <目录> arXiv:1704.04110` 下载 DeepAR 全文 | `python -m researchtool.gapr check <目录> --step 2` | `原句核对未通过：原文中找不到这段文字`。把 `quote` 换成全文里的真句子（`In this paper we propose DeepAR, a methodology for producing accurate probabilistic forecasts, …`）后显示 `通过` |
| V4a | `nodes.csv` 加一行新节点（例如复制第一行，id 改为 `patchtst`） | `python -m researchtool.gapr check <目录> --step 1` | `确认点 1：确认之后节点有变化（新增 ['patchtst']，删除 无），需要重新确认` |
| V4b | 接 V4a，让 Claude 运行确认命令 | `python -m researchtool.gapr confirm <目录> 1` | `确认点只能由你本人在自己的终端（PowerShell）里运行；检测到当前不是交互式终端，可能是模型在代跑，已拒绝。`（你本人在 PowerShell 里运行时，会显示清单并要求输入“确认”） |
| V5 | `--upto 5` 的项目，删掉 `filtered.csv` 的第一行 | `python -m researchtool.gapr check <目录> --step 5` | `这些候选既没并入 gaps.csv 也没写进 filtered.csv：C03` |
| V6 | `--upto 5` 的项目，把 `gaps.csv` 里 G01 的 `rules` 改成 `R1;R3` | `python -m researchtool.gapr check <目录> --step 5` | `gaps.csv G01：rules 应为 R1（由 merged_from 推出），现在是 'R1;R3'` |
| V7 | `nodes.csv` 里 DeepAR 的 `first_public` 改成 `2022-11-27` | `python -m researchtool.gapr check <目录> --step 1` | `首次公开于 2022-11-27，晚于回测截止日 2022-06-30` |
| V8 | 删掉 `matrix_evidence.csv` 的最后一行 | `python -m researchtool.gapr gaps <目录>` | `不能列出候选空白…matrix_evidence.csv：1 个 ✓ 没有依据：DLinear×H` |
| V9 | 不改（第 5 步还没做） | `python -m researchtool.gapr search <目录> patch --tool arxiv --post-cutoff` | `不能查看截止日之后的论文…第 5 步（列出候选空白）还没做`（被拦在联网之前） |
| V10 | 新建正式调研项目 `python -m researchtool.gapr init <目录> --mode survey --domain 演示 --start 2020-01-01`；在 `gaps.csv` 写一行：id `G01`、verdict `确认空白`、confidence `高`、closest_work 和 independent_review 随便填 | `python -m researchtool.gapr check <目录> --step 6` | `判为确认空白，但只有 0 种说法的检索，且没有覆盖最近 12 个月的 arXiv 检索`；`置信度写的是高，检索记录只支持低（说法 0 种、最近 12 个月 arXiv 无、正向追踪 无、跨领域 无）` |

**写这份文件时，这些场景发现了两个问题，已经修好**：
- V2 原先提示用已删除的 `gapr freeze --force`；
- V4b 原先在非交互环境里先把清单显示出来、之后才拒绝。

---

## 4. 变异核验：测试是不是真的在检查

`python -m tests.gapr_mutation_check` 依次把下列实现改坏（`unittest.mock` 在内存里替换），然后运行对应的测试。测试**失败**才算“抓到”。

| 编号 | 改坏了什么 | 应该失败的测试 | 2026-11-21 结果 |
|---|---|---|---|
| M1 | 把 △ 当作覆盖 | `test_gapr_matrix::TestLtsfBacktestMatrix::test_r1_columns` | 抓到 |
| M2 | 冻结后不再核对规则文件 | `test_gapr_workflow::TestGatesAndGaps::test_rubric_change_after_confirm2` | 抓到 |
| M3 | 原句核对一律放行 | `test_gapr_workflow::TestStep2Quotes::test_claimed_quote_must_be_found` | 抓到 |
| M4 | 确认点不再要求交互式终端 | `test_gapr_workflow::TestConfirm::test_model_cannot_confirm` | 抓到 |
| M5 | 去掉第 5 步检查 | `test_gapr_workflow::TestStep5::test_candidate_cannot_vanish` | 抓到 |
| M6 | 置信度不看检索记录 | `test_gapr_workflow::TestSurveyStep6And7::test_confidence_levels` | 抓到 |
| M7 | 回测日期防火墙失效 | `test_gapr_sources::TestFirewall::test_apply` | 抓到 |
| M8 | “确认空白”不再要求检索记录 | `test_gapr_workflow::TestSurveyStep6And7::test_confirmed_gap_needs_searches` | 抓到 |
| M9 | 去掉第 4 步检查（✓ 不再要求依据） | `test_gapr_workflow::TestGatesAndGaps::test_missing_evidence_blocks_gaps` | 抓到 |
| M10 | 回测不再核对候选锁定 | `test_gapr_workflow::TestStep5::test_backtest_lock` | 抓到 |

变异只覆盖了最关键的 10 条规则，不是每一行代码都做了变异。

---

## 5. 代码管不到、或只做到一部分的

**需要判断，代码只保证留下记录**（表中标 ✋）：
- 挑哪些综述、选哪些节点、归纳出什么属性、格子填 ✓ 还是 △；
- 需求写得对不对，拼接测试的推理是否成立，哪些候选应该合并；
- 检索结果有没有被认真读过，独立复核者是否真的没看过推导过程；
- 报告是否用读者听得懂的语言。

**做了，但有已知缺口**：
- **事后偏差**：回测的日期防火墙只挡检索。模型训练时就知道截止日之后的事，挑属性时仍可能被带偏。
- **引用句**：
  - arXiv HTML 里按参考文献链接定位，是精确的；
  - PDF 只能按“姓 et al. 年份”匹配，数字编号格式的引用抽不到；
  - 按关键词标出的“批评句”里，有不少其实是称赞。
- **审稿意见**：OpenReview 匿名取审稿意见会被人机验证拦下，工具不绕过；ARR 的审稿意见大多不公开。
- **原句核对**：
  - 只比较字母和数字，少于 20 个字母数字的原句不核对；
  - 公式多的段落，HTML 和 PDF 的公式文本可能不同，会导致核对不通过。
- **节点日期**：手填的日期不会自动去 arXiv 核对。
- **跨领域检索**：只检查标了 `kind=cross`，不核对是否真的用了术语对照表。
- **确认点的防护**：只防误操作，防不住刻意绕过（见 C3）。

**未实现**：最终报告生成（O1）、“每个已知缺陷至少保留一个专门解决它的节点”的检查（S1d）。

**依赖外部服务、结果会变的**：
- 第 3 节 V3b 的全文下载；
- `gapr contexts` 的抽取结果（2026-11-21 实测：PatchTST 的 6 篇引用者共抽到 73 句；ACL 2020 “Learning to Deceive with Attention-Based Explanations” 的 6 篇引用者里有 5 篇抽到句子）；
- 各服务的限额：OpenAlex 没有 key 时按 IP 共享每日额度，Semantic Scholar 没有 key 时几乎总是 429。

---

## 6. “代码正确”不等于“方法有效”

上面所有核验只说明：`gapr` 按模板的规则办事，违规会被拦下。

**这套方法能不能找到真问题**，要靠回测检验（模板第 6 节）：
- 目前唯一的回测是 [backtest-ltsf-2022.md](backtest-ltsf-2022.md)，它是手工完成的，矩阵凭事后知识填写，而且只有一个样本。
- 测试数据 `tests/fixtures/gapr_ltsf_2022/` 取自这次回测，所以测试通过只说明**代码对那张矩阵算对了**。
- 用 `gapr` 重跑这次回测、在新领域另做回测，都**还没做**。做完之后，结果应该补进这一节。

---

## 7. 维护本文件

- 改动任何一条规则（`docs/gap-research.md`）或检查代码时，同步更新第 2 节对应的行。
- 新增检查时，至少补一个“故意违规”的测试；关键规则再在 `tests/gapr_mutation_check.py` 里加一条变异。
- 重新运行第 1 节的三条命令，把第 3、4 节的“实际输出”和日期更新为新的结果。
